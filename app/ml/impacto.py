from datetime import datetime, timezone
from typing import Any

from app.config import configuracion
from app.core.control_ir import estado_deseado_desde_accion


CONSUMO_AC_KW = 1.5
TARIFA_KWH = 0.18
CANTIDAD_AIRES = 1
DURACION_BLOQUE_HORAS = 45 / 60
BLOQUES_TOTALES_SEMANA = 95
SEMANAS_MES = 4
HORAS_TOTALES_SEMANA = BLOQUES_TOTALES_SEMANA * DURACION_BLOQUE_HORAS
HORAS_OPERACION_MES = HORAS_TOTALES_SEMANA * SEMANAS_MES

ACCIONES_APAGADO = {"apagar", "off", "apagado"}
ACCIONES_ESPERA = {"esperar_apagado"}
ACCIONES_ENCENDIDO = {
    "encender_22",
    "ahorro_24",
    "enfriar_fuerte",
    "on",
    "encendido",
}

IMPACTO_REAL_SALONES = {
    "LARSO": {
        "consumo_mensual_kwh": (897.2 + 1009.6) / 2,
        "bloques_vacios": 29,
    },
    "Robótica": {
        "consumo_mensual_kwh": (836.4 + 940.8) / 2,
        "bloques_vacios": 24,
    },
    "LAI 1": {
        "consumo_mensual_kwh": (745.2 + 838.4) / 2,
        "bloques_vacios": 29,
    },
}


