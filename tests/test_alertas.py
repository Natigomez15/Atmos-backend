"""
Pruebas para el router /api/v1/alertas
"""
from unittest.mock import MagicMock, patch

SALA_ID  = "00000000-0000-0000-0000-000000000002"
ALERTA_ID = 1


async def test_listar_alertas_sin_resolver(
    cliente_prueba, mock_supabase, alerta_ejemplo
):
    """GET /alertas → 200 con alertas no resueltas (filtro por defecto)."""
    mock_supabase.execute.return_value = MagicMock(data=[alerta_ejemplo])

    respuesta = await cliente_prueba.get("/api/v1/alertas/")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert isinstance(datos, list)
    assert datos[0]["esta_resuelta"] is False


async def test_listar_alertas_por_severidad(
    cliente_prueba, mock_supabase, alerta_ejemplo
):
    """GET /alertas?severidad=high → 200 filtrando por severidad."""
    alerta_high = {**alerta_ejemplo, "severidad": "high"}
    mock_supabase.execute.return_value = MagicMock(data=[alerta_high])

    respuesta = await cliente_prueba.get(
        "/api/v1/alertas/", params={"severidad": "high"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json()[0]["severidad"] == "high"


async def test_resumen_alertas(cliente_prueba, mock_supabase, alerta_ejemplo):
    """GET /alertas/resumen → 200 con totales agrupados."""
    alertas = [
        {**alerta_ejemplo, "severidad": "high",   "tipo_alerta": "node_offline"},
        {**alerta_ejemplo, "severidad": "medium",  "tipo_alerta": "power_anomaly"},
    ]
    mock_supabase.execute.return_value = MagicMock(data=alertas)

    respuesta = await cliente_prueba.get("/api/v1/alertas/resumen")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert "total_sin_resolver" in datos
    assert "por_severidad" in datos
    assert "por_tipo" in datos
    assert datos["total_sin_resolver"] == 2
    assert datos["por_severidad"]["high"] == 1
    assert datos["por_tipo"]["node_offline"] == 1


async def test_resolver_alerta_exitoso(
    cliente_prueba, mock_supabase, alerta_ejemplo
):
    """PATCH /alertas/{id}/resolver → 200 marcando la alerta resuelta."""
    alerta_resuelta = {
        **alerta_ejemplo,
        "esta_resuelta": True,
        "resuelto_en": "2025-01-01T15:00:00+00:00",
    }
    mock_supabase.execute.side_effect = [
        MagicMock(data=alerta_ejemplo),     # lookup
        MagicMock(data=[alerta_resuelta]),  # update
    ]

    respuesta = await cliente_prueba.patch(
        f"/api/v1/alertas/{ALERTA_ID}/resolver"
    )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["esta_resuelta"] is True
    assert datos["resuelto_en"] is not None


async def test_resolver_alerta_ya_resuelta(
    cliente_prueba, mock_supabase, alerta_ejemplo
):
    """PATCH /alertas/{id}/resolver → 409 si la alerta ya está resuelta."""
    alerta_ya_resuelta = {**alerta_ejemplo, "esta_resuelta": True}
    mock_supabase.execute.return_value = MagicMock(data=alerta_ya_resuelta)

    respuesta = await cliente_prueba.patch(
        f"/api/v1/alertas/{ALERTA_ID}/resolver"
    )

    assert respuesta.status_code == 409


async def test_resolver_alerta_no_encontrada(cliente_prueba, mock_supabase):
    """PATCH /alertas/{id}/resolver → 404 cuando la alerta no existe."""
    mock_supabase.execute.return_value = MagicMock(data=None)

    respuesta = await cliente_prueba.patch(
        f"/api/v1/alertas/{ALERTA_ID}/resolver"
    )

    assert respuesta.status_code == 404


async def test_ejecutar_verificaciones_secreto_valido(
    cliente_prueba, mock_supabase, encabezados_cron
):
    """POST /alertas/ejecutar-verificaciones → 200 con secreto correcto."""
    with patch("app.api.alerts.ServicioAlertas") as mock_servicio:
        mock_servicio.return_value.ejecutar_todas_las_verificaciones.return_value = {
            "resueltas": 0, "nuevas_desconexion": 1,
            "nuevas_potencia": 0, "nuevas_temperatura": 0, "total_nuevas": 1,
        }
        respuesta = await cliente_prueba.post(
            "/api/v1/alertas/ejecutar-verificaciones",
            headers=encabezados_cron,
        )

    assert respuesta.status_code == 200
    assert respuesta.json()["total_nuevas"] == 1


async def test_ejecutar_verificaciones_secreto_invalido(
    cliente_prueba, mock_supabase
):
    """POST /alertas/ejecutar-verificaciones → 401 con secreto incorrecto."""
    respuesta = await cliente_prueba.post(
        "/api/v1/alertas/ejecutar-verificaciones",
        headers={"X-Cron-Secret": "secreto-incorrecto"},
    )

    assert respuesta.status_code == 401
