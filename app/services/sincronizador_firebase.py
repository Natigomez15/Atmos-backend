import math
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

import httpx

from app.config import configuracion
from app.core.aires import es_aire_ignorado
from app.core.control_ir import estado_deseado_desde_accion
from app.core.database import obtener_cliente, obtener_firebase
from app.ml.impacto import (
    ACCIONES_APAGADO,
    ACCIONES_ENCENDIDO,
    estimar_consumo_registro,
    evaluar_estado_electrico,
    inferir_accion,
    obtener_medicion_electrica_firebase,
)


ZONA_HORARIA_PANAMA = timezone(timedelta(hours=-5), "America/Panama")
MAX_LECTURAS_FIREBASE_POR_CONSULTA = 100

CAMPOS_ELECTRICOS_FIREBASE = {
    "corriente_rms",
    "corriente_rms_cruda",
    "corriente_rms_instantanea",
    "corriente_calculada_vpp",
    "factor_calibracion_sct",
    "corriente_retenida_por_filtro",
    "ceros_consecutivos_sct",
    "voltaje_red_v",
    "factor_potencia",
    "potencia_aparente_va",
    "potencia_activa_w",
    "potencia_activa_kw",
    "consumo_intervalo_kwh",
    "consumo_acumulado_sesion_kwh",
    "tarifa_kwh",
    "costo_intervalo",
    "costo_acumulado_sesion",
    "dht_ok",
    "ds18b20_ok",
    "fallos_dht",
    "fallos_ds18b20",
}

CAMPOS_SEGURIDAD_REGISTROS = {
    "estado_deseado",
    "ultimo_comando_enviado",
    "estado_reportado_por_software",
    "estado_electrico_observado",
    "compresor_confirmado",
}


def _a_booleano(valor: Any) -> bool:
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    return texto in {
        "1",
        "true",
        "si",
        "sí",
        "ocupado",
        "con_ocupacion",
        "detectado",
        "presente",
    }


