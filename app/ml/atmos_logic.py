
import pandas as pd


# ==========================================
# PARÁMETROS GENERALES DEL SISTEMA ATMOS
# ==========================================

LIMITE_CALOR_MODERADO = 28
LIMITE_CALOR_FUERTE = 30
LIMITE_HUMEDAD_ALTA = 70
DELTA_MINIMO_ESPERADO = 6
TEMP_AC_POCO_FRIA = 22

TEMP_MANTENER = 24
TEMP_ENFRIAR_MODERADO = 23
TEMP_ENFRIAR_FUERTE = 22

TIEMPO_ESPERA_APAGADO = 10  # minutos

TIEMPO_MINIMO_FALLA = 30
DESCENSO_MINIMO_ESPERADO = 3
TEMP_AC_ALTA = 23


# ==========================================
# FUNCIÓN DE DECISIÓN BASE
# ==========================================

def decidir(row):
    if row["presencia"] == 0:
        return "apagar"

    if row["temp_ambiente"] >= LIMITE_CALOR_FUERTE:
        return "enfriar_fuerte"

    if row["temp_ambiente"] >= LIMITE_CALOR_MODERADO:
        señales_calor = 0

        if row["humedad"] >= LIMITE_HUMEDAD_ALTA:
            señales_calor += 1

        if row["delta_t"] < DELTA_MINIMO_ESPERADO:
            señales_calor += 1

        if row["temp_ac"] >= TEMP_AC_POCO_FRIA:
            señales_calor += 1

        if señales_calor >= 2:
            return "enfriar_fuerte"
        else:
            return "mantener"

    return "mantener"


# ==========================================
# VALIDACIÓN DE ENTRADAS
# ==========================================

def validar_lectura(presencia, temp_ambiente, temp_ac, humedad, minutos_sin_presencia=0):
    errores = []

    if presencia not in [0, 1]:
        errores.append("La variable 'presencia' debe ser 0 o 1.")

    if temp_ambiente < 10 or temp_ambiente > 45:
        errores.append("La temperatura ambiente está fuera de un rango lógico (10°C a 45°C).")

    if temp_ac < 5 or temp_ac > 35:
        errores.append("La temperatura del AC está fuera de un rango lógico (5°C a 35°C).")

    if humedad < 0 or humedad > 100:
        errores.append("La humedad debe estar entre 0% y 100%.")

    if minutos_sin_presencia < 0:
        errores.append("Los minutos sin presencia no pueden ser negativos.")

    return {
        "valido": len(errores) == 0,
        "errores": errores
    }


# ==========================================
# PREDICCIÓN DE UNA LECTURA
# ==========================================

def predecir_lectura(modelo, presencia, temp_ambiente, temp_ac, humedad):
    delta_t = temp_ambiente - temp_ac

    lectura = pd.DataFrame([{
        "presencia": presencia,
        "temp_ambiente": temp_ambiente,
        "temp_ac": temp_ac,
        "delta_t": delta_t,
        "humedad": humedad
    }])

    decision_ml = modelo.predict(lectura)[0]

    return decision_ml, delta_t


# ==========================================
# CAPA DE SEGURIDAD
# ==========================================

def aplicar_capa_seguridad(decision_ml, presencia, temp_ambiente, temp_ac, delta_t, humedad):
    caso = pd.Series({
        "presencia": presencia,
        "temp_ambiente": temp_ambiente,
        "temp_ac": temp_ac,
        "delta_t": delta_t,
        "humedad": humedad
    })

    decision_regla = decidir(caso)

    if presencia == 0:
        return decision_ml, "Sin presencia detectada. Se mantiene la decisión del modelo."

    if presencia == 1 and decision_ml == "apagar":
        return "mantener", "Capa de seguridad: había presencia, se evita apagar el AC."

    if decision_ml == "enfriar_fuerte" and decision_regla == "mantener":
        return "mantener", "Capa de seguridad: caso frontera. Se evita enfriar fuerte innecesariamente."

    return decision_ml, "La decisión del modelo fue aceptada por la capa de seguridad."


# ==========================================
# TEMPORIZADOR DE AUSENCIA
# ==========================================

def aplicar_temporizador(decision_ml, presencia, minutos_sin_presencia):
    if presencia == 0 and decision_ml == "apagar":

        if minutos_sin_presencia >= TIEMPO_ESPERA_APAGADO:
            return "apagar"

        else:
            return "esperar_apagado"

    return decision_ml


# ==========================================
# TRADUCCIÓN A ACCIÓN DEL AC
# ==========================================

def traducir_decision_ac(decision_final, temp_ambiente):
    if decision_final == "apagar":
        return {
            "estado_ac": "apagado",
            "temperatura_objetivo": None,
            "modo": "off",
            "ventilacion": "apagada",
            "accion": "Enviar comando IR para apagar el aire acondicionado"
        }

    elif decision_final == "esperar_apagado":
        return {
            "estado_ac": "encendido",
            "temperatura_objetivo": "sin cambio",
            "modo": "sin cambio",
            "ventilacion": "sin cambio",
            "accion": "No enviar nuevo comando. Esperar hasta cumplir los 10 minutos sin presencia"
        }

    elif decision_final == "mantener":
        return {
            "estado_ac": "encendido",
            "temperatura_objetivo": TEMP_MANTENER,
            "modo": "cool",
            "ventilacion": "automatica",
            "accion": f"Mantener el aire acondicionado en {TEMP_MANTENER}°C"
        }

    elif decision_final == "enfriar_fuerte":

        if temp_ambiente >= LIMITE_CALOR_FUERTE:
            temperatura = TEMP_ENFRIAR_FUERTE
            ventilacion = "alta"
        else:
            temperatura = TEMP_ENFRIAR_MODERADO
            ventilacion = "media"

        return {
            "estado_ac": "encendido",
            "temperatura_objetivo": temperatura,
            "modo": "cool",
            "ventilacion": ventilacion,
            "accion": f"Configurar el aire a {temperatura}°C con ventilación {ventilacion}"
        }

    return {
        "estado_ac": "desconocido",
        "temperatura_objetivo": None,
        "modo": "desconocido",
        "ventilacion": "desconocida",
        "accion": "Decisión no reconocida"
    }


