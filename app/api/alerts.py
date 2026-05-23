from fastapi import APIRouter, HTTPException, Query, Header, Request
from datetime import datetime, timezone
from typing import Annotated, Literal, Optional
from uuid import UUID

from app.models.schemas import AlertaRespuesta
from app.core.database import obtener_cliente
from app.services.alert_service import ServicioAlertas
from app.config import configuracion
from app.main import limitador

enrutador = APIRouter(prefix="/alertas", tags=["alertas"])


@enrutador.get("/", response_model=list[AlertaRespuesta])
@limitador.limit("30/minute")
async def listar_alertas(
    solicitud: Request,
    sala_id: Optional[UUID] = None,
    esta_resuelta: Optional[bool] = False,
    severidad: Optional[Literal["low", "medium", "high"]] = None,
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

    respuesta = consulta.execute()
    return respuesta.data


@enrutador.patch("/{alerta_id}/resolver", response_model=AlertaRespuesta)
async def resolver_alerta(alerta_id: int):
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


@enrutador.get("/resumen")
@limitador.limit("60/minute")
async def resumen_alertas(solicitud: Request):
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
    por_tipo = {"node_offline": 0, "power_anomaly": 0, "temperature_stuck": 0}

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
