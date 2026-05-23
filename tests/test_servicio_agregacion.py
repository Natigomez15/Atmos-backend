"""
Pruebas unitarias para ServicioAgregacion (sin capa HTTP).
"""
from unittest.mock import MagicMock, patch, call
from uuid import UUID

from app.services.aggregation import ServicioAgregacion

SALA_UUID = UUID("00000000-0000-0000-0000-000000000002")


def _crear_mock_cliente() -> MagicMock:
    mock = MagicMock()
    for metodo in (
        "table", "select", "insert", "update", "upsert", "delete",
        "eq", "neq", "gt", "gte", "lt", "lte", "is_", "in_",
        "order", "limit", "single",
    ):
        getattr(mock, metodo).return_value = mock
    mock.execute.return_value = MagicMock(data=[])
    return mock


def _lecturas_falsas(cantidad: int, temperatura: float = 24.5, presencia: bool = True) -> list[dict]:
    return [
        {
            "temperatura":  temperatura,
            "humedad":      60.0,
            "presencia":    presencia,
            "potencia_w":   400.0,
            "energia_kwh":  float(i) * 0.1,
            "registrado_en": f"2025-01-01T13:{i:02d}:00+00:00",
        }
        for i in range(cantidad)
    ]


@patch("app.services.aggregation.obtener_cliente")
def test_agregar_ultima_hora_con_datos(mock_obtener):
    """agregar_ultima_hora devuelve el agregado cuando hay datos."""
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    lecturas = _lecturas_falsas(10)
    agregado_guardado = {
        "sala_id":            str(SALA_UUID),
        "cubo_hora":          "2025-01-01T13:00:00+00:00",
        "temperatura_promedio": 24.5,
        "razon_presencia":    1.0,
        "cantidad_lecturas":  10,
        "dia_semana":         2,
        "hora_del_dia":       13,
    }
    mock_cliente.execute.side_effect = [
        MagicMock(data=lecturas),         # sensor_readings query
        MagicMock(data=[agregado_guardado]),  # hourly_aggregates upsert
    ]

    resultado = ServicioAgregacion().agregar_ultima_hora(SALA_UUID)

    assert resultado is not None
    assert resultado["cantidad_lecturas"] == 10
    assert 0 <= resultado["dia_semana"] <= 6
    assert 0 <= resultado["hora_del_dia"] <= 23
    assert 0.0 <= resultado["razon_presencia"] <= 1.0


@patch("app.services.aggregation.obtener_cliente")
def test_agregar_ultima_hora_sin_datos(mock_obtener):
    """agregar_ultima_hora devuelve None y no hace upsert cuando no hay datos."""
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente
    mock_cliente.execute.return_value = MagicMock(data=[])  # sin lecturas

    resultado = ServicioAgregacion().agregar_ultima_hora(SALA_UUID)

    assert resultado is None
    # Verificar que no se hizo upsert
    mock_cliente.upsert.assert_not_called()


@patch("app.services.aggregation.obtener_cliente")
def test_razon_presencia_todos_presentes(mock_obtener):
    """razon_presencia == 1.0 cuando todos los registros tienen presencia=True."""
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    lecturas = _lecturas_falsas(10, presencia=True)
    mock_cliente.execute.side_effect = [
        MagicMock(data=lecturas),
        MagicMock(data=[{"razon_presencia": 1.0}]),
    ]

    resultado = ServicioAgregacion().agregar_ultima_hora(SALA_UUID)

    assert resultado is not None
    assert resultado["razon_presencia"] == 1.0


@patch("app.services.aggregation.obtener_cliente")
def test_razon_presencia_ninguno_presente(mock_obtener):
    """razon_presencia == 0.0 cuando ningún registro tiene presencia=True."""
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    lecturas = _lecturas_falsas(10, presencia=False)
    mock_cliente.execute.side_effect = [
        MagicMock(data=lecturas),
        MagicMock(data=[{"razon_presencia": 0.0}]),
    ]

    resultado = ServicioAgregacion().agregar_ultima_hora(SALA_UUID)

    assert resultado is not None
    assert resultado["razon_presencia"] == 0.0


@patch("app.services.aggregation.obtener_cliente")
def test_agregar_todas_las_salas_mixto(mock_obtener):
    """aggregate_all_rooms: 1 procesada, 1 omitida, 1 con error."""
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    sala_1 = "00000000-0000-0000-0000-000000000001"
    sala_2 = "00000000-0000-0000-0000-000000000002"
    sala_3 = "00000000-0000-0000-0000-000000000003"

    mock_cliente.execute.return_value = MagicMock(
        data=[
            {"sala_id": sala_1},
            {"sala_id": sala_2},
            {"sala_id": sala_3},
        ]
    )

    servicio = ServicioAgregacion()

    respuestas = {
        sala_1: {"sala_id": sala_1, "cantidad_lecturas": 5},
        sala_2: None,
    }

    original = servicio.agregar_ultima_hora

    def _mockear_por_sala(sala_id: UUID):
        key = str(sala_id)
        if key == sala_3:
            raise ConnectionError("error de conexión")
        return respuestas.get(key)

    servicio.agregar_ultima_hora = _mockear_por_sala

    resultado = servicio.agregar_todas_las_salas()

    assert resultado["procesadas"] == 1
    assert resultado["omitidas"] == 1
    assert len(resultado["errores"]) == 1
    assert "error de conexión" in resultado["errores"][0]
