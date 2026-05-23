import asyncio
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect

from app.core.websocket_manager import gestor
from app.core.security import verificar_api_key
from app.core.logger import log

enrutador = APIRouter(prefix="/ws", tags=["websockets"])


@enrutador.websocket("/salas/{sala_id}")
async def ws_sala(websocket: WebSocket, sala_id: str, api_key: str | None = None):
    if not verificar_api_key(api_key):
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


@enrutador.websocket("/alertas")
async def ws_alertas(websocket: WebSocket, api_key: str | None = None):
    sala_id = "alertas"

    if not verificar_api_key(api_key):
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


@enrutador.get("/estado")
async def estado_conexiones():
    return {
        "total_conexiones": gestor.cantidad_conexiones(),
        "salas": {
            sala_id: len(lista)
            for sala_id, lista in gestor.conexiones.items()
        },
    }