def _a_numero(valor: Any, defecto: float = 0.0) -> float:
    if valor is None or valor == "":
        return defecto
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def _a_entero(valor: Any, defecto: int = 0) -> int:
    if valor is None or valor == "":
        return defecto
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def _leer_numero_opcional(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def _a_uuid(valor: Any) -> str | None:
    if not valor:
        return None
    try:
        return str(UUID(str(valor)))
    except (TypeError, ValueError):
        return None


def _normalizar_texto(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def obtener_valor_lectura(valor: dict, *claves: str) -> Any:
    for clave in claves:
        if clave in valor:
            return valor.get(clave)
    return None


def validar_lectura_firebase(valor: dict) -> tuple[bool, list[str]]:
    temperatura_ambiente = _leer_numero_opcional(
        obtener_valor_lectura(
            valor,
            "temperatura_ambiente",
            "temperatura",
            "temperatura_dht11",
            "temp_ambiente",
        )
    )
    humedad = _leer_numero_opcional(valor.get("humedad"))
    razones: list[str] = []

    if temperatura_ambiente is None:
        razones.append("temperatura_ambiente ausente")
    # elif temperatura_ambiente == 0:
    #     razones.append("temperatura_ambiente en 0")
    # elif temperatura_ambiente < 10 or temperatura_ambiente > 45:
    #     razones.append("temperatura_ambiente fuera de rango")

    if humedad is None:
        razones.append("humedad ausente")
    # elif humedad <= 0:
    #     razones.append("humedad en 0 o negativa")
    # elif humedad > 100:
    #     razones.append("humedad fuera de rango")

    return len(razones) == 0, razones


def construir_diagnostico_lecturas(lecturas: dict, seleccion: dict) -> dict:
    items = list(sorted(lecturas.items())) if isinstance(lecturas, dict) else []
    total = len(items)
    validas = 0
    invalidas = 0
    razones_invalidas: dict[str, int] = {}
    ultimas_invalidas_consecutivas = 0
    ceros_repetidos = 0
    ultima_key = items[-1][0] if items else None
    ultima_valida = None

    for firebase_key, valor in items:
        if not isinstance(valor, dict):
            invalidas += 1
            razones = ["formato invalido"]
        else:
            es_valida, razones = validar_lectura_firebase(valor)
            if es_valida:
                validas += 1
                ultima_valida = firebase_key
                continue
            invalidas += 1
            temperatura = _leer_numero_opcional(
                obtener_valor_lectura(
                    valor,
                    "temperatura_ambiente",
                    "temperatura",
                    "temperatura_dht11",
                    "temp_ambiente",
                )
            )
            humedad = _leer_numero_opcional(valor.get("humedad"))
            if temperatura == 0 and humedad == 0:
                ceros_repetidos += 1

        for razon in razones:
            razones_invalidas[razon] = razones_invalidas.get(razon, 0) + 1

    for _firebase_key, valor in reversed(items[-5:]):
        if not isinstance(valor, dict):
            ultimas_invalidas_consecutivas += 1
            continue
        es_valida, _razones = validar_lectura_firebase(valor)
        if es_valida:
            break
        ultimas_invalidas_consecutivas += 1

    porcentaje_invalidas = round((invalidas / total) * 100, 2) if total else 0
    ultima_lectura_recibida_valida = False
    if items and isinstance(items[-1][1], dict):
        ultima_lectura_recibida_valida = validar_lectura_firebase(items[-1][1])[0]

    posible_fallo_sensor = (
        (total > 0 and porcentaje_invalidas > 50)
        or ultimas_invalidas_consecutivas >= 5
        or ceros_repetidos >= 3
        or validas == 0
    )
    mensaje = (
        "Se detectaron muchas lecturas invalidas recientes con temperatura/humedad en 0"
        if posible_fallo_sensor
        else "Sensor estable segun las lecturas recientes"
    )

    return {
        "lecturas_revisadas": total,
        "lecturas_validas": validas,
        "lecturas_invalidas": invalidas,
        "porcentaje_invalidas": porcentaje_invalidas,
        "ultima_lectura_recibida_key": ultima_key,
        "ultima_lectura_recibida_valida": ultima_lectura_recibida_valida,
        "ultima_lectura_valida_key": seleccion.get("firebase_key") or ultima_valida,
        "firebase_key_usado": seleccion.get("firebase_key"),
        "razones_invalidas": razones_invalidas,
        "ultimas_invalidas_consecutivas": ultimas_invalidas_consecutivas,
        "lecturas_cero_repetidas": ceros_repetidos,
        "posible_fallo_sensor": posible_fallo_sensor,
        "estado_sensor": "Sensor con posibles fallos" if posible_fallo_sensor else "Sensor estable",
        "mensaje": mensaje,
    }


def seleccionar_ultima_lectura_valida(lecturas: dict) -> dict:
    if not isinstance(lecturas, dict) or not lecturas:
        return {
            "valida": False,
            "firebase_key": None,
            "lectura": None,
            "lecturas_invalidas_ignoradas": 0,
            "advertencias": ["No hay lecturas en Firebase"],
            "diagnostico": construir_diagnostico_lecturas({}, {"firebase_key": None}),
        }

    lecturas_invalidas = 0
    advertencias: list[str] = []

    for firebase_key, valor in reversed(sorted(lecturas.items())):
        if not isinstance(valor, dict):
            lecturas_invalidas += 1
            advertencias.append(f"{firebase_key}: formato invalido")
            continue

        es_valida, razones = validar_lectura_firebase(valor)
        if es_valida:
            seleccion = {
                "valida": True,
                "firebase_key": firebase_key,
                "lectura": valor,
                "lecturas_invalidas_ignoradas": lecturas_invalidas,
                "advertencias": advertencias[:10],
            }
            seleccion["diagnostico"] = construir_diagnostico_lecturas(
                lecturas, seleccion
            )
            return seleccion

        lecturas_invalidas += 1
        advertencias.append(f"{firebase_key}: {', '.join(razones)}")

    seleccion = {
        "valida": False,
        "firebase_key": None,
        "lectura": None,
        "lecturas_invalidas_ignoradas": lecturas_invalidas,
        "advertencias": advertencias[:10],
    }
    seleccion["diagnostico"] = construir_diagnostico_lecturas(lecturas, seleccion)
    return seleccion


def resolver_sala_id(supabase, pabellon: str, aire: str, valor: dict) -> str | None:
    sala_id = _a_uuid(valor.get("sala_id"))
    if sala_id:
        return sala_id

    nombre_sala = valor.get("sala", valor.get("nombre_sala", valor.get("room")))
    objetivo = _normalizar_texto(nombre_sala or aire)

    try:
        respuesta = (
            supabase.table("rooms")
            .select("id,nombre,pabellon,edificio")
            .or_(f"pabellon.eq.{pabellon},edificio.eq.{pabellon}")
            .execute()
        )
    except Exception:
        return None

    salas = respuesta.data or []
    for sala in salas:
        if _normalizar_texto(sala.get("nombre")) == objetivo:
            return sala.get("id")

    if len(salas) == 1:
        return salas[0].get("id")

    return None


def resolver_nodo_id(supabase, sala_id: str | None, valor: dict) -> str | None:
    nodo_id = _a_uuid(valor.get("nodo_id"))
    if nodo_id:
        return nodo_id

    direccion_mac = valor.get("direccion_mac", valor.get("mac"))
    try:
        if direccion_mac:
            respuesta = (
                supabase.table("nodes")
                .select("id")
                .eq("direccion_mac", str(direccion_mac).upper())
                .limit(1)
                .execute()
            )
            return respuesta.data[0]["id"] if respuesta.data else None

        if sala_id:
            respuesta = (
                supabase.table("nodes")
                .select("id")
                .eq("sala_id", sala_id)
                .eq("esta_activo", True)
                .limit(2)
                .execute()
            )
            if len(respuesta.data or []) == 1:
                return respuesta.data[0]["id"]
    except Exception:
        return None

    return None


def preparar_registro_supabase(
    pabellon: str,
    aire: str,
    firebase_key: str,
    valor: dict,
    sala_id: str | None = None,
    nodo_id: str | None = None,
    registro_anterior: dict | None = None,
) -> dict:
    fecha_sync = datetime.now(ZONA_HORARIA_PANAMA)
    temperatura_ambiente = _a_numero(
        valor.get(
            "temperatura_ambiente",
            valor.get("temperatura", valor.get("temperatura_dht11")),
        )
    )
    temperatura_salida_aire = _a_numero(
        valor.get(
            "temperatura_salida_aire",
            valor.get("temperatura_ac", valor.get("temperatura_ds18b20")),
        )
    )
    delta_t = _a_numero(
        valor.get("delta_t"),
        temperatura_ambiente - temperatura_salida_aire,
    )
    consumo_estimado = estimar_consumo_registro(
        valor=valor,
        registro_anterior=registro_anterior,
        fecha_actual=fecha_sync,
    )
    medicion_electrica = obtener_medicion_electrica_firebase(valor)
    estado_electrico = evaluar_estado_electrico(valor)

    # La medición activa nueva tiene prioridad incluso cuando el firmware
    # conserva un campo legado ``potencia_w: 0`` en la misma lectura.
    potencia_w = medicion_electrica["potencia_activa_w"]
    if potencia_w is None:
        potencia_w = _a_numero(
            valor.get("potencia_w", valor.get("power_w")),
            defecto=None,
        )
    energia_kwh = _a_numero(
        valor.get("energia_kwh", valor.get("energy_kwh")),
        defecto=None,
    )
    accion = inferir_accion(valor)

    registro = {
        "firebase_key": f"{pabellon}_{aire}_{firebase_key}",
        "sala_id": sala_id,
        "nodo_id": nodo_id,
        "pabellon": valor.get("pabellon", pabellon),
        "aire": valor.get("aire", aire),
        "temperatura_ambiente": temperatura_ambiente,
        "humedad": _a_numero(valor.get("humedad")),
        "temperatura_salida_aire": temperatura_salida_aire,
        "delta_t": delta_t,
        "movimiento": _a_entero(valor.get("movimiento")),
        "corriente_rms_cruda": _a_numero(valor.get("corriente_rms_cruda"), defecto=None),
        "corriente_rms_instantanea": _a_numero(valor.get("corriente_rms_instantanea"), defecto=None),
        "corriente_calculada_vpp": _a_numero(valor.get("corriente_calculada_vpp"), defecto=None),
        "factor_calibracion_sct": _a_numero(valor.get("factor_calibracion_sct"), defecto=None),
        "corriente_retenida_por_filtro": (
            _a_booleano(valor.get("corriente_retenida_por_filtro"))
            if valor.get("corriente_retenida_por_filtro") is not None else None
        ),
        "ceros_consecutivos_sct": _a_entero(valor.get("ceros_consecutivos_sct"), defecto=None),
        "potencia_w": potencia_w if potencia_w is not None else consumo_estimado["potencia_w"],
        # Algunos nodos envian energia_kwh=0 aunque esten consumiendo potencia.
        # En ese caso se conserva la estimacion acumulada basada en potencia y tiempo.
        "energia_kwh": (
            energia_kwh
            if energia_kwh is not None and energia_kwh > 0
            else consumo_estimado["energia_kwh"]
        ),
        "estado_ocupacion": _a_booleano(
            valor.get("estado_ocupacion", valor.get("presencia", valor.get("ocupado", 0)))
        ),
        "recomendacion_local": valor.get(
            "recomendacion_local",
            valor.get("recomendacion", ""),
        ),
        "control_ir_activo": _a_booleano(valor.get("control_ir_activo", False)),
        "dht_ok": _a_booleano(valor.get("dht_ok")) if valor.get("dht_ok") is not None else None,
        "ds18b20_ok": _a_booleano(valor.get("ds18b20_ok")) if valor.get("ds18b20_ok") is not None else None,
        "fallos_dht": _a_entero(valor.get("fallos_dht"), defecto=None),
        "fallos_ds18b20": _a_entero(valor.get("fallos_ds18b20"), defecto=None),
        # Campo legado: conserva únicamente lo reportado por software; nunca se
        # usa como confirmación física ni se deriva de la acción deseada.
        "aire_encendido_atmos": (
            _a_booleano(valor.get("aire_encendido_atmos"))
            if valor.get("aire_encendido_atmos") is not None
            else None
        ),
        "estado_deseado": estado_deseado_desde_accion(accion),
        "ultimo_comando_enviado": valor.get("ultima_accion_ejecutada"),
        "estado_reportado_por_software": (
            "encendido"
            if _a_booleano(valor.get("aire_encendido_atmos"))
            else "apagado"
            if valor.get("aire_encendido_atmos") is not None
            else "no_reportado"
        ),
        "estado_electrico_observado": estado_electrico["estado_electrico"],
        "compresor_confirmado": estado_electrico["compresor_confirmado"],
        "ultima_accion_ejecutada": valor.get("ultima_accion_ejecutada"),
        "fecha_sync": fecha_sync.isoformat(),
    }
    registro.update({
        campo: numero
        for campo, numero in medicion_electrica.items()
        if numero is not None
    })
    return registro


def leer_ultimas_lecturas_firebase_rest(
    pabellon: str,
    aire: str,
    limite: int = 1,
) -> dict:
    # Esta función es la única puerta de lectura REST del historial Firebase.
    # Nunca debe convertir una consulta de pantalla en una descarga completa.
    limite = min(max(1, int(limite)), MAX_LECTURAS_FIREBASE_POR_CONSULTA)
    database_url = configuracion.FIREBASE_DATABASE_URL.rstrip("/")
    ruta = (
        f"Atmos/registro/{quote(str(pabellon).strip(), safe='')}/"
        f"{quote(str(aire).strip(), safe='')}/lecturas.json"
    )
    parametros = urlencode({
        "orderBy": '"$key"',
        "limitToLast": limite,
    })
    url = f"{database_url}/{ruta}?{parametros}"

    respuesta = httpx.get(url, timeout=8)
    respuesta.raise_for_status()

    datos = respuesta.json()
    return datos if isinstance(datos, dict) else {}


def leer_ultima_lectura_valida_firebase_rest(
    pabellon: str,
    aire: str,
    limite: int = 50,
) -> dict:
    lecturas = leer_ultimas_lecturas_firebase_rest(
        pabellon=pabellon,
        aire=aire,
        limite=limite,
    )
    seleccion = seleccionar_ultima_lectura_valida(lecturas)
    return {
        **seleccion,
        "cantidad_lecturas_revisadas": len(lecturas),
    }


def leer_comando_firebase_rest(pabellon: str, aire: str) -> dict:
    database_url = configuracion.FIREBASE_DATABASE_URL.rstrip("/")
    ruta = (
        f"Atmos/comandos/{quote(str(pabellon).strip(), safe='')}/"
        f"{quote(str(aire).strip(), safe='')}.json"
    )
    respuesta = httpx.get(f"{database_url}/{ruta}", timeout=8)
    respuesta.raise_for_status()
    datos = respuesta.json()
    return datos if isinstance(datos, dict) else {}


ESTADOS_EJECUCION_FIREBASE = {
    "sin_registro": "No existe un registro de comando en Firebase.",
    "pendiente": "La acción está pendiente de envío o ejecución.",
    "senal_enviada": "La señal infrarroja fue enviada.",
    "enviada_sin_confirmacion": "Señal enviada, funcionamiento del aire no confirmado.",
    "confirmada": "Ejecución infrarroja confirmada.",
    "fallida": "La ejecución infrarroja falló.",
    "inconsistente": "Firebase contiene estados de ejecución contradictorios.",
}


def _normalizar_token_decision(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or "").strip())
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    return texto.lower().replace(" ", "_").replace("-", "_")


def normalizar_pabellon_firebase(pabellon: str) -> str:
    """Normaliza el segmento de Firebase sin alterar el identificador del aire."""
    return _normalizar_token_decision(pabellon)


def _numero_opcional(valor: Any) -> float | int | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numero):
        return None
    return int(numero) if numero.is_integer() else numero


