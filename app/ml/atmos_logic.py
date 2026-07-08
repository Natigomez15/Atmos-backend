from datetime import datetime, time
from zoneinfo import ZoneInfo

try:
    import pandas as pd
except ImportError:
    pd = None


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
# PARÁMETROS DE CONFIABILIDAD Y CONTROL IR
# ==========================================

# Estos valores no cambian el modelo. Sirven para decidir si una lectura
# es suficientemente confiable como para ejecutar una señal IR real.
MARGEN_TEMP_SENSORES = 1.0
TEMP_SALIDA_AC_MUY_ALTA = 28
TEMP_AMBIENTE_ALTA_PARA_VALIDAR = 28

COMANDO_IR_APAGAR = "APAGAR"
COMANDO_IR_TEMP_24 = "TEMP_24"
COMANDO_IR_TEMP_23 = "TEMP_23"
COMANDO_IR_TEMP_22 = "TEMP_22"
COMANDO_IR_NINGUNO = "NINGUNO"
ZONA_HORARIA_ATMOS = ZoneInfo("America/Panama")
HORA_INICIO_OPERACION = time(6, 0)
HORA_FIN_OPERACION = time(23, 0)
COLUMNAS_MODELO = ["presencia", "temp_ambiente", "temp_ac", "delta_t", "humedad"]


def crear_lectura_modelo(presencia, temp_ambiente, temp_ac, delta_t, humedad):
    valores = {
        "presencia": presencia,
        "temp_ambiente": temp_ambiente,
        "temp_ac": temp_ac,
        "delta_t": delta_t,
        "humedad": humedad,
    }

    if pd is not None:
        return pd.DataFrame([valores])

    return [[valores[columna] for columna in COLUMNAS_MODELO]]


def dentro_de_horario_operacion(ahora=None):
    if ahora is None:
        ahora_panama = datetime.now(ZONA_HORARIA_ATMOS)
    elif ahora.tzinfo is None:
        ahora_panama = ahora.replace(tzinfo=ZONA_HORARIA_ATMOS)
    else:
        ahora_panama = ahora.astimezone(ZONA_HORARIA_ATMOS)

    es_dia_operativo = ahora_panama.weekday() <= 5
    hora_actual = ahora_panama.time()
    return es_dia_operativo and HORA_INICIO_OPERACION <= hora_actual < HORA_FIN_OPERACION


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
# NUEVA CAPA: CONFIABILIDAD DE LECTURA
# ==========================================

def evaluar_confiabilidad_lectura(
    presencia,
    temp_ambiente,
    temp_ac,
    humedad,
    delta_t,
    minutos_sin_presencia=0
):
    """
    Clasifica la lectura como confiable o dudosa.

    Esta función NO reemplaza al modelo ML.
    Su objetivo es evitar que el sistema mande señales IR automáticas
    cuando los sensores entregan datos raros o poco consistentes.

    Estados:
    - confiable: se puede usar para recomendar y autorizar IR.
    - dudosa: se puede usar para recomendar, pero se bloquea el IR automático.
    """

    motivos = []
    estado_lectura = "confiable"

    # Caso 1: la salida del AC aparece más caliente que el salón.
    # Esto puede indicar sensor mal ubicado, lectura mezclada o dato inestable.
    if temp_ac > temp_ambiente + MARGEN_TEMP_SENSORES:
        motivos.append(
            "La temperatura de salida del AC aparece mayor que la temperatura ambiente. "
            "La lectura puede estar afectada por ubicación del sensor o ruido."
        )

    # Caso 2: el salón está caliente, pero el sensor del AC marca aire muy caliente.
    # En pruebas reales esto puede significar que el sensor no está midiendo bien la salida del aire
    # o que el AC no está enfriando correctamente.
    if temp_ambiente >= TEMP_AMBIENTE_ALTA_PARA_VALIDAR and temp_ac >= TEMP_SALIDA_AC_MUY_ALTA:
        motivos.append(
            "El salón está caliente y la salida del AC también aparece caliente. "
            "Se considera lectura dudosa para control automático."
        )

    # Caso 3: en condiciones de calor, la temperatura ambiente y la de salida del AC son casi iguales.
    # Eso no necesariamente invalida el dato, pero no es ideal para mandar comandos automáticos.
    if temp_ambiente >= TEMP_AMBIENTE_ALTA_PARA_VALIDAR and abs(delta_t) <= MARGEN_TEMP_SENSORES:
        motivos.append(
            "La temperatura ambiente y la temperatura de salida del AC son demasiado parecidas "
            "en una condición de calor. Puede ser sensor mal ubicado o lectura poco representativa."
        )

    # Caso 4: humedad extrema. No se invalida, pero se marca como dudosa.
    if humedad <= 5 or humedad >= 98:
        motivos.append(
            "La humedad está en un extremo poco común. Se recomienda revisar el sensor de humedad."
        )

    if len(motivos) > 0:
        estado_lectura = "dudosa"

    puede_ejecutar_ir = estado_lectura == "confiable"

    if estado_lectura == "confiable":
        mensaje = "Lectura confiable. El sistema puede recomendar y autorizar control IR si corresponde."
    else:
        mensaje = "Lectura dudosa. El sistema puede recomendar, pero bloquea el control IR automático."

    return {
        "estado_lectura": estado_lectura,
        "puede_ejecutar_ir": puede_ejecutar_ir,
        "motivos": motivos,
        "mensaje": mensaje
    }


