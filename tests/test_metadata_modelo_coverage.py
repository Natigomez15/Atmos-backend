import json
from pathlib import Path

from app.ml import metadata_modelo as metadata


class ModeloFalso:
    n_estimators = 25
    classes_ = ["mantener", "enfriar_fuerte"]
    feature_names_in_ = ["humedad", "temp_ac", "desconocida"]
    feature_importances_ = [0.1, 0.7, 0.2]


def test_metadata_ausente_devuelve_none_y_ruta_hermana(tmp_path):
    ruta_modelo = tmp_path / "modelo.pkl"

    assert metadata.ruta_metadata(ruta_modelo) == tmp_path / "modelo_atmos_metadata.json"
    assert metadata.cargar_metadata_entrenamiento(ruta_modelo) is None


def test_metadata_valida_e_invalida_se_manejan_de_forma_controlada(tmp_path):
    ruta_modelo = tmp_path / "modelo.pkl"
    ruta = metadata.ruta_metadata(ruta_modelo)
    ruta.write_text(json.dumps({"fecha_entrenamiento": "2026-07-20T10:00:00Z", "accuracy": 0.91}), encoding="utf-8")

    assert metadata.cargar_metadata_entrenamiento(ruta_modelo)["accuracy"] == 0.91

    ruta.write_text("{json roto", encoding="utf-8")
    assert metadata.cargar_metadata_entrenamiento(ruta_modelo) is None


def test_importancias_reales_ordenan_etiquetan_y_modelo_incompleto_devuelve_vacio():
    filas = metadata.importancia_variables(ModeloFalso())

    assert [fila["variable"] for fila in filas] == ["temperatura_salida_aire", "desconocida", "humedad"]
    assert filas[0]["importancia_pct"] == 70.0
    assert metadata.importancia_variables(object()) == []


def test_panel_incluye_metricas_fecha_y_fallback_de_fecha_invalida(tmp_path):
    ruta_modelo = tmp_path / "modelo.pkl"
    metadata.ruta_metadata(ruta_modelo).write_text(
        json.dumps({"fecha_entrenamiento": "2026-07-20T10:30:00Z", "precision": 0.8}),
        encoding="utf-8",
    )

    panel = metadata.construir_panel_modelo(ModeloFalso(), ruta_modelo, "v2")

    assert panel["tipo_modelo"] == "ModeloFalso"
    assert panel["metricas_disponibles"] is True
    assert panel["fecha_entrenamiento"] == "20/07/2026 05:30"
    assert metadata._formatear_fecha_panama("fecha inválida") is None
    assert metadata._formatear_fecha_panama("2026-07-20T10:30:00") == "20/07/2026 05:30"
