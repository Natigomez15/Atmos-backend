import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, Header, Request
from typing import Annotated, Literal, Optional
from uuid import UUID
from pydantic import BaseModel, Field

from app.ml.predictor import ServicioPredictor
from app.ml.impacto import resumir_impacto_decisiones, resumir_impacto_real
from app.core.database import obtener_cliente
from app.config import configuracion
from app.core.limiter import limitador
from app.core.security import requerir_admin, requerir_mantenimiento_o_admin
from app.services.sincronizador_firebase import (
    leer_comando_firebase_rest,
    normalizar_pabellon_firebase,
    normalizar_ultima_decision_firebase,
)

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
    ultima_accion_ir: Optional[str] = None
    modo_control: str = "experimental"


class EntradaAtmosFirebase(BaseModel):
    area: str = "robotica"
    aire: str = "Aire_1"


class RespuestaDecisionML(BaseModel):
    decision_id: str
    area: str = "robotica"
    aire: str = "Aire_1"
    motivo: Optional[str] = None


class PrediccionDecisionFirebase(BaseModel):
    valor: Optional[str] = None
    confianza: Optional[Annotated[float, Field(ge=0.0, le=1.0)]] = None
    modelo_usado: Optional[bool] = None
    origen: Optional[str] = None
    tipo_modelo: Optional[str] = None
    version_modelo: Optional[str] = None


class RecomendacionDecisionFirebase(BaseModel):
    recomendacion_final: Optional[str] = None
    accion_solicitada: Optional[str] = None
    modificada_por_reglas: bool
    motivo: Optional[str] = None


class EjecucionDecisionFirebase(BaseModel):
    ultima_accion: Optional[str] = None
    temperatura: Optional[float] = None
    estado: Literal[
        "sin_registro",
        "pendiente",
        "senal_enviada",
        "enviada_sin_confirmacion",
        "confirmada",
        "fallida",
        "inconsistente",
    ]
    mensaje: str
    resultado_raw: Optional[str] = None
    confirmacion_ir_raw: Optional[str] = None
    firma: Optional[str] = None


class EstadoControlFirebase(BaseModel):
    estado_deseado: Optional[str] = None
    ultimo_comando_enviado: Optional[str] = None
    estado_reportado_por_software: str
    estado_electrico_observado: Literal["encendido", "apagado", "no_confirmado"]
    compresor_confirmado: bool


class UltimaDecisionFirebaseRespuesta(BaseModel):
    pabellon: str
    aire: str
    actualizado_en: Optional[str] = None
    prediccion: PrediccionDecisionFirebase
    decision: RecomendacionDecisionFirebase
    ejecucion: EjecucionDecisionFirebase
    estado_control: EstadoControlFirebase
    advertencias: list[str]


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
    prediccion_original = instantanea.get("prediccion_modelo")
    recomendacion_final = instantanea.get("recomendacion_final")
    if recomendacion_final is None:
        recomendacion_final = instantanea.get("accion_final")
    accion_solicitada = instantanea.get("accion_solicitada")
    if accion_solicitada is None:
        accion_solicitada = instantanea.get("accion_final")
    recomendacion_modificada = (
        prediccion_original is not None
        and recomendacion_final is not None
        and str(prediccion_original).strip().lower()
        != str(recomendacion_final).strip().lower()
    )
    motivo_reglas = instantanea.get("motivo_reglas_seguridad")
    modelo_ml = {
        "modelo_disponible": True,
        "modelo_usado": instantanea.get("fuente") == "modelo_pkl",
        "tipo_modelo": instantanea.get("tipo_modelo"),
        "version_modelo": prediccion.get("version_modelo"),
        "features_usadas": features_usadas,
        "prediccion_modelo": prediccion_original,
        "probabilidades": instantanea.get("probabilidades"),
        "accion_final": instantanea.get("accion_final"),
        "recomendacion_final": recomendacion_final,
        "accion_solicitada": accion_solicitada,
        "motivo_reglas_seguridad": motivo_reglas,
    }
    # Confianza de la predicción (RandomForest.predict_proba): probabilidad de
    # la clase elegida. Se expone lista para el frontend ("Apagar aire —
    # confianza 87%").
    confianza = prediccion.get("puntaje_confianza")
    confianza_pct = round(float(confianza) * 100) if confianza is not None else None
    texto_accion = _texto_accion(prediccion_original) if prediccion_original is not None else None
    recomendacion_texto = None
    if texto_accion is not None:
        recomendacion_texto = (
            f"{texto_accion} — confianza {confianza_pct}%"
            if confianza_pct is not None
            else texto_accion
        )
    return {
        **prediccion,
        "confianza_pct": confianza_pct,
        "recomendacion_texto": recomendacion_texto,
        "prediccion_original": prediccion_original,
        "confianza_prediccion": confianza,
        "recomendacion_final": recomendacion_final,
        "accion_solicitada": accion_solicitada,
        "recomendacion_modificada": recomendacion_modificada,
        "motivo_reglas_seguridad": motivo_reglas,
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


@enrutador.get("/decisions/latest", response_model=UltimaDecisionFirebaseRespuesta)
async def ultima_decision_firebase(
    pabellon: Annotated[str, Query(min_length=1)],
    aire: Annotated[str, Query(min_length=1)],
):
    pabellon_normalizado = normalizar_pabellon_firebase(pabellon)
    aire_real = aire.strip()
    if not pabellon_normalizado or not aire_real:
        raise HTTPException(
            status_code=422,
            detail="Los parámetros pabellon y aire no pueden estar vacíos.",
        )

    try:
        datos = leer_comando_firebase_rest(pabellon_normalizado, aire_real)
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=(
                    "No existe una decisión en Firebase para "
                    f"/Atmos/comandos/{pabellon_normalizado}/{aire_real}."
                ),
            ) from error
        raise HTTPException(
            status_code=502,
            detail="No se pudo leer la última decisión desde Firebase.",
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail="No se pudo leer la última decisión desde Firebase.",
        ) from error

    if not datos:
        raise HTTPException(
            status_code=404,
            detail=(
                "No existe una decisión en Firebase para "
                f"/Atmos/comandos/{pabellon_normalizado}/{aire_real}."
            ),
        )
    return normalizar_ultima_decision_firebase(
        pabellon=pabellon_normalizado,
        aire=aire_real,
        datos=datos,
    )


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
        "mantener_monitoreo": "Mantener estado actual",
        "esperar_apagado": "Mantener estado actual",
        "encender_22": "Encender a 22 °C",
        "ahorro_24": "Modo ahorro a 24 °C",
        "enfriar_fuerte": "Enfriamiento fuerte",
        "fuera_horario_apagar": "Apagar aire por horario",
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
            "prediccion_original": None,
            "confianza_prediccion": None,
            "recomendacion_final": None,
            "accion_solicitada": None,
            "recomendacion_modificada": False,
            "motivo_reglas_seguridad": None,
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
        "prediccion_original": None,
        "confianza_prediccion": None,
        "recomendacion_final": None,
        "accion_solicitada": None,
        "recomendacion_modificada": False,
        "motivo_reglas_seguridad": None,
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


