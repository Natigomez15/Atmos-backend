from unittest.mock import AsyncMock

import pytest
from fastapi.websockets import WebSocketDisconnect

from app.core.websocket_manager import GestorConexiones
from app.api import websockets


@pytest.mark.asyncio
async def test_gestor_conecta_transmite_limpia_y_broadcast():
    gestor = GestorConexiones()
    bueno, roto = AsyncMock(), AsyncMock()
    roto.send_json.side_effect = WebSocketDisconnect()
    await gestor.conectar(bueno, "s1")
    await gestor.conectar(roto, "s1")
    await gestor.transmitir_a_sala("s1", {"tipo": "dato"})
    bueno.send_json.assert_awaited_once()
    assert gestor.cantidad_conexiones("s1") == 1
    await gestor.transmitir_a_todos({"tipo": "global"})
    assert gestor.cantidad_conexiones() == 1
    gestor.desconectar(bueno, "s1")
    assert gestor.cantidad_conexiones() == 0


@pytest.mark.asyncio
async def test_rutas_websocket_rechazan_y_limpian(monkeypatch):
    websocket = AsyncMock()
    monkeypatch.setattr(websockets, "verificar_api_key", lambda key: False)
    await websockets.ws_sala(websocket, "s1", "mala")
    websocket.close.assert_awaited_once_with(code=1008)

    websocket = AsyncMock()
    monkeypatch.setattr(websockets, "verificar_api_key", lambda key: True)
    monkeypatch.setattr(websockets.asyncio, "sleep", AsyncMock(side_effect=WebSocketDisconnect()))
    gestor = GestorConexiones()
    monkeypatch.setattr(websockets, "gestor", gestor)
    await websockets.ws_sala(websocket, "s1", "valida")
    assert gestor.cantidad_conexiones() == 0
    assert (await websockets.estado_conexiones())["total_conexiones"] == 0
