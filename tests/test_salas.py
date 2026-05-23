"""
Pruebas para el router /api/v1/salas
"""
from unittest.mock import MagicMock

SALA_ID = "00000000-0000-0000-0000-000000000002"

CUERPO_CREAR = {
    "nombre":    "Salon 3A-101",
    "pabellon":  "Pabellon A",
    "capacidad": 30,
    "area_m2":   45.0,
    "piso":      1,
    "marca_ac":  "LG",
    "modelo_ac": "LW1216ER",
}


async def test_crear_sala_exitoso(cliente_prueba, mock_supabase, sala_ejemplo):
    """POST /salas → 201 con los datos de la sala creada."""
    mock_supabase.execute.return_value = MagicMock(data=[sala_ejemplo])

    respuesta = await cliente_prueba.post("/api/v1/salas/", json=CUERPO_CREAR)

    assert respuesta.status_code == 201
    datos = respuesta.json()
    assert datos["nombre"] == "Salon 3A-101"
    assert datos["pabellon"] == "Pabellon A"


async def test_crear_sala_fallo_supabase(cliente_prueba, mock_supabase):
    """POST /salas → 400 cuando Supabase no devuelve datos."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.post("/api/v1/salas/", json=CUERPO_CREAR)

    assert respuesta.status_code == 400


async def test_listar_salas_exitoso(cliente_prueba, mock_supabase, sala_ejemplo):
    """GET /salas → 200 con lista de salas."""
    mock_supabase.execute.return_value = MagicMock(data=[sala_ejemplo])

    respuesta = await cliente_prueba.get("/api/v1/salas/")

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert isinstance(datos, list)
    assert len(datos) == 1
    assert datos[0]["nombre"] == "Salon 3A-101"


async def test_obtener_sala_por_id_exitoso(cliente_prueba, mock_supabase, sala_ejemplo):
    """GET /salas/{id} → 200 con la sala encontrada."""
    mock_supabase.execute.return_value = MagicMock(data=sala_ejemplo)

    respuesta = await cliente_prueba.get(f"/api/v1/salas/{SALA_ID}")

    assert respuesta.status_code == 200
    assert respuesta.json()["nombre"] == "Salon 3A-101"


async def test_obtener_sala_no_encontrada(cliente_prueba, mock_supabase):
    """GET /salas/{id} → 404 cuando la sala no existe."""
    mock_supabase.execute.return_value = MagicMock(data=None)

    respuesta = await cliente_prueba.get(f"/api/v1/salas/id-inexistente")

    assert respuesta.status_code == 404


async def test_actualizar_sala_exitoso(cliente_prueba, mock_supabase, sala_ejemplo):
    """PATCH /salas/{id} → 200 con la sala actualizada."""
    sala_actualizada = {**sala_ejemplo, "capacidad": 35}
    mock_supabase.execute.return_value = MagicMock(data=[sala_actualizada])

    respuesta = await cliente_prueba.patch(
        f"/api/v1/salas/{SALA_ID}",
        json={"capacidad": 35},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["capacidad"] == 35


async def test_actualizar_sala_sin_campos(cliente_prueba, mock_supabase):
    """PATCH /salas/{id} → 400 si no se provee ningún campo."""
    respuesta = await cliente_prueba.patch(f"/api/v1/salas/{SALA_ID}", json={})

    assert respuesta.status_code == 400


async def test_actualizar_sala_no_encontrada(cliente_prueba, mock_supabase):
    """PATCH /salas/{id} → 404 si la sala no existe."""
    mock_supabase.execute.return_value = MagicMock(data=[])

    respuesta = await cliente_prueba.patch(
        f"/api/v1/salas/{SALA_ID}",
        json={"capacidad": 35},
    )

    assert respuesta.status_code == 404
