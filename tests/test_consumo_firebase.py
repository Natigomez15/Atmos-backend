from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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


def test_potencia_fisica_tiene_prioridad_sobre_estado_del_comando():
    assert inferir_ac_encendido({
        "potencia_activa_w": 120.0,
        "aire_encendido_atmos": False,
    }) is True
    assert inferir_ac_encendido({
        "potencia_activa_w": 0.0,
        "aire_encendido_atmos": True,
    }) is False


def test_histeresis_conserva_estado_anterior_en_zona_de_ruido():
    assert inferir_ac_encendido(
        {"potencia_activa_w": 35.0},
        {"aire_encendido_atmos": True},
    ) is True
    assert inferir_ac_encendido(
        {"potencia_activa_w": 35.0},
        {"aire_encendido_atmos": False},
    ) is False


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


def test_no_republica_mismo_comando_ir():
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


def test_publica_comando_ir_cuando_cambia_accion():
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
    assert resultado["motivo"] == "control_sin_envio_ir"
    assert firebase.actualizaciones == []
    assert firebase.estado["accion"] == "ahorro_24"


def test_reconcilia_mantener_invalido_con_ultima_firma_ejecutada():
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
    assert resultado["motivo"] == "estado_reconciliado_con_firma_ejecutada"
    assert firebase.estado["accion"] == "ahorro_24"
    assert firebase.estado["actualizado_en"] == "fecha-ahorro"
    assert firebase.estado["resultado"] == "ejecutado"
    firma_actual = (
        f"accion:{firebase.estado['accion']}|actualizado_en:"
        f"{firebase.estado['actualizado_en']}"
    )
    assert firma_actual == firebase.estado["firma_ejecutada"]
