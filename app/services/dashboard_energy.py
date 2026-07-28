from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import configuracion


ZONA_PANAMA = ZoneInfo("America/Panama")
MAX_INTERVALO_INTEGRACION_MIN = 10
COBERTURA_MINIMA_DIA = 0.80


@dataclass(frozen=True)
class ConfiguracionRango:
    dias: int
    modo: str
    bucket_horas: int
    etiqueta: str
    agrupacion: str


RANGOS: dict[str, ConfiguracionRango] = {
    "24h": ConfiguracionRango(1, "energy", 1, "24 h", "hour"),
    "7d": ConfiguracionRango(7, "energy", 24, "7 días", "day"),
    "15d": ConfiguracionRango(15, "energy", 24, "15 días", "day"),
    "30d": ConfiguracionRango(30, "energy", 24, "30 días", "day"),
    "3m": ConfiguracionRango(90, "energy", 168, "3 meses", "week"),
}


def rango_dashboard(valor: str | None) -> tuple[str, ConfiguracionRango]:
    clave = (valor or "24h").strip().lower()
    if clave not in RANGOS:
        clave = "24h"
    return clave, RANGOS[clave]


def ahora_panama() -> datetime:
    return datetime.now(ZONA_PANAMA)


def inicio_dia_local(dt: datetime) -> datetime:
    local = dt.astimezone(ZONA_PANAMA)
    return datetime.combine(local.date(), time.min, tzinfo=ZONA_PANAMA)


def esta_en_horario_operativo(dt: datetime) -> bool:
    local = dt.astimezone(ZONA_PANAMA)
    if local.weekday() == 6:
        return False
    return 6 <= local.hour < 23


def parsear_fecha(valor) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        dt = valor
    else:
        try:
            dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def a_float(valor) -> float | None:
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def a_bool(valor) -> bool | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = str(valor).strip().lower()
    if texto in {"1", "true", "si", "sí", "ocupado", "on", "encendido"}:
        return True
    if texto in {"0", "false", "no", "vacio", "vacío", "off", "apagado"}:
        return False
    return None


def preparar_registros(filas: list[dict]) -> list[dict]:
    registros = []
    for fila in filas:
        fecha = parsear_fecha(fila.get("fecha_sync"))
        if not fecha:
            continue
        registros.append({
            **fila,
            "_fecha": fecha,
            "_potencia_w": a_float(fila.get("potencia_w")),
            "_energia_kwh": a_float(fila.get("energia_kwh")),
            "_consumo_intervalo_kwh": a_float(fila.get("consumo_intervalo_kwh")),
            "_tarifa_kwh": a_float(fila.get("tarifa_kwh")),
            "_costo_intervalo": a_float(fila.get("costo_intervalo")),
            "_ocupado": a_bool(fila.get("estado_ocupacion")),
            "_ac_encendido": a_bool(
                fila.get("aire_encendido_atmos")
                if fila.get("aire_encendido_atmos") is not None
                else fila.get("ac_encendido")
            ),
        })
    return sorted(registros, key=lambda r: r["_fecha"])


def clave_bucket(dt: datetime, config: ConfiguracionRango) -> datetime:
    local = dt.astimezone(ZONA_PANAMA)
    if config.agrupacion == "week":
        inicio_semana = local.date() - timedelta(days=local.weekday())
        return datetime.combine(inicio_semana, time.min, tzinfo=ZONA_PANAMA)
    if config.agrupacion == "day":
        return datetime.combine(local.date(), time.min, tzinfo=ZONA_PANAMA)
    return local.replace(minute=0, second=0, microsecond=0)


def etiqueta_bucket(dt: datetime, config: ConfiguracionRango) -> str:
    if config.agrupacion == "hour":
        return dt.strftime("%H:00")
    if config.agrupacion == "week":
        return f"Sem. {dt.strftime('%d/%m')}"
    return dt.strftime("%d/%m")


def crear_bucket(dt: datetime) -> dict:
    return {
        "inicio": dt,
        "suma_potencia": 0.0,
        "muestras_potencia": 0,
        "ocupadas": 0,
        "muestras_ocupacion": 0,
        "energia_kwh": 0.0,
        "intervalos_energia": 0,
    }