# ==========================================
# PREDICCIÓN DE UNA LECTURA
# ==========================================

def predecir_lectura(modelo, presencia, temp_ambiente, temp_ac, humedad):
    delta_t = temp_ambiente - temp_ac

    lectura = crear_lectura_modelo(presencia, temp_ambiente, temp_ac, delta_t, humedad)

    decision_ml = str(modelo.predict(lectura)[0])

    return decision_ml, delta_t


# ==========================================
# CAPA DE SEGURIDAD DEL MODELO
# ==========================================

def aplicar_capa_seguridad(decision_ml, presencia, temp_ambiente, temp_ac, delta_t, humedad):
    caso = {
        "presencia": presencia,
        "temp_ambiente": temp_ambiente,
        "temp_ac": temp_ac,
        "delta_t": delta_t,
        "humedad": humedad
    }

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
# NUEVA CAPA: COMANDO IR Y AUTORIZACIÓN
# ==========================================

def obtener_comando_ir_sugerido(decision_final, accion_ac):
    """
    Convierte la decisión final del sistema en un nombre de comando IR.
    Aquí NO se pone el código raw IR todavía; solo el nombre lógico.
    El ESP32 o backend debe mapear estos nombres a los códigos IR reales.
    """

    temperatura_objetivo = accion_ac.get("temperatura_objetivo")

    if decision_final == "apagar":
        return COMANDO_IR_APAGAR

    if decision_final == "mantener" and temperatura_objetivo == TEMP_MANTENER:
        return COMANDO_IR_TEMP_24

    if decision_final == "enfriar_fuerte" and temperatura_objetivo == TEMP_ENFRIAR_MODERADO:
        return COMANDO_IR_TEMP_23

    if decision_final == "enfriar_fuerte" and temperatura_objetivo == TEMP_ENFRIAR_FUERTE:
        return COMANDO_IR_TEMP_22

    return COMANDO_IR_NINGUNO


