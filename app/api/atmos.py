from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException

from app.config import configuracion
from app.ml.predictor import ServicioPredictor
from app.services.sincronizador_firebase import (
    leer_ultima_lectura_valida_firebase_rest,
    sincronizar_firebase_supabase,
)


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
            "modelo_ml": decision.get("modelo_ml"),
            "diagnostico": decision.get("diagnostico") or sincronizacion.get("diagnostico"),
            "flujo": "firebase_supabase_ml_firebase",
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Error procesando ATMOS: {error}")


@enrutador.get("/diagnostico")
def diagnostico_lecturas_atmos(
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    try:
        seleccion = leer_ultima_lectura_valida_firebase_rest(
            pabellon=pabellon,
            aire=aire,
            limite=50,
        )
        return {
            "pabellon": pabellon,
            "aire": aire,
            "lectura_valida": seleccion["valida"],
            "firebase_key_usado": seleccion["firebase_key"],
            "lectura_usada": seleccion["lectura"],
            "lecturas_invalidas_ignoradas": seleccion["lecturas_invalidas_ignoradas"],
            "advertencias": seleccion["advertencias"],
            "diagnostico": seleccion["diagnostico"],
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Error diagnosticando ATMOS: {error}")
