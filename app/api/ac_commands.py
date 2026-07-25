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
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from app.models.schemas import (
    ComandoACCrear,
    ComandoACRespuesta,
    ComandoPendienteRespuesta,
)
from app.core.database import obtener_cliente
from app.core.control_ir import (
    accion_es_no_op,
    construir_comando_ir,
    control_manual_ir_habilitado,
    control_ir_habilitado,
    control_solo_manual,
    evaluar_comando_para_ejecucion,
    motivo_bloqueo_control_manual_ir,
    motivo_bloqueo_control_ir,
    normalizar_accion_ir,
)
from app.core.websocket_manager import gestor
from app.core.limiter import limitador

enrutador = APIRouter(prefix="/comandos-ac", tags=["comandos-ac"])


def _bloquear_si_control_inactivo(*, automatico: bool = False) -> None:
    habilitado = control_ir_habilitado() if automatico else control_manual_ir_habilitado()
    if not habilitado:
        raise HTTPException(
            status_code=503,
            detail={
                "estado": "control_ir_bloqueado",
                "motivo": (
                    motivo_bloqueo_control_ir()
                    if automatico
                    else motivo_bloqueo_control_manual_ir()
                ),
            },
        )


def _accion_desde_comando(comando: ComandoACCrear) -> str:
    if comando.accion:
        return normalizar_accion_ir(comando.accion)
    if comando.tipo_comando == "off":
        return "apagar"
    if comando.tipo_comando == "on":
        return "encender_23" if comando.setpoint == 23 else "encender_22"
    if comando.tipo_comando == "setpoint":
        return {
            22: "encender_22",
            23: "encender_23",
            24: "ahorro_24",
        }.get(comando.setpoint, "")
    return ""


@limitador.limit("30/minute")
@enrutador.post("/", response_model=ComandoACRespuesta, status_code=201)
async def crear_comando(request: Request, comando: ComandoACCrear):
    _bloquear_si_control_inactivo()
    if control_solo_manual() and comando.origen != "manual":
        raise HTTPException(status_code=403, detail="Solo se permiten comandos manuales explícitos.")
    if comando.tipo_comando == "setpoint" and comando.setpoint is None:
        raise HTTPException(
            status_code=422,
            detail="El valor de setpoint es obligatorio cuando tipo_comando es 'setpoint'",
        )

    accion = _accion_desde_comando(comando)
    if accion_es_no_op(accion):
        raise HTTPException(status_code=422, detail="La solicitud no contiene una acción IR explícita.")
    if not comando.pabellon or not comando.aire:
        raise HTTPException(status_code=422, detail="pabellon y aire son obligatorios para una orden IR.")
    sobre = construir_comando_ir(
        pabellon=comando.pabellon,
        aire=comando.aire,
        accion=accion,
    )
    cliente = obtener_cliente()
    datos = comando.model_dump(mode="json")
    datos.update(sobre)
    datos["fue_ejecutado"] = False
    datos["ejecutado_en"] = None

    respuesta = cliente.table("ac_commands").insert(datos).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar el comando")
    return respuesta.data[0]


