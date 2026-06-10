from pywebpush import webpush, WebPushException
from app.config import configuracion
from app.core.database import obtener_cliente
from datetime import datetime, timezone, timedelta
from loguru import logger
import json
import os

# Zona horaria de Panamá (UTC-5, sin horario de verano)
ZONA_PANAMA = timezone(timedelta(hours=-5))
TABLA_SUSCRIPCIONES_PUSH = "push_subscriptions"

# Roles destinatarios según tipo de alerta
ROLES_POR_TIPO_ALERTA: dict[str, list[str]] = {
    "node_offline":      ["admin", "mantenimiento"],
    "power_anomaly":     ["admin", "mantenimiento"],
    "temperature_stuck": ["admin", "mantenimiento"],
    "sensor_datos_invalidos": ["admin", "mantenimiento"],
    "temperatura_alta": ["admin", "mantenimiento"],
    "temperatura_fuera_rango": ["admin", "mantenimiento"],
    "humedad_alta": ["admin", "mantenimiento"],
    "humedad_invalida": ["admin", "mantenimiento"],
}

MENSAJES_PUSH_ATMOS: dict[str, dict[str, str]] = {
    "sensor_datos_invalidos": {
        "titulo": "ATMOS: posible fallo de sensor",
        "cuerpo": (
            "Se detectaron lecturas invalidas recientes del ESP32 o sensor. "
            "Revisa el modulo de monitoreo."
        ),
    },
    "temperatura_alta": {
        "titulo": "ATMOS: temperatura alta",
        "cuerpo": "La temperatura ambiente supero el limite configurado.",
    },
    "temperatura_fuera_rango": {
        "titulo": "ATMOS: temperatura fuera de rango",
        "cuerpo": "Se detecto una temperatura ambiente fuera del rango logico.",
    },
    "humedad_alta": {
        "titulo": "ATMOS: humedad alta",
        "cuerpo": "La humedad supero el limite configurado.",
    },
    "humedad_invalida": {
        "titulo": "ATMOS: humedad invalida",
        "cuerpo": "Se detecto una lectura de humedad invalida.",
    },
}


def esta_en_horario_usuario(suscripcion: dict) -> bool:
    """Retorna True si la hora actual (Panamá) cae dentro del horario configurado."""
    if not all(campo in suscripcion for campo in ("dias_activos", "hora_inicio", "hora_fin")):
        return True

    ahora = datetime.now(ZONA_PANAMA)
    dias_activos = [int(d) for d in suscripcion["dias_activos"].split(",")]

    dia_coincide  = ahora.weekday() in dias_activos
    hora_coincide = suscripcion["hora_inicio"] <= ahora.hour < suscripcion["hora_fin"]
    return dia_coincide and hora_coincide


def _obtener_vapid_privada() -> str:
    clave = configuracion.vapid_clave_privada or os.getenv("VAPID_PRIVATE_KEY", "")
    return clave.replace("\\n", "\n").strip()


def _obtener_vapid_correo() -> str:
    correo = (
        configuracion.vapid_correo
        or os.getenv("VAPID_EMAIL", "")
        or os.getenv("VAPID_CORREO", "")
    ).strip()
    if correo and "@" in correo and not correo.startswith(("mailto:", "https://")):
        return f"mailto:{correo}"
    return correo


def enviar_notificacion_push(
    suscripcion: dict,
    titulo:      str,
    cuerpo:      str,
    datos:       dict = None,
) -> bool:
    """
    Envía una notificación push a un endpoint registrado.
    Nunca lanza excepciones — una falla no debe interrumpir el flujo de alertas.
    """
    resultado = enviar_notificacion_push_detallada(suscripcion, titulo, cuerpo, datos)
    return bool(resultado.get("enviada"))


