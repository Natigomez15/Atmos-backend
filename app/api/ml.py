from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from typing import Annotated, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.ml.predictor import ServicioPredictor
from app.ml.impacto import resumir_impacto_decisiones, resumir_impacto_real
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
    temperatura_salida_aire: Optional[float] = Field(default=None, ge=5, le=35)
    temp_ac: Optional[float] = Field(default=None, ge=5, le=35)
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


def _mapear_prediccion(prediccion: dict) -> dict:
    instantanea = prediccion.get("instantanea_caracteristicas") or {}
    features_usadas = instantanea.get("features_usadas")
    if isinstance(features_usadas, dict) and "temp_ac" in features_usadas:
        features_usadas = dict(features_usadas)
        features_usadas["temperatura_salida_aire"] = features_usadas.pop("temp_ac")
        instantanea = {
            **instantanea,
            "features_usadas": features_usadas,
        }
    modelo_ml = {
        "modelo_disponible": True,
        "modelo_usado": instantanea.get("fuente") == "modelo_pkl",
        "tipo_modelo": "RandomForestClassifier",
        "version_modelo": prediccion.get("version_modelo"),
        "features_usadas": features_usadas,
        "prediccion_modelo": instantanea.get("prediccion_modelo"),
        "probabilidades": instantanea.get("probabilidades"),
        "accion_final": instantanea.get("accion_final"),
        "motivo_reglas_seguridad": instantanea.get("motivo_reglas_seguridad"),
    }
    return {
        **prediccion,
        "room_id": prediccion.get("sala_id"),
        "recommended_setpoint": prediccion.get("setpoint_recomendado"),
        "predicted_savings_pct": prediccion.get("ahorro_predicho_pct"),
        "confidence_score": prediccion.get("puntaje_confianza"),
        "model_version": prediccion.get("version_modelo"),
        "snapshot_features": instantanea,
        "actual_savings_pct": prediccion.get("ahorro_real_pct"),
        "was_applied": prediccion.get("fue_aplicado"),
        "predicted_at": prediccion.get("predicho_en"),
        "modelo_ml": modelo_ml,
    }


def _mapear_caracteristica(fila: dict) -> dict:
    return {
        **fila,
        "bucket_hour": fila.get("cubo_hora"),
        "avg_temp": fila.get("temperatura_promedio"),
        "avg_humidity": fila.get("humedad_promedio"),
        "presence_ratio": fila.get("razon_presencia"),
        "avg_power_w": fila.get("potencia_promedio_w"),
        "total_energy_kwh": fila.get("energia_total_kwh"),
        "weekday": fila.get("dia_semana"),
        "hour_of_day": fila.get("hora_del_dia"),
        "reading_count": fila.get("cantidad_lecturas"),
        "last_reading_at": fila.get("fecha_ultima_lectura"),
        "source": fila.get("fuente"),
    }


def _texto_accion(accion: str | None) -> str:
    acciones = {
        "apagar": "Apagar aire",
        "mantener": "Mantener estado actual",
        "encender_22": "Encender a 22 °C",
        "ahorro_24": "Modo ahorro a 24 °C",
        "enfriar_fuerte": "Enfriamiento fuerte",
    }
    return acciones.get(str(accion or "").strip().lower(), "Sin recomendación operativa")


def _setpoint_accion(accion: str | None) -> int | None:
    accion_normalizada = str(accion or "").strip().lower()
    if accion_normalizada in {"encender_22", "enfriar_fuerte"}:
        return 22
    if accion_normalizada == "ahorro_24":
        return 24
    return None


