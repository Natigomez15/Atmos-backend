from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.ml.impacto import estimar_consumo_registro, inferir_ac_encendido
from app.ml.predictor import ServicioPredictor
from app.config import configuracion
from app.api.readings import (
    _agrupar_potencia_activa_firebase,
    _completar_potencia_historica_desde_firebase,
    _normalizar_historial_firebase,
)
from app.api.compat import _resumir_consumo_total_aires
from app.services.dashboard_energy import construir_resumen_dashboard
from app.services.sincronizador_firebase import (
    MAX_LECTURAS_FIREBASE_POR_CONSULTA,
    leer_ultimas_lecturas_firebase_rest,
    preparar_registro_supabase,
    sincronizar_firebase_supabase,
)


def test_historico_ml_reemplaza_potencia_agregada_cero_con_registros_reales(monkeypatch):
    cliente = MagicMock()
    for metodo in ("table", "select", "eq", "gte", "gt", "order"):
        getattr(cliente, metodo).return_value = cliente
    cliente.execute.side_effect = [
        MagicMock(data=[{
            "cubo_hora": "2026-07-17T14:00:00Z",
            "temperatura_promedio": 25.0,
            "humedad_promedio": 60.0,
            "razon_presencia": 0.5,
            "potencia_promedio_w": 0.0,
            "energia_total_kwh": 0.0,
            "dia_semana": 4,
            "hora_del_dia": 14,
            "cantidad_lecturas": 2,
        }]),
        MagicMock(data=[
            {
                "fecha_sync": "2026-07-17T14:10:00+00:00",
                "temperatura_ambiente": 25.0,
                "humedad": 60.0,
                "potencia_w": 1000.0,
                "estado_ocupacion": True,
            },
            {
                "fecha_sync": "2026-07-17T14:20:00+00:00",
                "temperatura_ambiente": 25.2,
                "humedad": 61.0,
                "potencia_w": 1200.0,
                "estado_ocupacion": False,
            },
        ]),
    ]
    monkeypatch.setattr("app.ml.predictor.obtener_cliente", lambda: cliente)

    resultado = ServicioPredictor().obtener_caracteristicas_entrenamiento(
        "78c6626d-940e-4168-a94e-3da8ddc2add0", 7
    )

    assert resultado[0]["potencia_promedio_w"] == 1100.0
    assert resultado[0]["fuente"] == "hourly_aggregates+registros"


def test_historico_potencia_firebase_usa_solo_potencia_activa(monkeypatch):
    ahora = datetime(2026, 7, 17, 15, tzinfo=timezone.utc)
    fechas = {
        "lectura_activa_w": datetime(2026, 7, 17, 14, 10, tzinfo=timezone.utc),
        "lectura_activa_kw": datetime(2026, 7, 17, 14, 20, tzinfo=timezone.utc),
        "lectura_legada": datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(
        "app.api.readings._fecha_desde_firebase_push_id",
        lambda clave: fechas[clave],
    )

    resultado = _agrupar_potencia_activa_firebase({
        "lectura_activa_w": {"potencia_activa_w": 1400, "potencia_w": 0},
        "lectura_activa_kw": {"potencia_activa_kw": 1.6},
        "lectura_legada": {"potencia_w": 900},
    }, dias=1, ahora=ahora)

    assert resultado == [{
        "bucket_hour": "2026-07-17T14:00:00+00:00",
        "avg_power_w": 1500.0,
        "reading_count": 2,
        "source": "firebase_potencia_activa_w",
    }]


def test_lecturas_firebase_rest_limita_cualquier_solicitud_a_100(monkeypatch):
    urls = []

    class Respuesta:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    monkeypatch.setattr(
        "app.services.sincronizador_firebase.httpx.get",
        lambda url, timeout: (urls.append(url) or Respuesta()),
    )

    leer_ultimas_lecturas_firebase_rest("robotica", "Aire_1", limite=20_000)

    assert f"limitToLast={MAX_LECTURAS_FIREBASE_POR_CONSULTA}" in urls[0]
    assert "Atmos/registro/robotica/Aire_1/lecturas.json" in urls[0]


def test_sincronizacion_rechaza_lectura_global_de_registro():
    try:
        sincronizar_firebase_supabase(pabellon_objetivo=None, aire_objetivo=None)
    except ValueError as error:
        assert "/Atmos/registro completo" in str(error)
    else:
        raise AssertionError("La sincronización global no debe estar permitida")


def test_consumo_total_suma_intervalos_de_todos_los_aires():
    filas = [
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.10,
            "tarifa_kwh": 0.18,
            "costo_intervalo": 0.018,
        },
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:02:00+00:00",
            "consumo_intervalo_kwh": 0.20,
            "tarifa_kwh": 0.18,
            "costo_intervalo": 0.036,
        },
        {
            "pabellon": "robotica", "aire": "Aire_2",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.30,
            "tarifa_kwh": 0.20,
            "costo_intervalo": 0.060,
        },
    ]

    resumen = _resumir_consumo_total_aires(filas)

    assert resumen["total_energy_kwh"] == 0.6
    assert resumen["total_cost_usd"] == 0.114
    assert resumen["rooms_count"] == 2


