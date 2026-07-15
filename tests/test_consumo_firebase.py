from datetime import datetime, timedelta, timezone

from app.ml.impacto import estimar_consumo_registro, inferir_ac_encendido
from app.ml.predictor import ServicioPredictor
from app.services.dashboard_energy import construir_resumen_dashboard
from app.services.sincronizador_firebase import preparar_registro_supabase


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
