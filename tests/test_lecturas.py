"""
Pruebas para el router /api/v1/lecturas
"""
from unittest.mock import MagicMock, patch

SALA_ID = "00000000-0000-0000-0000-000000000002"
NODO_ID = "00000000-0000-0000-0000-000000000001"

CUERPO_LECTURA = {
    "nodo_id":      NODO_ID,
    "sala_id":      SALA_ID,
    "temperatura":  24.5,
    "humedad":      65.0,
    "presencia":    True,
    "setpoint_ac":  22,
    "ac_encendido": True,
    "voltaje":      120.0,
    "corriente_a":  3.5,
    "potencia_w":   420.0,
    "energia_kwh":  1.25,
}


async def test_crear_lectura_exitoso(
    cliente_prueba, mock_supabase, nodo_ejemplo, lectura_ejemplo
):
    """POST /lecturas → 201 cuando el nodo existe y la lectura se inserta."""
    mock_supabase.execute.side_effect = [
        MagicMock(data=[{"id": NODO_ID}]),  # búsqueda del nodo
        MagicMock(data=[]),                  # actualizar ultima_vez_visto
        MagicMock(data=[lectura_ejemplo]),   # insertar lectura
    ]

    with patch("app.api.readings.gestor") as mock_gestor:
        mock_gestor.transmitir_a_sala = MagicMock(return_value=None)
        respuesta = await cliente_prueba.post("/api/v1/lecturas/", json=CUERPO_LECTURA)

    assert respuesta.status_code == 201
    datos = respuesta.json()
    assert datos["temperatura"] == 24.5


async def test_crear_lectura_nodo_no_encontrado(cliente_prueba, mock_supabase):
    """POST /lecturas → 404 cuando el nodo no existe."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.post("/api/v1/lecturas/", json=CUERPO_LECTURA)

    assert respuesta.status_code == 404


async def test_crear_lectura_temperatura_invalida(cliente_prueba, mock_supabase):
    """POST /lecturas → 422 cuando la temperatura no es numérica."""
    cuerpo = {**CUERPO_LECTURA, "temperatura": "no-es-numero"}

    respuesta = await cliente_prueba.post("/api/v1/lecturas/", json=cuerpo)

    assert respuesta.status_code == 422


async def test_crear_lote_exitoso(
    cliente_prueba, mock_supabase, lectura_ejemplo
):
    """POST /lecturas/lote → 200 con 5 lecturas insertadas."""
    lote = [CUERPO_LECTURA] * 5
    mock_supabase.execute.return_value = MagicMock(
        data=[lectura_ejemplo] * 5
    )

    respuesta = await cliente_prueba.post("/api/v1/lecturas/lote", json=lote)

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["insertados"] == 5
    assert datos["errores"] == 0


async def test_crear_lote_demasiado_grande(cliente_prueba, mock_supabase):
    """POST /lecturas/lote → 422 cuando se envían más de 50 lecturas."""
    lote = [CUERPO_LECTURA] * 51

    respuesta = await cliente_prueba.post("/api/v1/lecturas/lote", json=lote)

    assert respuesta.status_code == 422


async def test_listar_lecturas_exitoso(
    cliente_prueba, mock_supabase, lectura_ejemplo
):
    """GET /lecturas → 200 con lista de lecturas del período."""
    mock_supabase.execute.return_value = MagicMock(data=[lectura_ejemplo])

    respuesta = await cliente_prueba.get(
        "/api/v1/lecturas/",
        params={
            "sala_id": SALA_ID,
            "inicio":  "2025-01-01T00:00:00+00:00",
            "fin":     "2025-01-02T00:00:00+00:00",
        },
    )

    assert respuesta.status_code == 200
    assert isinstance(respuesta.json(), list)


async def test_ultima_lectura_exitoso(
    cliente_prueba, mock_supabase, lectura_ejemplo
):
    """GET /lecturas/reciente/{sala_id} → 200 con la lectura más reciente."""
    mock_supabase.execute.return_value = MagicMock(data=[lectura_ejemplo])

    respuesta = await cliente_prueba.get(f"/api/v1/lecturas/reciente/{SALA_ID}")

    assert respuesta.status_code == 200
    assert respuesta.json()["temperatura"] == 24.5


async def test_ultima_lectura_no_encontrada(cliente_prueba, mock_supabase):
    """GET /lecturas/reciente/{sala_id} → 404 cuando no hay lecturas."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.get(f"/api/v1/lecturas/reciente/{SALA_ID}")

    assert respuesta.status_code == 404


async def test_disparar_agregacion_secreto_valido(
    cliente_prueba, mock_supabase, encabezados_cron
):
    """POST /lecturas/disparar-agregacion → 200 con secreto correcto."""
    with patch("app.api.readings.ServicioAgregacion") as mock_servicio:
        mock_servicio.return_value.agregar_todas_las_salas.return_value = {
            "procesadas": 3,
            "omitidas":   1,
            "errores":    [],
        }
        respuesta = await cliente_prueba.post(
            "/api/v1/lecturas/disparar-agregacion",
            headers=encabezados_cron,
        )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["procesadas"] == 3


async def test_disparar_agregacion_secreto_invalido(cliente_prueba, mock_supabase):
    """POST /lecturas/disparar-agregacion → 401 con secreto incorrecto."""
    respuesta = await cliente_prueba.post(
        "/api/v1/lecturas/disparar-agregacion",
        headers={"X-Cron-Secret": "secreto-incorrecto"},
    )

    assert respuesta.status_code == 401