def autorizar_control_ir(
    decision_final,
    accion_ac,
    confiabilidad,
    ultima_accion_ir=None,
    modo_control="experimental"
):
    """
    Decide si se autoriza mandar una señal IR real.

    Reglas:
    - Si el modo es 'recomendacion', nunca ejecuta IR.
    - Si la lectura es dudosa, bloquea IR.
    - Si la decisión es esperar_apagado, no envía IR.
    - Si el comando es igual al último enviado, evita repetirlo.
    """

    comando_sugerido = obtener_comando_ir_sugerido(decision_final, accion_ac)
    motivos = []

    ejecutar_ir = True

    if modo_control == "recomendacion":
        ejecutar_ir = False
        motivos.append("Modo recomendación activo: no se ejecutan señales IR automáticas.")

    if confiabilidad["estado_lectura"] != "confiable":
        ejecutar_ir = False
        motivos.append("Control IR bloqueado porque la lectura fue clasificada como dudosa.")

    if decision_final == "esperar_apagado":
        ejecutar_ir = False
        motivos.append("No se envía IR porque el sistema está esperando completar el temporizador de ausencia.")

    if comando_sugerido == COMANDO_IR_NINGUNO:
        ejecutar_ir = False
        motivos.append("No hay comando IR asociado a esta decisión.")

    if ultima_accion_ir is not None and comando_sugerido == ultima_accion_ir:
        ejecutar_ir = False
        motivos.append("No se repite el comando IR porque ya fue enviado anteriormente.")

    if ejecutar_ir:
        comando_ir = comando_sugerido
        mensaje = f"Control IR autorizado. Comando a ejecutar: {comando_ir}."
    else:
        comando_ir = COMANDO_IR_NINGUNO
        if len(motivos) == 0:
            motivos.append("Control IR no autorizado.")
        mensaje = " ".join(motivos)

    return {
        "ejecutar_ir": ejecutar_ir,
        "comando_ir": comando_ir,
        "comando_ir_sugerido": comando_sugerido,
        "accion_enviada": False,
        "motivo_autorizacion": mensaje
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
    usar_capa_seguridad=True,
    ultima_accion_ir=None,
    modo_control="experimental"
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
            "mensaje": "La lectura no pudo procesarse porque contiene datos inválidos.",
            "seguridad": {
                "estado_lectura": "invalida",
                "puede_ejecutar_ir": False,
                "comando_ir": COMANDO_IR_NINGUNO,
                "mensaje": "No se ejecuta el modelo ni el control IR porque la lectura es inválida."
            },
            "control": {
                "decision_final": "no_procesada",
                "ejecutar_ir": False,
                "comando_ir": COMANDO_IR_NINGUNO,
                "accion_enviada": False
            }
        }

    decision_ml, delta_t = predecir_lectura(
        modelo,
        presencia,
        temp_ambiente,
        temp_ac,
        humedad
    )

    lectura_prob = crear_lectura_modelo(presencia, temp_ambiente, temp_ac, delta_t, humedad)

    probabilidades_array = modelo.predict_proba(lectura_prob)[0]

    probabilidades = {
        str(clase): float(round(prob * 100, 2))
        for clase, prob in zip(modelo.classes_, probabilidades_array)
    }

    confiabilidad = evaluar_confiabilidad_lectura(
        presencia,
        temp_ambiente,
        temp_ac,
        humedad,
        delta_t,
        minutos_sin_presencia
    )

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
        mensaje_seguridad = "Capa de seguridad del modelo desactivada."

    decision_final = aplicar_temporizador(
        decision_segura,
        presencia,
        minutos_sin_presencia
    )

    accion_ac = traducir_decision_ac(
        decision_final,
        temp_ambiente
    )

    autorizacion_ir = autorizar_control_ir(
        decision_final,
        accion_ac,
        confiabilidad,
        ultima_accion_ir=ultima_accion_ir,
        modo_control=modo_control
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
            "temperatura_salida_aire": temp_ac,
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
            "mensaje": mensaje_seguridad,
            "estado_lectura": confiabilidad["estado_lectura"],
            "puede_ejecutar_ir": autorizacion_ir["ejecutar_ir"],
            "motivos_confiabilidad": confiabilidad["motivos"],
            "mensaje_confiabilidad": confiabilidad["mensaje"],
            "motivo_control_ir": autorizacion_ir["motivo_autorizacion"]
        },
        "control": {
            "decision_final": decision_final,
            "accion_ac": accion_ac,
            "ejecutar_ir": autorizacion_ir["ejecutar_ir"],
            "comando_ir": autorizacion_ir["comando_ir"],
            "comando_ir_sugerido": autorizacion_ir["comando_ir_sugerido"],
            "accion_enviada": autorizacion_ir["accion_enviada"]
        },
        "deteccion_fallas": falla_ac
    }
