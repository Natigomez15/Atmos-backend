"""
Pruebas para el router /api/v1/nodos
"""
from unittest.mock import MagicMock

NODO_ID  = "00000000-0000-0000-0000-000000000001"
SALA_ID  = "00000000-0000-0000-0000-000000000002"

CUERPO_CREAR = {
    "sala_id":          SALA_ID,
    "direccion_mac":    "AA:BB:CC:DD:EE:FF",
    "tipo_nodo":        "master",
    "version_firmware": "1.0.0",
}


async def test_crear_nodo_exitoso(cliente_prueba, mock_supabase, nodo_ejemplo):
    """POST /nodos → 201 cuando la MAC no existe previamente."""
    # 1ª llamada: búsqueda de MAC duplicada → vacía
    # 2ª llamada: inserción → nodo_ejemplo
    mock_supabase.execute.side_effect = [
        MagicMock(data=[]),
        MagicMock(data=[nodo_ejemplo]),
    ]

    respuesta = await cliente_prueba.post("/api/v1/nodos/", json=CUERPO_CREAR)

    assert respuesta.status_code == 201
    assert respuesta.json()["direccion_mac"] == "AA:BB:CC:DD:EE:FF"


async def test_crear_nodo_mac_duplicada(cliente_prueba, mock_supabase, nodo_ejemplo):
    """POST /nodos → 409 cuando la dirección MAC ya está registrada."""
    mock_supabase.execute.return_value = MagicMock(data=[nodo_ejemplo])

    respuesta = await cliente_prueba.post("/api/v1/nodos/", json=CUERPO_CREAR)

    assert respuesta.status_code == 409


async def test_crear_nodo_mac_invalida(cliente_prueba, mock_supabase):
    """POST /nodos → 422 cuando el formato de la MAC es incorrecto."""
    cuerpo = {**CUERPO_CREAR, "direccion_mac": "MAC-INVALIDA"}

    respuesta = await cliente_prueba.post("/api/v1/nodos/", json=cuerpo)

    assert respuesta.status_code == 422


async def test_listar_nodos_filtrado_por_sala(
    cliente_prueba, mock_supabase, nodo_ejemplo
):
    """GET /nodos?sala_id=... → 200 con nodos de esa sala."""
    mock_supabase.execute.return_value = MagicMock(data=[nodo_ejemplo])

    respuesta = await cliente_prueba.get(f"/api/v1/nodos/?sala_id={SALA_ID}")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert isinstance(datos, list)
    assert datos[0]["sala_id"] == SALA_ID


async def test_listar_nodos_sin_filtro(cliente_prueba, mock_supabase, nodo_ejemplo):
    """GET /nodos → 200 con todos los nodos."""
    mock_supabase.execute.return_value = MagicMock(data=[nodo_ejemplo])

    respuesta = await cliente_prueba.get("/api/v1/nodos/")

    assert respuesta.status_code == 200
    assert isinstance(respuesta.json(), list)


async def test_heartbeat_exitoso(cliente_prueba, mock_supabase, nodo_ejemplo):
    """PATCH /nodos/{id}/heartbeat → 200 con ultima_vez_visto actualizado."""
    mock_supabase.execute.return_value = MagicMock(data=[nodo_ejemplo])

    respuesta = await cliente_prueba.patch(f"/api/v1/nodos/{NODO_ID}/heartbeat")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert "nodo_id" in datos
    assert "ultima_vez_visto" in datos


async def test_heartbeat_nodo_no_encontrado(cliente_prueba, mock_supabase):
    """PATCH /nodos/{id}/heartbeat → 404 cuando el nodo no existe."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.patch(f"/api/v1/nodos/{NODO_ID}/heartbeat")

    assert respuesta.status_code == 404


async def test_desactivar_nodo_exitoso(cliente_prueba, mock_supabase, nodo_ejemplo):
    """PATCH /nodos/{id}/desactivar → 200 con esta_activo=False."""
    nodo_inactivo = {**nodo_ejemplo, "esta_activo": False}
    mock_supabase.execute.return_value = MagicMock(data=[nodo_inactivo])

    respuesta = await cliente_prueba.patch(f"/api/v1/nodos/{NODO_ID}/desactivar")

    assert respuesta.status_code == 200
    assert respuesta.json()["esta_activo"] is False