def test_historico_ml_reemplaza_potencia_agregada_cero_con_registros_reales(monkeypatch):
    cliente = MagicMock()
    for metodo in ("table", "select", "eq", "gte", "gt", "order"):
        getattr(cliente, metodo).return_value = cliente
    cliente.execute.side_effect = [
        MagicMock(data=[{
            "cubo_hora": "2026-07-17T14:00:00Z",
            "temperatura_promedio": 25.0,
            "humedad_promedio": 60.0,
            "razon_presencia": 0.5,
            "potencia_promedio_w": 0.0,
            "energia_total_kwh": 0.0,
            "dia_semana": 4,
            "hora_del_dia": 14,
            "cantidad_lecturas": 2,
        }]),
        MagicMock(data=[
            {
                "fecha_sync": "2026-07-17T14:10:00+00:00",
                "temperatura_ambiente": 25.0,
                "humedad": 60.0,
                "potencia_w": 1000.0,
                "estado_ocupacion": True,
            },
            {
                "fecha_sync": "2026-07-17T14:20:00+00:00",
                "temperatura_ambiente": 25.2,
                "humedad": 61.0,
                "potencia_w": 1200.0,
                "estado_ocupacion": False,
            },
        ]),
    ]
    monkeypatch.setattr("app.ml.predictor.obtener_cliente", lambda: cliente)

    resultado = ServicioPredictor().obtener_caracteristicas_entrenamiento(
        "78c6626d-940e-4168-a94e-3da8ddc2add0", 7
    )

    assert resultado[0]["potencia_promedio_w"] == 1100.0
    assert resultado[0]["fuente"] == "hourly_aggregates+registros"


def test_historico_potencia_firebase_usa_solo_potencia_activa(monkeypatch):
    ahora = datetime(2026, 7, 17, 15, tzinfo=timezone.utc)
    fechas = {
        "lectura_activa_w": datetime(2026, 7, 17, 14, 10, tzinfo=timezone.utc),
        "lectura_activa_kw": datetime(2026, 7, 17, 14, 20, tzinfo=timezone.utc),
        "lectura_legada": datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc),
    }
    monkeypatch.setattr(
        "app.api.readings._fecha_desde_firebase_push_id",
        lambda clave: fechas[clave],
    )

    resultado = _agrupar_potencia_activa_firebase({
        "lectura_activa_w": {"potencia_activa_w": 1400, "potencia_w": 0},
        "lectura_activa_kw": {"potencia_activa_kw": 1.6},
        "lectura_legada": {"potencia_w": 900},
    }, dias=1, ahora=ahora)

    assert resultado == [{
        "bucket_hour": "2026-07-17T14:00:00+00:00",
        "avg_power_w": 1500.0,
        "reading_count": 2,
        "source": "firebase_potencia_activa_w",
    }]


def test_consumo_total_suma_intervalos_de_todos_los_aires():
    filas = [
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.10,
            "tarifa_kwh": 0.18,
            "costo_intervalo": 0.018,
        },
        {
            "pabellon": "robotica", "aire": "Aire_1",
            "fecha_sync": "2026-07-01T00:02:00+00:00",
            "consumo_intervalo_kwh": 0.20,
            "tarifa_kwh": 0.18,
            "costo_intervalo": 0.036,
        },
        {
            "pabellon": "robotica", "aire": "Aire_2",
            "fecha_sync": "2026-07-01T00:01:00+00:00",
            "consumo_intervalo_kwh": 0.30,
            "tarifa_kwh": 0.20,
            "costo_intervalo": 0.060,
        },
    ]

    resumen = _resumir_consumo_total_aires(filas)

    assert resumen["total_energy_kwh"] == 0.6
    assert resumen["total_cost_usd"] == 0.114
    assert resumen["rooms_count"] == 2