def _confianza_opcional(valor: Any) -> float | None:
    numero = _numero_opcional(valor)
    if numero is None:
        return None
    confianza = float(numero)
    return confianza if 0.0 <= confianza <= 1.0 else None


def _booleano_opcional(valor: Any) -> bool | None:
    if isinstance(valor, bool):
        return valor
    token = _normalizar_token_decision(valor)
    if token in {"true", "1", "si"}:
        return True
    if token in {"false", "0", "no"}:
        return False
    return None


def _clasificar_ejecucion_firebase(datos: dict) -> tuple[str, list[str]]:
    """Clasifica la ejecución usando primero los estados más específicos.

    Orden intencional:
    1) contradicción confirmada/fallida;
    2) señal explícitamente enviada sin confirmación;
    3) fallo explícito;
    4) confirmación explícita;
    5) señal enviada;
    6) pendiente o sin evidencia de envío.

    Así, ``sin_confirmacion`` nunca se interpreta como ``confirmada`` por una
    coincidencia parcial de texto.
    """
    resultado = _normalizar_token_decision(datos.get("resultado"))
    confirmacion = _normalizar_token_decision(datos.get("confirmacion_ir"))

    confirmacion_sin_respuesta = "sin_confirmacion" in confirmacion
    confirmacion_fallida = any(
        marcador in confirmacion
        for marcador in {"fallida", "fallo", "error", "rechazada", "no_enviada"}
    )
    confirmacion_explicita = confirmacion in {
        "confirmada",
        "confirmado",
        "ejecucion_confirmada",
        "accion_confirmada",
        "confirmacion_ir_exitosa",
        "confirmada_por_el_aire",
    }
    senal_enviada = any(
        marcador in confirmacion
        for marcador in {"senal_ir_enviada", "senal_enviada", "enviada"}
    )

    resultado_fallido = any(
        marcador in resultado
        for marcador in {"fallida", "fallo", "error", "no_reconocida", "rechazada"}
    )
    resultado_exitoso = resultado in {
        "ok",
        "exito",
        "exitosa",
        "ejecutada",
        "accion_ejecutada",
        "confirmada",
    }

    advertencias: list[str] = []
    if (confirmacion_explicita and resultado_fallido) or (
        confirmacion_fallida and resultado_exitoso
    ):
        advertencias.append(
            "El resultado y la confirmación infrarroja de Firebase son contradictorios."
        )
        return "inconsistente", advertencias
    if confirmacion_sin_respuesta:
        return "enviada_sin_confirmacion", advertencias
    if confirmacion_fallida or resultado_fallido:
        return "fallida", advertencias
    if confirmacion_explicita:
        return "confirmada", advertencias
    if senal_enviada or resultado_exitoso or datos.get("firma_ejecutada"):
        return "senal_enviada", advertencias
    return "pendiente", advertencias


