import asyncio
import time
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from app.api import rooms, nodes, readings, ml, ac_commands, alerts, websockets, notificaciones, compat, atmos, ajustes
from app.config import configuracion
from app.core.database import obtener_cliente
from app.core.limiter import limitador
from app.core.logger import log
from app.core.websocket_manager import gestor
from app.services.alert_service import ServicioAlertas
from app.services.sincronizador_firebase import sincronizar_firebase_supabase

app = FastAPI(title="ATMOS API")
aplicacion = app

app.state.limiter = limitador
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(rooms.enrutador, prefix="/api/v1")
app.include_router(nodes.enrutador, prefix="/api/v1")
app.include_router(readings.enrutador, prefix="/api/v1")
app.include_router(ml.enrutador, prefix="/api/v1")
app.include_router(ac_commands.enrutador, prefix="/api/v1")
app.include_router(alerts.enrutador, prefix="/api/v1")
app.include_router(websockets.enrutador, prefix="/api/v1")
app.include_router(notificaciones.enrutador, prefix="/api/v1")
app.include_router(compat.enrutador, prefix="/api/v1")
app.include_router(atmos.enrutador, prefix="/api/v1")
app.include_router(ajustes.enrutador, prefix="/api/v1")


@app.on_event("startup")
async def iniciar_heartbeat():
    async def bucle_heartbeat():
        while True:
            await asyncio.sleep(30)
            await gestor.transmitir_a_todos({
                "tipo":      "heartbeat",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    asyncio.create_task(bucle_heartbeat())


@app.on_event("startup")
async def iniciar_sincronizacion_firebase():
    if not configuracion.FIREBASE_SYNC_AUTOSTART:
        return

    async def bucle_sincronizacion():
        while True:
            try:
                resultado = await asyncio.to_thread(
                    sincronizar_firebase_supabase,
                    None,
                    None,
                )
                log.info({
                    "evento": "sincronizacion_firebase_periodica",
                    "resultado": resultado,
                })
                alertas = await asyncio.to_thread(
                    ServicioAlertas().verificar_alertas_registros_atmos
                )
                log.info({
                    "evento": "alertas_atmos_periodicas",
                    "resultado": alertas,
                })
            except Exception as error:
                log.error({
                    "evento": "sincronizacion_firebase_error",
                    "error": str(error),
                })
            await asyncio.sleep(configuracion.FIREBASE_SYNC_INTERVAL_SECONDS)

    asyncio.create_task(bucle_sincronizacion())


@app.middleware("http")
async def registrar_peticion(solicitud: Request, siguiente) -> Response:
    inicio = time.monotonic()
    respuesta: Response = await siguiente(solicitud)
    duracion_ms = round((time.monotonic() - inicio) * 1000, 2)

    datos = {
        "metodo":        solicitud.method,
        "ruta":          solicitud.url.path,
        "codigo_estado": respuesta.status_code,
        "duracion_ms":   duracion_ms,
    }

    if respuesta.status_code < 400:
        log.info(datos)
    elif respuesta.status_code < 500:
        log.warning(datos)
    else:
        log.error(datos)

    return respuesta


@app.get("/health")
async def estado_servicio():
    try:
        obtener_cliente().table("rooms").select("count", count="exact").limit(1).execute()
        return {"estado": "ok", "servicio": "atmos-api", "base_de_datos": "conectada"}
    except Exception:
        return {
            "estado": "degradado",
            "servicio": "atmos-api",
            "base_de_datos": "inalcanzable",
        }


@app.get("/")
async def raiz():
    return {"estado": "ok", "servicio": "ATMOS API"}


@app.head("/")
async def raiz_head():
    return Response(status_code=200)