def _normalizar(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def _a_bool(valor: Any) -> bool | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return valor
    texto = _normalizar(valor)
    if texto in {"1", "true", "si", "sí", "on", "encendido", "ocupado"}:
        return True
    if texto in {"0", "false", "no", "off", "apagado", "sin_ocupacion"}:
        return False
    return None


def _numero_opcional(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def obtener_medicion_electrica_firebase(valor: dict[str, Any]) -> dict[str, float | None]:
    """Normaliza la telemetría eléctrica calculada por el ESP32.

    La potencia activa medida tiene prioridad sobre cualquier estimación fija.
    ``potencia_aparente_va`` se conserva aparte: no se usa como watts porque
    incluye potencia reactiva y no equivale a consumo activo.
    """
    potencia_activa_w = _numero_opcional(
        valor.get("potencia_activa_w", valor.get("potencia_w", valor.get("power_w")))
    )
    potencia_activa_kw = _numero_opcional(valor.get("potencia_activa_kw"))
    if potencia_activa_w is None and potencia_activa_kw is not None:
        potencia_activa_w = potencia_activa_kw * 1000

    return {
        "corriente_rms": _numero_opcional(
            valor.get("corriente_rms", valor.get("corriente_a"))
        ),
        "voltaje_red_v": _numero_opcional(valor.get("voltaje_red_v")),
        "factor_potencia": _numero_opcional(valor.get("factor_potencia")),
        "potencia_aparente_va": _numero_opcional(valor.get("potencia_aparente_va")),
        "potencia_activa_w": potencia_activa_w,
        "potencia_activa_kw": (
            potencia_activa_kw
            if potencia_activa_kw is not None
            else potencia_activa_w / 1000 if potencia_activa_w is not None else None
        ),
        "consumo_intervalo_kwh": _numero_opcional(valor.get("consumo_intervalo_kwh")),
        "consumo_acumulado_sesion_kwh": _numero_opcional(
            valor.get("consumo_acumulado_sesion_kwh")
        ),
        "tarifa_kwh": _numero_opcional(valor.get("tarifa_kwh")),
        "costo_intervalo": _numero_opcional(valor.get("costo_intervalo")),
        "costo_acumulado_sesion": _numero_opcional(valor.get("costo_acumulado_sesion")),
    }


def _fecha(valor: Any) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        fecha = valor
    else:
        try:
            fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    if fecha.tzinfo is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha


def inferir_accion(valor: dict[str, Any]) -> str:
    return _normalizar(
        valor.get("ultima_accion_ejecutada")
        or valor.get("accion")
        or valor.get("decision_final")
        or valor.get("recomendacion_local")
        or valor.get("recomendacion")
    )


def evaluar_estado_electrico(valor: dict[str, Any]) -> dict[str, Any]:
    """Evalúa telemetría sin declarar estados físicos antes de calibrar."""
    medicion = obtener_medicion_electrica_firebase(valor)
    potencia_activa_w = medicion["potencia_activa_w"]
    if potencia_activa_w is None or configuracion.AC_POWER_THRESHOLDS_CALIBRATED is not True:
        return {
            "estado_electrico": "no_confirmado",
            "compresor_confirmado": False,
            "potencia_activa_w": potencia_activa_w,
            "umbrales_calibrados": False,
        }
    if potencia_activa_w < configuracion.AC_POWER_OFF_THRESHOLD_W:
        estado = "apagado"
    elif potencia_activa_w >= configuracion.AC_POWER_ON_THRESHOLD_W:
        estado = "encendido"
    else:
        estado = "no_confirmado"
    return {
        "estado_electrico": estado,
        "compresor_confirmado": (
            potencia_activa_w >= configuracion.AC_COMPRESSOR_ON_THRESHOLD_W
        ),
        "potencia_activa_w": potencia_activa_w,
        "umbrales_calibrados": True,
    }


def inferir_ac_encendido(
    valor: dict[str, Any],
    registro_anterior: dict[str, Any] | None = None,
) -> bool | None:
    """Compatibilidad: solo responde cuando la evidencia eléctrica lo confirma."""
    estado = evaluar_estado_electrico(valor)["estado_electrico"]
    if estado == "encendido":
        return True
    if estado == "apagado":
        return False
    return None


def estimar_consumo_registro(
    valor: dict[str, Any],
    registro_anterior: dict[str, Any] | None,
    fecha_actual: datetime,
) -> dict[str, float]:
    estado_software = _a_bool(
        valor.get("aire_encendido_atmos", valor.get("ac_encendido", valor.get("estado_ac")))
    )
    medicion = obtener_medicion_electrica_firebase(valor)
    potencia_medida_w = medicion["potencia_activa_w"]
    potencia_w = (
        potencia_medida_w
        if potencia_medida_w is not None
        else CONSUMO_AC_KW * CANTIDAD_AIRES * 1000 if estado_software is True else 0.0
    )

    energia_anterior = 0.0
    if registro_anterior and registro_anterior.get("energia_kwh") is not None:
        energia_anterior = float(registro_anterior["energia_kwh"])

    fecha_anterior = _fecha(registro_anterior.get("fecha_sync")) if registro_anterior else None
    horas_transcurridas = 0.0
    if fecha_anterior:
        horas_transcurridas = max(
            0.0,
            (fecha_actual - fecha_anterior.astimezone(fecha_actual.tzinfo)).total_seconds() / 3600,
        )

    consumo_intervalo = medicion["consumo_intervalo_kwh"]
    if consumo_intervalo is not None:
        # El acumulado de sesión puede volver a cero cuando el aire reinicia.
        # Sumando intervalos al acumulado del backend se mantiene una serie
        # monótona útil para el dashboard aunque cambie la sesión eléctrica.
        energia_kwh = energia_anterior + max(0.0, consumo_intervalo)
    else:
        energia_kwh = energia_anterior + (potencia_w / 1000) * horas_transcurridas

    tarifa = medicion["tarifa_kwh"] if medicion["tarifa_kwh"] is not None else TARIFA_KWH
    costo_estimado = energia_kwh * tarifa

    return {
        "potencia_w": round(potencia_w, 2),
        "energia_kwh": round(energia_kwh, 6),
        "costo_estimado": round(costo_estimado, 4),
    }


def resumir_impacto_decisiones(
    registros: list[dict[str, Any]],
    horas_operacion_mes: float = HORAS_OPERACION_MES,
    consumo_ac_kw: float = CONSUMO_AC_KW,
    tarifa_kwh: float = TARIFA_KWH,
    cantidad_aires: int = CANTIDAD_AIRES,
) -> dict[str, Any]:
    total_lecturas = len(registros)
    if total_lecturas == 0:
        porcentaje_apagado = 0.0
        porcentaje_espera = 0.0
        porcentaje_mantener = 0.0
        porcentaje_enfriar = 0.0
        apagados = esperas = mantener = enfriar = 0
    else:
        decisiones = [_normalizar(r.get("decision_final") or r.get("ultima_accion_ejecutada")) for r in registros]
        apagados = sum(1 for decision in decisiones if decision in ACCIONES_APAGADO)
        esperas = sum(1 for decision in decisiones if decision in ACCIONES_ESPERA)
        mantener = sum(1 for decision in decisiones if decision == "mantener")
        enfriar = sum(1 for decision in decisiones if decision == "enfriar_fuerte")

        porcentaje_apagado = apagados / total_lecturas
        porcentaje_espera = esperas / total_lecturas
        porcentaje_mantener = mantener / total_lecturas
        porcentaje_enfriar = enfriar / total_lecturas

    horas_ahorro_mes = horas_operacion_mes * porcentaje_apagado
    kwh_sin_atmos = horas_operacion_mes * consumo_ac_kw * cantidad_aires
    kwh_ahorrados = horas_ahorro_mes * consumo_ac_kw * cantidad_aires
    kwh_con_atmos = max(0.0, kwh_sin_atmos - kwh_ahorrados)
    costo_sin_atmos = kwh_sin_atmos * tarifa_kwh
    costo_con_atmos = kwh_con_atmos * tarifa_kwh
    ahorro_mensual = kwh_ahorrados * tarifa_kwh

    return {
        "total_lecturas": total_lecturas,
        "conteo_decisiones": {
            "apagar": apagados,
            "esperar_apagado": esperas,
            "mantener": mantener,
            "enfriar_fuerte": enfriar,
        },
        "porcentaje_apagado": round(porcentaje_apagado * 100, 2),
        "porcentaje_espera": round(porcentaje_espera * 100, 2),
        "porcentaje_mantener": round(porcentaje_mantener * 100, 2),
        "porcentaje_enfriar": round(porcentaje_enfriar * 100, 2),
        "horas_operacion_mes": round(horas_operacion_mes, 2),
        "horas_ahorro_mes": round(horas_ahorro_mes, 2),
        "consumo_ac_kw": consumo_ac_kw,
        "tarifa_kwh": tarifa_kwh,
        "cantidad_aires": cantidad_aires,
        "kwh_sin_atmos": round(kwh_sin_atmos, 2),
        "kwh_con_atmos": round(kwh_con_atmos, 2),
        "kwh_ahorrados": round(kwh_ahorrados, 2),
        "costo_sin_atmos": round(costo_sin_atmos, 2),
        "costo_con_atmos": round(costo_con_atmos, 2),
        "ahorro_mensual": round(ahorro_mensual, 2),
        "ahorro_anual": round(ahorro_mensual * 12, 2),
        "ahorro_predicho_pct": round(porcentaje_apagado * 100, 2),
    }


def resumir_impacto_real(
    tarifa_kwh: float = 0.22,
    meses_academicos: int = 10,
) -> dict[str, Any]:
    salones = []
    ahorro_total_mes_kwh = 0.0
    consumo_total_mes_kwh = 0.0

    for nombre, datos in IMPACTO_REAL_SALONES.items():
        consumo_mes = datos["consumo_mensual_kwh"]
        vacancia_pct = datos["bloques_vacios"] / BLOQUES_TOTALES_SEMANA
        ahorro_mes = consumo_mes * vacancia_pct
        consumo_con_atmos = consumo_mes - ahorro_mes

        ahorro_total_mes_kwh += ahorro_mes
        consumo_total_mes_kwh += consumo_mes

        salones.append({
            "nombre": nombre,
            "consumo_sin_atmos_kwh": round(consumo_mes, 2),
            "consumo_con_atmos_kwh": round(consumo_con_atmos, 2),
            "ahorro_kwh_mes": round(ahorro_mes, 2),
            "vacancia_pct": round(vacancia_pct * 100, 2),
            "ahorro_usd_mes": round(ahorro_mes * tarifa_kwh, 2),
        })

    costo_sin_atmos_anual = consumo_total_mes_kwh * tarifa_kwh * meses_academicos
    ahorro_anual = ahorro_total_mes_kwh * tarifa_kwh * meses_academicos

    return {
        "salones": salones,
        "tarifa_kwh": tarifa_kwh,
        "meses_academicos": meses_academicos,
        "ahorro_total_mes_kwh": round(ahorro_total_mes_kwh, 2),
        "ahorro_total_mes_usd": round(ahorro_total_mes_kwh * tarifa_kwh, 2),
        "costo_sin_atmos_anual": round(costo_sin_atmos_anual, 2),
        "costo_con_atmos_anual": round(costo_sin_atmos_anual - ahorro_anual, 2),
        "ahorro_total_anual_usd": round(ahorro_anual, 2),
        "ahorro_promedio_pct": round(
            (ahorro_total_mes_kwh / consumo_total_mes_kwh) * 100 if consumo_total_mes_kwh else 0,
            2,
        ),
    }