def normalizar_ultima_decision_firebase(
    pabellon: str,
    aire: str,
    datos: dict | None,
) -> dict:
    """Construye el contrato de lectura sin escribir ni completar datos ausentes."""
    datos = datos if isinstance(datos, dict) else {}
    estado, advertencias = (
        _clasificar_ejecucion_firebase(datos)
        if datos
        else ("sin_registro", [])
    )

    prediccion_original = datos.get("prediccion_modelo")
    recomendacion_final = datos.get("recomendacion_ml")
    accion_solicitada = datos.get("accion")
    ultima_accion = datos.get("ultima_accion_ejecutada")

    if (
        prediccion_original is not None
        and recomendacion_final is not None
        and _normalizar_token_decision(prediccion_original)
        != _normalizar_token_decision(recomendacion_final)
    ):
        advertencias.append(
            "La recomendación final es diferente de la predicción original."
        )
    if (
        accion_solicitada is not None
        and ultima_accion is not None
        and _normalizar_token_decision(accion_solicitada)
        != _normalizar_token_decision(ultima_accion)
    ):
        advertencias.append(
            "La acción solicitada es diferente de la última acción enviada."
        )
    if "no_reconocida" in _normalizar_token_decision(datos.get("resultado")):
        advertencias.append(
            "Firebase contiene un resultado de acción no reconocida."
        )
    if (
        datos.get("confianza_ml") is not None
        and _confianza_opcional(datos.get("confianza_ml")) is None
    ):
        advertencias.append("La confianza de Firebase está fuera del rango válido de 0 a 1.")

    motivo = datos.get("motivo_reglas_seguridad")
    if motivo is None:
        motivo = datos.get("motivo")
    estado_electrico = evaluar_estado_electrico(datos)
    estado_software_raw = datos.get("aire_encendido_atmos")
    estado_software = (
        "encendido"
        if estado_software_raw is not None and _a_booleano(estado_software_raw)
        else "apagado"
        if estado_software_raw is not None
        else "no_reportado"
    )

    return {
        "pabellon": normalizar_pabellon_firebase(pabellon),
        "aire": str(aire).strip(),
        "actualizado_en": datos.get("actualizado_en"),
        "prediccion": {
            "valor": prediccion_original,
            "confianza": _confianza_opcional(datos.get("confianza_ml")),
            "modelo_usado": _booleano_opcional(datos.get("modelo_usado")),
            "origen": datos.get("origen"),
            "tipo_modelo": datos.get("tipo_modelo"),
            "version_modelo": datos.get("version_modelo"),
        },
        "decision": {
            "recomendacion_final": recomendacion_final,
            "accion_solicitada": accion_solicitada,
            "modificada_por_reglas": (
                prediccion_original is not None
                and recomendacion_final is not None
                and _normalizar_token_decision(prediccion_original)
                != _normalizar_token_decision(recomendacion_final)
            ),
            "motivo": motivo,
        },
        "ejecucion": {
            "ultima_accion": ultima_accion,
            "temperatura": _numero_opcional(datos.get("temperatura_ejecutada")),
            "estado": estado,
            "mensaje": ESTADOS_EJECUCION_FIREBASE[estado],
            "resultado_raw": datos.get("resultado"),
            "confirmacion_ir_raw": datos.get("confirmacion_ir"),
            "firma": datos.get("firma_ejecutada"),
        },
        "estado_control": {
            "estado_deseado": estado_deseado_desde_accion(accion_solicitada),
            "ultimo_comando_enviado": ultima_accion,
            "estado_reportado_por_software": estado_software,
            "estado_electrico_observado": estado_electrico["estado_electrico"],
            "compresor_confirmado": estado_electrico["compresor_confirmado"],
        },
        "advertencias": advertencias,
    }


