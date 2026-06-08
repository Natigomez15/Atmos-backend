from fastapi import APIRouter, Depends, HTTPException, status
from datetime import datetime, timezone

from app.models.schemas import (
    SuscripcionPushCrear,
    SuscripcionPushRespuesta,
    ActualizarHorarioNotificaciones,
)
from app.core.database import obtener_cliente
from app.core.security import obtener_usuario_actual
from app.services.notificaciones_service import (
    enviar_notificacion_push,
    obtener_clave_publica_vapid,
)

enrutador = APIRouter(prefix="/notificaciones", tags=["notificaciones"])


@enrutador.get("/clave-publica")
async def obtener_clave_publica():
    """Retorna la clave pública VAPID para que el frontend pueda suscribirse."""
    return {"clave_publica": obtener_clave_publica_vapid()}


@enrutador.post(
    "/suscribir",
    response_model=SuscripcionPushRespuesta,
    status_code=status.HTTP_201_CREATED,
)
async def suscribir_notificaciones(
    datos: SuscripcionPushCrear,
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    """Registra o actualiza una suscripción push para el usuario autenticado."""
    cliente = obtener_cliente()

    fila = {
        "usuario_id":   usuario_actual["id"],
        "rol":          usuario_actual["rol"],
        "endpoint":     datos.endpoint,
        "clave_p256dh": datos.clave_p256dh,
        "clave_auth":   datos.clave_auth,
        "dias_activos": datos.dias_activos,
        "hora_inicio":  datos.hora_inicio,
        "hora_fin":     datos.hora_fin,
        "esta_activa":  True,
    }

    # Upsert por endpoint (restricción UNIQUE de la tabla)
    respuesta = (
        cliente.table("suscripciones_push")
        .upsert(fila, on_conflict="endpoint")
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No se pudo registrar la suscripción",
        )

    return respuesta.data[0]


@enrutador.patch("/horario", response_model=SuscripcionPushRespuesta)
async def actualizar_horario_notificaciones(
    cambios: ActualizarHorarioNotificaciones,
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    """Actualiza el horario o estado de las suscripciones del usuario."""
    cliente = obtener_cliente()

    campos = {k: v for k, v in cambios.model_dump().items() if v is not None}
    if not campos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Se debe enviar al menos un campo para actualizar",
        )

    respuesta = (
        cliente.table("suscripciones_push")
        .update(campos)
        .eq("usuario_id", usuario_actual["id"])
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontró suscripción activa para este usuario",
        )

    return respuesta.data[0]


@enrutador.delete("/cancelar")
async def cancelar_suscripcion(
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    """Desactiva todas las suscripciones push del usuario."""
    cliente = obtener_cliente()

    cliente.table("suscripciones_push").update(
        {"esta_activa": False}
    ).eq("usuario_id", usuario_actual["id"]).execute()

    return {"cancelada": True}


@enrutador.post("/prueba")
async def enviar_notificacion_prueba(
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    """Envía una notificación push de prueba al usuario autenticado."""
    cliente = obtener_cliente()

    respuesta = (
        cliente.table("suscripciones_push")
        .select("*")
        .eq("usuario_id", usuario_actual["id"])
        .eq("esta_activa", True)
        .limit(1)
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay suscripción activa para este usuario",
        )

    suscripcion = respuesta.data[0]
    enviada = enviar_notificacion_push(
        suscripcion=suscripcion,
        titulo="🔔 ATMOS — Notificación de prueba",
        cuerpo="Las notificaciones push están funcionando correctamente.",
        datos={"tipo": "prueba"},
    )

    return {"enviada": enviada}
