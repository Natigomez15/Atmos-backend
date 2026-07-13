"""Metadatos e introspección del modelo ATMOS.

Expone información REAL del RandomForest entrenado (importancia de variables,
tipo, clases) y las métricas de validación persistidas durante el
entrenamiento. Si el modelo activo no tiene metadata de métricas todavía,
se reporta explícitamente "No disponible para esta versión" en lugar de
inventar valores. Ver también el script de entrenamiento
`modelo_ia_atmos_organizado.py`, donde se persiste el JSON de metadata.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.logger import log


ZONA_HORARIA = ZoneInfo("America/Panama")

# Etiquetas públicas de las variables del modelo. La clave es el nombre interno
# de la feature (tal como se entrenó el RandomForest); temp_ac se publica como
# "temperatura_salida_aire" porque proviene del sensor de salida del aire.
ETIQUETAS_FEATURES: dict[str, dict[str, str]] = {
    "presencia": {
        "nombre_publico": "presencia",
        "etiqueta": "Presencia",
        "descripcion": "Ocupación detectada en el espacio (sensor de presencia).",
    },
    "temp_ambiente": {
        "nombre_publico": "temp_ambiente",
        "etiqueta": "Temperatura ambiente",
        "descripcion": "Temperatura del ambiente medida por el sensor DHT.",
    },
    "temp_ac": {
        "nombre_publico": "temperatura_salida_aire",
        "etiqueta": "Temperatura de salida del aire",
        "descripcion": "Temperatura del aire que expulsa el equipo, medida por el sensor DS18B20.",
    },
    "delta_t": {
        "nombre_publico": "delta_t",
        "etiqueta": "Diferencia de temperatura (ΔT)",
        "descripcion": "Diferencia entre la temperatura ambiente y la de salida del aire.",
    },
    "humedad": {
        "nombre_publico": "humedad",
        "etiqueta": "Humedad relativa",
        "descripcion": "Humedad relativa del ambiente.",
    },
}


def ruta_metadata(ruta_modelo: Path) -> Path:
    """Ruta del JSON de metadata que acompaña al .pkl del modelo."""
    return ruta_modelo.with_name("modelo_atmos_metadata.json")


def cargar_metadata_entrenamiento(ruta_modelo: Path) -> dict | None:
    """Lee las métricas de validación persistidas junto al modelo.

    Devuelve None si el modelo activo se entrenó antes de que el pipeline
    persistiera metadata (caso actual): el frontend debe mostrar
    "No disponible para esta versión".
    """
    ruta = ruta_metadata(ruta_modelo)
    if not ruta.exists():
        return None
    try:
        with ruta.open(encoding="utf-8") as archivo:
            return json.load(archivo)
    except (OSError, json.JSONDecodeError) as error:
        log.warning({
            "evento": "metadata_modelo_no_legible",
            "ruta": str(ruta),
            "error": str(error),
        })
        return None


def importancia_variables(modelo) -> list[dict]:
    """Importancia REAL de cada variable, calculada durante el entrenamiento.

    Se lee de `feature_importances_` del RandomForest cargado, ordenada de
    mayor a menor y expresada en porcentaje.
    """
    importancias = getattr(modelo, "feature_importances_", None)
    nombres = getattr(modelo, "feature_names_in_", None)
    if importancias is None or nombres is None:
        return []

    filas = []
    for nombre, peso in zip(nombres, importancias):
        meta = ETIQUETAS_FEATURES.get(str(nombre), {})
        filas.append({
            "variable": meta.get("nombre_publico", str(nombre)),
            "etiqueta": meta.get("etiqueta", str(nombre)),
            "descripcion": meta.get("descripcion"),
            "importancia_pct": round(float(peso) * 100, 2),
        })

    filas.sort(key=lambda fila: fila["importancia_pct"], reverse=True)
    return filas


def construir_panel_modelo(modelo, ruta_modelo: Path, version: str) -> dict:
    """Datos para el panel 'Sobre el motor' de la vista de predicciones."""
    metadata = cargar_metadata_entrenamiento(ruta_modelo)
    entrenado = None
    if metadata and metadata.get("fecha_entrenamiento"):
        entrenado = _formatear_fecha_panama(metadata["fecha_entrenamiento"])

    return {
        "tipo_modelo": type(modelo).__name__,
        "version_modelo": version,
        "n_estimadores": getattr(modelo, "n_estimators", None),
        "clases": [str(c) for c in getattr(modelo, "classes_", [])],
        "importancia_variables": importancia_variables(modelo),
        "metricas_disponibles": metadata is not None,
        "metricas": metadata,
        "fecha_entrenamiento": entrenado,
    }


def _formatear_fecha_panama(iso: str) -> str | None:
    try:
        fecha = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha.astimezone(ZONA_HORARIA).strftime("%d/%m/%Y %H:%M")
