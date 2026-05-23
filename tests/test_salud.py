"""
Pruebas para el endpoint GET /health
"""
from unittest.mock import MagicMock


async def test_salud_ok(cliente_prueba, mock_supabase):
    """Responde 200 con estado 'ok' cuando Supabase está disponible."""
    mock_supabase.execute.return_value = MagicMock(data=[{"count": 1}])

    respuesta = await cliente_prueba.get("/health")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["estado"] == "ok"
    assert datos["servicio"] == "atmos-api"
    assert datos["base_de_datos"] == "conectada"


async def test_salud_degradada(cliente_prueba, mock_supabase):
    """Responde 503 con estado 'degradado' cuando Supabase falla."""
    mock_supabase.execute.side_effect = Exception("Conexión rechazada")

    respuesta = await cliente_prueba.get("/health")

    assert respuesta.status_code == 503
    datos = respuesta.json()
    assert datos["estado"] == "degradado"
    assert datos["servicio"] == "atmos-api"
    assert datos["base_de_datos"] == "inalcanzable"
