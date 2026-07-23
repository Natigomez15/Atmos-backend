from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.config import configuracion
from app.core import security


def test_verificar_api_key_y_usuario_opcional_sin_header():
    assert security.verificar_api_key(None) is False
    assert security.verificar_api_key(configuracion.CRON_SECRET) is True
    assert security.obtener_usuario_opcional(None) is None


def test_usuario_actual_rechaza_headers_y_tokens_invalidos(monkeypatch):
    with pytest.raises(HTTPException, match="No autenticado"):
        security.obtener_usuario_actual(None)
    with pytest.raises(HTTPException, match="No autenticado"):
        security.obtener_usuario_actual("Token invalido")

    cliente = MagicMock()
    cliente.auth.get_user.return_value = MagicMock(user=None)
    monkeypatch.setattr(security, "obtener_cliente", lambda: cliente)
    with pytest.raises(HTTPException, match="Token inv"):
        security.obtener_usuario_actual("Bearer invalido")


def test_usuario_actual_y_roles(monkeypatch):
    cliente = MagicMock()
    cliente.auth.get_user.return_value = MagicMock(user=MagicMock(id="u-1"))
    cliente.execute.return_value = MagicMock(
        data={"id": "u-1", "rol": "admin", "esta_activo": True}
    )
    for metodo in ("table", "select", "eq", "single"):
        getattr(cliente, metodo).return_value = cliente
    monkeypatch.setattr(security, "obtener_cliente", lambda: cliente)

    assert security.requerir_admin("Bearer valido")["rol"] == "admin"
    assert security.requerir_mantenimiento_o_admin("Bearer valido")["id"] == "u-1"


def test_usuario_rechaza_perfil_inactivo_y_rol_no_permitido(monkeypatch):
    cliente = MagicMock()
    cliente.auth.get_user.return_value = MagicMock(user=MagicMock(id="u-1"))
    for metodo in ("table", "select", "eq", "single"):
        getattr(cliente, metodo).return_value = cliente
    monkeypatch.setattr(security, "obtener_cliente", lambda: cliente)

    cliente.execute.return_value = MagicMock(data={"esta_activo": False, "rol": "admin"})
    with pytest.raises(HTTPException, match="inactivo"):
        security.obtener_usuario_actual("Bearer valido")

    cliente.execute.return_value = MagicMock(data={"esta_activo": True, "rol": "usuario"})
    with pytest.raises(HTTPException, match="rol admin"):
        security.requerir_admin("Bearer valido")
    with pytest.raises(HTTPException, match="mantenimiento"):
        security.requerir_mantenimiento_o_admin("Bearer valido")
