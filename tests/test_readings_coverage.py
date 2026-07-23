from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.api import readings
from app.services.sincronizador_firebase import MAX_LECTURAS_FIREBASE_POR_CONSULTA


def _firebase_key(fecha: datetime) -> str:
    milisegundos = int(fecha.timestamp() * 1000)
    caracteres = []
    for _ in range(8):
        caracteres.append(readings.FIREBASE_PUSH_CHARS[milisegundos % 64])
        milisegundos //= 64
    return "".join(reversed(caracteres)) + "clave"


def _lectura_firebase() -> dict:
    return {
        "temperatura": "27.5",
        "temperatura_ac": "20.0",
        "humedad": "65",
        "movimiento": 1,
        "potencia_activa_w": "640",
    }


def test_fecha_push_id_y_agregacion_descartan_datos_malformados():
    ahora = datetime.now(timezone.utc)
    clave = _firebase_key(ahora)

    assert readings._fecha_desde_firebase_push_id(clave).replace(microsecond=0) == ahora.replace(microsecond=0)
    assert readings._fecha_desde_firebase_push_id("corta") is None
    assert readings._fecha_desde_firebase_push_id("!!!!!!!!") is None

    agrupado = readings._agrupar_potencia_activa_firebase(
        {
            clave: {"potencia_activa_kw": "0.64"},
            "malformada": [],
            _firebase_key(ahora.replace(year=2020)): {"potencia_activa_w": 999},
            _firebase_key(ahora + timedelta(seconds=1)): {"potencia_activa_w": "texto"},
        },
        dias=7,
        ahora=ahora,
    )

    assert agrupado == [{
        "bucket_hour": ahora.replace(minute=0, second=0, microsecond=0).isoformat(),
        "avg_power_w": 640.0,
        "reading_count": 1,
        "source": "firebase_potencia_activa_w",
    }]


def test_normalizar_historial_preserva_telemetria_y_omite_valores_invalidos():
    clave = _firebase_key(datetime.now(timezone.utc))

    registros = readings._normalizar_historial_firebase(
        {clave: _lectura_firebase(), "invalida": "sin estructura"}, "robotica", "Aire_1"
    )

    assert len(registros) == 1
    assert registros[0]["firebase_key"] == f"robotica_Aire_1_{clave}"
    assert registros[0]["potencia_activa_w"] == 640.0
    assert registros[0]["fecha_sync"].endswith("+00:00")


@pytest.mark.asyncio
async def test_historial_firebase_respeta_limite_100_y_normaliza(cliente_prueba):
    clave = _firebase_key(datetime.now(timezone.utc))
    with patch("app.api.readings.leer_ultimas_lecturas_firebase_rest", return_value={clave: _lectura_firebase()}) as lector:
        respuesta = await cliente_prueba.get(
            "/api/v1/lecturas/registros",
            params={"pabellon": "robotica", "aire": "Aire_1", "limite": 100},
        )

    assert respuesta.status_code == 200
    assert respuesta.json()[0]["potencia_activa_w"] == 640.0
    lector.assert_called_once_with(pabellon="robotica", aire="Aire_1", limite=100)


@pytest.mark.asyncio
async def test_historial_firebase_rechaza_limite_superior_y_devuelve_vacio_para_aire_inexistente(cliente_prueba):
    with patch("app.api.readings.leer_ultimas_lecturas_firebase_rest", return_value={}) as lector:
        vacio = await cliente_prueba.get(
            "/api/v1/lecturas/registros",
            params={"pabellon": "inexistente", "aire": "Aire_99"},
        )
        limite_invalido = await cliente_prueba.get(
            "/api/v1/lecturas/registros",
            params={"pabellon": "robotica", "aire": "Aire_1", "limite": 101},
        )

    assert vacio.status_code == 200
    assert vacio.json() == []
    assert limite_invalido.status_code == 422
    lector.assert_called_once_with(pabellon="inexistente", aire="Aire_99", limite=100)


@pytest.mark.asyncio
async def test_historial_con_parametros_incompletos_no_consulta_firebase_global(cliente_prueba, mock_supabase):
    mock_supabase.execute.return_value.data = []
    with patch("app.api.readings.leer_ultimas_lecturas_firebase_rest") as lector:
        respuesta = await cliente_prueba.get(
            "/api/v1/lecturas/registros", params={"pabellon": "robotica"}
        )

    assert respuesta.status_code == 200
    assert respuesta.json() == []
    lector.assert_not_called()


@pytest.mark.asyncio
async def test_errores_y_timeout_firebase_se_exponen_como_502_sin_red_real(cliente_prueba):
    with patch(
        "app.api.readings.leer_ultimas_lecturas_firebase_rest",
        side_effect=TimeoutError("timeout simulado"),
    ):
        historial = await cliente_prueba.get(
            "/api/v1/lecturas/registros", params={"pabellon": "robotica", "aire": "Aire_1"}
        )
        potencia = await cliente_prueba.get(
            "/api/v1/lecturas/registros/potencia-activa", params={"pabellon": "robotica", "aire": "Aire_1"}
        )

    assert historial.status_code == 502
    assert potencia.status_code == 502


@pytest.mark.asyncio
async def test_potencia_usa_tope_global_y_no_acepta_fecha_invalida(cliente_prueba):
    with patch("app.api.readings.leer_ultimas_lecturas_firebase_rest", return_value={}) as lector:
        respuesta = await cliente_prueba.get(
            "/api/v1/lecturas/registros/potencia-activa",
            params={"pabellon": "robotica", "aire": "Aire_1", "dias": 7},
        )
        dias_invalidos = await cliente_prueba.get(
            "/api/v1/lecturas/registros/potencia-activa",
            params={"pabellon": "robotica", "aire": "Aire_1", "dias": "fecha"},
        )

    assert respuesta.status_code == 200
    assert respuesta.json() == []
    lector.assert_called_once_with(
        pabellon="robotica", aire="Aire_1", limite=MAX_LECTURAS_FIREBASE_POR_CONSULTA
    )
    assert dias_invalidos.status_code == 422


@pytest.mark.asyncio
async def test_aires_inexistentes_y_sincronizacion_requieren_contrato_esperado(cliente_prueba, mock_supabase):
    mock_supabase.execute.return_value.data = [{"aire": "Aire_2"}, {"aire": None}, {"aire": "Aire_1"}]
    aires = await cliente_prueba.get("/api/v1/lecturas/registros/aires", params={"pabellon": "robotica"})
    sin_token = await cliente_prueba.post("/api/v1/lecturas/firebase/sincronizar")

    assert aires.status_code == 200
    assert aires.json() == ["Aire_1", "Aire_2"]
    assert sin_token.status_code == 401
