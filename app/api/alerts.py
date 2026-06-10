from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID

from app.models.schemas import AlertaRespuesta
from app.core.database import obtener_cliente
from app.services.alert_service import ServicioAlertas
from app.config import configuracion
from app.core.limiter import limitador
from app.core.security import requerir_mantenimiento_o_admin
from app.services.sincronizador_firebase import leer_ultima_lectura_valida_firebase_rest

enrutador = APIRouter(prefix="/alertas", tags=["alertas"])


@limitador.limit("30/minute")
@enrutador.get("/", response_model=list[AlertaRespuesta])
async def listar_alertas(
    request: Request,
    sala_id: Optional[UUID] = None,
    esta_resuelta: Optional[bool] = False,
    severidad: Optional[Literal["low", "medium", "high"]] = None,
    tipo_alerta: Optional[str] = None,
    limite: Annotated[int, Query(ge=1, le=200)] = 50,
):
    cliente = obtener_cliente()
    consulta = (
        cliente.table("alerts")
        .select("*")
        .order("creado_en", desc=True)
        .limit(limite)
    )
    if sala_id is not None:
        consulta = consulta.eq("sala_id", str(sala_id))
    if esta_resuelta is not None:
        consulta = consulta.eq("esta_resuelta", esta_resuelta)
    if severidad is not None:
        consulta = consulta.eq("severidad", severidad)
    if tipo_alerta is not None:
        consulta = consulta.eq("tipo_alerta", tipo_alerta)

    respuesta = consulta.execute()
    return respuesta.data


@enrutador.patch("/{alerta_id}/resolver", response_model=AlertaRespuesta)
async def resolver_alerta(alerta_id: int, _=Depends(requerir_mantenimiento_o_admin)):
    cliente = obtener_cliente()

    existente = (
        cliente.table("alerts").select("*").eq("id", alerta_id).single().execute()
    )
    if not existente.data:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    if existente.data.get("esta_resuelta"):
        raise HTTPException(status_code=409, detail="La alerta ya está resuelta")

    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = (
        cliente.table("alerts")
        .update({"esta_resuelta": True, "resuelto_en": ahora})
        .eq("id", alerta_id)
        .execute()
    )
    return respuesta.data[0]


@limitador.limit("60/minute")
@enrutador.get("/resumen")
async def resumen_alertas(request: Request):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("alerts")
        .select("severidad, tipo_alerta")
        .eq("esta_resuelta", False)
        .execute()
    )
    filas = respuesta.data

    total = len(filas)
    por_severidad = {"high": 0, "medium": 0, "low": 0}
    por_tipo = {
        "node_offline": 0,
        "power_anomaly": 0,
        "temperature_stuck": 0,
        "sensor_datos_invalidos": 0,
        "temperatura_alta": 0,
        "temperatura_fuera_rango": 0,
        "humedad_alta": 0,
        "humedad_invalida": 0,
    }

    for fila in filas:
        sev = fila.get("severidad")
        if sev in por_severidad:
            por_severidad[sev] += 1
        tipo = fila.get("tipo_alerta")
        if tipo in por_tipo:
            por_tipo[tipo] += 1

    return {
        "total_sin_resolver": total,
        "por_severidad":      por_severidad,
        "por_tipo":           por_tipo,
    }


@enrutador.post("/ejecutar-verificaciones")
async def ejecutar_verificaciones(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
):
    if not x_cron_secret or x_cron_secret != configuracion.CRON_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    resultado = ServicioAlertas().ejecutar_todas_las_verificaciones()
    return resultado


@enrutador.post("/ejecutar-verificaciones-atmos")
async def ejecutar_verificaciones_atmos(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    if not x_cron_secret or x_cron_secret != configuracion.CRON_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")
    seleccion = leer_ultima_lectura_valida_firebase_rest(
        pabellon=pabellon,
        aire=aire,
        limite=50,
    )
    resultado = ServicioAlertas().verificar_alertas_atmos(
        pabellon=pabellon,
        aire=aire,
        diagnostico=seleccion.get("diagnostico"),
    )
    return resultado
