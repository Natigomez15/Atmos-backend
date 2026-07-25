"""
Pruebas para el router /api/v1/comandos-ac
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import configuracion
from app.core.control_ir import construir_comando_ir

SALA_ID    = "00000000-0000-0000-0000-000000000002"
NODO_ID    = "00000000-0000-0000-0000-000000000001"
COMANDO_ID = 1

CUERPO_SETPOINT = {
    "sala_id":       SALA_ID,
    "tipo_comando":  "setpoint",
    "setpoint":      22,
    "origen":        "manual",
    "pabellon":      "robotica",
    "aire":          "Aire_1",
}


@pytest.fixture(autouse=True)
def control_activo_solo_con_mocks(
    monkeypatch, mock_supabase, comando_ejemplo, prediccion_ejemplo
):
    """El modo activo existe únicamente dentro del proceso de pruebas."""
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "active")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)
    monkeypatch.setattr("app.api.ac_commands.obtener_cliente", lambda: mock_supabase)
    comando_ejemplo.update(
        construir_comando_ir(
            pabellon="robotica",
            aire="Aire_1",
            accion="encender_22",
        )
    )
    prediccion_ejemplo["instantanea_caracteristicas"] = {
        "accion_solicitada": "encender_22",
        "pabellon": "robotica",
        "aire": "Aire_1",
    }


async def test_crear_comando_setpoint_exitoso(
    cliente_prueba, mock_supabase, comando_ejemplo
):
    """POST /comandos-ac → 201 con comando de setpoint válido."""
    mock_supabase.execute.return_value = MagicMock(data=[comando_ejemplo])

    respuesta = await cliente_prueba.post("/api/v1/comandos-ac/", json=CUERPO_SETPOINT)

    assert respuesta.status_code == 201
    assert respuesta.json()["tipo_comando"] == "setpoint"


async def test_crear_comando_setpoint_sin_valor(cliente_prueba, mock_supabase):
    """POST /comandos-ac → 422 cuando tipo=setpoint pero setpoint es None."""
    cuerpo = {**CUERPO_SETPOINT, "setpoint": None}

    respuesta = await cliente_prueba.post("/api/v1/comandos-ac/", json=cuerpo)

    assert respuesta.status_code == 422


async def test_crear_comando_setpoint_fuera_de_rango(cliente_prueba, mock_supabase):
    """POST /comandos-ac → 422 cuando setpoint > 30."""
    cuerpo = {**CUERPO_SETPOINT, "setpoint": 35}

    respuesta = await cliente_prueba.post("/api/v1/comandos-ac/", json=cuerpo)

    assert respuesta.status_code == 422


async def test_obtener_pendientes_exitoso(
    cliente_prueba, mock_supabase, nodo_ejemplo, comando_ejemplo
):
    """GET /comandos-ac/pendientes/{nodo_id} → 200 con comandos pendientes."""
    # 1ª: búsqueda del nodo (sala_id), 2ª: comandos del nodo, 3ª: comandos de sala
    mock_supabase.execute.side_effect = [
        MagicMock(data={"sala_id": SALA_ID}),  # single() → dict
        MagicMock(data=[comando_ejemplo]),
        MagicMock(data=[]),
    ]

    respuesta = await cliente_prueba.get(
        f"/api/v1/comandos-ac/pendientes/{NODO_ID}",
        params={"pabellon": "robotica", "aire": "Aire_1"},
    )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert isinstance(datos, list)


async def test_obtener_pendientes_nodo_no_encontrado(
    cliente_prueba, mock_supabase
):
    """GET /comandos-ac/pendientes/{nodo_id} → 404 cuando el nodo no existe."""
    mock_supabase.execute.return_value = MagicMock(data=None)

    respuesta = await cliente_prueba.get(
        f"/api/v1/comandos-ac/pendientes/{NODO_ID}",
        params={"pabellon": "robotica", "aire": "Aire_1"},
    )

    assert respuesta.status_code == 404


async def test_confirmar_comando_exitoso(
    cliente_prueba, mock_supabase, comando_ejemplo
):
    """PATCH /comandos-ac/{id}/confirmar → 200 marcando el comando ejecutado."""
    comando_ejecutado = {**comando_ejemplo, "fue_ejecutado": True, "estado": "confirmado"}
    mock_supabase.execute.side_effect = [
        MagicMock(data={**comando_ejemplo, "fue_ejecutado": False}),   # existente
        MagicMock(data=[comando_ejecutado]),                             # update
    ]

    with patch("app.api.ac_commands.gestor") as mock_gestor:
        mock_gestor.transmitir_a_sala = AsyncMock(return_value=None)
        respuesta = await cliente_prueba.patch(
            f"/api/v1/comandos-ac/{COMANDO_ID}/confirmar"
        )

    assert respuesta.status_code == 200
    assert respuesta.json()["fue_ejecutado"] is True


async def test_confirmar_comando_ya_ejecutado(
    cliente_prueba, mock_supabase, comando_ejemplo
):
    """PATCH /comandos-ac/{id}/confirmar → 409 si el comando ya fue ejecutado."""
    mock_supabase.execute.return_value = MagicMock(
        data={**comando_ejemplo, "fue_ejecutado": True}
    )

    respuesta = await cliente_prueba.patch(
        f"/api/v1/comandos-ac/{COMANDO_ID}/confirmar"
    )

    assert respuesta.status_code == 409


async def test_confirmar_comando_no_encontrado(cliente_prueba, mock_supabase):
    """PATCH /comandos-ac/{id}/confirmar → 404 si el id no existe."""
    mock_supabase.execute.return_value = MagicMock(data=None)

    respuesta = await cliente_prueba.patch(
        f"/api/v1/comandos-ac/{COMANDO_ID}/confirmar"
    )

    assert respuesta.status_code == 404


async def test_listar_comandos_exitoso(
    cliente_prueba, mock_supabase, comando_ejemplo
):
    """GET /comandos-ac?sala_id=... → 200 con historial de comandos."""
    mock_supabase.execute.return_value = MagicMock(data=[comando_ejemplo])

    respuesta = await cliente_prueba.get(
        "/api/v1/comandos-ac/",
        params={"sala_id": SALA_ID},
    )

    assert respuesta.status_code == 200
    assert isinstance(respuesta.json(), list)


async def test_comando_desde_prediccion_exitoso(
    cliente_prueba, mock_supabase, comando_ejemplo, prediccion_ejemplo
):
    """POST /comandos-ac/desde-prediccion/{id} → 201 con origen ml_model."""
    mock_supabase.execute.side_effect = [
        MagicMock(data=prediccion_ejemplo),
        MagicMock(data=[{**comando_ejemplo, "origen": "ml_model"}]),  # insert
    ]

    respuesta = await cliente_prueba.post(
        "/api/v1/comandos-ac/desde-prediccion/1"
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["origen"] == "ml_model"


async def test_comando_desde_prediccion_ya_aplicada(
    cliente_prueba, mock_supabase, prediccion_ejemplo
):
    """POST /comandos-ac/desde-prediccion/{id} → 409 si ya fue aplicada."""
    prediccion_aplicada = {**prediccion_ejemplo, "fue_aplicado": True}
    mock_supabase.execute.return_value = MagicMock(data=prediccion_aplicada)

    respuesta = await cliente_prueba.post(
        "/api/v1/comandos-ac/desde-prediccion/1"
    )

    assert respuesta.status_code == 409


async def test_comando_desde_prediccion_no_encontrada(
    cliente_prueba, mock_supabase
):
    """POST /comandos-ac/desde-prediccion/{id} → 404 si la predicción no existe."""
    mock_supabase.execute.return_value = MagicMock(data=None)

    respuesta = await cliente_prueba.post(
        "/api/v1/comandos-ac/desde-prediccion/999"
    )

    assert respuesta.status_code == 404