def aplicar_estado_comando_firebase(valor: dict, pabellon: str, aire: str) -> dict:
    """Anexa intención/comando; nunca lo presenta como estado físico ejecutado."""
    try:
        comando = leer_comando_firebase_rest(pabellon, aire)
    except Exception:
        return valor

    accion = inferir_accion(comando)
    if accion not in ACCIONES_ENCENDIDO and accion not in ACCIONES_APAGADO:
        return valor

    return {
        **valor,
        "estado_deseado": estado_deseado_desde_accion(accion),
        "ultimo_comando_enviado": accion,
    }


def guardar_registro_supabase_rest(registro: dict) -> dict:
    supabase_url = configuracion.SUPABASE_URL.strip().strip('"').strip("'").rstrip("/")
    supabase_key = (
        configuracion.SUPABASE_KEY
        .strip()
        .strip('"')
        .strip("'")
        .removeprefix("Bearer ")
        .strip()
    )
    url = f"{supabase_url}/rest/v1/registros?on_conflict=firebase_key"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    respuesta = httpx.post(url, headers=headers, json=registro, timeout=8)
    if respuesta.status_code == 400:
        # Compatibilidad mientras se aplica la migración de columnas. Los
        # campos potencia_w y energia_kwh ya contienen la medición normalizada.
        registro_compatible = {
            campo: valor
            for campo, valor in registro.items()
            if campo not in CAMPOS_ELECTRICOS_FIREBASE
            and campo not in CAMPOS_SEGURIDAD_REGISTROS
        }
        respuesta = httpx.post(url, headers=headers, json=registro_compatible, timeout=8)
    respuesta.raise_for_status()
    datos = respuesta.json()
    return datos[0] if isinstance(datos, list) and datos else registro


