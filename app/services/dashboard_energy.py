from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import configuracion


ZONA_PANAMA = ZoneInfo("America/Panama")
MAX_INTERVALO_INTEGRACION_MIN = 10


@dataclass(frozen=True)
class ConfiguracionRango:
    dias: int
    modo: str
    bucket_horas: int
    etiqueta: str


RANGOS: dict[str, ConfiguracionRango] = {
    "24h": ConfiguracionRango(dias=1, modo="power", bucket_horas=1, etiqueta="24 h"),
    "7d": ConfiguracionRango(dias=7, modo="power", bucket_horas=6, etiqueta="7 dias"),
    "15d": ConfiguracionRango(dias=15, modo="power", bucket_horas=6, etiqueta="15 dias"),
    "30d": ConfiguracionRango(dias=30, modo="energy", bucket_horas=24, etiqueta="30 dias"),
    "3m": ConfiguracionRango(dias=90, modo="energy", bucket_horas=24, etiqueta="3 meses"),
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
    if config.modo == "energy":
        return datetime.combine(local.date(), time.min, tzinfo=ZONA_PANAMA)
    hora = (local.hour // config.bucket_horas) * config.bucket_horas
    return local.replace(hour=hora, minute=0, second=0, microsecond=0)


def etiqueta_bucket(dt: datetime, config: ConfiguracionRango) -> str:
    if config.modo == "energy":
        return dt.strftime("%d/%m")
    if config.bucket_horas == 1:
        return dt.strftime("%H:00")
    fin = dt + timedelta(hours=config.bucket_horas)
    return f"{dt.strftime('%d/%m %H')}h-{fin.strftime('%H')}h"


def crear_bucket(dt: datetime) -> dict:
    return {
        "inicio": dt,
        "suma_potencia": 0.0,
        "muestras_potencia": 0,
        "ocupadas": 0,
        "muestras_ocupacion": 0,
        "energia_kwh": 0.0,
    }


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
            yield anterior, inicio, horas, max(0.0, consumo_medido)
            continue
        potencia_w = anterior["_potencia_w"]
        if potencia_w is not None:
            yield anterior, inicio, horas, potencia_w * horas / 1000


def energia_por_acumulado(registros: list[dict]) -> float | None:
    valores = [r["_energia_kwh"] for r in registros if r.get("_energia_kwh") is not None]
    if len(valores) >= 2:
        return max(0.0, max(valores) - min(valores))
    return None


def integrar_metricas(registros: list[dict], *, inicio_periodo: datetime, fin_periodo: datetime) -> dict:
    hoy_inicio = inicio_dia_local(fin_periodo)
    semana_inicio = hoy_inicio - timedelta(days=7)
    manana_inicio = hoy_inicio + timedelta(days=1)

    acumulados = {
        "periodo_kwh": 0.0,
        "hoy_kwh": 0.0,
        "semana_previa_kwh_por_dia": {},
        "hoy_vacio_kwh": 0.0,
        "semana_vacio_kwh": 0.0,
        "horas_ac_hoy": 0.0,
        "horas_ac_fuera_horario_hoy": 0.0,
        "energia_util_por_dia": {},
        "energia_vacia_por_dia": {},
        "heatmap": {},
    }

    for anterior, inicio, horas, kwh in iterar_intervalos(registros):
        if inicio < semana_inicio.astimezone(timezone.utc) or inicio > fin_periodo:
            continue
        local = inicio.astimezone(ZONA_PANAMA)
        dia = local.date().isoformat()

        if inicio_periodo <= inicio <= fin_periodo:
            acumulados["periodo_kwh"] += kwh
        if hoy_inicio <= local < manana_inicio:
            acumulados["hoy_kwh"] += kwh
            if anterior["_ac_encendido"]:
                acumulados["horas_ac_hoy"] += horas
                if not esta_en_horario_operativo(inicio):
                    acumulados["horas_ac_fuera_horario_hoy"] += horas
        elif semana_inicio <= local < hoy_inicio:
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

        if local.weekday() < 6 and 6 <= local.hour < 23:
            clave = (local.weekday(), local.hour)
            celda = acumulados["heatmap"].setdefault(clave, {"kwh": 0.0, "muestras": 0})
            celda["kwh"] += kwh
            celda["muestras"] += 1

    registros_periodo = [r for r in registros if inicio_periodo <= r["_fecha"] <= fin_periodo]
    registros_hoy = [
        r
        for r in registros
        if hoy_inicio <= r["_fecha"].astimezone(ZONA_PANAMA) < manana_inicio
    ]
    acumulado_periodo = energia_por_acumulado(registros_periodo)
    if acumulado_periodo is not None:
        acumulados["periodo_kwh"] = acumulado_periodo
    acumulado_hoy = energia_por_acumulado(registros_hoy)
    if acumulado_hoy is not None:
        acumulados["hoy_kwh"] = acumulado_hoy
    return acumulados


def construir_puntos(registros: list[dict], *, config: ConfiguracionRango, inicio_periodo: datetime, fin_periodo: datetime) -> list[dict]:
    buckets: dict[datetime, dict] = {}
    for registro in registros:
        fecha = registro["_fecha"]
        if fecha < inicio_periodo or fecha > fin_periodo:
            continue
        bucket_dt = clave_bucket(fecha, config)
        bucket = buckets.setdefault(bucket_dt, crear_bucket(bucket_dt))
        potencia_w = registro["_potencia_w"]
        if potencia_w is not None:
            bucket["suma_potencia"] += potencia_w
            bucket["muestras_potencia"] += 1
        ocupado = registro["_ocupado"]
        if ocupado is not None:
            bucket["ocupadas"] += 1 if ocupado else 0
            bucket["muestras_ocupacion"] += 1

    if config.modo == "energy":
        for anterior, inicio, _horas, kwh in iterar_intervalos(registros):
            if inicio < inicio_periodo or inicio > fin_periodo:
                continue
            bucket_dt = clave_bucket(inicio, config)
            bucket = buckets.setdefault(bucket_dt, crear_bucket(bucket_dt))
            bucket["energia_kwh"] += kwh

    puntos = []
    for inicio, bucket in sorted(buckets.items()):
        ocupacion_pct = (
            bucket["ocupadas"] / bucket["muestras_ocupacion"]
            if bucket["muestras_ocupacion"]
            else None
        )
        if config.modo == "energy":
            valor = bucket["energia_kwh"]
            campo = "energia_kwh"
        else:
            valor = (
                bucket["suma_potencia"] / bucket["muestras_potencia"]
                if bucket["muestras_potencia"]
                else None
            )
            campo = "potencia_w"
        if valor is None:
            continue
        puntos.append({
            "bucket_start": inicio.isoformat(),
            "label": etiqueta_bucket(inicio, config),
            campo: round(valor, 3),
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
    dias = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb"]
    horas = list(range(6, 23))
    puntos = []
    maximo = 0.0
    for dia_idx, dia in enumerate(dias):
        for hora in horas:
            celda = metricas["heatmap"].get((dia_idx, hora), {"kwh": 0.0, "muestras": 0})
            valor = celda["kwh"] / celda["muestras"] if celda["muestras"] else 0.0
            maximo = max(maximo, valor)
            puntos.append({
                "day": dia,
                "day_index": dia_idx,
                "hour": hora,
                "label": f"{hora}:00",
                "kwh": round(valor, 3),
            })
    fechas = {r["_fecha"].astimezone(ZONA_PANAMA).date() for r in registros}
    return {
        "days": dias,
        "hours": horas,
        "points": puntos,
        "max_kwh": round(maximo, 3),
        "insufficient_data": len(fechas) < 14,
        "empty_message": "Acumulando datos históricos",
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
    baseline_dia = configuracion.DASHBOARD_BASELINE_KWH_DIA
    baseline_periodo = baseline_dia * config.dias
    ahorro_estimado = (baseline_periodo - metricas["periodo_kwh"]) * tarifa
    ahorro_disponible = ahorro_estimado >= 0 and len(puntos) >= 2
    dias_semana = list(metricas["semana_previa_kwh_por_dia"].values())
    promedio_semana = sum(dias_semana) / len(dias_semana) if dias_semana else None
    comparacion_hoy = (
        ((metricas["hoy_kwh"] - promedio_semana) / promedio_semana) * 100
        if promedio_semana and promedio_semana > 0
        else None
    )

    energia_vacio = metricas["hoy_vacio_kwh"]
    energia_vacio_scope = "hoy"
    if energia_vacio <= 0 and metricas["semana_vacio_kwh"] > 0:
        energia_vacio = metricas["semana_vacio_kwh"]
        energia_vacio_scope = "semana"

    return {
        "range": clave_rango,
        "range_label": config.etiqueta,
        "generated_at": fin_utc.isoformat(),
        "timezone": "America/Panama",
        "tariff_usd_per_kwh": tarifa,
        "baseline": {
            "kwh_per_day": baseline_dia,
            "period_kwh": round(baseline_periodo, 3),
            "chart_value": round(baseline_dia / 24 * 1000 if config.modo == "power" else baseline_dia, 3),
            "chart_unit": "W" if config.modo == "power" else "kWh/dia",
            "configured_in": "app.config.Configuracion.DASHBOARD_BASELINE_KWH_DIA",
        },
        "chart": {
            "mode": config.modo,
            "unit": "W" if config.modo == "power" else "kWh/dia",
            "value_key": "potencia_w" if config.modo == "power" else "energia_kwh",
            "points": puntos,
            "insufficient_data": len(puntos) < 2,
            "empty_message": "Datos insuficientes",
        },
        "metrics": {
            "today_energy_kwh": round(metricas["hoy_kwh"], 1),
            "today_vs_week_avg_pct": round(comparacion_hoy, 1) if comparacion_hoy is not None else None,
            "week_avg_kwh": round(promedio_semana, 1) if promedio_semana is not None else None,
            "period_energy_kwh": round(metricas["periodo_kwh"], 1),
            "period_cost_usd": round(metricas["periodo_kwh"] * tarifa, 2),
            "ac_hours_today": round(metricas["horas_ac_hoy"], 1),
            "ac_outside_schedule_hours_today": round(metricas["horas_ac_fuera_horario_hoy"], 1),
            "empty_energy_kwh": round(energia_vacio, 1),
            "empty_energy_scope": energia_vacio_scope,
            "estimated_savings_usd": round(ahorro_estimado, 2) if ahorro_disponible else None,
            "estimated_savings_available": ahorro_disponible,
            "estimated_savings_reason": None if ahorro_disponible else "Historial insuficiente o consumo superior al baseline.",
        },
        "phase2": {
            "useful_vs_empty": construir_util_vs_vacio(metricas, fin_utc),
            "heatmap": construir_heatmap(metricas, registros),
        },
    }
