import asyncio
import time
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded, _rate_limit_exceeded_handler

from app.api import rooms, nodes, readings, ml, ac_commands, alerts, websockets, telegram
from app.core.database import obtener_cliente
from app.core.logger import log
from app.core.websocket_manager import gestor

limitador = Limiter(key_func=get_remote_address)

aplicacion = FastAPI(title="ATMOS API")

aplicacion.state.limiter = limitador
aplicacion.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

aplicacion.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

aplicacion.include_router(rooms.enrutador, prefix="/api/v1")
aplicacion.include_router(nodes.enrutador, prefix="/api/v1")
aplicacion.include_router(readings.enrutador, prefix="/api/v1")
aplicacion.include_router(ml.enrutador, prefix="/api/v1")
aplicacion.include_router(ac_commands.enrutador, prefix="/api/v1")
aplicacion.include_router(alerts.enrutador, prefix="/api/v1")
aplicacion.include_router(websockets.enrutador, prefix="/api/v1")
aplicacion.include_router(telegram.enrutador, prefix="/api/v1")


@aplicacion.on_event("startup")
async def iniciar_heartbeat():
    async def bucle_heartbeat():
        while True:
            await asyncio.sleep(30)
            await gestor.transmitir_a_todos({
                "tipo":      "heartbeat",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    asyncio.create_task(bucle_heartbeat())


@aplicacion.middleware("http")
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


@aplicacion.get("/health")
async def estado_servicio():
    try:
        obtener_cliente().table("rooms").select("count", count="exact").limit(1).execute()
        return {"estado": "ok", "servicio": "atmos-api", "base_de_datos": "conectada"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"estado": "degradado", "servicio": "atmos-api", "base_de_datos": "inalcanzable"},
        )
