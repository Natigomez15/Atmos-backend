# Flujo ESP32 (referencia para firmware, no implementar en FastAPI):
#
# Después de llamar POST /api/v1/lecturas, el ESP32 debe inmediatamente llamar:
#   GET /api/v1/comandos-ac/pendientes/{node_id}
#
# Si la lista de respuesta no está vacía:
#   Para cada comando en la lista:
#     1. Ejecutar la señal IR usando IRremoteESP8266
#     2. Llamar PATCH /api/v1/comandos-ac/{comando.id}/confirmar
#     3. Esperar 500ms entre comandos si hay múltiples pendientes
#
# De esta forma el ESP32 nunca necesita una conexión persistente —
# consulta comandos en su propio ciclo de lectura.

from fastapi import APIRouter, HTTPException, Query, Request
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from app.models.schemas import (
    ComandoACCrear,
    ComandoACRespuesta,
    ComandoPendienteRespuesta,
)
from app.core.database import obtener_cliente
from app.core.websocket_manager import gestor
from app.main import limitador

enrutador = APIRouter(prefix="/comandos-ac", tags=["comandos-ac"])


@enrutador.post("/", response_model=ComandoACRespuesta, status_code=201)
@limitador.limit("30/minute")
async def crear_comando(solicitud: Request, comando: ComandoACCrear):
    if comando.tipo_comando == "setpoint" and comando.setpoint is None:
        raise HTTPException(
            status_code=422,
            detail="El valor de setpoint es obligatorio cuando tipo_comando es 'setpoint'",
        )

    cliente = obtener_cliente()
    datos = comando.model_dump(mode="json")
    datos["fue_ejecutado"] = False
    datos["ejecutado_en"] = None

    respuesta = cliente.table("ac_commands").insert(datos).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar el comando")
    return respuesta.data[0]


@enrutador.get("/pendientes/{nodo_id}", response_model=list[ComandoPendienteRespuesta])
@limitador.limit("120/minute")
async def obtener_comandos_pendientes(solicitud: Request, nodo_id: UUID):
    # Solo retorna — NO marca como ejecutados. El ESP32 confirma por separado.
    cliente = obtener_cliente()
    hace_30_min = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    # Buscar la sala del nodo para incluir comandos broadcast (nodo_id IS NULL)
    nodo_resp = (
        cliente.table("nodes")
        .select("sala_id")
        .eq("id", str(nodo_id))
        .single()
        .execute()
    )
    if not nodo_resp.data:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")

    sala_id = nodo_resp.data["sala_id"]

    # Comandos dirigidos al nodo específico
    resp_nodo = (
        cliente.table("ac_commands")
        .select("id, tipo_comando, setpoint, modo, enviado_en")
        .eq("nodo_id", str(nodo_id))
        .eq("fue_ejecutado", False)
        .gte("enviado_en", hace_30_min)
        .order("enviado_en", desc=False)
        .execute()
    )

    # Comandos broadcast de la sala (nodo_id IS NULL)
    resp_sala = (
        cliente.table("ac_commands")
        .select("id, tipo_comando, setpoint, modo, enviado_en")
        .eq("sala_id", sala_id)
        .is_("nodo_id", "null")
        .eq("fue_ejecutado", False)
        .gte("enviado_en", hace_30_min)
        .order("enviado_en", desc=False)
        .execute()
    )

    ids_vistos: set[int] = set()
    comandos: list[dict] = []
    for cmd in (resp_nodo.data or []) + (resp_sala.data or []):
        if cmd["id"] not in ids_vistos:
            ids_vistos.add(cmd["id"])
            comandos.append(cmd)

    comandos.sort(key=lambda c: c["enviado_en"])
    return comandos


@enrutador.patch("/{comando_id}/confirmar", response_model=ComandoACRespuesta)
async def confirmar_comando(comando_id: int):
    cliente = obtener_cliente()

    existente = (
        cliente.table("ac_commands")
        .select("*")
        .eq("id", comando_id)
        .single()
        .execute()
    )
    if not existente.data:
        raise HTTPException(status_code=404, detail="Comando no encontrado")
    if existente.data.get("fue_ejecutado"):
        raise HTTPException(
            status_code=409,
            detail="El comando ya fue confirmado como ejecutado",
        )

    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = (
        cliente.table("ac_commands")
        .update({"fue_ejecutado": True, "ejecutado_en": ahora})
        .eq("id", comando_id)
        .execute()
    )
    comando = respuesta.data[0]

    await gestor.transmitir_a_sala(
        sala_id=str(comando["sala_id"]),
        datos={
            "tipo":          "comando_ejecutado",
            "sala_id":       str(comando["sala_id"]),
            "comando_id":    comando["id"],
            "tipo_comando":  comando["tipo_comando"],
            "setpoint":      comando.get("setpoint"),
            "ejecutado_en":  ahora,
        },
    )
    return comando


@enrutador.get("/", response_model=list[ComandoACRespuesta])
async def listar_comandos(
    sala_id: UUID,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
    solo_pendientes: bool = False,
):
    cliente = obtener_cliente()
    consulta = (
        cliente.table("ac_commands")
        .select("*")
        .eq("sala_id", str(sala_id))
        .order("enviado_en", desc=True)
        .limit(limite)
    )
    if solo_pendientes:
        consulta = consulta.eq("fue_ejecutado", False)

    respuesta = consulta.execute()
    return respuesta.data


@enrutador.post("/desde-prediccion/{prediccion_id}", response_model=ComandoACRespuesta, status_code=201)
async def comando_desde_prediccion(prediccion_id: int):
    cliente = obtener_cliente()

    pred_resp = (
        cliente.table("ml_predictions")
        .select("*")
        .eq("id", prediccion_id)
        .single()
        .execute()
    )
    if not pred_resp.data:
        raise HTTPException(status_code=404, detail="Predicción no encontrada")

    prediccion = pred_resp.data
    if prediccion.get("fue_aplicado"):
        raise HTTPException(
            status_code=409,
            detail="Esta predicción ya fue aplicada como comando",
        )

    nuevo_comando = {
        "sala_id":       prediccion["sala_id"],
        "nodo_id":       None,
        "tipo_comando":  "setpoint",
        "setpoint":      prediccion["setpoint_recomendado"],
        "modo":          None,
        "origen":        "ml_model",
        "fue_ejecutado": False,
        "ejecutado_en":  None,
    }

    cmd_resp = cliente.table("ac_commands").insert(nuevo_comando).execute()
    if not cmd_resp.data:
        raise HTTPException(status_code=400, detail="Error al crear el comando")

    cliente.table("ml_predictions").update({"fue_aplicado": True}).eq(
        "id", prediccion_id
    ).execute()

    return cmd_resp.data[0]
