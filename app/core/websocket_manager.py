import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect


class GestorConexiones:

    def __init__(self):
        # sala_id → lista de websockets activos
        self.conexiones: dict[str, list[WebSocket]] = {}

    async def conectar(self, websocket: WebSocket, sala_id: str) -> None:
        await websocket.accept()
        if sala_id not in self.conexiones:
            self.conexiones[sala_id] = []
        self.conexiones[sala_id].append(websocket)

    def desconectar(self, websocket: WebSocket, sala_id: str) -> None:
        if sala_id in self.conexiones:
            self.conexiones[sala_id] = [
                ws for ws in self.conexiones[sala_id] if ws is not websocket
            ]
            if not self.conexiones[sala_id]:
                del self.conexiones[sala_id]

    async def transmitir_a_sala(self, sala_id: str, datos: dict) -> None:
        clientes = list(self.conexiones.get(sala_id, []))
        desconectados: list[WebSocket] = []

        for ws in clientes:
            try:
                await ws.send_json(datos)
            except (WebSocketDisconnect, Exception):
                desconectados.append(ws)

        for ws in desconectados:
            self.desconectar(ws, sala_id)

    async def transmitir_a_todos(self, datos: dict) -> None:
        salas = list(self.conexiones.keys())
        await asyncio.gather(
            *(self.transmitir_a_sala(sala_id, datos) for sala_id in salas),
            return_exceptions=True,
        )

    def cantidad_conexiones(self, sala_id: Optional[str] = None) -> int:
        if sala_id is not None:
            return len(self.conexiones.get(sala_id, []))
        return sum(len(lista) for lista in self.conexiones.values())

    async def enviar_heartbeat(self) -> None:
        await self.transmitir_a_todos({
            "tipo":        "heartbeat",
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "conexiones":  self.cantidad_conexiones(),
        })


gestor = GestorConexiones()
