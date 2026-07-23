from unittest.mock import MagicMock

import pytest

from app.ml.predictor import ServicioPredictor


def _registro(fecha, potencia):
    return {
        "fecha_sync": fecha,
        "temperatura_ambiente": 25,
        "humedad": 60,
        "potencia_w": potencia,
        "estado_ocupacion": True,
    }


def _historico(monkeypatch, potencia_agregada, registros):
    cliente = MagicMock()
    for metodo in ("table", "select", "eq", "gte", "gt", "order"):
        getattr(cliente, metodo).return_value = cliente
    cliente.execute.side_effect = [
        MagicMock(data=[{
            "cubo_hora": "2026-07-17T14:00:00Z",
            "temperatura_promedio": 25,
            "humedad_promedio": 60,
            "razon_presencia": 1,
            "potencia_promedio_w": potencia_agregada,
            "energia_total_kwh": 0,
            "dia_semana": 4,
            "hora_del_dia": 14,
            "cantidad_lecturas": 2,
        }]),
        MagicMock(data=registros),
    ]
    monkeypatch.setattr("app.ml.predictor.obtener_cliente", lambda: cliente)
    return ServicioPredictor().obtener_caracteristicas_entrenamiento("sala-1", 7)


@pytest.mark.parametrize("potencia_agregada", [0, None])
def test_reemplaza_agregado_no_utilizable_con_promedio_de_registros(monkeypatch, potencia_agregada):
    resultado = _historico(monkeypatch, potencia_agregada, [
        _registro("2026-07-17T14:10:00+00:00", 1000),
        _registro("2026-07-17T14:20:00+00:00", 1200),
    ])

    assert resultado[0]["potencia_promedio_w"] == 1100.0
    assert resultado[0]["fuente"] == "hourly_aggregates+registros"


def test_conserva_agregado_positivo(monkeypatch):
    resultado = _historico(monkeypatch, 900, [
        _registro("2026-07-17T14:10:00+00:00", 1000),
        _registro("2026-07-17T14:20:00+00:00", 1200),
    ])

    assert resultado[0]["potencia_promedio_w"] == 900
    assert resultado[0]["fuente"] == "hourly_aggregates"


@pytest.mark.parametrize("registros", [[], [
    _registro("2026-07-17T14:10:00+00:00", None),
    _registro("2026-07-17T14:20:00+00:00", "invalida"),
]])
def test_conserva_fallback_si_registros_no_aportan_potencia(monkeypatch, registros):
    resultado = _historico(monkeypatch, 0, registros)

    assert resultado[0]["potencia_promedio_w"] == 0.0
    assert resultado[0]["fuente"] == "hourly_aggregates"


def test_acepta_potencia_numerica_en_texto_y_descarta_valor_malformado(monkeypatch):
    resultado = _historico(monkeypatch, "sin valor", [
        _registro("2026-07-17T14:10:00+00:00", "1000"),
        _registro("2026-07-17T14:20:00+00:00", "malformada"),
        _registro("2026-07-17T14:30:00+00:00", "1200"),
    ])

    assert resultado[0]["potencia_promedio_w"] == 1100.0
    assert resultado[0]["fuente"] == "hourly_aggregates+registros"
