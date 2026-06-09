from datetime import datetime, timezone
from typing import Any


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
    "mantener",
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


def inferir_ac_encendido(valor: dict[str, Any], registro_anterior: dict[str, Any] | None = None) -> bool:
    estado_explicito = _a_bool(
        valor.get("aire_encendido_atmos", valor.get("ac_encendido", valor.get("estado_ac")))
    )
    if estado_explicito is not None:
        return estado_explicito

    accion = inferir_accion(valor)
    if accion in ACCIONES_APAGADO:
        return False
    if accion in ACCIONES_ENCENDIDO:
        return True

    if registro_anterior and registro_anterior.get("potencia_w") is not None:
        return float(registro_anterior["potencia_w"]) > 0

    return False


def estimar_consumo_registro(
    valor: dict[str, Any],
    registro_anterior: dict[str, Any] | None,
    fecha_actual: datetime,
) -> dict[str, float]:
    ac_encendido = inferir_ac_encendido(valor, registro_anterior)
    potencia_w = CONSUMO_AC_KW * CANTIDAD_AIRES * 1000 if ac_encendido else 0.0

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

    energia_kwh = energia_anterior + (potencia_w / 1000) * horas_transcurridas
    costo_estimado = energia_kwh * TARIFA_KWH

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