def acumular_intervalo_heatmap(
    heatmap: dict,
    *,
    inicio: datetime,
    horas: float,
    energia_kwh: float,
) -> None:
    duracion = timedelta(hours=horas)
    fin = inicio + duracion
    cursor = inicio
    duracion_segundos = duracion.total_seconds()
    if duracion_segundos <= 0:
        return

    while cursor < fin:
        local = cursor.astimezone(ZONA_PANAMA)
        siguiente_hora_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        fin_tramo = min(fin, siguiente_hora_local.astimezone(timezone.utc))
        proporcion = (fin_tramo - cursor).total_seconds() / duracion_segundos
        fecha = local.date().isoformat()
        clave = (local.weekday(), local.hour)
        celda = heatmap.setdefault(
            clave,
            {"por_fecha": {}, "cobertura_por_fecha": {}},
        )
        celda["por_fecha"][fecha] = (
            celda["por_fecha"].get(fecha, 0.0) + energia_kwh * proporcion
        )
        celda["cobertura_por_fecha"][fecha] = (
            celda["cobertura_por_fecha"].get(fecha, 0.0)
            + (fin_tramo - cursor).total_seconds() / 3600
        )
        cursor = fin_tramo


def iterar_intervalos(registros: list[dict]):
    for anterior, actual in zip(registros, registros[1:]):
        inicio = anterior["_fecha"]
        fin = actual["_fecha"]
        if fin <= inicio:
            continue
        segundos = min((fin - inicio).total_seconds(), MAX_INTERVALO_INTEGRACION_MIN * 60)
        if segundos <= 0:
            continue
        horas = segundos / 3600
        consumo_medido = actual["_consumo_intervalo_kwh"]
        if consumo_medido is not None:
            yield anterior, inicio, horas, max(0.0, consumo_medido), "consumo_intervalo_kwh"
            continue
        energia_anterior = anterior["_energia_kwh"]
        energia_actual = actual["_energia_kwh"]
        if energia_anterior is not None and energia_actual is not None:
            delta = energia_actual - energia_anterior
            if delta >= 0:
                yield anterior, inicio, horas, delta, "delta_energia_kwh"
                continue
            if energia_actual >= 0:
                yield anterior, inicio, horas, energia_actual, "reset_energia_kwh"
                continue
        potencia_w = anterior["_potencia_w"]
        if potencia_w is not None:
            yield anterior, inicio, horas, potencia_w * horas / 1000, "integracion_potencia"


def integrar_metricas(registros: list[dict], *, inicio_periodo: datetime, fin_periodo: datetime) -> dict:
    hoy_inicio = inicio_dia_local(fin_periodo)
    semana_inicio = hoy_inicio - timedelta(days=7)
    manana_inicio = hoy_inicio + timedelta(days=1)

    acumulados = {
        "periodo_kwh": 0.0,
        "hoy_kwh": 0.0,
        "semana_previa_kwh_por_dia": {},
        "cobertura_por_dia_horas": {},
        "hoy_vacio_kwh": 0.0,
        "semana_vacio_kwh": 0.0,
        "horas_ac_hoy": 0.0,
        "horas_ac_vacio_hoy": 0.0,
        "horas_ac_fuera_horario_hoy": 0.0,
        "energia_util_por_dia": {},
        "energia_vacia_por_dia": {},
        "heatmap": {},
        "fuentes_energia": {},
    }

    for anterior, inicio, horas, kwh, fuente in iterar_intervalos(registros):
        acumular_intervalo_heatmap(
            acumulados["heatmap"],
            inicio=inicio,
            horas=horas,
            energia_kwh=kwh,
        )
        if inicio < semana_inicio.astimezone(timezone.utc) or inicio > fin_periodo:
            continue
        local = inicio.astimezone(ZONA_PANAMA)
        dia = local.date().isoformat()
        acumulados["cobertura_por_dia_horas"][dia] = (
            acumulados["cobertura_por_dia_horas"].get(dia, 0.0) + horas
        )
        acumulados["fuentes_energia"][fuente] = acumulados["fuentes_energia"].get(fuente, 0) + 1

        if inicio_periodo <= inicio <= fin_periodo:
            acumulados["periodo_kwh"] += kwh
        if hoy_inicio <= local < manana_inicio:
            acumulados["hoy_kwh"] += kwh
            if anterior["_ac_encendido"]:
                acumulados["horas_ac_hoy"] += horas
                if not esta_en_horario_operativo(inicio):
                    acumulados["horas_ac_fuera_horario_hoy"] += horas
        elif (
            semana_inicio <= local < hoy_inicio
            and local.timetz().replace(tzinfo=None)
            <= fin_periodo.astimezone(ZONA_PANAMA).timetz().replace(tzinfo=None)
        ):
            acumulados["semana_previa_kwh_por_dia"][dia] = (
                acumulados["semana_previa_kwh_por_dia"].get(dia, 0.0) + kwh
            )

        if anterior["_ocupado"] is True:
            acumulados["energia_util_por_dia"][dia] = acumulados["energia_util_por_dia"].get(dia, 0.0) + kwh
        elif anterior["_ocupado"] is False:
            acumulados["energia_vacia_por_dia"][dia] = acumulados["energia_vacia_por_dia"].get(dia, 0.0) + kwh

        en_vacio = anterior["_ocupado"] is False and anterior["_ac_encendido"] is True
        if en_vacio and semana_inicio <= local < manana_inicio:
            acumulados["semana_vacio_kwh"] += kwh
            if hoy_inicio <= local < manana_inicio:
                acumulados["hoy_vacio_kwh"] += kwh
                acumulados["horas_ac_vacio_hoy"] += horas

    return acumulados


