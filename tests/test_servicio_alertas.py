"""
Pruebas unitarias para ServicioAlertas (sin capa HTTP).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import UUID

from app.services.alert_service import ServicioAlertas

SALA_ID = "00000000-0000-0000-0000-000000000002"
NODO_ID = "00000000-0000-0000-0000-000000000001"


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


def _nodo_offline() -> dict:
    hace_20_min = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    return {
        "id":            NODO_ID,
        "sala_id":       SALA_ID,
        "direccion_mac": "AA:BB:CC:DD:EE:FF",
        "esta_activo":   True,
        "ultima_vez_visto": hace_20_min,
        "version_firmware": "1.0.0",
    }


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_verificar_nodos_desconectados_crea_alerta(mock_obtener, mock_gestor):
    """Crea alerta node_offline cuando el nodo lleva más de 10 min sin datos."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    alerta_nueva = {
        "id": 1, "tipo_alerta": "node_offline",
        "severidad": "high", "sala_id": SALA_ID, "nodo_id": NODO_ID,
        "mensaje": "El nodo AA:BB:CC:DD:EE:FF no ha enviado datos en más de 10 minutos",
        "detalle": {}, "esta_resuelta": False,
        "creado_en": "2025-01-01T00:00:00+00:00", "resuelto_en": None,
    }
    mock_cliente.execute.side_effect = [
        MagicMock(data=[_nodo_offline()]),  # nodos activos
        MagicMock(data=[]),                 # alerta existente → vacía (no duplicado)
        MagicMock(data=[alerta_nueva]),     # insertar alerta
    ]

    resultado = ServicioAlertas().verificar_nodos_desconectados()

    assert len(resultado) == 1
    assert resultado[0]["tipo_alerta"] == "node_offline"
    assert resultado[0]["severidad"] == "high"


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_verificar_nodos_no_duplica_alerta(mock_obtener, mock_gestor):
    """No crea una segunda alerta si ya existe una sin resolver para ese nodo."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    mock_cliente.execute.side_effect = [
        MagicMock(data=[_nodo_offline()]),            # nodos activos
        MagicMock(data=[{"id": 99}]),                 # alerta existente → existe
    ]

    resultado = ServicioAlertas().verificar_nodos_desconectados()

    assert resultado == []
    # No debe haberse llamado insert
    mock_cliente.insert.assert_not_called()


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_verificar_anomalia_potencia_crea_alerta(mock_obtener, mock_gestor):
    """Crea alerta power_anomaly cuando el consumo supera 1.5× el promedio histórico."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    alerta_potencia = {
        "id": 2, "tipo_alerta": "power_anomaly", "severidad": "medium",
        "sala_id": SALA_ID, "nodo_id": None,
        "mensaje": "Consumo elevado", "detalle": {"exceso_pct": 75.0},
        "esta_resuelta": False, "creado_en": "2025-01-01T00:00:00+00:00", "resuelto_en": None,
    }
    mock_cliente.execute.side_effect = [
        MagicMock(data=[{"sala_id": SALA_ID}]),         # nodos activos
        MagicMock(data=[{"potencia_promedio_w": 400.0}]),# baseline histórico
        MagicMock(data=[{"potencia_w": 700.0}]),         # consumo actual
        MagicMock(data=[]),                              # alerta existente → vacía
        MagicMock(data={"nombre": "Salon Test"}),        # nombre sala (single)
        MagicMock(data=[alerta_potencia]),               # insertar
    ]

    resultado = ServicioAlertas().verificar_anomalias_potencia()

    assert len(resultado) == 1
    assert resultado[0]["tipo_alerta"] == "power_anomaly"


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_verificar_anomalia_potencia_dentro_umbral(mock_obtener, mock_gestor):
    """No crea alerta cuando el consumo está dentro del 1.5× del promedio."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    mock_cliente.execute.side_effect = [
        MagicMock(data=[{"sala_id": SALA_ID}]),          # nodos activos
        MagicMock(data=[{"potencia_promedio_w": 400.0}]),# baseline 400 W
        MagicMock(data=[{"potencia_w": 500.0}]),         # actual 500 W → 1.25× (< 1.5)
    ]

    resultado = ServicioAlertas().verificar_anomalias_potencia()

    assert resultado == []


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_verificar_temperatura_estancada_crea_alerta(mock_obtener, mock_gestor):
    """Crea alerta temperature_stuck cuando temp no baja con AC encendido."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    # 30 lecturas con temperatura 27.5°C (creciente: primeras 10 = 27.3, últimas 10 = 27.7)
    lecturas = []
    for i in range(30):
        lecturas.append({
            "temperatura":   27.3 + (i * 0.02),  # sube ligeramente
            "presencia":     True,
            "ac_encendido":  True,
            "setpoint_ac":   22,
            "registrado_en": f"2025-01-01T13:{i:02d}:00+00:00",
        })

    alerta_temp = {
        "id": 3, "tipo_alerta": "temperature_stuck", "severidad": "medium",
        "sala_id": SALA_ID, "nodo_id": None,
        "mensaje": "AC encendido pero temperatura no baja",
        "detalle": {}, "esta_resuelta": False,
        "creado_en": "2025-01-01T00:00:00+00:00", "resuelto_en": None,
    }
    mock_cliente.execute.side_effect = [
        MagicMock(data=[{"sala_id": SALA_ID}]),  # nodos activos
        MagicMock(data=lecturas),                # lecturas con AC encendido
        MagicMock(data=[]),                      # alerta existente → vacía
        MagicMock(data={"nombre": "Salon Test"}),# nombre sala (single)
        MagicMock(data=[alerta_temp]),           # insertar
    ]

    resultado = ServicioAlertas().verificar_temperatura_estancada()

    assert len(resultado) == 1
    assert resultado[0]["tipo_alerta"] == "temperature_stuck"


@patch("app.services.alert_service.gestor")
@patch("app.services.alert_service.obtener_cliente")
def test_resolver_alertas_obsoletas_nodo_reconectado(mock_obtener, mock_gestor):
    """Resuelve alerta node_offline cuando el nodo volvió a conectarse."""
    mock_gestor.transmitir_a_todos = AsyncMock()
    mock_cliente = _crear_mock_cliente()
    mock_obtener.return_value = mock_cliente

    hace_2_min = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()

    mock_cliente.execute.side_effect = [
        # node_offline
        MagicMock(data=[{"id": 1, "nodo_id": NODO_ID}]),   # alertas sin resolver
        MagicMock(data={"ultima_vez_visto": hace_2_min}),   # nodo reconectado (single)
        MagicMock(data=[]),                                  # update alerta
        # power_anomaly
        MagicMock(data=[]),
        # temperature_stuck
        MagicMock(data=[]),
    ]

    total = ServicioAlertas().resolver_alertas_obsoletas()

    assert total == 1
