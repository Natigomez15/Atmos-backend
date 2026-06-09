from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException

from app.config import configuracion
from app.ml.predictor import ServicioPredictor
from app.services.sincronizador_firebase import sincronizar_firebase_supabase


enrutador = APIRouter(prefix="/atmos", tags=["atmos"])


@enrutador.post("/procesar-lectura")
def procesar_lectura_atmos(
    x_atmos_token: Annotated[Optional[str], Header()] = None,
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    if not configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ATMOS_DEVICE_TOKEN no esta configurado en el servidor",
        )

    if x_atmos_token != configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(status_code=401, detail="Token ATMOS invalido")

    try:
        sincronizacion = sincronizar_firebase_supabase(
            pabellon_objetivo=pabellon,
            aire_objetivo=aire,
        )
        decision = ServicioPredictor().decidir_atmos_desde_firebase(
            area=pabellon,
            aire=aire,
        )
        return {
            "sincronizacion": sincronizacion,
            "decision": decision,
            "flujo": "firebase_supabase_ml_firebase",
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Error procesando ATMOS: {error}")
