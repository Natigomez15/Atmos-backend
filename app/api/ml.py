from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from typing import Annotated, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.ml.predictor import ServicioPredictor
from app.core.database import obtener_cliente
from app.config import configuracion
from app.core.limiter import limitador
from app.core.security import requerir_admin

enrutador = APIRouter(prefix="/ml", tags=["ml"])


# ---------------------------------------------------------------------------
# Schemas de entrada (solo para este router)
# ---------------------------------------------------------------------------

class EntradaPrediccion(BaseModel):
    sala_id: UUID
    setpoint_recomendado: Annotated[int, Field(ge=16, le=30)]
    ahorro_predicho_pct: Annotated[float, Field(ge=0.0, le=100.0)]
    puntaje_confianza: Annotated[float, Field(ge=0.0, le=1.0)]
    version_modelo: str
    instantanea_caracteristicas: dict


class EntradaAtmos(BaseModel):
    sala_id: Optional[UUID] = None
    nodo_id: Optional[UUID] = None
    presencia: Annotated[int, Field(ge=0, le=1)]
    temp_ambiente: Annotated[float, Field(ge=10, le=45)]
    temp_ac: Annotated[float, Field(ge=5, le=35)]
    humedad: Annotated[float, Field(ge=0, le=100)]
    minutos_sin_presencia: Annotated[int, Field(ge=0)] = 0
    minutos_enfriando: Annotated[int, Field(ge=0)] = 0
    temp_inicio: Optional[float] = None
    temp_actual: Optional[float] = None
    temp_ac_actual: Optional[float] = None
    usar_capa_seguridad: bool = True


class EntradaAtmosFirebase(BaseModel):
    area: str = "robotica"
    aire: str = "Aire_1"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@limitador.limit("60/minute")
@enrutador.post("/atmos/decidir")
async def decidir_atmos(request: Request, entrada: EntradaAtmos):
    resultado = ServicioPredictor().decidir_atmos(entrada.model_dump())
    if not resultado.get("valido", False):
        raise HTTPException(status_code=422, detail=resultado)
    return resultado


@limitador.limit("30/minute")
@enrutador.post("/atmos/firebase/decidir")
async def decidir_atmos_desde_firebase(
    request: Request,
    entrada: EntradaAtmosFirebase = EntradaAtmosFirebase(),
    x_atmos_token: Annotated[Optional[str], Header()] = None,
):
    if not configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ATMOS_DEVICE_TOKEN no está configurado en el servidor",
        )

    if x_atmos_token != configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(status_code=401, detail="Token ATMOS inválido")

    try:
        return ServicioPredictor().decidir_atmos_desde_firebase(
            area=entrada.area,
            aire=entrada.aire,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@limitador.limit("10/minute")
@enrutador.get("/caracteristicas/{sala_id}")
async def obtener_caracteristicas(
    request: Request,
    sala_id: UUID,
    dias_atras: Annotated[int, Query(ge=1, le=90)] = 30,
    _=Depends(requerir_admin),
):
    caracteristicas = ServicioPredictor().obtener_caracteristicas_entrenamiento(
        sala_id, dias_atras
    )
    if not caracteristicas:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron datos agregados para esta sala en los últimos {dias_atras} días",
        )
    return caracteristicas


@limitador.limit("20/minute")
@enrutador.post("/predicciones", status_code=201)
async def guardar_prediccion(
    request: Request,
    entrada: EntradaPrediccion,
    _=Depends(requerir_admin),
):
    carga = entrada.model_dump(mode="json")
    sala_id = carga.pop("sala_id")
    try:
        resultado = ServicioPredictor().guardar_prediccion(sala_id, carga)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return resultado


@enrutador.get("/predicciones/{sala_id}/reciente")
async def ultima_prediccion_sala(sala_id: UUID):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("ml_predictions")
        .select("*")
        .eq("sala_id", str(sala_id))
        .order("predicho_en", desc=True)
        .limit(1)
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron predicciones para esta sala",
        )
    return respuesta.data[0]


@enrutador.post("/evaluar")
async def evaluar_predicciones(
    sala_id: Optional[UUID] = None,
    x_cron_secret: Annotated[Optional[str], Header()] = None,
):
    if not x_cron_secret or x_cron_secret != configuracion.CRON_SECRET:
        raise HTTPException(status_code=401, detail="No autorizado")

    cliente = obtener_cliente()

    if sala_id:
        salas_ids = [sala_id]
    else:
        respuesta = (
            cliente.table("ml_predictions").select("sala_id").execute()
        )
        salas_ids = list({UUID(f["sala_id"]) for f in respuesta.data if f.get("sala_id")})

    servicio = ServicioPredictor()
    todos_resultados: list[dict] = []

    for sid in salas_ids:
        try:
            resultados = servicio.evaluar_predicciones_pasadas(sid)
            todos_resultados.extend(resultados)
        except Exception:
            pass

    return {"evaluadas": len(todos_resultados), "resultados": todos_resultados}
