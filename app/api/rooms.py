from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.schemas import SalaCrear, SalaActualizar, SalaRespuesta
from app.core.database import obtener_cliente, obtener_firebase
from app.core.logger import log
from app.core.security import requerir_admin

enrutador = APIRouter(prefix="/salas", tags=["salas"])


def _resolver_pabellon_y_aire(sala: dict) -> tuple[str | None, str | None]:
    pabellon = sala.get("pabellon") or sala.get("edificio") or sala.get("pavilion")
    aires = sala.get("aires") or []
    if isinstance(aires, list) and aires:
        aire = aires[0]
    else:
        aire = sala.get("aire") or sala.get("nombre") or sala.get("name")
    return pabellon, aire


def _inicializar_comando_firebase(sala: dict) -> None:
    pabellon, aire = _resolver_pabellon_y_aire(sala)
    if not pabellon or not aire:
        log.warning({
            "evento": "sala_firebase_comando_no_inicializado",
            "motivo": "No se pudo resolver pabellon/aire para inicializar comandos.",
            "sala_id": str(sala.get("id")),
            "pabellon": pabellon,
            "aire": aire,
        })
        return

    try:
        firebase_db = obtener_firebase()
        firebase_db.child("Atmos").child("comandos").child(pabellon).child(aire).update({
            "accion": "mantener",
        })
        log.info({
            "evento": "sala_firebase_comando_inicializado",
            "sala_id": str(sala.get("id")),
            "ruta": f"/Atmos/comandos/{pabellon}/{aire}/accion",
            "accion": "mantener",
        })
    except Exception as error:
        log.warning({
            "evento": "sala_firebase_comando_error",
            "sala_id": str(sala.get("id")),
            "pabellon": pabellon,
            "aire": aire,
            "error": str(error),
        })


@enrutador.post("/", response_model=SalaRespuesta, status_code=201)
async def crear_sala(sala: SalaCrear, _=Depends(requerir_admin)):
    cliente = obtener_cliente()
    try:
        respuesta = cliente.table("rooms").insert(sala.model_dump(exclude_none=True)).execute()
    except Exception:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    sala_creada = respuesta.data[0]
    _inicializar_comando_firebase(sala_creada)
    return sala_creada


@enrutador.get("/", response_model=list[SalaRespuesta])
async def listar_salas(incluir_inactivos: bool = Query(False)):
    cliente = obtener_cliente()
    consulta = cliente.table("rooms").select("*")
    if not incluir_inactivos:
        consulta = consulta.eq("activo", True)
    respuesta = consulta.order("nombre", desc=False).execute()
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
async def actualizar_sala(sala_id: str, cambios: SalaActualizar, _=Depends(requerir_admin)):
    cliente = obtener_cliente()
    datos_a_actualizar = {
        campo: valor
        for campo, valor in cambios.model_dump(exclude_unset=True).items()
        if valor is not None
    }
    if not datos_a_actualizar:
        raise HTTPException(status_code=400, detail="No se proporcionaron campos para actualizar")
    try:
        respuesta = (
            cliente.table("rooms")
            .update(datos_a_actualizar)
            .eq("id", sala_id)
            .execute()
        )
    except Exception as error:
        log.error({
            "evento": "sala_actualizacion_error",
            "sala_id": sala_id,
            "campos": sorted(datos_a_actualizar.keys()),
            "error": str(error),
        })
        raise HTTPException(
            status_code=400,
            detail=f"No se pudo actualizar la sala: {error}",
        )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return respuesta.data[0]


@enrutador.delete("/{sala_id}", response_model=SalaRespuesta)
async def eliminar_sala(sala_id: str, _=Depends(requerir_admin)):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("rooms")
        .update({"activo": False})
        .eq("id", sala_id)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return respuesta.data[0]
