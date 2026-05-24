"""
Servicio de notificaciones Telegram para ATMOS.
"""
from datetime import datetime, timezone, timedelta

import requests

from app.config import configuracion
from app.core.database import obtener_cliente

ROLES_POR_ALERTA: dict[str, list[str]] = {
    "node_offline":      ["admin", "tecnico"],
    "power_anomaly":     ["admin", "tecnico"],
    "temperature_stuck": ["admin", "tecnico"],
}

EMOJI_ALERTA: dict[str, str] = {
    "node_offline":      "📡",
    "power_anomaly":     "⚡",
    "temperature_stuck": "🌡️",
}

EMOJI_SEVERIDAD: dict[str, str] = {
    "high":   "🔴",
    "medium": "🟡",
    "low":    "🟢",
}

_ETIQUETA_TIPO: dict[str, str] = {
    "node_offline":      "Nodo sin señal",
    "power_anomaly":     "Consumo anómalo",
    "temperature_stuck": "Temperatura estancada",
}

_ETIQUETA_SEVERIDAD: dict[str, str] = {
    "high":   "Alta",
    "medium": "Media",
    "low":    "Baja",
}

_MESES_ES = [
    "ene", "feb", "mar", "abr", "may", "jun",
    "jul", "ago", "sep", "oct", "nov", "dic",
]
_DIAS_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


# ---------------------------------------------------------------------------
# Horario laboral
# ---------------------------------------------------------------------------

def es_horario_laboral() -> bool:
    zona_panama = timezone(timedelta(hours=-5))
    ahora = datetime.now(zona_panama)
    dias_laborales = [
        int(d) for d in configuracion.WORKING_DAYS.split(",") if d.strip()
    ]
    return (
        ahora.weekday() in dias_laborales
        and configuracion.WORKING_HOURS_START <= ahora.hour < configuracion.WORKING_HOURS_END
    )


# ---------------------------------------------------------------------------
# Envío de mensaje
# ---------------------------------------------------------------------------

def enviar_mensaje(chat_id: str, texto: str) -> bool:
    if not configuracion.TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{configuracion.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Formato del mensaje
# ---------------------------------------------------------------------------

def formatear_mensaje_alerta(
    tipo_alerta: str,
    severidad: str,
    mensaje: str,
    nombre_sala: str,
    detalle: dict | None = None,
) -> str:
    zona_panama = timezone(timedelta(hours=-5))
    ahora = datetime.now(zona_panama)
    fecha_str = (
        f"{ahora.hour:02d}:{ahora.minute:02d} — "
        f"{_DIAS_ES[ahora.weekday()]} "
        f"{ahora.day:02d} {_MESES_ES[ahora.month - 1]} {ahora.year}"
    )

    emoji_sev = EMOJI_SEVERIDAD.get(severidad, "⚪")
    emoji_alerta = EMOJI_ALERTA.get(tipo_alerta, "❗")
    etiqueta_sev = _ETIQUETA_SEVERIDAD.get(severidad, severidad)
    etiqueta_tipo = _ETIQUETA_TIPO.get(tipo_alerta, tipo_alerta)

    lineas = [
        f"{emoji_sev} <b>ATMOS — Alerta {etiqueta_sev}</b>",
        "",
        f"📍 <b>Salón:</b> {nombre_sala}",
        f"{emoji_alerta} <b>Tipo:</b> {etiqueta_tipo}",
        f"📝 {mensaje}",
    ]

    if detalle:
        if tipo_alerta == "node_offline":
            lineas.append(
                f"\n⏱ Sin señal desde: {detalle.get('ultima_vez_visto') or 'Nunca'}"
            )
        elif tipo_alerta == "power_anomaly":
            lineas.append(f"\n📊 Consumo actual: {detalle.get('potencia_actual_w')}W")
            lineas.append(f"📊 Promedio histórico: {detalle.get('potencia_base_w')}W")
            lineas.append(f"📈 Exceso: {detalle.get('exceso_pct')}%")
        elif tipo_alerta == "temperature_stuck":
            lineas.append(f"\n🌡 Temperatura: {detalle.get('temperatura_promedio')}°C")
            lineas.append(f"❄️ Setpoint AC: {detalle.get('setpoint_ac')}°C")

    lineas += [
        "",
        f"🕐 {fecha_str}",
        "",
        "✅ Ver en dashboard:",
        "https://atmos-frontend.vercel.app/alerts",
    ]

    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Notificar alerta
# ---------------------------------------------------------------------------

def notificar_alerta(
    tipo_alerta: str,
    severidad: str,
    mensaje: str,
    sala_id: str,
    detalle: dict | None = None,
) -> dict:
    if not es_horario_laboral():
        return {"enviados": 0, "razon": "fuera_horario_laboral"}

    cliente = obtener_cliente()

    resp_sala = (
        cliente.table("rooms")
        .select("nombre")
        .eq("id", sala_id)
        .single()
        .execute()
    )
    nombre_sala = (
        resp_sala.data.get("nombre", sala_id) if resp_sala.data else "Salón desconocido"
    )

    roles = ROLES_POR_ALERTA.get(tipo_alerta, ["admin"])
    resp_subs = (
        cliente.table("telegram_subscribers")
        .select("chat_id, nombre")
        .eq("esta_activo", True)
        .in_("rol", roles)
        .execute()
    )
    suscriptores = resp_subs.data or []

    texto = formatear_mensaje_alerta(tipo_alerta, severidad, mensaje, nombre_sala, detalle)

    enviados = 0
    for sub in suscriptores:
        if enviar_mensaje(sub["chat_id"], texto):
            enviados += 1

    return {"enviados": enviados, "total": len(suscriptores)}


# ---------------------------------------------------------------------------
# Gestión de suscriptores
# ---------------------------------------------------------------------------

def registrar_suscriptor(chat_id: str, nombre: str, rol: str) -> dict:
    from datetime import datetime, timezone
    cliente = obtener_cliente()
    resp = (
        cliente.table("telegram_subscribers")
        .upsert(
            {
                "chat_id":    chat_id,
                "nombre":     nombre,
                "rol":        rol,
                "esta_activo": True,
                "ultima_vez_visto": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="chat_id",
        )
        .execute()
    )
    return resp.data[0] if resp.data else {}


def obtener_suscriptores() -> list[dict]:
    cliente = obtener_cliente()
    resp = (
        cliente.table("telegram_subscribers")
        .select("*")
        .order("creado_en", desc=True)
        .execute()
    )
    return resp.data or []


def desactivar_suscriptor(chat_id: str) -> bool:
    cliente = obtener_cliente()
    resp = (
        cliente.table("telegram_subscribers")
        .update({"esta_activo": False})
        .eq("chat_id", chat_id)
        .execute()
    )
    return bool(resp.data)