def test_telemetria_electrica_firebase_tiene_prioridad_sobre_constante():
    valor = {
        "pabellon": "robotica",
        "aire": "Aire_1",
        "temperatura_ambiente": 27.5,
        "temperatura_salida_aire": 20.0,
        "humedad": 60,
        "estado_ocupacion": True,
        "aire_encendido_atmos": True,
        "corriente_rms": 10.0,
        "voltaje_red_v": 230.0,
        "factor_potencia": 0.8,
        "potencia_aparente_va": 2300.0,
        "potencia_activa_w": 1840.0,
        "potencia_activa_kw": 1.84,
        "consumo_intervalo_kwh": 0.012,
        "consumo_acumulado_sesion_kwh": 0.42,
        "tarifa_kwh": 0.18,
        "costo_intervalo": 0.00216,
        "costo_acumulado_sesion": 0.0756,
        "corriente_rms_cruda": 10.2,
        "corriente_rms_instantanea": 9.9,
        "corriente_calculada_vpp": 14.4,
        "factor_calibracion_sct": 1.007,
        "corriente_retenida_por_filtro": False,
        "ceros_consecutivos_sct": 0,
        "dht_ok": True,
        "ds18b20_ok": True,
        "fallos_dht": 0,
        "fallos_ds18b20": 0,
    }
    anterior = {
        "energia_kwh": 2.5,
        "fecha_sync": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }

    registro = preparar_registro_supabase(
        "robotica", "Aire_1", "firebase-id", valor, registro_anterior=anterior
    )

    assert registro["potencia_w"] == 1840.0
    assert registro["energia_kwh"] == 2.512
    assert registro["corriente_rms"] == 10.0
    assert registro["consumo_intervalo_kwh"] == 0.012
    assert registro["consumo_acumulado_sesion_kwh"] == 0.42
    assert registro["factor_potencia"] == 0.8
    assert registro["costo_intervalo"] == 0.00216
    assert registro["corriente_rms_cruda"] == 10.2
    assert registro["corriente_calculada_vpp"] == 14.4
    assert registro["dht_ok"] is True
    assert registro["corriente_retenida_por_filtro"] is False


async def test_csv_ml_incluye_telemetria_electrica_y_calidad(
    cliente_prueba, mock_supabase, monkeypatch
):
    monkeypatch.setattr("app.api.compat.obtener_cliente", lambda: mock_supabase)
    mock_supabase.execute.side_effect = [
        MagicMock(data=[{"nombre": "Robótica", "pabellon": "robotica"}]),
        MagicMock(data=[{
            "fecha_sync": "2026-07-17T17:34:54+00:00",
            "pabellon": "robotica",
            "aire": "Aire_1",
            "temperatura_ambiente": 30.4,
            "temperatura_salida_aire": 22.5,
            "humedad": 44.3,
            "delta_t": 7.9,
            "estado_ocupacion": False,
            "potencia_activa_w": 907.97,
            "consumo_intervalo_kwh": 0.015,
            "dht_ok": True,
            "ds18b20_ok": True,
        }]),
    ]

    respuesta = await cliente_prueba.post(
        "/api/v1/reports/energy", json={"format": "csv"}
    )

    assert respuesta.status_code == 200
    encabezado, fila = respuesta.text.splitlines()[:2]
    assert "delta_t_c" in encabezado
    assert "potencia_activa_w" in encabezado
    assert "consumo_intervalo_kwh" in encabezado
    assert "dht_ok" in encabezado
    assert "7.9" in fila
    assert "907.97" in fila


def test_potencia_activa_tiene_prioridad_sobre_cero_legado():
    registro = preparar_registro_supabase(
        "robotica",
        "Aire_1",
        "firebase-con-campo-legado",
        {
            "potencia_w": 0,
            "potencia_activa_w": 1429.1,
            "aire_encendido_atmos": False,
            "temperatura_ambiente": 27.2,
            "temperatura_salida_aire": 23.8,
            "humedad": 52,
        },
    )

    assert registro["potencia_w"] == 1429.1