def _fallback_prediccion_desde_registro(sala_id: UUID) -> dict:
    servicio = ServicioPredictor()
    info_modelo = servicio.informacion_modelo()
    registro = servicio.obtener_ultimo_registro_sala(sala_id)
    if not registro:
        return {
            "disponible": False,
            "motivo": "No hay predicción guardada ni lecturas válidas en registros",
            "fallback_disponible": False,
            "fuente": "registros",
            "room_id": str(sala_id),
            "sala_id": str(sala_id),
            "recommended_setpoint": None,
            "predicted_savings_pct": None,
            "confidence_score": None,
            "model_version": "motor_decision_atmos_v1",
            "modelo_ml": {
                **info_modelo,
                "modelo_usado": False,
                "motivo_no_usado": "no hay lecturas validas suficientes en registros",
                "features_usadas": None,
                "prediccion_modelo": None,
                "probabilidades": None,
            },
            "operational_recommendation": None,
            "recommendation_text": "Datos insuficientes",
            "predicted_at": None,
        }

    accion = (
        registro.get("ultima_accion_ejecutada")
        or registro.get("recomendacion_local")
        or "mantener"
    )
    features = {
        "temperatura_ambiente": registro.get("temperatura_ambiente"),
        "temperatura_salida_aire": registro.get("temperatura_salida_aire"),
        "humedad": registro.get("humedad"),
        "estado_ocupacion": registro.get("estado_ocupacion"),
        "potencia_w": registro.get("potencia_w"),
        "energia_kwh": registro.get("energia_kwh"),
        "fecha_sync": registro.get("fecha_sync"),
        "fuente": "registros",
    }
    return {
        "disponible": False,
        "motivo": "No hay predicción guardada en ml_predictions",
        "fallback_disponible": True,
        "fuente": "registros",
        "room_id": str(sala_id),
        "sala_id": str(sala_id),
        "id": None,
        "recommended_setpoint": _setpoint_accion(accion),
        "setpoint_recomendado": _setpoint_accion(accion),
        "predicted_savings_pct": None,
        "ahorro_predicho_pct": None,
        "confidence_score": None,
        "puntaje_confianza": None,
        "model_version": "motor_decision_atmos_v1",
        "version_modelo": "motor_decision_atmos_v1",
        "modelo_ml": {
            **info_modelo,
            "modelo_usado": False,
            "motivo_no_usado": "no hay prediccion guardada en ml_predictions; se muestra fallback operativo desde registros",
            "features_usadas": None,
            "prediccion_modelo": None,
            "probabilidades": None,
        },
        "snapshot_features": features,
        "instantanea_caracteristicas": features,
        "actual_savings_pct": None,
        "was_applied": False,
        "fue_aplicado": False,
        "predicted_at": registro.get("fecha_sync"),
        "predicho_en": registro.get("fecha_sync"),
        "operational_recommendation": accion,
        "recomendacion_actual": accion,
        "recommendation_text": _texto_accion(accion),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@limitador.limit("60/minute")
@enrutador.post("/atmos/decidir")
async def decidir_atmos(request: Request, entrada: EntradaAtmos):
    carga = entrada.model_dump()
    if carga.get("temp_ac") is None:
        carga["temp_ac"] = carga.get("temperatura_salida_aire")
    if carga.get("temp_ac") is None:
        raise HTTPException(
            status_code=422,
            detail="Falta temperatura_salida_aire para ejecutar el modelo ATMOS",
        )
    resultado = ServicioPredictor().decidir_atmos(carga)
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


@enrutador.get("/predictions/{sala_id}/latest")
async def latest_prediction_alias(sala_id: UUID):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("ml_predictions")
        .select("*")
        .eq("sala_id", str(sala_id))
        .order("predicho_en", desc=True)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return {
            "disponible": True,
            "fallback_disponible": False,
            "fuente": "ml_predictions",
            **_mapear_prediccion(respuesta.data[0]),
        }
    return _fallback_prediccion_desde_registro(sala_id)


@enrutador.get("/features/{sala_id}")
async def features_alias(
    sala_id: UUID,
    days_back: Annotated[int, Query(ge=1, le=90)] = 30,
):
    caracteristicas = ServicioPredictor().obtener_caracteristicas_entrenamiento(
        sala_id, days_back
    )
    return [_mapear_caracteristica(fila) for fila in caracteristicas]


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


@enrutador.post("/evaluate")
async def evaluate_alias(
    sala_id: Optional[UUID] = None,
    x_cron_secret: Annotated[Optional[str], Header()] = None,
):
    return await evaluar_predicciones(sala_id=sala_id, x_cron_secret=x_cron_secret)


@enrutador.get("/impacto/decisiones")
async def impacto_decisiones(
    pabellon: str = "robotica",
    aire: str = "Aire_1",
    limite: Annotated[int, Query(ge=1, le=1000)] = 500,
):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("registros")
        .select("ultima_accion_ejecutada,energia_kwh,potencia_w,fecha_sync")
        .eq("pabellon", pabellon)
        .eq("aire", aire)
        .order("fecha_sync", desc=True)
        .limit(limite)
        .execute()
    )
    return resumir_impacto_decisiones(respuesta.data or [])


@enrutador.get("/impacto/real")
async def impacto_real():
    return resumir_impacto_real()