@limitador.limit("120/minute")
@enrutador.get("/pendientes/{nodo_id}", response_model=list[ComandoPendienteRespuesta])
async def obtener_comandos_pendientes(request: Request, nodo_id: UUID):
    # Solo retorna — NO marca como ejecutados. El ESP32 confirma por separado.
    if not control_manual_ir_habilitado():
        return []
    cliente = obtener_cliente()
    ahora = datetime.now(timezone.utc)
    pabellon = request.query_params.get("pabellon", "").strip()
    aire = request.query_params.get("aire", "").strip()
    ultimo_command_id = request.query_params.get("ultimo_command_id")
    if not pabellon or not aire:
        return []

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
        .select("id,command_id,created_at,expires_at,estado,pabellon,aire,accion,tipo_comando,setpoint,modo,origen,enviado_en")
        .eq("nodo_id", str(nodo_id))
        .eq("fue_ejecutado", False)
        .eq("pabellon", pabellon)
        .eq("aire", aire)
        .eq("estado", "pendiente")
        .gt("expires_at", ahora.isoformat())
        .order("enviado_en", desc=False)
        .execute()
    )

    # Comandos broadcast de la sala (nodo_id IS NULL)
    resp_sala = (
        cliente.table("ac_commands")
        .select("id,command_id,created_at,expires_at,estado,pabellon,aire,accion,tipo_comando,setpoint,modo,origen,enviado_en")
        .eq("sala_id", sala_id)
        .is_("nodo_id", "null")
        .eq("fue_ejecutado", False)
        .eq("pabellon", pabellon)
        .eq("aire", aire)
        .eq("estado", "pendiente")
        .gt("expires_at", ahora.isoformat())
        .order("enviado_en", desc=False)
        .execute()
    )

    ids_vistos: set[int] = set()
    comandos: list[dict] = []
    for cmd in (resp_nodo.data or []) + (resp_sala.data or []):
        if control_solo_manual() and cmd.get("origen") != "manual":
            continue
        evaluacion = evaluar_comando_para_ejecucion(
            cmd,
            pabellon=pabellon,
            aire=aire,
            ultimo_command_id=ultimo_command_id,
            ahora=ahora,
        )
        if evaluacion["ejecutable"] and cmd["id"] not in ids_vistos:
            ids_vistos.add(cmd["id"])
            comandos.append(cmd)

    comandos.sort(key=lambda c: c["enviado_en"])
    return comandos


@enrutador.patch("/{comando_id}/confirmar", response_model=ComandoACRespuesta)
async def confirmar_comando(comando_id: int):
    _bloquear_si_control_inactivo()
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
    if control_solo_manual() and existente.data.get("origen") != "manual":
        raise HTTPException(status_code=403, detail="El modo manual no confirma comandos automáticos.")

    ahora_dt = datetime.now(timezone.utc)
    evaluacion = evaluar_comando_para_ejecucion(
        existente.data,
        pabellon=existente.data.get("pabellon"),
        aire=existente.data.get("aire"),
        ahora=ahora_dt,
    )
    if not evaluacion["ejecutable"]:
        raise HTTPException(status_code=409, detail=evaluacion)
    ahora = ahora_dt.isoformat()
    respuesta = (
        cliente.table("ac_commands")
        .update({"fue_ejecutado": True, "ejecutado_en": ahora, "estado": "confirmado"})
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
    _bloquear_si_control_inactivo(automatico=True)
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

    instantanea = prediccion.get("instantanea_caracteristicas") or {}
    accion = normalizar_accion_ir(
        instantanea.get("accion_solicitada") or instantanea.get("accion_final")
    )
    if accion_es_no_op(accion):
        raise HTTPException(
            status_code=409,
            detail="La recomendación es no-op; mantener nunca crea un comando IR.",
        )
    pabellon = str(instantanea.get("pabellon") or "").strip()
    aire = str(instantanea.get("aire") or "").strip()
    if not pabellon or not aire:
        raise HTTPException(
            status_code=422,
            detail="La predicción no contiene un destino pabellon/aire inequívoco.",
        )
    sobre = construir_comando_ir(pabellon=pabellon, aire=aire, accion=accion)
    setpoints = {
        "encender_22": 22,
        "enfriar_fuerte": 22,
        "encender_23": 23,
        "ahorro_24": 24,
    }
    nuevo_comando = {
        "sala_id":       prediccion["sala_id"],
        "nodo_id":       None,
        "tipo_comando":  "off" if accion == "apagar" else "setpoint",
        "setpoint":      setpoints.get(accion),
        "modo":          None,
        "origen":        "ml_model",
        "fue_ejecutado": False,
        "ejecutado_en":  None,
        **sobre,
    }

    cmd_resp = cliente.table("ac_commands").insert(nuevo_comando).execute()
    if not cmd_resp.data:
        raise HTTPException(status_code=400, detail="Error al crear el comando")

    return cmd_resp.data[0]