def test_historial_reemplaza_cero_por_potencia_de_la_misma_firebase_key():
    registros = [{
        "firebase_key": "robotica_Aire_1_-lectura123",
        "fecha_sync": "2026-07-16T14:20:53Z",
        "potencia_w": 0.0,
    }]
    lecturas_firebase = {
        "-lectura123": {
            "potencia_activa_w": 1416.60754,
            "corriente_rms": 7.57544,
            "voltaje_red_v": 220.0,
        }
    }

    resultado = _completar_potencia_historica_desde_firebase(
        registros,
        lecturas_firebase,
        "robotica",
        "Aire_1",
    )

    assert resultado[0]["potencia_w"] == 1416.60754
    assert resultado[0]["potencia_activa_w"] == 1416.60754
    assert resultado[0]["corriente_rms"] == 7.57544
    assert registros[0]["potencia_w"] == 0.0


def test_historial_se_construye_directamente_desde_firebase():
    resultado = _normalizar_historial_firebase(
        {
            "-OxfIX4rzpWXRaAyzLcf": {
                "temperatura_ambiente": 27.3,
                "potencia_activa_w": 1416.60754,
                "corriente_rms": 7.57544,
            }
        },
        "robotica",
        "Aire_1",
    )

    assert resultado[0]["firebase_key"] == "robotica_Aire_1_-OxfIX4rzpWXRaAyzLcf"
    assert resultado[0]["potencia_w"] == 1416.60754
    assert resultado[0]["potencia_activa_w"] == 1416.60754
    assert resultado[0]["fecha_sync"].startswith("2026-07-16T14:21:50")


def test_constante_solo_se_usa_si_firebase_no_envia_medicion_electrica():
    ahora = datetime.now(timezone.utc)
    resultado = estimar_consumo_registro(
        valor={"aire_encendido_atmos": True},
        registro_anterior={
            "energia_kwh": 1.0,
            "fecha_sync": (ahora - timedelta(hours=1)).isoformat(),
        },
        fecha_actual=ahora,
    )

    assert resultado["potencia_w"] == 1500.0
    assert resultado["energia_kwh"] == 2.5


def test_dashboard_prefiere_consumo_intervalo_y_tarifa_de_firebase():
    ahora = datetime.now(timezone.utc)
    filas = [
        {
            "fecha_sync": (ahora - timedelta(minutes=1)).isoformat(),
            "potencia_w": 1000.0,
            "estado_ocupacion": True,
            "aire_encendido_atmos": True,
            "tarifa_kwh": 0.21,
        },
        {
            "fecha_sync": ahora.isoformat(),
            "potencia_w": 1100.0,
            "consumo_intervalo_kwh": 0.25,
            "costo_intervalo": 0.0525,
            "estado_ocupacion": True,
            "aire_encendido_atmos": True,
            "tarifa_kwh": 0.21,
        },
    ]

    resumen = construir_resumen_dashboard(filas, "24h")

    assert resumen["tariff_usd_per_kwh"] == 0.21
    assert resumen["metrics"]["today_energy_kwh"] == 0.2
    assert resumen["metrics"]["period_cost_usd"] == 0.05
    assert resumen["metrics"]["today_cost_usd"] == 0.05
    assert resumen["metrics"]["estimated_savings_usd"] is None
    assert resumen["metrics"]["estimated_savings_available"] is False
    assert resumen["calculation_trace"]["today_cost"]["tariff_usd_kwh"] == 0.21
    assert resumen["calculation_trace"]["today_cost"]["result_usd"] == 0.0525


def test_dashboard_suma_deltas_y_trata_reset_del_acumulador():
    ahora = datetime.now(timezone.utc)
    filas = [
        {
            "fecha_sync": (ahora - timedelta(minutes=3)).isoformat(),
            "energia_kwh": 10.0,
            "potencia_w": 1000.0,
        },
        {
            "fecha_sync": (ahora - timedelta(minutes=2)).isoformat(),
            "energia_kwh": 10.2,
            "potencia_w": 1000.0,
        },
        {
            "fecha_sync": (ahora - timedelta(minutes=1)).isoformat(),
            "energia_kwh": 0.1,
            "potencia_w": 1000.0,
        },
    ]

    resumen = construir_resumen_dashboard(filas, "24h")

    assert resumen["metrics"]["today_energy_kwh"] == 0.3
    assert resumen["calculation_trace"]["data_coverage"]["energy_sources"] == {
        "delta_energia_kwh": 1,
        "reset_energia_kwh": 1,
    }