def inicios_buckets_grafica(config: ConfiguracionRango, fin_periodo: datetime) -> list[datetime]:
    fin_local = fin_periodo.astimezone(ZONA_PANAMA)
    if config.agrupacion == "hour":
        ultimo = fin_local.replace(minute=0, second=0, microsecond=0)
        return [ultimo - timedelta(hours=offset) for offset in range(23, -1, -1)]
    if config.agrupacion == "week":
        ultimo = clave_bucket(fin_periodo, config)
        semanas = (config.dias + 6) // 7
        return [ultimo - timedelta(weeks=offset) for offset in range(semanas - 1, -1, -1)]
    ultimo = datetime.combine(fin_local.date(), time.min, tzinfo=ZONA_PANAMA)
    return [ultimo - timedelta(days=offset) for offset in range(config.dias - 1, -1, -1)]


def siguiente_bucket(inicio: datetime, config: ConfiguracionRango) -> datetime:
    if config.agrupacion == "week":
        return inicio + timedelta(weeks=1)
    if config.agrupacion == "day":
        return inicio + timedelta(days=1)
    return inicio + timedelta(hours=1)


def construir_puntos(registros: list[dict], *, config: ConfiguracionRango, inicio_periodo: datetime, fin_periodo: datetime) -> list[dict]:
    inicios = inicios_buckets_grafica(config, fin_periodo)
    buckets = {inicio: crear_bucket(inicio) for inicio in inicios}
    inicio_grafica = inicios[0].astimezone(timezone.utc)

    for registro in registros:
        fecha = registro["_fecha"]
        if fecha < inicio_grafica or fecha > fin_periodo:
            continue
        bucket_dt = clave_bucket(fecha, config)
        bucket = buckets.get(bucket_dt)
        if bucket is None:
            continue
        ocupado = registro["_ocupado"]
        if ocupado is not None:
            bucket["ocupadas"] += 1 if ocupado else 0
            bucket["muestras_ocupacion"] += 1

    for _anterior, inicio, horas, kwh, _fuente in iterar_intervalos(registros):
        fin_intervalo = inicio + timedelta(hours=horas)
        duracion_total = (fin_intervalo - inicio).total_seconds()
        cursor = max(inicio, inicio_grafica)
        fin_intervalo = min(fin_intervalo, fin_periodo)
        if duracion_total <= 0 or cursor >= fin_intervalo:
            continue
        while cursor < fin_intervalo:
            bucket_dt = clave_bucket(cursor, config)
            bucket = buckets.get(bucket_dt)
            if bucket is None:
                break
            fin_tramo = min(
                fin_intervalo,
                siguiente_bucket(bucket_dt, config).astimezone(timezone.utc),
            )
            segundos_tramo = (fin_tramo - cursor).total_seconds()
            if segundos_tramo <= 0:
                break
            bucket["energia_kwh"] += kwh * (segundos_tramo / duracion_total)
            bucket["intervalos_energia"] += 1
            cursor = fin_tramo

    puntos = []
    for inicio, bucket in sorted(buckets.items()):
        ocupacion_pct = (
            bucket["ocupadas"] / bucket["muestras_ocupacion"]
            if bucket["muestras_ocupacion"]
            else None
        )
        valor = bucket["energia_kwh"] if bucket["intervalos_energia"] else None
        local = inicio.astimezone(ZONA_PANAMA)
        etiqueta_tooltip = (
            local.strftime("%d/%m/%Y · %H:00")
            if config.agrupacion == "hour"
            else etiqueta_bucket(local, config)
        )
        puntos.append({
            "bucket_start": inicio.isoformat(),
            "label": etiqueta_bucket(inicio, config),
            "tooltip_label": etiqueta_tooltip,
            "energia_kwh": round(valor, 3) if valor is not None else None,
            "ocupacion_pct": round(ocupacion_pct, 3) if ocupacion_pct is not None else None,
            "ocupado": ocupacion_pct is not None and ocupacion_pct >= 0.5,
            "ocupacion_banda": 1 if ocupacion_pct is not None and ocupacion_pct >= 0.5 else 0,
        })
    return puntos