def obtener_ultimo_registro_supabase_rest(pabellon: str, aire: str) -> dict | None:
    supabase_url = configuracion.SUPABASE_URL.strip().strip('"').strip("'").rstrip("/")
    supabase_key = (
        configuracion.SUPABASE_KEY
        .strip()
        .strip('"')
        .strip("'")
        .removeprefix("Bearer ")
        .strip()
    )
    parametros = urlencode({
        "select": "firebase_key,fecha_sync,potencia_w,energia_kwh,aire_encendido_atmos,ultima_accion_ejecutada",
        "pabellon": f"eq.{pabellon}",
        "aire": f"eq.{aire}",
        "order": "fecha_sync.desc",
        "limit": "1",
    })
    url = f"{supabase_url}/rest/v1/registros?{parametros}"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    respuesta = httpx.get(url, headers=headers, timeout=8)
    respuesta.raise_for_status()
    datos = respuesta.json()
    return datos[0] if isinstance(datos, list) and datos else None


def obtener_registro_por_firebase_key(firebase_key: str) -> dict | None:
    cliente = obtener_cliente()
    try:
        respuesta = (
            cliente.table("registros")
            .select("*")
            .eq("firebase_key", firebase_key)
            .limit(1)
            .execute()
        )
        return respuesta.data[0] if respuesta.data else None
    except Exception:
        return None


