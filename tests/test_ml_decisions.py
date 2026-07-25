"""Contrato normalizado de decisiones ML leídas desde Firebase."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.sincronizador_firebase import normalizar_ultima_decision_firebase


SALA_ID = "00000000-0000-0000-0000-000000000002"

DECISION_FIREBASE = {
    "accion": "mantener",
    "actualizado_en": "2026-07-15T21:57:44.014020+00:00",
    "confianza_ml": 1,
    "confirmacion_ir": "senal_ir_enviada_sin_confirmacion_del_aire",
    "firma_ejecutada": "accion:ahorro_24|actualizado_en:2026-07-15T21:57:21.767309+00:00",
    "modelo_usado": True,
    "origen": "modelo_ml",
    "prediccion_modelo": "apagar",
    "recomendacion_ml": "mantener",
    "recomendacion_ml_ejecutada": "codex_manual",
    "resultado": "accion_no_reconocida",
    "temperatura_ejecutada": 24,
    "tipo_modelo": "RandomForestClassifier",
    "ultima_accion_ejecutada": "ahorro_24",
    "version_modelo": "modelo_atmos_rf_v1",
}


async def _consultar(cliente_prueba, datos, **parametros):
    params = {"pabellon": "robotica", "aire": "Aire_1", **parametros}
    with patch("app.api.ml.leer_comando_firebase_rest", return_value=datos):
        return await cliente_prueba.get("/api/v1/ml/decisions/latest", params=params)


async def test_decision_separa_modelo_reglas_y_ejecucion_sin_confirmar(cliente_prueba):
    respuesta = await _consultar(cliente_prueba, DECISION_FIREBASE)

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["prediccion"] == {
        "valor": "apagar",
        "confianza": 1.0,
        "modelo_usado": True,
        "origen": "modelo_ml",
        "tipo_modelo": "RandomForestClassifier",
        "version_modelo": "modelo_atmos_rf_v1",
    }
    assert datos["decision"] == {
        "recomendacion_final": "mantener",
        "accion_solicitada": "mantener",
        "modificada_por_reglas": True,
        "motivo": None,
    }
    assert datos["ejecucion"]["ultima_accion"] == "ahorro_24"
    assert datos["ejecucion"]["estado"] == "enviada_sin_confirmacion"
    assert datos["ejecucion"]["mensaje"] == (
        "Señal enviada, funcionamiento del aire no confirmado."
    )
    assert "La recomendación final es diferente de la predicción original." in datos["advertencias"]
    assert "La acción solicitada es diferente de la última acción enviada." in datos["advertencias"]
    assert "Firebase contiene un resultado de acción no reconocida." in datos["advertencias"]


@pytest.mark.parametrize("confianza", [0.96, 1])
async def test_confianza_permanece_decimal_entre_cero_y_uno(cliente_prueba, confianza):
    respuesta = await _consultar(
        cliente_prueba,
        {**DECISION_FIREBASE, "confianza_ml": confianza},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["prediccion"]["confianza"] == float(confianza)


async def test_confianza_ausente_permanece_null(cliente_prueba):
    datos = {**DECISION_FIREBASE}
    datos.pop("confianza_ml")

    respuesta = await _consultar(cliente_prueba, datos)

    assert respuesta.status_code == 200
    assert respuesta.json()["prediccion"]["confianza"] is None


async def test_comando_pendiente(cliente_prueba):
    respuesta = await _consultar(
        cliente_prueba,
        {"accion": "mantener", "actualizado_en": "2026-07-15T21:57:44+00:00"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ejecucion"]["estado"] == "pendiente"


async def test_ejecucion_solo_es_confirmada_con_confirmacion_explicita(cliente_prueba):
    respuesta = await _consultar(
        cliente_prueba,
        {
            **DECISION_FIREBASE,
            "confirmacion_ir": "ejecucion_confirmada",
            "resultado": "ok",
        },
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ejecucion"]["estado"] == "confirmada"


async def test_accion_fallida(cliente_prueba):
    respuesta = await _consultar(
        cliente_prueba,
        {
            **DECISION_FIREBASE,
            "confirmacion_ir": "error_ir",
            "resultado": "accion_fallida",
        },
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["ejecucion"]["estado"] == "fallida"


async def test_registro_incompleto_devuelve_null_sin_fallar(cliente_prueba):
    respuesta = await _consultar(cliente_prueba, {"actualizado_en": None})

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["prediccion"]["valor"] is None
    assert datos["prediccion"]["confianza"] is None
    assert datos["decision"]["recomendacion_final"] is None
    assert datos["ejecucion"]["ultima_accion"] is None
    assert datos["ejecucion"]["estado"] == "pendiente"


async def test_ruta_inexistente_devuelve_404_claro(cliente_prueba):
    respuesta = await _consultar(cliente_prueba, {})

    assert respuesta.status_code == 404
    assert "/Atmos/comandos/robotica/Aire_1" in respuesta.json()["detail"]


async def test_aire_1_y_aire_2_se_consultan_independientemente(cliente_prueba):
    def leer(_pabellon, aire):
        return {
            **DECISION_FIREBASE,
            "accion": "mantener" if aire == "Aire_1" else "apagar",
            "ultima_accion_ejecutada": "ahorro_24" if aire == "Aire_1" else "apagar",
        }

    with patch("app.api.ml.leer_comando_firebase_rest", side_effect=leer) as lector:
        aire_1 = await cliente_prueba.get(
            "/api/v1/ml/decisions/latest",
            params={"pabellon": "robotica", "aire": "Aire_1"},
        )
        aire_2 = await cliente_prueba.get(
            "/api/v1/ml/decisions/latest",
            params={"pabellon": "robotica", "aire": "Aire_2"},
        )

    assert aire_1.json()["aire"] == "Aire_1"
    assert aire_1.json()["decision"]["accion_solicitada"] == "mantener"
    assert aire_2.json()["aire"] == "Aire_2"
    assert aire_2.json()["decision"]["accion_solicitada"] == "apagar"
    assert lector.call_args_list[0].args == ("robotica", "Aire_1")
    assert lector.call_args_list[1].args == ("robotica", "Aire_2")


async def test_resultado_y_confirmacion_contradictorios(cliente_prueba):
    respuesta = await _consultar(
        cliente_prueba,
        {
            **DECISION_FIREBASE,
            "confirmacion_ir": "ejecucion_confirmada",
            "resultado": "accion_no_reconocida",
        },
    )

    datos = respuesta.json()
    assert datos["ejecucion"]["estado"] == "inconsistente"
    assert datos["ejecucion"]["resultado_raw"] == "accion_no_reconocida"
    assert datos["ejecucion"]["confirmacion_ir_raw"] == "ejecucion_confirmada"
    assert "El resultado y la confirmación infrarroja de Firebase son contradictorios." in datos["advertencias"]


@pytest.mark.parametrize("pabellon", ["Robótica", "robotica"])
async def test_robotica_con_y_sin_diacritico_resuelve_la_misma_ruta(
    cliente_prueba,
    pabellon,
):
    with patch(
        "app.api.ml.leer_comando_firebase_rest",
        return_value=DECISION_FIREBASE,
    ) as lector:
        respuesta = await cliente_prueba.get(
            "/api/v1/ml/decisions/latest",
            params={"pabellon": pabellon, "aire": "Aire_1"},
        )

    assert respuesta.status_code == 200
    assert respuesta.json()["pabellon"] == "robotica"
    lector.assert_called_once_with("robotica", "Aire_1")


async def test_error_real_de_firebase_devuelve_502(cliente_prueba):
    with patch(
        "app.api.ml.leer_comando_firebase_rest",
        side_effect=RuntimeError("firebase no disponible"),
    ):
        respuesta = await cliente_prueba.get(
            "/api/v1/ml/decisions/latest",
            params={"pabellon": "robotica", "aire": "Aire_1"},
        )

    assert respuesta.status_code == 502
    assert "Firebase" in respuesta.json()["detail"]


async def test_parametro_compuesto_solo_por_espacios_devuelve_422(cliente_prueba):
    respuesta = await cliente_prueba.get(
        "/api/v1/ml/decisions/latest",
        params={"pabellon": "   ", "aire": "Aire_1"},
    )

    assert respuesta.status_code == 422


def test_normalizador_documenta_estado_sin_registro():
    datos = normalizar_ultima_decision_firebase("robotica", "Aire_1", None)

    assert datos["ejecucion"]["estado"] == "sin_registro"


async def test_latest_agrega_campos_inequivocos_sin_quitar_compatibilidad(
    cliente_prueba,
):
    prediccion = {
        "id": 7,
        "sala_id": SALA_ID,
        "predicho_en": "2026-07-15T21:57:44+00:00",
        "setpoint_recomendado": None,
        "ahorro_predicho_pct": None,
        "puntaje_confianza": 0.96,
        "version_modelo": "modelo_atmos_rf_v1",
        "instantanea_caracteristicas": {
            "fuente": "modelo_pkl",
            "tipo_modelo": "RandomForestClassifier",
            "prediccion_modelo": "apagar",
            "recomendacion_final": "mantener",
            "accion_solicitada": "mantener",
            "accion_final": "mantener",
            "motivo_reglas_seguridad": "Regla de protección activa",
        },
        "fue_aplicado": True,
        "ahorro_real_pct": None,
    }
    cliente_supabase = MagicMock()
    for metodo in ("table", "select", "eq", "order", "limit"):
        getattr(cliente_supabase, metodo).return_value = cliente_supabase
    cliente_supabase.execute.return_value = MagicMock(data=[prediccion])

    with patch("app.api.ml.obtener_cliente", return_value=cliente_supabase):
        respuesta = await cliente_prueba.get(
            f"/api/v1/ml/predictions/{SALA_ID}/latest"
        )

    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["prediccion_original"] == "apagar"
    assert datos["confianza_prediccion"] == 0.96
    assert datos["recomendacion_final"] == "mantener"
    assert datos["accion_solicitada"] == "mantener"
    assert datos["recomendacion_modificada"] is True
    assert datos["motivo_reglas_seguridad"] == "Regla de protección activa"
    assert datos["recomendacion_texto"].startswith("Apagar")
    assert "Mantener" not in datos["recomendacion_texto"]
    # Campos anteriores conservados para compatibilidad.
    assert datos["confidence_score"] == 0.96
    assert datos["was_applied"] is True
    assert datos["modelo_ml"]["accion_final"] == "mantener"
