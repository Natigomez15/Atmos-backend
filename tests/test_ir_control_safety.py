"""Pruebas unitarias fail-closed. No usan Firebase, Supabase ni hardware reales."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.ac_commands import comando_desde_prediccion
from app.api.compat import _resolver_destino_firebase_comando
from app.api.ml import ultima_decision_firebase
from app.config import configuracion
from app.core.control_ir import (
    construir_comando_ir,
    control_manual_ir_habilitado,
    control_ir_habilitado,
    control_solo_manual,
    evaluar_autorizacion_automatica,
    evaluar_comando_para_ejecucion,
)
from app.ml.atmos_logic import (
    COMANDO_IR_NINGUNO,
    autorizar_control_ir,
    obtener_comando_ir_sugerido,
    traducir_decision_ac,
)
from app.ml.impacto import evaluar_estado_electrico
from app.ml.predictor import ServicioPredictor


class FirebaseProhibido:
    def child(self, _nombre):
        raise AssertionError("No debía accederse a Firebase")


class FirebaseFalso:
    def __init__(self, estado=None):
        self.estado = dict(estado or {})
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


def activar_control(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "active")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)


def test_mantener_nunca_produce_comando_ir():
    accion_ac = traducir_decision_ac("mantener", 29)

    assert accion_ac["temperatura_objetivo"] is None
    assert obtener_comando_ir_sugerido("mantener", accion_ac) == COMANDO_IR_NINGUNO
    autorizacion = autorizar_control_ir(
        "mantener",
        accion_ac,
        {"estado_lectura": "confiable"},
    )
    assert autorizacion["ejecutar_ir"] is False
    assert autorizacion["comando_ir"] == COMANDO_IR_NINGUNO
    assert ServicioPredictor().traducir_accion_esp32({
        "control": {
            "ejecutar_ir": True,
            "comando_ir": None,
            "decision_final": "mantener",
            "accion_ac": {"temperatura_objetivo": 24},
        }
    }) == "mantener"


@pytest.mark.parametrize("estado", ["apagado", "no_confirmado", None])
def test_mantener_es_no_op_con_estado_apagado_o_desconocido(estado):
    resultado = evaluar_autorizacion_automatica("mantener", estado)

    assert resultado == {"autorizada": False, "motivo": "decision_no_op"}


def test_publicador_mantener_no_accede_a_firebase_aun_con_control_activo(monkeypatch):
    activar_control(monkeypatch)

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        FirebaseProhibido(),
        "robotica",
        "Aire_1",
        "mantener",
    )

    assert resultado["publicado"] is False
    assert resultado["motivo"] == "decision_no_op"


def test_ahorro_24_es_accion_explicita_con_sobre_completo(monkeypatch):
    activar_control(monkeypatch)
    firebase = FirebaseFalso({"accion": "apagar"})

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        firebase,
        "robotica",
        "Aire_1",
        "ahorro_24",
    )

    assert resultado["publicado"] is True
    comando = firebase.actualizaciones[0]
    assert comando["accion"] == "ahorro_24"
    assert comando["command_id"]
    assert comando["created_at"]
    assert comando["expires_at"]
    assert comando["estado"] == "pendiente"
    assert comando["pabellon"] == "robotica"
    assert comando["aire"] == "Aire_1"


async def test_get_decision_es_estrictamente_solo_lectura():
    datos_firebase = {
        "accion": "mantener",
        "prediccion_modelo": "apagar",
        "recomendacion_ml": "mantener",
    }
    with (
        patch("app.api.ml.leer_comando_firebase_rest", return_value=datos_firebase) as lector,
        patch("app.api.ml.obtener_cliente") as supabase,
        patch("app.api.ml.ServicioPredictor") as predictor,
        patch("app.core.database.obtener_firebase") as sdk_firebase,
    ):
        respuesta = await ultima_decision_firebase("robotica", "Aire_1")

    assert respuesta["decision"]["accion_solicitada"] == "mantener"
    lector.assert_called_once_with("robotica", "Aire_1")
    supabase.assert_not_called()
    predictor.assert_not_called()
    sdk_firebase.assert_not_called()


def _comando_vigente(ahora):
    return construir_comando_ir(
        pabellon="robotica",
        aire="Aire_1",
        accion="apagar",
        ahora=ahora - timedelta(seconds=10),
        ttl_segundos=60,
    )


def test_comando_vencido_no_es_ejecutable():
    ahora = datetime.now(timezone.utc)
    comando = construir_comando_ir(
        pabellon="robotica",
        aire="Aire_1",
        accion="apagar",
        ahora=ahora - timedelta(minutes=5),
        ttl_segundos=30,
    )

    resultado = evaluar_comando_para_ejecucion(
        comando, pabellon="robotica", aire="Aire_1", ahora=ahora
    )
    assert resultado == {"ejecutable": False, "motivo": "comando_vencido"}


def test_comando_legado_sin_vencimiento_no_es_ejecutable():
    ahora = datetime.now(timezone.utc)
    comando = _comando_vigente(ahora)
    comando.pop("expires_at")

    resultado = evaluar_comando_para_ejecucion(
        comando, pabellon="robotica", aire="Aire_1", ahora=ahora
    )
    assert resultado["ejecutable"] is False
    assert resultado["motivo"] == "expires_at_ausente_o_invalido"


def test_reconexion_no_repite_ultimo_command_id():
    ahora = datetime.now(timezone.utc)
    comando = _comando_vigente(ahora)

    resultado = evaluar_comando_para_ejecucion(
        comando,
        pabellon="robotica",
        aire="Aire_1",
        ultimo_command_id=comando["command_id"],
        ahora=ahora,
    )
    assert resultado == {"ejecutable": False, "motivo": "command_id_ya_ejecutado"}


def test_aire_1_rechaza_comando_destinado_a_aire_2():
    ahora = datetime.now(timezone.utc)
    comando = construir_comando_ir(
        pabellon="robotica",
        aire="Aire_2",
        accion="apagar",
        ahora=ahora - timedelta(seconds=1),
    )

    resultado = evaluar_comando_para_ejecucion(
        comando, pabellon="robotica", aire="Aire_1", ahora=ahora
    )
    assert resultado == {"ejecutable": False, "motivo": "aire_no_coincide"}


def test_sala_con_dos_aires_exige_destino_explicito():
    sala = {"pabellon": "robotica", "aires": ["Aire_1", "Aire_2"]}

    with pytest.raises(ValueError, match="campo 'aire' es obligatorio"):
        _resolver_destino_firebase_comando(sala, {})


def test_destino_explicito_no_puede_salir_de_la_sala():
    sala = {"pabellon": "robotica", "aires": ["Aire_1"]}

    with pytest.raises(ValueError, match="no pertenece a la sala"):
        _resolver_destino_firebase_comando(sala, {"aire": "Aire_2"})


def test_dry_run_no_escribe_en_firebase(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "dry_run")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)

    resultado = ServicioPredictor().publicar_comando_si_cambio(
        FirebaseProhibido(), "robotica", "Aire_1", "apagar"
    )
    assert resultado["publicado"] is False
    assert resultado["motivo"] == "control_en_modo_dry_run"


def test_bloqueo_global_deshabilitado_impide_toda_orden(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "active")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", False)

    assert control_ir_habilitado() is False
    resultado = ServicioPredictor().publicar_comando_si_cambio(
        FirebaseProhibido(), "robotica", "Aire_1", "apagar"
    )
    assert resultado["publicado"] is False
    assert resultado["motivo"] == "control_ir_deshabilitado"


def test_manual_only_habilita_manual_y_bloquea_predictor(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "manual_only")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)

    assert control_manual_ir_habilitado() is True
    assert control_solo_manual() is True
    assert control_ir_habilitado() is False
    resultado = ServicioPredictor().publicar_comando_si_cambio(
        FirebaseProhibido(), "robotica", "Aire_1", "apagar"
    )
    assert resultado["publicado"] is False
    assert resultado["motivo"] == "control_en_modo_manual_only"


async def test_manual_only_bloquea_aplicar_prediccion_antes_de_bd(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "manual_only")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", True)

    with patch("app.api.ac_commands.obtener_cliente") as cliente:
        with pytest.raises(HTTPException) as error:
            await comando_desde_prediccion(1)

    assert error.value.status_code == 503
    cliente.assert_not_called()


async def test_aplicar_prediccion_no_crea_pendiente_con_control_bloqueado(monkeypatch):
    monkeypatch.setattr(configuracion, "ATMOS_CONTROL_MODE", "dry_run")
    monkeypatch.setattr(configuracion, "ATMOS_IR_CONTROL_ENABLED", False)

    with patch("app.api.ac_commands.obtener_cliente") as cliente:
        with pytest.raises(HTTPException) as error:
            await comando_desde_prediccion(1)

    assert error.value.status_code == 503
    cliente.assert_not_called()


def test_64_5_w_no_confirma_aire_ni_compresor_sin_calibracion(monkeypatch):
    monkeypatch.setattr(configuracion, "AC_POWER_THRESHOLDS_CALIBRATED", False)

    resultado = evaluar_estado_electrico({"potencia_activa_w": 64.5})
    assert resultado["estado_electrico"] == "no_confirmado"
    assert resultado["compresor_confirmado"] is False


def test_discrepancia_estado_deseado_observado_bloquea_automatico():
    resultado = evaluar_autorizacion_automatica("apagar", "encendido")

    assert resultado == {
        "autorizada": False,
        "motivo": "discrepancia_estado_deseado_observado",
    }


def test_guardar_recomendacion_no_finge_ultima_accion_ejecutada():
    cliente = MagicMock()
    for metodo in ("table", "select", "eq", "order", "limit", "update"):
        getattr(cliente, metodo).return_value = cliente
    cliente.execute.return_value = MagicMock(data=[{"id": 3, "sala_id": "sala-1"}])
    resultado_modelo = {
        "modelo": {"decision_ml": "apagar"},
        "control": {"decision_final": "mantener"},
    }

    with patch("app.ml.predictor.obtener_cliente", return_value=cliente):
        ServicioPredictor().guardar_decision_en_registro(
            pabellon="robotica",
            aire="Aire_1",
            accion="mantener",
            resultado=resultado_modelo,
            horario={"dentro_horario": True},
            comando_publicado=False,
        )

    datos_actualizados = cliente.update.call_args.args[0]
    assert datos_actualizados["recomendacion_local"] == "mantener"
    assert "ultima_accion_ejecutada" not in datos_actualizados
    assert "ultimo_comando_enviado" not in datos_actualizados