def construir_util_vs_vacio(metricas: dict, fin_periodo: datetime) -> list[dict]:
    hoy = fin_periodo.astimezone(ZONA_PANAMA).date()
    puntos = []
    for offset in range(6, -1, -1):
        dia = hoy - timedelta(days=offset)
        clave = dia.isoformat()
        puntos.append({
            "date": clave,
            "label": dia.strftime("%d/%m"),
            "util_kwh": round(metricas["energia_util_por_dia"].get(clave, 0.0), 3),
            "vacio_kwh": round(metricas["energia_vacia_por_dia"].get(clave, 0.0), 3),
        })
    return puntos


def construir_heatmap(metricas: dict, registros: list[dict]) -> dict:
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    horas = list(range(24))
    puntos = []
    maximo = 0.0
    celdas_validas = 0
    for dia_idx, dia in enumerate(dias):
        for hora in horas:
            celda = metricas["heatmap"].get(
                (dia_idx, hora),
                {"por_fecha": {}, "cobertura_por_fecha": {}},
            )
            valores_diarios = list(celda["por_fecha"].values())
            valor = sum(valores_diarios) / len(valores_diarios) if valores_diarios else None
            if valores_diarios:
                celdas_validas += 1
                maximo = max(maximo, valor)
            puntos.append({
                "day": dia,
                "day_index": dia_idx,
                "hour": hora,
                "label": f"{hora:02d}:00",
                "kwh": round(valor, 3) if valor is not None else None,
                "sample_days": len(valores_diarios),
            })
    total_celdas = len(dias) * len(horas)
    cobertura_celdas_pct = celdas_validas / total_celdas * 100 if total_celdas else 0
    return {
        "days": dias,
        "hours": horas,
        "points": puntos,
        "max_kwh": round(maximo, 3),
        "insufficient_data": celdas_validas == 0,
        "coverage_cells_pct": round(cobertura_celdas_pct, 1),
        "valid_cells": celdas_validas,
        "empty_message": "Estamos recopilando historial suficiente para identificar patrones.",
    }


