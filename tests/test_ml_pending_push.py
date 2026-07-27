from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.ml.predictor import ServicioPredictor


class ReferenciaMemoria:
    def __init__(self, datos=None):
        self.datos = dict(datos or {})

    def get(self):
        return dict(self.datos)

    def update(self, cambios):
        self.datos.update(cambios)


def preparar_servicio(monkeypatch, actual=None):
    servicio = ServicioPredictor()
    referencia = ReferenciaMemoria(actual)
    historial = {}

    monkeypatch.setattr(
        servicio,
        "_referencia_alerta_ml",
        lambda *_args: referencia,
    )
    monkeypatch.setattr(
        servicio,
        "_referencia_historial_alerta_ml",
        lambda _db, _area, _aire, decision_id: historial.setdefault(
            decision_id,
            ReferenciaMemoria(),
        ),
    )
    return servicio, referencia, historial


def test_decision_nueva_genera_un_solo_push_y_reintento_no_duplica(monkeypatch):
    servicio, referencia, _ = preparar_servicio(monkeypatch)
    push = MagicMock(return_value={
        "enviadas": 1,
        "fallidas": 0,
        "total_suscriptores": 1,
    })
    monkeypatch.setattr("app.ml.predictor.notificar_decision_ml_pendiente", push)

    primera = servicio.registrar_alerta_ml_pendiente(
        object(),
        "robotica",
        "Aire_1",
        "encender_22",
        metadata={"confianza_ml": 0.91},
        lectura={"temperatura_ambiente": 28.4},
    )
    segunda = servicio.registrar_alerta_ml_pendiente(
        object(),
        "robotica",
        "Aire_1",
        "encender_22",
        metadata={"confianza_ml": 0.91},
        lectura={"temperatura_ambiente": 28.4},
    )

    assert primera["creada"] is True
    assert segunda["creada"] is False
    assert primera["decision_id"] == segunda["decision_id"]
    push.assert_called_once()
    assert referencia.datos["push_status"] == "enviada"
    assert referencia.datos["push_sent_at"]


def test_rechazar_no_publica_comando(monkeypatch):
    servicio, referencia, _ = preparar_servicio(monkeypatch, {
        "decision_id": "decision-1",
        "estado": "pendiente",
        "accion": "encender_22",
    })
    publicar = MagicMock()
    monkeypatch.setattr(servicio, "publicar_comando_si_cambio", publicar)
    monkeypatch.setattr("app.ml.predictor.obtener_firebase", lambda: object())

    resultado = servicio.rechazar_decision_ml_pendiente(
        "robotica",
        "Aire_1",
        "decision-1",
        {"id": "u-1", "nombre": "Admin", "rol": "admin"},
    )

    assert resultado["estado"] == "rechazada"
    assert resultado["firebase_escrito"] is False
    assert referencia.datos["estado"] == "rechazada"
    publicar.assert_not_called()


def test_aceptar_expirada_no_publica_comando(monkeypatch):
    servicio, referencia, _ = preparar_servicio(monkeypatch, {
        "decision_id": "decision-expirada",
        "estado": "pendiente",
        "accion": "encender_22",
        "expira_en": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    publicar = MagicMock()
    monkeypatch.setattr(servicio, "publicar_comando_si_cambio", publicar)
    monkeypatch.setattr("app.ml.predictor.obtener_firebase", lambda: object())

    with pytest.raises(ValueError, match="expiró"):
        servicio.aceptar_decision_ml_pendiente(
            "robotica",
            "Aire_1",
            "decision-expirada",
            {"id": "u-1", "nombre": "Admin", "rol": "admin"},
        )

    publicar.assert_not_called()
    assert referencia.datos["estado"] == "obsoleta"


def test_aceptar_vigente_publica_y_actualiza_estado(monkeypatch):
    servicio, referencia, _ = preparar_servicio(monkeypatch, {
        "decision_id": "decision-vigente",
        "estado": "pendiente",
        "accion": "encender_22",
        "expira_en": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    })
    publicar = MagicMock(return_value={
        "publicado": True,
        "motivo": "accion_cambiada",
        "command_id": "command-1",
    })
    monkeypatch.setattr(servicio, "publicar_comando_si_cambio", publicar)
    monkeypatch.setattr("app.ml.predictor.obtener_firebase", lambda: object())

    resultado = servicio.aceptar_decision_ml_pendiente(
        "robotica",
        "Aire_1",
        "decision-vigente",
        {"id": "u-1", "nombre": "Admin", "rol": "admin"},
    )

    assert resultado["estado"] == "aceptada"
    assert resultado["firebase_escrito"] is True
    assert referencia.datos["estado"] == "aceptada"
    publicar.assert_called_once()
