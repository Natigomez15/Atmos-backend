from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from uuid import UUID
from typing import Optional
from app.models.schemas import NodoCrear, NodoRespuesta
from app.core.database import obtener_cliente

enrutador = APIRouter(prefix="/nodos", tags=["nodos"])


@enrutador.post("/", response_model=NodoRespuesta, status_code=201)
async def crear_nodo(nodo: NodoCrear):
    cliente = obtener_cliente()

    # Verificar si la dirección MAC ya existe
    existente = (
        cliente.table("nodes")
        .select("id")
        .eq("direccion_mac", nodo.direccion_mac)
        .execute()
    )
    if existente.data:
        raise HTTPException(
            status_code=409,
            detail=f"Ya existe un nodo con la MAC {nodo.direccion_mac}",
        )

    try:
        respuesta = cliente.table("nodes").insert(nodo.model_dump(mode="json")).execute()
    except Exception:
        raise HTTPException(status_code=400, detail="Error al registrar el nodo")
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al registrar el nodo")
    return respuesta.data[0]


@enrutador.get("/", response_model=list[NodoRespuesta])
async def listar_nodos(sala_id: Optional[UUID] = None):
    cliente = obtener_cliente()
    consulta = cliente.table("nodes").select("*")
    if sala_id:
        consulta = consulta.eq("sala_id", str(sala_id))
    respuesta = consulta.execute()
    return respuesta.data


@enrutador.patch("/{nodo_id}/heartbeat")
async def registrar_heartbeat(nodo_id: str):
    cliente = obtener_cliente()
    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = (
        cliente.table("nodes")
        .update({"ultima_vez_visto": ahora})
        .eq("id", nodo_id)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")
    return {"nodo_id": nodo_id, "ultima_vez_visto": ahora}


@enrutador.patch("/{nodo_id}/desactivar", response_model=NodoRespuesta)
async def desactivar_nodo(nodo_id: str):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("nodes")
        .update({"esta_activo": False})
        .eq("id", nodo_id)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")
    return respuesta.data[0]