@limitador.limit("30/minute")
@enrutador.post("/recommendations/current")
async def obtener_recomendacion_actual(
    request: Request,
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    """Evalúa la última lectura real; nunca presenta una lectura vieja como actual."""
    try:
        return ServicioPredictor().decidir_atmos_desde_firebase(
            area=pabellon.strip(),
            aire=aire.strip(),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo evaluar la lectura actual de Firebase: {error}",
        ) from error



@enrutador.get("/decisiones/pendiente")
async def obtener_decision_ml_pendiente(
    area: str = "robotica",
    aire: str = "Aire_1",
):
    area_limpia = area.strip()
    aire_limpio = aire.strip()
    if not area_limpia or not aire_limpio:
        raise HTTPException(
            status_code=422,
            detail="area y aire son obligatorios",
        )
    try:
        return ServicioPredictor().obtener_decision_ml_pendiente(
            area=area_limpia,
            aire=aire_limpio,
        )
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f"No se pudo consultar la decisión ML pendiente: {error}",
        ) from error


@limitador.limit("20/minute")
@enrutador.post("/decisiones/pendiente/aceptar")
async def aceptar_decision_ml_pendiente(
    request: Request,
    entrada: RespuestaDecisionML,
    usuario: dict = Depends(requerir_mantenimiento_o_admin),
):
    try:
        resultado = ServicioPredictor().aceptar_decision_ml_pendiente(
            area=entrada.area.strip(),
            aire=entrada.aire.strip(),
            decision_id=entrada.decision_id,
            usuario=usuario,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    if not resultado.get("ok"):
        raise HTTPException(status_code=409, detail=resultado)

    return resultado


@limitador.limit("20/minute")
@enrutador.post("/decisiones/pendiente/rechazar")
async def rechazar_decision_ml_pendiente(
    request: Request,
    entrada: RespuestaDecisionML,
    usuario: dict = Depends(requerir_mantenimiento_o_admin),
):
    try:
        return ServicioPredictor().rechazar_decision_ml_pendiente(
            area=entrada.area.strip(),
            aire=entrada.aire.strip(),
            decision_id=entrada.decision_id,
            usuario=usuario,
            motivo=entrada.motivo,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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


@enrutador.get("/modelo/info")
async def informacion_modelo():
    """Panel 'Sobre el motor': tipo de modelo, clases, importancia REAL de
    variables (feature_importances_ del RandomForest) y métricas de
    validación persistidas. Si el modelo activo no tiene metadata de
    métricas, `metricas_disponibles` es False y el frontend muestra
    'No disponible para esta versión'."""
    servicio = ServicioPredictor()
    try:
        panel = servicio.panel_modelo()
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo introspeccionar el modelo: {error}",
        )
    # Aciertos en producción (Fase 2.3): degrada a estado "pendiente"/"sin_datos"
    # sin romper el panel si el log de decisiones aún no tiene datos evaluables.
    try:
        panel["aciertos_produccion"] = servicio.precision_apagados_produccion()
    except Exception:
        panel["aciertos_produccion"] = {"estado": "pendiente"}
    return panel