def sincronizar_firebase_supabase(
    pabellon_objetivo: str | None = "robotica",
    aire_objetivo: str | None = "Aire_1",
) -> dict:
    inicio_total = time.monotonic()
    etapas: list[dict] = []

    if pabellon_objetivo and aire_objetivo:
        inicio = time.monotonic()
        lecturas_recientes = leer_ultimas_lecturas_firebase_rest(
            pabellon=pabellon_objetivo,
            aire=aire_objetivo,
            limite=50,
        )
        seleccion = seleccionar_ultima_lectura_valida(lecturas_recientes)
        lecturas = (
            {seleccion["firebase_key"]: seleccion["lectura"]}
            if seleccion["valida"]
            else {}
        )
        etapas.append({
            "paso": "leer_firebase_rest",
            "duracion_ms": round((time.monotonic() - inicio) * 1000, 2),
            "cantidad_lecturas": len(lecturas_recientes),
            "lecturas_invalidas_ignoradas": seleccion["lecturas_invalidas_ignoradas"],
            "firebase_key_usado": seleccion["firebase_key"],
            "lectura_valida": seleccion["valida"],
        })
        if not seleccion["valida"]:
            return {
                "sincronizados": 0,
                "errores": 0,
                "detalles_errores": [],
                "lectura_valida": False,
                "lecturas_invalidas_ignoradas": seleccion["lecturas_invalidas_ignoradas"],
                "firebase_key_usado": None,
                "diagnostico": seleccion["diagnostico"],
                "advertencias": seleccion["advertencias"],
                "mensaje": "No hay lectura valida suficiente en Firebase.",
                "etapas": etapas,
                "duracion_total_ms": round((time.monotonic() - inicio_total) * 1000, 2),
            }

        datos = {
            pabellon_objetivo: {
                aire_objetivo: {"lecturas": lecturas or {}}
            }
        }
        metadata_seleccion = seleccion
    else:
        # El modo heredado leía /Atmos/registro completo, incluyendo el
        # historial de todos los aires. No hay un consumidor actual que deba
        # hacerlo: las llamadas soportadas siempre identifican pabellón y aire.
        raise ValueError(
            "La sincronización Firebase requiere pabellon_objetivo y aire_objetivo; "
            "no se permite leer /Atmos/registro completo."
        )

    if not datos:
        return {"sincronizados": 0, "errores": 0, "mensaje": "No hay datos en Firebase."}

    sincronizados = 0
    errores = 0
    detalles_errores: list[str] = []
    duplicados_ignorados = 0

    for pabellon, aires in datos.items():
        if not isinstance(aires, dict):
            continue

        for aire, contenido in aires.items():
            if es_aire_ignorado(aire):
                etapas.append({
                    "paso": "skip_aire_ignorado",
                    "pabellon": pabellon,
                    "aire": aire,
                })
                continue

            if not isinstance(contenido, dict):
                continue

            lecturas = contenido.get("lecturas", {})
            if not isinstance(lecturas, dict):
                continue

            for firebase_key, valor in lecturas.items():
                if not isinstance(valor, dict):
                    errores += 1
                    continue

                sala_id = _a_uuid(valor.get("sala_id"))
                nodo_id = _a_uuid(valor.get("nodo_id"))
                firebase_key_unica = f"{pabellon}_{aire}_{firebase_key}"

                registro_existente = obtener_registro_por_firebase_key(firebase_key_unica)
                potencia_firebase = obtener_medicion_electrica_firebase(valor).get(
                    "potencia_activa_w"
                )
                if registro_existente:
                    potencia_guardada = _a_numero(
                        registro_existente.get("potencia_w"),
                        defecto=None,
                    )
                    potencia_ya_actualizada = (
                        potencia_firebase is None
                        or (
                            potencia_guardada is not None
                            and abs(potencia_guardada - potencia_firebase) < 0.01
                        )
                    )
                    if potencia_ya_actualizada:
                        duplicados_ignorados += 1
                        etapas.append({
                            "paso": "skip_duplicado",
                            "firebase_key": firebase_key_unica,
                        })
                        continue
                    etapas.append({
                        "paso": "corregir_potencia_duplicado",
                        "firebase_key": firebase_key_unica,
                        "potencia_anterior_w": potencia_guardada,
                        "potencia_firebase_w": potencia_firebase,
                    })

                valor = aplicar_estado_comando_firebase(valor, pabellon, aire)
                registro_anterior = obtener_ultimo_registro_supabase_rest(pabellon, aire)
                registro = preparar_registro_supabase(
                    pabellon=pabellon,
                    aire=aire,
                    firebase_key=firebase_key,
                    valor=valor,
                    sala_id=sala_id,
                    nodo_id=nodo_id,
                    registro_anterior=registro_anterior,
                )

                try:
                    inicio = time.monotonic()
                    guardar_registro_supabase_rest(registro)
                    etapas.append({
                        "paso": "upsert_supabase",
                        "firebase_key": registro["firebase_key"],
                        "duracion_ms": round((time.monotonic() - inicio) * 1000, 2),
                    })
                    sincronizados += 1
                except Exception as error:
                    errores += 1
                    detalles_errores.append(f"{registro['firebase_key']}: {error}")

    return {
        "sincronizados": sincronizados,
        "errores": errores,
        "detalles_errores": detalles_errores[:10],
        "duplicados_ignorados": duplicados_ignorados,
        "lectura_valida": True if metadata_seleccion else None,
        "lecturas_invalidas_ignoradas": (
            metadata_seleccion["lecturas_invalidas_ignoradas"]
            if metadata_seleccion
            else 0
        ),
        "firebase_key_usado": (
            f"{pabellon_objetivo}_{aire_objetivo}_{metadata_seleccion['firebase_key']}"
            if metadata_seleccion and metadata_seleccion["firebase_key"]
            else None
        ),
        "advertencias": metadata_seleccion["advertencias"] if metadata_seleccion else [],
        "diagnostico": metadata_seleccion["diagnostico"] if metadata_seleccion else None,
        "etapas": etapas,
        "duracion_total_ms": round((time.monotonic() - inicio_total) * 1000, 2),
    }


def sincronizar(intervalo_segundos: int = 60):
    print("Sincronizando ATMOS Firebase -> Supabase...")

    while True:
        resultado = sincronizar_firebase_supabase()
        print("Resultado sincronización:", resultado)
        time.sleep(max(60, intervalo_segundos))


if __name__ == "__main__":
    sincronizar()
