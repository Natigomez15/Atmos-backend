from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_historial_por_horas_consulta_rango_completo_firebase(cliente_prueba):
    ahora = datetime.now(timezone.utc)
    lectura = {
        "temperatura": "27.5",
        "temperatura_ac": "20.0",
        "humedad": "65",
        "movimiento": 1,
        "potencia_activa_w": "640",
    }

    with (
        patch("app.api.readings.datetime") as fecha,
        patch(
            "app.api.readings.leer_lecturas_firebase_rango_rest",
            return_value={"firebase-key": lectura},
        ) as lector_rango,
        patch("app.api.readings.leer_ultimas_lecturas_firebase_rest") as lector_limite,
    ):
        fecha.now.return_value = ahora
        respuesta = await cliente_prueba.get(
            "/api/v1/lecturas/registros",
            params={"pabellon": "robotica", "aire": "Aire_1", "horas": 24},
        )

    assert respuesta.status_code == 200
    assert respuesta.json()[0]["temperatura_ambiente"] == 27.5
    lector_rango.assert_called_once_with(
        pabellon="robotica",
        aire="Aire_1",
        desde=ahora - timedelta(hours=24),
        hasta=ahora,
    )
    lector_limite.assert_not_called()


@pytest.mark.asyncio
async def test_historial_rechaza_rango_mayor_a_24_horas(cliente_prueba):
    respuesta = await cliente_prueba.get(
        "/api/v1/lecturas/registros",
        params={"pabellon": "robotica", "aire": "Aire_1", "horas": 25},
    )

    assert respuesta.status_code == 422
