from fastapi import APIRouter, HTTPException, Query, Header, Request
from datetime import datetime, timezone
from uuid import UUID
from typing import Annotated, Optional
from pydantic import Field

from app.models.schemas import (
    RegistroCrear,
    RegistroRespuesta,
    LecturaSensorCrear,
    LecturaSensorRespuesta,
)
from app.core.database import obtener_cliente
from app.core.logger import log
from app.core.websocket_manager import gestor
from app.services.aggregation import agregar_lecturas, ServicioAgregacion
from app.services.sincronizador_firebase import sincronizar_firebase_supabase
from app.config import configuracion
from app.core.limiter import limitador

enrutador = APIRouter(prefix="/lecturas", tags=["lecturas"])


# ---------------------------------------------------------------------------
# Lecturas de sensores ESP32
# ---------------------------------------------------------------------------

@limitador.limit("60/minute")
@enrutador.post("/", response_model=LecturaSensorRespuesta, status_code=201)
async def crear_lectura(request: Request, lectura: LecturaSensorCrear):
    cliente = obtener_cliente()
    try:
        nodo_existente = (
            cliente.table("nodes").select("id").eq("id", str(lectura.nodo_id)).execute()
        )
        if not nodo_existente.data:
            raise HTTPException(status_code=404, detail="Nodo no encontrado")

        ahora = datetime.now(timezone.utc).isoformat()
        cliente.table("nodes").update({"ultima_vez_visto": ahora}).eq(
            "id", str(lectura.nodo_id)
        ).execute()

        respuesta = (
            cliente.table("sensor_readings")
            .insert(lectura.model_dump(mode="json"))
            .execute()
        )
        if not respuesta.data:
            raise HTTPException(status_code=400, detail="Error al insertar la lectura")

        fila = respuesta.data[0]
        log.info({
            "evento":     "lectura_guardada",
            "nodo_id":    str(lectura.nodo_id),
            "sala_id":    str(lectura.sala_id),
            "potencia_w": lectura.potencia_w,
        })

        await gestor.transmitir_a_sala(
            sala_id=str(lectura.sala_id),
            datos={
                "tipo":          "nueva_lectura",
                "sala_id":       str(lectura.sala_id),
                "registrado_en": fila.get("registrado_en"),
                "temperatura":   lectura.temperatura,
                "humedad":       lectura.humedad,
                "presencia":     lectura.presencia,
                "potencia_w":    lectura.potencia_w,
                "ac_encendido":  lectura.ac_encendido,
                "setpoint_ac":   lectura.setpoint_ac,
            },
        )
        return fila

    except HTTPException:
        raise
    except Exception as error:
        log.error({
            "evento":  "lectura_fallida",
            "error":   str(error),
            "payload": lectura.model_dump(mode="json"),
        })
        raise HTTPException(status_code=500, detail="Error interno al guardar la lectura")


@limitador.limit("10/minute")
@enrutador.post("/lote")
async def crear_lecturas_lote(request: Request, lecturas: list[LecturaSensorCrear]):
    if len(lecturas) > 50:
        raise HTTPException(
            status_code=422, detail="El lote no puede superar 50 lecturas"
        )

    cliente = obtener_cliente()
    datos = [l.model_dump(mode="json") for l in lecturas]

    insertados = 0
    errores = 0
    try:
        respuesta = cliente.table("sensor_readings").upsert(datos).execute()
        insertados = len(respuesta.data) if respuesta.data else 0
        errores = len(lecturas) - insertados
    except Exception:
        errores = len(lecturas)

    return {"insertados": insertados, "errores": errores}


@limitador.limit("30/minute")
@enrutador.get("/", response_model=list[LecturaSensorRespuesta])
async def listar_lecturas(
    request: Request,
    sala_id: UUID,
    inicio: datetime,
    fin: datetime,
    limite: Annotated[int, Query(ge=1, le=2000)] = 500,
):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("sensor_readings")
        .select("*")
        .eq("sala_id", str(sala_id))
        .gte("registrado_en", inicio.isoformat())
        .lte("registrado_en", fin.isoformat())
        .order("registrado_en", desc=False)
        .limit(limite)
        .execute()
    )
    return respuesta.data


@enrutador.get("/reciente/{sala_id}", response_model=LecturaSensorRespuesta)
async def ultima_lectura_sala(sala_id: UUID):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("sensor_readings")
        .select("*")
        .eq("sala_id", str(sala_id))
        .order("registrado_en", desc=True)
        .limit(1)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(
            status_code=404, detail="No se encontraron lecturas para esta sala"
        )
    return respuesta.data[0]


# ---------------------------------------------------------------------------
# Agregación horaria (disparada por cron-job.org)
# ---------------------------------------------------------------------------

@enrutador.post("/disparar-agregacion")
async def disparar_agregacion(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
):
    if not x_cron_secret or x_cron_secret != configuracion.CRON_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    resultado = ServicioAgregacion().agregar_todas_las_salas()
    return resultado


# ---------------------------------------------------------------------------
# Registros Firebase (sincronización legacy)
# ---------------------------------------------------------------------------

@enrutador.get("/registros", response_model=list[RegistroRespuesta])
async def listar_registros(sensor: str | None = None, limite: int = 100):
    cliente = obtener_cliente()
    consulta = (
        cliente.table("registros").select("*").limit(limite).order("fecha", desc=True)
    )
    if sensor:
        consulta = consulta.eq("sensor", sensor)
    respuesta = consulta.execute()
    return respuesta.data


@enrutador.get("/registros/agregado")
async def obtener_agregado(sensor: str | None = None):
    cliente = obtener_cliente()
    consulta = cliente.table("registros").select("*")
    if sensor:
        consulta = consulta.eq("sensor", sensor)
    respuesta = consulta.execute()
    return agregar_lecturas(respuesta.data)


@enrutador.post("/registros", response_model=RegistroRespuesta, status_code=201)
async def crear_registro(registro: RegistroCrear):
    cliente = obtener_cliente()
    respuesta = cliente.table("registros").insert(registro.model_dump()).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar el registro")
    return respuesta.data[0]


@limitador.limit("30/minute")
@enrutador.post("/firebase/sincronizar")
async def sincronizar_registros_firebase(
    request: Request,
    x_atmos_token: Annotated[Optional[str], Header()] = None,
):
    if not configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ATMOS_DEVICE_TOKEN no está configurado en el servidor",
        )

    if x_atmos_token != configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(status_code=401, detail="Token ATMOS inválido")

    return sincronizar_firebase_supabase()
