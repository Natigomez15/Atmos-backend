"""
Pruebas para el router /api/v1/ml
"""
from unittest.mock import MagicMock, patch

SALA_ID = "00000000-0000-0000-0000-000000000002"

CARACTERISTICA_EJEMPLO = {
    "cubo_hora":            "2025-01-01T13:00:00+00:00",
    "temperatura_promedio": 24.5,
    "humedad_promedio":     65.0,
    "razon_presencia":      0.8,
    "potencia_promedio_w":  420.0,
    "energia_total_kwh":    0.5,
    "dia_semana":           2,
    "hora_del_dia":         13,
    "cantidad_lecturas":    10,
}

CUERPO_PREDICCION = {
    "sala_id":                   SALA_ID,
    "setpoint_recomendado":      22,
    "ahorro_predicho_pct":       15.0,
    "puntaje_confianza":         0.85,
    "version_modelo":            "rf_v1.0",
    "instantanea_caracteristicas": {"temperatura_promedio": 24.5},
}


async def test_obtener_caracteristicas_exitoso(
    cliente_prueba, mock_supabase
):
    """GET /ml/caracteristicas/{sala_id} → 200 con lista de características."""
    caracteristicas = [CARACTERISTICA_EJEMPLO] * 5
    mock_supabase.execute.return_value = MagicMock(data=caracteristicas)

    respuesta = await cliente_prueba.get(
        f"/api/v1/ml/caracteristicas/{SALA_ID}",
        params={"dias_atras": 30},
    )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert isinstance(datos, list)
    assert len(datos) == 5
    # Verificar que no hay valores None
    for caracteristica in datos:
        for clave, valor in caracteristica.items():
            assert valor is not None, f"Campo '{clave}' es None"


async def test_obtener_caracteristicas_sin_datos(cliente_prueba, mock_supabase):
    """GET /ml/caracteristicas/{sala_id} → 404 cuando no hay datos agregados."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.get(
        f"/api/v1/ml/caracteristicas/{SALA_ID}"
    )

    assert respuesta.status_code == 404


async def test_obtener_caracteristicas_dias_excesivos(
    cliente_prueba, mock_supabase
):
    """GET /ml/caracteristicas/{sala_id}?dias_atras=200 → 422 (máx 90)."""
    respuesta = await cliente_prueba.get(
        f"/api/v1/ml/caracteristicas/{SALA_ID}",
        params={"dias_atras": 200},
    )

    assert respuesta.status_code == 422


async def test_guardar_prediccion_exitoso(
    cliente_prueba, mock_supabase, prediccion_ejemplo
):
    """POST /ml/predicciones → 201 con predicción guardada."""
    mock_supabase.execute.return_value = MagicMock(data=[prediccion_ejemplo])

    respuesta = await cliente_prueba.post(
        "/api/v1/ml/predicciones", json=CUERPO_PREDICCION
    )

    assert respuesta.status_code == 201
    assert respuesta.json()["version_modelo"] == "rf_v1.0"


async def test_guardar_prediccion_setpoint_invalido(
    cliente_prueba, mock_supabase
):
    """POST /ml/predicciones → 422 cuando setpoint_recomendado > 30."""
    cuerpo = {**CUERPO_PREDICCION, "setpoint_recomendado": 35}

    respuesta = await cliente_prueba.post(
        "/api/v1/ml/predicciones", json=cuerpo
    )

    assert respuesta.status_code == 422


async def test_ultima_prediccion_exitoso(
    cliente_prueba, mock_supabase, prediccion_ejemplo
):
    """GET /ml/predicciones/{sala_id}/reciente → 200 con la predicción más reciente."""
    mock_supabase.execute.return_value = MagicMock(data=[prediccion_ejemplo])

    respuesta = await cliente_prueba.get(
        f"/api/v1/ml/predicciones/{SALA_ID}/reciente"
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["version_modelo"] == "rf_v1.0"


async def test_ultima_prediccion_no_encontrada(cliente_prueba, mock_supabase):
    """GET /ml/predicciones/{sala_id}/reciente → 404 cuando no hay predicciones."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.get(
        f"/api/v1/ml/predicciones/{SALA_ID}/reciente"
    )

    assert respuesta.status_code == 404


async def test_evaluar_predicciones_secreto_valido(
    cliente_prueba, mock_supabase, encabezados_cron
):
    """POST /ml/evaluar → 200 con secreto correcto."""
    with patch("app.api.ml.ServicioPredictor") as mock_servicio:
        mock_servicio.return_value.evaluar_predicciones_pasadas.return_value = []
        mock_supabase.execute.return_value = MagicMock(data=[])

        respuesta = await cliente_prueba.post(
            "/api/v1/ml/evaluar",
            headers=encabezados_cron,
        )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert "evaluadas" in datos
    assert "resultados" in datos


async def test_evaluar_predicciones_secreto_invalido(
    cliente_prueba, mock_supabase
):
    """POST /ml/evaluar → 401 con secreto incorrecto."""
    respuesta = await cliente_prueba.post(
        "/api/v1/ml/evaluar",
        headers={"X-Cron-Secret": "secreto-incorrecto"},
    )

    assert respuesta.status_code == 401
