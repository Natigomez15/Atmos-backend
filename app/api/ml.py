from fastapi import APIRouter, HTTPException, Query, Header, Request
from typing import Annotated, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.ml.predictor import ServicioPredictor
from app.core.database import obtener_cliente
from app.config import configuracion
from app.core.limiter import limitador

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@limitador.limit("10/minute")
@enrutador.get("/caracteristicas/{sala_id}")
async def obtener_caracteristicas(
    request: Request,
    sala_id: UUID,
    dias_atras: Annotated[int, Query(ge=1, le=90)] = 30,
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
async def guardar_prediccion(request: Request, entrada: EntradaPrediccion):
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
