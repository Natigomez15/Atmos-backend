import asyncio

import pytest
from fastapi.websockets import WebSocketDisconnect

from app.api import websockets
from app.core.websocket_manager import GestorConexiones


class WebSocketFalso:
    def __init__(self, desconectar_al_enviar=False):
        self.aceptado = False
        self.desconectar_al_enviar = desconectar_al_enviar
        self.mensajes = []

    async def accept(self):
        self.aceptado = True

    async def send_json(self, datos):
        if self.desconectar_al_enviar:
            raise WebSocketDisconnect()
        self.mensajes.append(datos)


@pytest.mark.asyncio
async def test_alertas_websocket_y_alias_limpian_desconexiones(monkeypatch):
    async def sin_espera(_):
        return None

    monkeypatch.setattr(websockets.asyncio, "sleep", sin_espera)
    cliente = WebSocketFalso(desconectar_al_enviar=True)
    await websockets.ws_alertas(cliente)

    assert cliente.aceptado is True
    assert "alertas" not in websockets.gestor.conexiones

    cliente_alias = WebSocketFalso(desconectar_al_enviar=True)
    await websockets.ws_alerts_alias(cliente_alias)
    assert cliente_alias.aceptado is True

    monkeypatch.setattr(websockets, "verificar_api_key", lambda _: True)
    cliente_sala = WebSocketFalso(desconectar_al_enviar=True)
    await websockets.ws_room_alias(cliente_sala, "sala-1", "clave")
    assert cliente_sala.aceptado is True


@pytest.mark.asyncio
async def test_heartbeat_del_gestor_envia_estado_a_clientes():
    gestor = GestorConexiones()
    cliente = WebSocketFalso()
    await gestor.conectar(cliente, "sala-1")

    await gestor.enviar_heartbeat()

    assert cliente.mensajes[0]["tipo"] == "heartbeat"
    assert cliente.mensajes[0]["conexiones"] == 1