def construir_resumen_dashboard(filas: list[dict], rango: str | None = "24h") -> dict:
    clave_rango, config = rango_dashboard(rango)
    fin_local = ahora_panama()
    fin_utc = fin_local.astimezone(timezone.utc)
    inicio_periodo = (fin_local - timedelta(days=config.dias)).astimezone(timezone.utc)
    inicio_consulta = min(
        inicio_periodo,
        (inicio_dia_local(fin_local) - timedelta(days=30)).astimezone(timezone.utc),
    )

    registros = [
        registro
        for registro in preparar_registros(filas)
        if inicio_consulta <= registro["_fecha"] <= fin_utc
    ]
    puntos = construir_puntos(registros, config=config, inicio_periodo=inicio_periodo, fin_periodo=fin_utc)
    metricas = integrar_metricas(registros, inicio_periodo=inicio_periodo, fin_periodo=fin_utc)

    tarifas_medidas = [
        registro["_tarifa_kwh"]
        for registro in registros
        if registro.get("_tarifa_kwh") is not None
    ]
    tarifa = tarifas_medidas[-1] if tarifas_medidas else configuracion.DASHBOARD_TARIFA_USD_KWH
    horas_objetivo = max(
        0.0,
        (fin_local - inicio_dia_local(fin_local)).total_seconds() / 3600,
    )
    cobertura_minima_horas = horas_objetivo * COBERTURA_MINIMA_DIA
    dias_validos = {
        fecha: energia
        for fecha, energia in metricas["semana_previa_kwh_por_dia"].items()
        if metricas["cobertura_por_dia_horas"].get(fecha, 0) >= cobertura_minima_horas
    }
    dias_semana = list(dias_validos.values())
    promedio_semana = sum(dias_semana) / len(dias_semana) if dias_semana else None
    comparacion_disponible = len(dias_semana) >= 3 and promedio_semana is not None and promedio_semana >= 0.1
    comparacion_hoy = (
        ((metricas["hoy_kwh"] - promedio_semana) / promedio_semana) * 100
        if comparacion_disponible
        else None
    )
    diferencia_hoy_kwh = (
        metricas["hoy_kwh"] - promedio_semana
        if comparacion_disponible
        else None
    )
    potencia_valida = [r for r in registros if r.get("_potencia_w") is not None]
    pico = max(potencia_valida, key=lambda r: r["_potencia_w"]) if potencia_valida else None
    potencia_promedio = (
        sum(r["_potencia_w"] for r in potencia_valida) / len(potencia_valida)
        if potencia_valida
        else None
    )
    relacion_pico_promedio = (
        pico["_potencia_w"] / potencia_promedio
        if pico and potencia_promedio and potencia_promedio > 0
        else None
    )
    costo_hoy = metricas["hoy_kwh"] * tarifa
    costo_vacio_hoy = metricas["hoy_vacio_kwh"] * tarifa
    porcentaje_vacio_hoy = (
        metricas["hoy_vacio_kwh"] / metricas["hoy_kwh"] * 100
        if metricas["hoy_kwh"] > 0
        else None
    )
    dias_util_vacio = len({
        *metricas["energia_util_por_dia"].keys(),
        *metricas["energia_vacia_por_dia"].keys(),
    })
    hoy_clave = fin_local.date().isoformat()
    cobertura_hoy_horas = metricas["cobertura_por_dia_horas"].get(hoy_clave, 0.0)
    cobertura_hoy_pct = (
        cobertura_hoy_horas / horas_objetivo * 100
        if horas_objetivo > 0
        else 0.0
    )
    registros_hoy = [
        registro
        for registro in registros
        if registro["_fecha"].astimezone(ZONA_PANAMA).date() == fin_local.date()
    ]
    ultima_lectura = registros[-1]["_fecha"] if registros else None
    edad_ultima_lectura_segundos = (
        max(0.0, (fin_utc - ultima_lectura).total_seconds())
        if ultima_lectura
        else None
    )
    datos_recientes = (
        edad_ultima_lectura_segundos is not None
        and edad_ultima_lectura_segundos <= 15 * 60
    )

    return {
        "range": clave_rango,
        "range_label": config.etiqueta,
        "generated_at": fin_utc.isoformat(),
        "timezone": "America/Panama",
        "tariff_usd_per_kwh": tarifa,
        "baseline": {
            "kwh_per_day": None,
            "period_kwh": None,
            "chart_value": None,
            "chart_unit": "kWh",
            "configured_in": None,
            "reason": "No se muestra ahorro sin un baseline histórico validado.",
        },
        "chart": {
            "mode": config.modo,
            "unit": "kWh",
            "value_key": "energia_kwh",
            "grouping": config.agrupacion,
            "points": puntos,
            "insufficient_data": not any(punto["energia_kwh"] is not None for punto in puntos),
            "empty_message": "Datos insuficientes",
        },
        "metrics": {
            "today_energy_kwh": round(metricas["hoy_kwh"], 1),
            "today_vs_week_avg_pct": round(comparacion_hoy, 1) if comparacion_hoy is not None else None,
            "week_avg_kwh": round(promedio_semana, 1) if promedio_semana is not None else None,
            "comparison_delta_kwh": round(diferencia_hoy_kwh, 2) if diferencia_hoy_kwh is not None else None,
            "comparison_days": len(dias_semana),
            "comparison_available": comparacion_disponible,
            "today_records": len(registros_hoy),
            "today_coverage_pct": round(min(100.0, cobertura_hoy_pct), 1),
            "data_recent": datos_recientes,
            "latest_reading_at": ultima_lectura.isoformat() if ultima_lectura else None,
            "period_energy_kwh": round(metricas["periodo_kwh"], 1),
            "period_cost_usd": round(metricas["periodo_kwh"] * tarifa, 2),
            "today_cost_usd": round(costo_hoy, 2),
            "ac_hours_today": round(metricas["horas_ac_hoy"], 1),
            "ac_empty_hours_today": round(metricas["horas_ac_vacio_hoy"], 1),
            "ac_outside_schedule_hours_today": round(metricas["horas_ac_fuera_horario_hoy"], 1),
            "empty_energy_kwh": round(metricas["hoy_vacio_kwh"], 1),
            "empty_energy_scope": "hoy",
            "empty_energy_pct": round(porcentaje_vacio_hoy, 1) if porcentaje_vacio_hoy is not None else None,
            "empty_cost_usd": round(costo_vacio_hoy, 2),
            "peak_power_w": round(pico["_potencia_w"], 0) if pico else None,
            "peak_power_at": pico["_fecha"].isoformat() if pico else None,
            "average_power_w": round(potencia_promedio, 1) if potencia_promedio is not None else None,
            "peak_vs_average_ratio": (
                round(relacion_pico_promedio, 2)
                if relacion_pico_promedio is not None
                else None
            ),
            "peak_relevant": relacion_pico_promedio is not None and relacion_pico_promedio >= 1.5,
            "estimated_savings_usd": None,
            "estimated_savings_available": False,
            "estimated_savings_reason": "No existe un baseline histórico validado para atribuir ahorro a ATMOS.",
        },
        "phase2": {
            "useful_vs_empty": construir_util_vs_vacio(metricas, fin_utc),
            "useful_vs_empty_insufficient_data": dias_util_vacio < 3,
            "heatmap": construir_heatmap(metricas, registros),
        },
        "calculation_trace": {
            "today_energy": {
                "formula": "Prioridad: consumo_intervalo_kwh; si falta, suma de deltas positivos del acumulador (tratando resets); finalmente potencia_w × horas.",
                "result_kwh": round(metricas["hoy_kwh"], 6),
            },
            "recent_average": {
                "formula": "Promedio de días con datos entre los 7 anteriores, limitado al mismo horario transcurrido de hoy.",
                "daily_values_kwh": [round(valor, 6) for valor in dias_semana],
                "days_used": len(dias_semana),
                "valid_dates": list(dias_validos.keys()),
                "required_coverage_pct": round(COBERTURA_MINIMA_DIA * 100),
                "target_hours_per_day": round(horas_objetivo, 6),
                "minimum_covered_hours": round(cobertura_minima_horas, 6),
                "covered_hours_by_date": {
                    fecha: round(metricas["cobertura_por_dia_horas"].get(fecha, 0.0), 6)
                    for fecha in metricas["semana_previa_kwh_por_dia"]
                },
                "result_kwh": round(promedio_semana, 6) if promedio_semana is not None else None,
            },
            "comparison": {
                "formula": "(consumo_hoy - promedio_reciente) / promedio_reciente × 100",
                "minimum_days": 3,
                "minimum_denominator_kwh": 0.1,
                "result_pct": round(comparacion_hoy, 6) if comparacion_hoy is not None else None,
            },
            "today_cost": {
                "formula": "consumo_hoy_kwh × tarifa_usd_kwh",
                "tariff_usd_kwh": tarifa,
                "result_usd": round(costo_hoy, 6),
            },
            "empty_consumption": {
                "formula": "Energía de intervalos de hoy con ocupación=false y AC reportado encendido.",
                "result_kwh": round(metricas["hoy_vacio_kwh"], 6),
                "result_pct": round(porcentaje_vacio_hoy, 6) if porcentaje_vacio_hoy is not None else None,
                "associated_cost_usd": round(costo_vacio_hoy, 6),
            },
            "ac_runtime": {
                "formula": "Suma de intervalos de hoy con aire_encendido_atmos/ac_encendido=true; cada hueco se limita a 10 minutos.",
                "result_hours": round(metricas["horas_ac_hoy"], 6),
                "empty_hours": round(metricas["horas_ac_vacio_hoy"], 6),
            },
            "peak_power": {
                "formula": "Máximo potencia_w observado dentro de las lecturas del período consultado.",
                "result_w": round(pico["_potencia_w"], 6) if pico else None,
                "timestamp": pico["_fecha"].isoformat() if pico else None,
                "average_w": round(potencia_promedio, 6) if potencia_promedio is not None else None,
            },
            "data_coverage": {
                "period_start": inicio_periodo.isoformat(),
                "period_end": fin_utc.isoformat(),
                "today_records": len(registros_hoy),
                "today_covered_hours": round(cobertura_hoy_horas, 6),
                "today_coverage_pct": round(min(100.0, cobertura_hoy_pct), 6),
                "latest_reading_at": ultima_lectura.isoformat() if ultima_lectura else None,
                "latest_reading_age_seconds": (
                    round(edad_ultima_lectura_segundos, 3)
                    if edad_ultima_lectura_segundos is not None
                    else None
                ),
                "energy_sources": metricas["fuentes_energia"],
            },
        },
    }