def enviar_notificacion_push_detallada(
    suscripcion: dict,
    titulo: str,
    cuerpo: str,
    datos: dict = None,
) -> dict:
    """Envia una notificacion push y devuelve diagnostico seguro."""
    datos = datos or {}
    url = datos.get("url", "/alerts")
    tipo_alerta = datos.get("tipo_alerta")
    sala_id = datos.get("sala_id")
    tag = datos.get("tag") or ":".join(
        str(parte) for parte in [tipo_alerta, sala_id] if parte
    )

    payload = json.dumps({
        "titulo": titulo,
        "title": titulo,
        "cuerpo": cuerpo,
        "body": cuerpo,
        "datos":  datos,
        "icono":  "/favicon.svg",
        "icon":   "/favicon.svg",
        "url":    url,
        "tipo_alerta": tipo_alerta,
        "severidad": datos.get("severidad"),
        "tag": tag,
        "renotify": False,
    })

    try:
        clave_privada = _obtener_vapid_privada()
        correo_vapid = _obtener_vapid_correo()
        if not clave_privada:
            logger.error("VAPID privada no configurada.")
            return {
                "enviada": False,
                "motivo": "vapid_privada_no_configurada",
                "detalle": "VAPID privada no configurada en backend.",
            }
        if not correo_vapid:
            logger.error("VAPID correo no configurado.")
            return {
                "enviada": False,
                "motivo": "vapid_correo_no_configurado",
                "detalle": "VAPID correo/subject no configurado en backend.",
            }

        p256dh = suscripcion.get("p256dh") or suscripcion.get("clave_p256dh")
        auth = suscripcion.get("auth") or suscripcion.get("clave_auth")
        if not p256dh or not auth:
            logger.warning("Suscripcion push sin p256dh/auth; se omite envio.")
            return {
                "enviada": False,
                "motivo": "suscripcion_sin_claves",
                "detalle": "La suscripcion no tiene p256dh/auth.",
            }

        webpush(
            subscription_info={
                "endpoint": suscripcion["endpoint"],
                "keys": {
                    "p256dh": p256dh,
                    "auth":   auth,
                },
            },
            data=payload,
            vapid_private_key=clave_privada,
            vapid_claims={"sub": correo_vapid},
        )

        # Registrar la hora del último envío exitoso
        cliente = obtener_cliente()
        cliente.table(TABLA_SUSCRIPCIONES_PUSH).update(
            {"actualizado_en": datetime.now(timezone.utc).isoformat()}
        ).eq("endpoint", suscripcion["endpoint"]).execute()

        return {"enviada": True, "motivo": "enviada"}

    except WebPushException as error_push:
        # Suscripción expirada o revocada por el navegador
        status_code = error_push.response.status_code if error_push.response is not None else None
        detalle_push = ""
        try:
            detalle_push = error_push.response.text if error_push.response is not None else ""
        except Exception:
            detalle_push = ""

        if status_code in (404, 410):
            logger.warning(
                f"Suscripción expirada, desactivando endpoint: {suscripcion['endpoint'][:50]}…"
            )
            try:
                cliente = obtener_cliente()
                cliente.table(TABLA_SUSCRIPCIONES_PUSH).update(
                    {
                        "activa": False,
                        "actualizado_en": datetime.now(timezone.utc).isoformat(),
                    }
                ).eq("endpoint", suscripcion["endpoint"]).execute()
            except Exception as error_db:
                logger.error(f"Error al desactivar suscripción expirada: {error_db}")
            return {
                "enviada": False,
                "motivo": "suscripcion_expirada_o_invalida",
                "status_push": status_code,
                "detalle": detalle_push[:300],
            }
        else:
            logger.error(f"Error al enviar notificación push: {error_push}")
            return {
                "enviada": False,
                "motivo": "error_pywebpush",
                "status_push": status_code,
                "detalle": str(error_push)[:300],
            }

    except Exception as error_general:
        logger.error(f"Error inesperado en notificación push: {error_general}")
        return {
            "enviada": False,
            "motivo": "error_backend_push",
            "detalle": str(error_general)[:300],
        }


def obtener_clave_publica_vapid() -> str:
    """Expone la clave pública VAPID para que el frontend pueda suscribirse."""
    return configuracion.vapid_clave_publica


def notificar_alerta_push(
    tipo_alerta:  str,
    severidad:    str,
    mensaje:      str,
    sala_id:      str,
    nombre_sala:  str,
    detalle:      dict = None,
) -> dict:
    """
    Envía notificaciones push a todos los usuarios con rol apropiado
    que estén dentro de su horario activo configurado.
    """
    roles_objetivo = ROLES_POR_TIPO_ALERTA.get(tipo_alerta, ["admin"])

    # Obtener suscripciones activas. La tabla push_subscriptions guarda
    # dispositivos; la autorizacion ocurre al suscribirse.
    try:
        cliente = obtener_cliente()
        respuesta = (
            cliente.table(TABLA_SUSCRIPCIONES_PUSH)
            .select("*")
            .eq("activa", True)
            .execute()
        )
        suscripciones = respuesta.data or []
    except Exception as error_query:
        logger.error(f"Error al consultar suscripciones push: {error_query}")
        return {"enviadas": 0, "fuera_de_horario": 0, "fallidas": 0}

    mensaje_atmos = MENSAJES_PUSH_ATMOS.get(tipo_alerta)

    # Título según severidad
    titulos_por_severidad = {
        "high":   "🔴 ATMOS — Alerta Alta",
        "medium": "🟡 ATMOS — Alerta Media",
        "low":    "🟢 ATMOS — Alerta Baja",
    }
    titulo = (
        mensaje_atmos["titulo"]
        if mensaje_atmos
        else titulos_por_severidad.get(severidad, "⚪ ATMOS — Alerta")
    )
    cuerpo_base = mensaje_atmos["cuerpo"] if mensaje_atmos else mensaje
    cuerpo = f"{nombre_sala}: {cuerpo_base}"

    datos_extra = {
        "tipo_alerta": tipo_alerta,
        "severidad":   severidad,
        "sala_id":     sala_id,
        "url":         "/alerts",
        "tag":         f"{tipo_alerta}:{sala_id or nombre_sala}",
        **(detalle or {}),
    }

    conteo_enviadas        = 0
    conteo_fuera_horario   = 0
    conteo_fallidas        = 0

    for suscripcion in suscripciones:
        rol = suscripcion.get("rol")
        if rol and rol not in roles_objetivo:
            continue

        if not esta_en_horario_usuario(suscripcion):
            conteo_fuera_horario += 1
            continue

        exito = enviar_notificacion_push(suscripcion, titulo, cuerpo, datos_extra)
        if exito:
            conteo_enviadas += 1
        else:
            conteo_fallidas += 1

    logger.info(
        f"Push '{tipo_alerta}' — enviadas: {conteo_enviadas}, "
        f"fuera de horario: {conteo_fuera_horario}, fallidas: {conteo_fallidas}"
    )

    return {
        "enviadas":           conteo_enviadas,
        "fuera_de_horario":   conteo_fuera_horario,
        "fallidas":           conteo_fallidas,
        "total_suscriptores": len(suscripciones),
    }
