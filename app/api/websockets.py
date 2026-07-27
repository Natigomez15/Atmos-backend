import asyncio
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.core.websocket_manager import gestor
from app.core.security import obtener_usuario_actual, verificar_api_key
from app.core.logger import log

enrutador = APIRouter(prefix="/ws", tags=["websockets"])


@enrutador.websocket("/salas/{sala_id}")
async def ws_sala(
    websocket: WebSocket,
    sala_id: str,
    api_key: str | None = None,
    access_token: str | None = None,
):
    autorizado = verificar_api_key(api_key)
    if not autorizado and access_token:
        try:
            await asyncio.to_thread(
                obtener_usuario_actual,
                f"Bearer {access_token}",
            )
            autorizado = True
        except Exception:
            autorizado = False

    if not autorizado:
        await websocket.close(code=1008)
        return

    await gestor.conectar(websocket, sala_id)
    log.info({"evento": "ws_conectado", "sala_id": sala_id})

    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"tipo": "ping"})
    except WebSocketDisconnect:
        gestor.desconectar(websocket, sala_id)
        log.info({"evento": "ws_desconectado", "sala_id": sala_id})


@enrutador.websocket("/rooms/{sala_id}")
async def ws_room_alias(
    websocket: WebSocket,
    sala_id: str,
    api_key: str | None = None,
    access_token: str | None = None,
):
    await ws_sala(websocket, sala_id, api_key, access_token)


@enrutador.websocket("/alertas")
async def ws_alertas(websocket: WebSocket, api_key: str | None = None):
    sala_id = "alertas"

    await gestor.conectar(websocket, sala_id)
    log.info({"evento": "ws_conectado", "sala_id": sala_id})

    try:
        while True:
            await asyncio.sleep(30)
            await websocket.send_json({"tipo": "ping"})
    except WebSocketDisconnect:
        gestor.desconectar(websocket, sala_id)
        log.info({"evento": "ws_desconectado", "sala_id": sala_id})


@enrutador.websocket("/alerts")
async def ws_alerts_alias(websocket: WebSocket, api_key: str | None = None):
    await ws_alertas(websocket, api_key)


@enrutador.get("/estado")
async def estado_conexiones():
    return {
        "total_conexiones": gestor.cantidad_conexiones(),
        "salas": {
            sala_id: len(lista)
            for sala_id, lista in gestor.conexiones.items()
        },
    }