def test_heatmap_renderiza_datos_parciales_y_distingue_cero_de_ausencia(monkeypatch):
    fin_panama = datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("America/Panama"))
    monkeypatch.setattr("app.services.dashboard_energy.ahora_panama", lambda: fin_panama)
    filas = [
        {"fecha_sync": "2026-07-27T14:50:00+00:00"},
        {
            "fecha_sync": "2026-07-27T15:00:00+00:00",
            "consumo_intervalo_kwh": 0.0,
        },
        {
            "fecha_sync": "2026-07-27T15:10:00+00:00",
            "consumo_intervalo_kwh": 0.24,
        },
    ]

    heatmap = construir_resumen_dashboard(filas, "24h")["phase2"]["heatmap"]
    lunes_09 = next(
        punto
        for punto in heatmap["points"]
        if punto["day"] == "Lunes" and punto["hour"] == 9
    )
    lunes_10 = next(
        punto
        for punto in heatmap["points"]
        if punto["day"] == "Lunes" and punto["hour"] == 10
    )
    lunes_11 = next(
        punto
        for punto in heatmap["points"]
        if punto["day"] == "Lunes" and punto["hour"] == 11
    )

    assert len(heatmap["days"]) == 7
    assert len(heatmap["hours"]) == 24
    assert len(heatmap["points"]) == 168
    assert heatmap["insufficient_data"] is False
    assert lunes_09["kwh"] == 0.0
    assert lunes_10["kwh"] == 0.24
    assert lunes_10["sample_days"] == 1
    assert lunes_11["kwh"] is None


def test_dashboard_24h_agrupa_energia_por_hora_local_sin_inventar_datos(monkeypatch):
    fin_panama = datetime(2026, 7, 28, 12, 30, tzinfo=ZoneInfo("America/Panama"))
    monkeypatch.setattr("app.services.dashboard_energy.ahora_panama", lambda: fin_panama)
    filas = [
        {"fecha_sync": "2026-07-28T15:50:00+00:00"},
        {
            "fecha_sync": "2026-07-28T16:00:00+00:00",
            "consumo_intervalo_kwh": 0.1,
        },
        {
            "fecha_sync": "2026-07-28T16:10:00+00:00",
            "consumo_intervalo_kwh": 0.2,
        },
    ]

    grafica = construir_resumen_dashboard(filas, "24h")["chart"]
    por_hora = {punto["label"]: punto for punto in grafica["points"]}

    assert grafica["grouping"] == "hour"
    assert grafica["unit"] == "kWh"
    assert grafica["value_key"] == "energia_kwh"
    assert len(grafica["points"]) == 24
    assert por_hora["10:00"]["energia_kwh"] == 0.1
    assert por_hora["11:00"]["energia_kwh"] == 0.2
    assert por_hora["12:00"]["energia_kwh"] is None
    assert por_hora["11:00"]["tooltip_label"] == "28/07/2026 · 11:00"


def test_dashboard_3m_agrupa_por_semana(monkeypatch):
    fin_panama = datetime(2026, 7, 28, 12, 30, tzinfo=ZoneInfo("America/Panama"))
    monkeypatch.setattr("app.services.dashboard_energy.ahora_panama", lambda: fin_panama)

    grafica = construir_resumen_dashboard([], "3m")["chart"]

    assert grafica["grouping"] == "week"
    assert len(grafica["points"]) == 13
    assert all(punto["label"].startswith("Sem. ") for punto in grafica["points"])
    assert all(punto["energia_kwh"] is None for punto in grafica["points"])


def test_potencia_no_confirma_estado_hasta_calibrar_umbrales(monkeypatch):
    monkeypatch.setattr(configuracion, "AC_POWER_THRESHOLDS_CALIBRATED", False)
    assert inferir_ac_encendido({
        "potencia_activa_w": 120.0,
        "aire_encendido_atmos": False,
    }) is None
    assert inferir_ac_encendido({
        "potencia_activa_w": 0.0,
        "aire_encendido_atmos": True,
    }) is None