# ==========================================
# DETECCIÓN DE FALLAS
# ==========================================

def detectar_falla_ac(decision_final, minutos_enfriando, temp_inicio, temp_actual, temp_ac_actual):
    if decision_final != "enfriar_fuerte":
        return {
            "estado_falla": "no_aplica",
            "alerta": False,
            "mensaje": "No se evalúa falla porque el sistema no está en modo enfriar_fuerte."
        }

    if minutos_enfriando < TIEMPO_MINIMO_FALLA:
        return {
            "estado_falla": "monitoreando",
            "alerta": False,
            "mensaje": f"El sistema lleva {minutos_enfriando} minutos enfriando. Aún no se evalúa falla."
        }

    descenso_temp = temp_inicio - temp_actual

    if descenso_temp < DESCENSO_MINIMO_ESPERADO:
        if temp_ac_actual >= TEMP_AC_ALTA:
            mensaje = (
                f"Posible falla del AC: después de {minutos_enfriando} minutos en enfriar_fuerte, "
                f"la temperatura solo bajó {descenso_temp:.1f}°C y el aire del AC sale a {temp_ac_actual}°C."
            )
        else:
            mensaje = (
                f"Posible bajo rendimiento: después de {minutos_enfriando} minutos en enfriar_fuerte, "
                f"la temperatura solo bajó {descenso_temp:.1f}°C."
            )

        return {
            "estado_falla": "posible_falla",
            "alerta": True,
            "descenso_temperatura": round(descenso_temp, 2),
            "mensaje": mensaje
        }

    return {
        "estado_falla": "funcionamiento_normal",
        "alerta": False,
        "descenso_temperatura": round(descenso_temp, 2),
        "mensaje": f"El AC funciona correctamente. La temperatura bajó {descenso_temp:.1f}°C."
    }


# ==========================================
# FUNCIÓN PRINCIPAL DEL SISTEMA ATMOS
# ==========================================

def ejecutar_atmos(
    modelo,
    presencia,
    temp_ambiente,
    temp_ac,
    humedad,
    minutos_sin_presencia=0,
    minutos_enfriando=0,
    temp_inicio=None,
    temp_actual=None,
    temp_ac_actual=None,
    usar_capa_seguridad=True
):
    validacion = validar_lectura(
        presencia,
        temp_ambiente,
        temp_ac,
        humedad,
        minutos_sin_presencia
    )

    if not validacion["valido"]:
        return {
            "valido": False,
            "errores": validacion["errores"],
            "mensaje": "La lectura no pudo procesarse porque contiene datos inválidos."
        }

    decision_ml, delta_t = predecir_lectura(
        modelo,
        presencia,
        temp_ambiente,
        temp_ac,
        humedad
    )

    lectura_prob = pd.DataFrame([{
        "presencia": presencia,
        "temp_ambiente": temp_ambiente,
        "temp_ac": temp_ac,
        "delta_t": delta_t,
        "humedad": humedad
    }])

    probabilidades_array = modelo.predict_proba(lectura_prob)[0]

    probabilidades = {
        clase: float(round(prob * 100, 2))
        for clase, prob in zip(modelo.classes_, probabilidades_array)
    }

    if usar_capa_seguridad:
        decision_segura, mensaje_seguridad = aplicar_capa_seguridad(
            decision_ml,
            presencia,
            temp_ambiente,
            temp_ac,
            delta_t,
            humedad
        )
    else:
        decision_segura = decision_ml
        mensaje_seguridad = "Capa de seguridad desactivada."

    decision_final = aplicar_temporizador(
        decision_segura,
        presencia,
        minutos_sin_presencia
    )

    accion_ac = traducir_decision_ac(
        decision_final,
        temp_ambiente
    )

    if temp_inicio is not None and temp_actual is not None and temp_ac_actual is not None:
        falla_ac = detectar_falla_ac(
            decision_final,
            minutos_enfriando,
            temp_inicio,
            temp_actual,
            temp_ac_actual
        )
    else:
        falla_ac = {
            "estado_falla": "no_evaluada",
            "alerta": False,
            "mensaje": "No se evaluó falla porque no se proporcionaron datos históricos de enfriamiento."
        }

    return {
        "valido": True,
        "lectura": {
            "presencia": presencia,
            "temp_ambiente": temp_ambiente,
            "temp_ac": temp_ac,
            "delta_t": round(delta_t, 2),
            "humedad": humedad,
            "minutos_sin_presencia": minutos_sin_presencia
        },
        "modelo": {
            "decision_ml": decision_ml,
            "probabilidades": probabilidades
        },
        "seguridad": {
            "decision_segura": decision_segura,
            "mensaje": mensaje_seguridad
        },
        "control": {
            "decision_final": decision_final,
            "accion_ac": accion_ac
        },
        "deteccion_fallas": falla_ac
    }
