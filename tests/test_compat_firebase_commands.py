"""Comandos manuales: Firebase es operativo y Supabase solo auditoría opcional."""

from unittest.mock import MagicMock

import pytest

from app.config import configuracion


SALA_ID = "00000000-0000-0000-0000-000000000002"
CUERPO_APAGAR = {
    "room_id": SALA_ID,
    "pabellon": "robotica",
    "aire": "Aire_1",
    "command_type": "off",
    "setpoint": None,
    "source": "manual",
}


class FirebaseFalso:
    def __init__(self, eventos):
        self.eventos = eventos
        self.actualizaciones = []

    def child(self, nombre):
        self.eventos.append(("firebase_child", nombre))
        return self

    def update(self, datos):
        self.eventos.append(("firebase_update", datos["accion"]))
        self.actualizaciones.append(dict(datos))


@pytest.fixture(autouse=True)
def control_manual(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "manual_only")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)


async def test_firebase_no_depende_de_supabase(cliente_prueba, monkeypatch):
    eventos = []
    firebase = FirebaseFalso(eventos)
    supabase = MagicMock()
    supabase.table.side_effect = RuntimeError("Supabase no disponible")

    monkeypatch.setattr("app.api.compat.obtener_firebase", lambda: firebase)
    monkeypatch.setattr("app.api.compat.obtener_cliente", lambda: supabase)

    respuesta = await cliente_prueba.post("/api/v1/ac-commands", json=CUERPO_APAGAR)

    assert respuesta.status_code == 201
    cuerpo = respuesta.json()
    assert cuerpo["firebase_escrito"] is True
    assert cuerpo["supabase_guardado"] is False
    assert firebase.actualizaciones[0]["accion"] == "apagar"
    assert firebase.actualizaciones[0]["pabellon"] == "robotica"
    assert firebase.actualizaciones[0]["aire"] == "Aire_1"
    assert eventos[-1] == ("firebase_update", "apagar")


async def test_rechazo_firebase_no_intenta_supabase(cliente_prueba, monkeypatch):
    supabase = MagicMock()

    class FirebaseRechaza(FirebaseFalso):
        def update(self, _datos):
            raise PermissionError("Permission denied")

    monkeypatch.setattr("app.api.compat.obtener_firebase", lambda: FirebaseRechaza([]))
    monkeypatch.setattr("app.api.compat.obtener_cliente", lambda: supabase)

    respuesta = await cliente_prueba.post("/api/v1/ac-commands", json=CUERPO_APAGAR)

    assert respuesta.status_code == 502
    assert respuesta.json()["detail"]["estado"] == "firebase_no_escrito"
    supabase.table.assert_not_called()
