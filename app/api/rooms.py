from fastapi import APIRouter, HTTPException
from app.models.schemas import SalaCrear, SalaActualizar, SalaRespuesta
from app.core.database import obtener_cliente

enrutador = APIRouter(prefix="/salas", tags=["salas"])


@enrutador.post("/", response_model=SalaRespuesta, status_code=201)
async def crear_sala(sala: SalaCrear):
    cliente = obtener_cliente()
    try:
        respuesta = cliente.table("rooms").insert(sala.model_dump()).execute()
    except Exception:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    return respuesta.data[0]


@enrutador.get("/", response_model=list[SalaRespuesta])
async def listar_salas():
    cliente = obtener_cliente()
    respuesta = cliente.table("rooms").select("*").order("nombre", desc=False).execute()
    return respuesta.data


@enrutador.get("/{sala_id}", response_model=SalaRespuesta)
async def obtener_sala(sala_id: str):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("rooms").select("*").eq("id", sala_id).single().execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return respuesta.data


@enrutador.patch("/{sala_id}", response_model=SalaRespuesta)
async def actualizar_sala(sala_id: str, cambios: SalaActualizar):
    cliente = obtener_cliente()
    datos_a_actualizar = {
        campo: valor
        for campo, valor in cambios.model_dump().items()
        if valor is not None
    }
    if not datos_a_actualizar:
        raise HTTPException(status_code=400, detail="No se proporcionaron campos para actualizar")
    respuesta = (
        cliente.table("rooms")
        .update(datos_a_actualizar)
        .eq("id", sala_id)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return respuesta.data[0]
