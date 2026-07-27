from datetime import datetime, timedelta, timezone

from app.config import configuracion
from app.ml import predictor as modulo_predictor
from app.ml.predictor import ServicioPredictor


def _lectura(timestamp: datetime) -> dict:
    return {
        "timestamp": timestamp.isoformat(),
        "temp_ambiente": 28.4,
        "temp_ac": 18.2,
        "humedad": 70.5,
        "presencia": 1,
        "delta_t": 10.2,
        "potencia_w": 820.0,
    }


def _preparar_servicio(monkeypatch, timestamp: datetime) -> ServicioPredictor:
    servicio = ServicioPredictor()
    monkeypatch.setattr(modulo_predictor, "obtener_firebase", lambda: object())
    monkeypatch.setattr(
        servicio,
        "obtener_ultima_lectura_firebase",
        lambda *_args: {
            "valida": True,
            "firebase_key": "-firebase-reading-id",
            "lectura": _lectura(timestamp),
            "lecturas_invalidas_ignoradas": 0,
            "advertencias": [],
            "diagnostico": {"ultima_lectura_valida_key": "-firebase-reading-id"},
        },
    )
    monkeypatch.setattr(
        servicio,
        "preparar_lectura_firebase",
        lambda lectura: {
            "temp_ambiente": lectura["temp_ambiente"],
            "temp_ac": lectura["temp_ac"],
            "humedad": lectura["humedad"],
            "presencia": lectura["presencia"],
            "delta_t": lectura["delta_t"],
        },
    )
    monkeypatch.setattr(
        servicio,
        "obtener_estado_horario_operacion",
        lambda: {"dentro_horario": True},
    )
    return servicio


def test_prediccion_actual_usa_timestamp_e_id_de_ultima_lectura(monkeypatch):
    ahora = datetime.now(timezone.utc)
    servicio = _preparar_servicio(monkeypatch, ahora - timedelta(seconds=2))
    monkeypatch.setattr(
        servicio,
        "decidir_atmos",
        lambda _datos: {
            "valido": True,
            "procesado": True,
            "modelo_ml": {
                "modelo_usado": True,
                "prediccion_modelo": "encender_22",
                "probabilidades": {"encender_22": 0.91},
            },
            "control": {
                "ejecutar_ir": False,
                "decision_final": "encender_22",
            },
        },
    )
    monkeypatch.setattr(servicio, "traducir_accion_esp32", lambda _resultado: "encender_22")
    monkeypatch.setattr(servicio, "cerrar_alerta_ml_pendiente", lambda *_args: None)
    monkeypatch.setattr(
        servicio,
        "guardar_decision_en_registro",
        lambda **_kwargs: {"actualizado": True},
    )
    monkeypatch.setattr(
        servicio,
        "guardar_prediccion_modelo_valida",
        lambda **_kwargs: {"guardada": True},
    )

    resultado = servicio.decidir_atmos_desde_firebase("robotica", "Aire_1")

    assert resultado["firebase_key_usado"] == "robotica_Aire_1_-firebase-reading-id"
    assert resultado["lectura_desactualizada"] is False
    assert resultado["edad_lectura_segundos"] <= 5
    assert resultado["lectura_timestamp"]
    assert resultado["prediccion_timestamp"]
    assert resultado["accion"] == "encender_22"
    assert resultado["modelo_ml"]["probabilidades"]["encender_22"] == 0.91


def test_lectura_firebase_vencida_no_ejecuta_modelo(monkeypatch):
    timestamp = datetime.now(timezone.utc) - timedelta(
        seconds=configuracion.ATMOS_MAX_LECTURA_EDAD_SECONDS + 60
    )
    servicio = _preparar_servicio(monkeypatch, timestamp)
    decidir = lambda _datos: (_ for _ in ()).throw(AssertionError("ML no debe ejecutarse"))
    monkeypatch.setattr(servicio, "decidir_atmos", decidir)

    resultado = servicio.decidir_atmos_desde_firebase("robotica", "Aire_1")

    assert resultado["lectura_desactualizada"] is True
    assert resultado["resultado_modelo"] is None
    assert resultado["accion"] is None
    assert resultado["edad_lectura_segundos"] > configuracion.ATMOS_MAX_LECTURA_EDAD_SECONDS
