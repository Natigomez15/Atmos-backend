import os

# Variables de entorno para pruebas — DEBEN ir antes de importar la app
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "clave-test-123")
os.environ.setdefault("CRON_SECRET", "secreto_cron_prueba")
os.environ.setdefault("ATMOS_DEVICE_TOKEN", "atmos-device-test")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-key")
os.environ.setdefault("FIREBASE_AUTH_DOMAIN", "test-proyecto.firebaseapp.com")
os.environ.setdefault("FIREBASE_DATABASE_URL", "https://test-proyecto-default-rtdb.firebaseio.com/")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-proyecto.appspot.com")

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from httpx import AsyncClient, ASGITransport

from app.main import aplicacion
from app.core.security import requerir_admin, requerir_mantenimiento_o_admin


# ---------------------------------------------------------------------------
# Utilidad: crea un mock de Supabase con encadenamiento completo
# ---------------------------------------------------------------------------

def _crear_mock_supabase() -> MagicMock:
    mock = MagicMock()
    # Cada método de la cadena devuelve el mismo mock
    for metodo in (
        "table", "select", "insert", "update", "upsert", "delete",
        "eq", "neq", "gt", "gte", "lt", "lte", "is_", "in_",
        "order", "limit", "single",
    ):
        getattr(mock, metodo).return_value = mock
    mock.execute.return_value = MagicMock(data=[])
    return mock


# ---------------------------------------------------------------------------
# Fixtures principales
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_supabase(monkeypatch) -> MagicMock:
    """Reemplaza obtener_cliente() con un mock en todos los módulos."""
    mock = _crear_mock_supabase()
    monkeypatch.setattr("app.core.database.obtener_cliente", lambda: mock)
    return mock


@pytest.fixture
async def cliente_prueba(mock_supabase) -> AsyncClient:
    """Cliente HTTP asíncrono que habla directamente con la app ASGI."""
    aplicacion.dependency_overrides[requerir_admin] = lambda: {
        "id": "usuario-admin-prueba", "rol": "admin", "esta_activo": True,
    }
    aplicacion.dependency_overrides[requerir_mantenimiento_o_admin] = lambda: {
        "id": "usuario-admin-prueba", "rol": "admin", "esta_activo": True,
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=aplicacion),
            base_url="http://test",
        ) as cliente:
            yield cliente
    finally:
        aplicacion.dependency_overrides.pop(requerir_admin, None)
        aplicacion.dependency_overrides.pop(requerir_mantenimiento_o_admin, None)


@pytest.fixture
def encabezados_cron() -> dict:
    """Cabecera X-Cron-Secret válida para endpoints protegidos."""
    return {"X-Cron-Secret": "secreto_cron_prueba"}


# ---------------------------------------------------------------------------
# Datos de ejemplo reutilizables
# ---------------------------------------------------------------------------

@pytest.fixture
def sala_ejemplo() -> dict:
    return {
        "id":        "00000000-0000-0000-0000-000000000002",
        "nombre":    "Salon 3A-101",
        "pabellon":  "Pabellon A",
        "capacidad": 30,
        "area_m2":   45.0,
        "piso":      1,
        "marca_ac":  "LG",
        "modelo_ac": "LW1216ER",
        "creado_en": "2025-01-01T00:00:00+00:00",
    }


@pytest.fixture
def nodo_ejemplo() -> dict:
    return {
        "id":               "00000000-0000-0000-0000-000000000001",
        "sala_id":          "00000000-0000-0000-0000-000000000002",
        "direccion_mac":    "AA:BB:CC:DD:EE:FF",
        "tipo_nodo":        "master",
        "version_firmware": "1.0.0",
        "esta_activo":      True,
        "ultima_vez_visto": "2025-01-01T12:00:00+00:00",
        "creado_en":        "2025-01-01T00:00:00+00:00",
    }


@pytest.fixture
def lectura_ejemplo() -> dict:
    return {
        "id":            1,
        "nodo_id":       "00000000-0000-0000-0000-000000000001",
        "sala_id":       "00000000-0000-0000-0000-000000000002",
        "registrado_en": "2025-01-01T14:00:00+00:00",
        "temperatura":   24.5,
        "humedad":       65.0,
        "presencia":     True,
        "setpoint_ac":   22,
        "ac_encendido":  True,
        "voltaje":       120.0,
        "corriente_a":   3.5,
        "potencia_w":    420.0,
        "energia_kwh":   1.25,
    }


@pytest.fixture
def comando_ejemplo() -> dict:
    return {
        "id":            1,
        "sala_id":       "00000000-0000-0000-0000-000000000002",
        "nodo_id":       None,
        "tipo_comando":  "setpoint",
        "setpoint":      22,
        "modo":          None,
        "origen":        "manual",
        "fue_ejecutado": False,
        "ejecutado_en":  None,
        "enviado_en":    "2025-01-01T14:00:00+00:00",
    }


@pytest.fixture
def alerta_ejemplo() -> dict:
    return {
        "id":           1,
        "sala_id":      "00000000-0000-0000-0000-000000000002",
        "nodo_id":      None,
        "tipo_alerta":  "node_offline",
        "severidad":    "high",
        "mensaje":      "El nodo AA:BB:CC no ha enviado datos",
        "detalle":      {"ultima_vez_visto": None},
        "esta_resuelta": False,
        "creado_en":    "2025-01-01T00:00:00+00:00",
        "resuelto_en":  None,
    }


@pytest.fixture
def prediccion_ejemplo() -> dict:
    return {
        "id":                        1,
        "sala_id":                   "00000000-0000-0000-0000-000000000002",
        "predicho_en":               "2025-01-01T14:00:00+00:00",
        "setpoint_recomendado":      22,
        "ahorro_predicho_pct":       15.0,
        "puntaje_confianza":         0.85,
        "version_modelo":            "rf_v1.0",
        "instantanea_caracteristicas": {},
        "fue_aplicado":              False,
        "ahorro_real_pct":           None,
    }
