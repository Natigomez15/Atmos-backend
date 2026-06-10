from fastapi import APIRouter, Depends, Header, HTTPException, status
from datetime import datetime, timezone
from loguru import logger

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
TABLA_SUSCRIPCIONES_PUSH = "push_subscriptions"


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
    user_agent: str | None = Header(default=None),
):
    """Registra o actualiza una suscripción push para el usuario autenticado."""
    cliente = obtener_cliente()
    ahora = datetime.now(timezone.utc).isoformat()

    if not datos.llave_p256dh or not datos.llave_auth:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La suscripcion debe incluir p256dh y auth",
        )

    fila_base = {
        "profile_id": usuario_actual["id"],
        "endpoint": datos.endpoint,
        "p256dh": datos.llave_p256dh,
        "auth": datos.llave_auth,
        "permiso": datos.permiso,
        "user_agent": datos.user_agent or user_agent,
        "activa": True,
        "actualizado_en": ahora,
    }

    try:
        existente = (
            cliente.table(TABLA_SUSCRIPCIONES_PUSH)
            .select("id")
            .eq("endpoint", datos.endpoint)
            .limit(1)
            .execute()
        )

        if existente.data:
            respuesta = (
                cliente.table(TABLA_SUSCRIPCIONES_PUSH)
                .update(fila_base)
                .eq("endpoint", datos.endpoint)
                .execute()
            )
        else:
            respuesta = (
                cliente.table(TABLA_SUSCRIPCIONES_PUSH)
                .insert({**fila_base, "creado_en": ahora})
                .execute()
            )
    except Exception as error:
        logger.error({
            "evento": "push_subscription_save_failed",
            "tabla": TABLA_SUSCRIPCIONES_PUSH,
            "profile_id": usuario_actual.get("id"),
            "error": str(error),
        })
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No se pudo guardar la suscripcion push en {TABLA_SUSCRIPCIONES_PUSH}: {error}",
        ) from error

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

    datos_cambios = cambios.model_dump()
    campos = {}
    if datos_cambios.get("esta_activa") is not None:
        campos["activa"] = datos_cambios["esta_activa"]
    campos["actualizado_en"] = datetime.now(timezone.utc).isoformat()
    if set(campos.keys()) == {"actualizado_en"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="La tabla push_subscriptions solo permite actualizar activa desde este endpoint",
        )

    respuesta = (
        cliente.table(TABLA_SUSCRIPCIONES_PUSH)
        .update(campos)
        .eq("profile_id", usuario_actual["id"])
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

    cliente.table(TABLA_SUSCRIPCIONES_PUSH).update(
        {
            "activa": False,
            "actualizado_en": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("profile_id", usuario_actual["id"]).execute()

    return {"cancelada": True}


@enrutador.post("/prueba")
async def enviar_notificacion_prueba(
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    """Envía una notificación push de prueba al usuario autenticado."""
    cliente = obtener_cliente()

    respuesta = (
        cliente.table(TABLA_SUSCRIPCIONES_PUSH)
        .select("*")
        .eq("profile_id", usuario_actual["id"])
        .eq("activa", True)
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
