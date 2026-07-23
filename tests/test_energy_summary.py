from app.api.compat import _resumir_consumo_total_aires
from app.config import configuracion


def test_resumen_usa_consumo_y_costo_intervalo_de_un_equipo():
    resumen = _resumir_consumo_total_aires([
        {
            "pabellon": "robotica",
            "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.25,
            "costo_intervalo": 0.05,
        }
    ])

    assert resumen == {
        "total_energy_kwh": 0.25,
        "total_cost_usd": 0.05,
        "rooms_count": 1,
    }


def test_resumen_suma_intervalos_de_equipos_independientes():
    resumen = _resumir_consumo_total_aires([
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:02:00+00:00",
            "consumo_intervalo_kwh": 0.2, "costo_intervalo": 0.04,
        },
        {
            "pabellon": "robotica", "aire": "Aire_2",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.3, "costo_intervalo": 0.06,
        },
    ])

    assert resumen == {
        "total_energy_kwh": 0.5,
        "total_cost_usd": 0.1,
        "rooms_count": 2,
    }


def test_resumen_calcula_fallback_de_potencia_y_tarifa():
    resumen = _resumir_consumo_total_aires([
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:00:00+00:00",
            "potencia_activa_w": 1200,
        },
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:10:00+00:00",
            "tarifa_kwh": 0.25,
        },
    ])

    assert resumen == {
        "total_energy_kwh": 0.2,
        "total_cost_usd": 0.05,
        "rooms_count": 1,
    }


def test_resumen_maneja_vacio_y_datos_malformados(monkeypatch):
    assert _resumir_consumo_total_aires([]) == {
        "total_energy_kwh": 0.0,
        "total_cost_usd": 0.0,
        "rooms_count": 0,
    }

    monkeypatch.setattr(configuracion, "DASHBOARD_TARIFA_USD_KWH", 0.18)
    resumen = _resumir_consumo_total_aires([
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": None,
            "consumo_intervalo_kwh": None,
            "costo_intervalo": "invalido",
            "tarifa_kwh": "invalida",
        },
        {
            "pabellon": "robotica", "aire": "Aire_2",
            "fecha_sync": "fecha-invalida",
            "consumo_intervalo_kwh": "0.1",
            "costo_intervalo": None,
            "tarifa_kwh": None,
        },
    ])

    assert resumen == {
        "total_energy_kwh": 0.1,
        "total_cost_usd": 0.018,
        "rooms_count": 1,
    }
