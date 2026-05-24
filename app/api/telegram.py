"""
Router /telegram — webhook de Telegram y gestión de suscriptores.
"""
from fastapi import APIRouter, Depends, HTTPException, Header

from app.config import configuracion
from app.services.telegram_service import (
    registrar_suscriptor,
    desactivar_suscriptor,
    obtener_suscriptores,
    enviar_mensaje,
)
from app.core.database import obtener_cliente

enrutador = APIRouter(prefix="/telegram", tags=["telegram"])


def _verificar_admin(x_cron_secret: str | None = Header(default=None)):
    if not x_cron_secret or x_cron_secret != configuracion.CRON_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")


# ---------------------------------------------------------------------------
# Webhook — Telegram llama aquí cuando el bot recibe un mensaje
# ---------------------------------------------------------------------------

@enrutador.post("/webhook", status_code=200)
async def webhook_telegram(update: dict):
    mensaje = update.get("message") or update.get("edited_message")
    if not mensaje:
        return {"ok": True}

    chat_id = str(mensaje.get("chat", {}).get("id", ""))
    nombre = mensaje.get("from", {}).get("first_name", "Usuario")
    texto = (mensaje.get("text") or "").strip()

    if not chat_id:
        return {"ok": True}

    if texto == "/start":
        registrar_suscriptor(chat_id=chat_id, nombre=nombre, rol="tecnico")
        enviar_mensaje(
            chat_id,
            "✅ <b>Bienvenido a ATMOS</b>\n\n"
            "Recibirás alertas de mantenimiento "
            "durante el horario laboral (Lun-Sáb 7am-6pm).\n\n"
            "Comandos disponibles:\n"
            "/start — Suscribirse\n"
            "/stop  — Cancelar suscripción\n"
            "/status — Ver tu estado",
        )

    elif texto == "/stop":
        desactivar_suscriptor(chat_id)
        enviar_mensaje(
            chat_id,
            "❌ Suscripción cancelada. "
            "Ya no recibirás alertas de ATMOS.\n"
            "Escribe /start para volver a suscribirte.",
        )

    elif texto == "/status":
        cliente = obtener_cliente()
        resp = (
            cliente.table("telegram_subscribers")
            .select("nombre, rol, esta_activo")
            .eq("chat_id", chat_id)
            .execute()
        )
        filas = resp.data or []
        if filas and filas[0].get("esta_activo"):
            sub = filas[0]
            enviar_mensaje(
                chat_id,
                f"✅ Suscrito como <b>{sub['nombre']}</b>\n"
                f"Rol: {sub['rol']}\n"
                "Estado: Activo",
            )
        else:
            enviar_mensaje(
                chat_id,
                "❌ No estás suscrito.\n"
                "Escribe /start para suscribirte.",
            )

    return {"ok": True}


# ---------------------------------------------------------------------------
# Listar suscriptores — solo admin
# ---------------------------------------------------------------------------

@enrutador.get("/suscriptores")
async def listar_suscriptores(_=Depends(_verificar_admin)):
    return obtener_suscriptores()


# ---------------------------------------------------------------------------
# Mensaje de prueba — solo admin
# ---------------------------------------------------------------------------

@enrutador.post("/prueba", status_code=200)
async def mensaje_prueba(_=Depends(_verificar_admin)):
    suscriptores = obtener_suscriptores()
    activos = [s for s in suscriptores if s.get("esta_activo")]
    enviados = 0
    for sub in activos:
        if enviar_mensaje(
            sub["chat_id"],
            "🧪 ATMOS — Mensaje de prueba\n"
            "El sistema de notificaciones está funcionando.",
        ):
            enviados += 1
    return {"enviados": enviados}