def test_zona_sin_calibrar_no_reutiliza_estado_software_como_prueba_fisica(monkeypatch):
    monkeypatch.setattr(configuracion, "AC_POWER_THRESHOLDS_CALIBRATED", False)
    assert inferir_ac_encendido(
        {"potencia_activa_w": 35.0},
        {"aire_encendido_atmos": True},
    ) is None
    assert inferir_ac_encendido(
        {"potencia_activa_w": 35.0},
        {"aire_encendido_atmos": False},
    ) is None


def test_umbrales_calibrados_determinan_estado_del_aire(monkeypatch):
    monkeypatch.setattr(configuracion, "AC_POWER_THRESHOLDS_CALIBRATED", True)
    monkeypatch.setattr(configuracion, "AC_POWER_OFF_THRESHOLD_W", 250.0)
    monkeypatch.setattr(configuracion, "AC_POWER_ON_THRESHOLD_W", 500.0)

    assert inferir_ac_encendido({"potencia_activa_w": 209.0}) is False
    assert inferir_ac_encendido({"potencia_activa_w": 250.0}) is None
    assert inferir_ac_encendido({"potencia_activa_w": 499.9}) is None
    assert inferir_ac_encendido({"potencia_activa_w": 500.0}) is True
    assert inferir_ac_encendido({"potencia_activa_w": 1413.0}) is True


class FirebaseFalso:
    def __init__(self, estado):
        self.estado = dict(estado)
        self.actualizaciones = []

    def child(self, _nombre):
        return self

    def get(self):
        return self

    def val(self):
        return dict(self.estado)

    def update(self, datos):
        self.actualizaciones.append(dict(datos))
        self.estado.update(datos)


def test_no_republica_mismo_comando_ir(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "active")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)
    firebase = FirebaseFalso({
        "accion": "ahorro_24",
        "actualizado_en": "fecha-original",
        "resultado": "ejecutado",
    })

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        firebase, "robotica", "Aire_1", "ahorro_24", {"confianza_ml": 0.96}
    )

    assert resultado["publicado"] is False
    assert resultado["motivo"] == "accion_sin_cambios"
    assert firebase.actualizaciones == []
    assert firebase.estado["actualizado_en"] == "fecha-original"


def test_publica_comando_ir_cuando_cambia_accion(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "active")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)
    firebase = FirebaseFalso({"accion": "ahorro_24", "resultado": "ejecutado"})

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        firebase, "robotica", "Aire_1", "apagar", {"confianza_ml": 0.99}
    )

    assert resultado["publicado"] is True
    assert len(firebase.actualizaciones) == 1
    assert firebase.estado["accion"] == "apagar"
    assert firebase.estado["resultado"] == "pendiente"
    assert firebase.estado["confianza_ml"] == 0.99


def test_mantener_sin_ir_no_reemplaza_modo_ahorro_actual():
    firebase = FirebaseFalso({
        "accion": "ahorro_24",
        "actualizado_en": "fecha-original",
        "resultado": "ejecutado",
    })

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        firebase,
        "robotica",
        "Aire_1",
        "mantener",
        {"recomendacion_ml": "mantener"},
        permitir_publicacion=False,
    )

    assert resultado["publicado"] is False
    assert resultado["motivo"] == "decision_no_op"
    assert firebase.actualizaciones == []
    assert firebase.estado["accion"] == "ahorro_24"


def test_mantener_no_reconcilia_ni_reescribe_un_comando_antiguo():
    firebase = FirebaseFalso({
        "accion": "mantener",
        "actualizado_en": "fecha-mantener",
        "resultado": "accion_no_reconocida",
        "firma_ejecutada": "accion:ahorro_24|actualizado_en:fecha-ahorro",
    })

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        firebase,
        "robotica",
        "Aire_1",
        "mantener",
        permitir_publicacion=False,
    )

    assert resultado["publicado"] is False
    assert resultado["motivo"] == "decision_no_op"
    assert firebase.actualizaciones == []
    assert firebase.estado["accion"] == "mantener"
    assert firebase.estado["resultado"] == "accion_no_reconocida"
