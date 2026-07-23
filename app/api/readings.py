from fastapi import APIRouter, HTTPException, Query, Header, Request
from datetime import datetime, timezone, timedelta
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
from app.services.alert_service import ServicioAlertas
from app.services.sincronizador_firebase import (
    MAX_LECTURAS_FIREBASE_POR_CONSULTA,
    aplicar_estado_comando_firebase,
    sincronizar_firebase_supabase,
    leer_ultimas_lecturas_firebase_rest,
    leer_ultima_lectura_valida_firebase_rest,
    preparar_registro_supabase,
)
from app.ml.impacto import obtener_medicion_electrica_firebase
from app.config import configuracion
from app.core.limiter import limitador

enrutador = APIRouter(prefix="/lecturas", tags=["lecturas"])

FIREBASE_PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"


def _fecha_desde_firebase_push_id(firebase_key: str) -> datetime | None:
    """Extrae la fecha UTC codificada en los primeros 8 caracteres del push ID."""
    if len(firebase_key) < 8:
        return None
    milisegundos = 0
    try:
        for caracter in firebase_key[:8]:
            milisegundos = milisegundos * 64 + FIREBASE_PUSH_CHARS.index(caracter)
        return datetime.fromtimestamp(milisegundos / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _normalizar_historial_firebase(
    lecturas_firebase: dict,
    pabellon: str,
    aire: str,
) -> list[dict]:
    registros = []
    for firebase_key, valor in sorted(lecturas_firebase.items(), reverse=True):
        if not isinstance(valor, dict):
            continue
        registro = preparar_registro_supabase(
            pabellon=pabellon,
            aire=aire,
            firebase_key=firebase_key,
            valor=valor,
        )
        fecha_lectura = _fecha_desde_firebase_push_id(firebase_key)
        if fecha_lectura is not None:
            registro["fecha_sync"] = fecha_lectura.isoformat()
        registros.append(registro)
    return registros


def _completar_potencia_historica_desde_firebase(
    registros: list[dict],
    lecturas_firebase: dict,
    pabellon: str,
    aire: str,
) -> list[dict]:
    """Cruza por firebase_key sin escribir ni alterar el orden histórico."""
    prefijo = f"{pabellon}_{aire}_"
    completados = []
    for registro_original in registros:
        registro = dict(registro_original)
        firebase_key = str(registro.get("firebase_key") or "")
        key_lectura = (
            firebase_key[len(prefijo):]
            if firebase_key.startswith(prefijo)
            else firebase_key
        )
        lectura = lecturas_firebase.get(key_lectura)
        if isinstance(lectura, dict):
            medicion = obtener_medicion_electrica_firebase(lectura)
            potencia_activa_w = medicion.get("potencia_activa_w")
            if potencia_activa_w is not None:
                registro["potencia_w"] = potencia_activa_w
            registro.update({
                campo: valor
                for campo, valor in medicion.items()
                if valor is not None
            })
        completados.append(registro)
    return completados


def _agrupar_potencia_activa_firebase(
    lecturas_firebase: dict,
    dias: int,
    ahora: datetime | None = None,
) -> list[dict]:
    """Agrupa por hora únicamente la potencia activa reportada por Firebase."""
    limite_fecha = (ahora or datetime.now(timezone.utc)) - timedelta(days=dias)
    cubos: dict[datetime, list[float]] = {}

    for firebase_key, valor in lecturas_firebase.items():
        if not isinstance(valor, dict):
            continue
        fecha = _fecha_desde_firebase_push_id(firebase_key)
        if fecha is None or fecha < limite_fecha:
            continue

        potencia = valor.get("potencia_activa_w")
        if potencia in (None, ""):
            potencia_kw = valor.get("potencia_activa_kw")
            try:
                potencia = float(potencia_kw) * 1000 if potencia_kw not in (None, "") else None
            except (TypeError, ValueError):
                potencia = None
        try:
            potencia_w = float(potencia) if potencia not in (None, "") else None
        except (TypeError, ValueError):
            potencia_w = None
        if potencia_w is None:
            continue

        cubo = fecha.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        cubos.setdefault(cubo, []).append(potencia_w)

    return [
        {
            "bucket_hour": cubo.isoformat(),
            "avg_power_w": round(sum(valores) / len(valores), 2),
            "reading_count": len(valores),
            "source": "firebase_potencia_activa_w",
        }
        for cubo, valores in sorted(cubos.items())
    ]


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


def _es_registro_antiguo(fecha_sync: str | None, umbral_segundos: int = 60) -> bool:
    if not fecha_sync:
        return True
    try:
        registrado_en = datetime.fromisoformat(str(fecha_sync).replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - registrado_en > timedelta(seconds=umbral_segundos)


def _obtener_lectura_firebase_directa(pabellon: str, aire: str, sala_id: str | None = None, nodo_id: str | None = None) -> dict | None:
    seleccion = leer_ultima_lectura_valida_firebase_rest(
        pabellon=pabellon,
        aire=aire,
        limite=50,
    )
    if not seleccion.get("valida") or not seleccion.get("lectura"):
        return None

    firebase_key = seleccion.get("firebase_key") or "directo"
    try:
        valor = aplicar_estado_comando_firebase(seleccion["lectura"], pabellon, aire)
        return preparar_registro_supabase(
            pabellon=pabellon,
            aire=aire,
            firebase_key=firebase_key,
            valor=valor,
            sala_id=sala_id,
            nodo_id=nodo_id,
            registro_anterior=None,
        )
    except Exception:
        return None


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

@enrutador.get("/registros/aires")
async def obtener_aires_de_pabellon(pabellon: str):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("registros")
        .select("aire")
        .eq("pabellon", pabellon)
        .execute()
    )
    aires = sorted({r["aire"] for r in respuesta.data if r.get("aire")})
    return aires


@enrutador.get("/registros/potencia-activa")
async def obtener_potencia_activa_firebase(
    pabellon: str,
    aire: str,
    dias: Annotated[int, Query(ge=1, le=7)] = 7,
):
    """Histórico horario de potencia activa; Firebase es la única fuente."""
    try:
        lecturas = leer_ultimas_lecturas_firebase_rest(
            pabellon=pabellon,
            aire=aire,
            # El gráfico se refresca desde el navegador: limitar la ventana
            # protege Firebase incluso si cambian los parámetros del cliente.
            limite=MAX_LECTURAS_FIREBASE_POR_CONSULTA,
        )
        return _agrupar_potencia_activa_firebase(lecturas, dias)
    except Exception as error:
        log.error({
            "evento": "potencia_activa_firebase_no_disponible",
            "pabellon": pabellon,
            "aire": aire,
            "error": str(error),
        })
        raise HTTPException(
            status_code=502,
            detail="No se pudo leer la potencia activa desde Firebase.",
        ) from error


@enrutador.get("/registros", response_model=list[RegistroRespuesta])
async def listar_registros(
    pabellon: str | None = None,
    aire: str | None = None,
    limite: Annotated[int, Query(ge=1, le=MAX_LECTURAS_FIREBASE_POR_CONSULTA)] = 100,
):
    # Para una zona/aire concretos, Firebase es la fuente primaria y única de
    # esta lectura. La copia en Supabase se mantiene mediante el sincronizador.
    if pabellon and aire:
        try:
            lecturas_firebase = leer_ultimas_lecturas_firebase_rest(
                pabellon=pabellon,
                aire=aire,
                limite=limite,
            )
            return _normalizar_historial_firebase(
                lecturas_firebase,
                pabellon,
                aire,
            )
        except Exception as error:
            log.error({
                "evento": "historial_firebase_no_disponible",
                "pabellon": pabellon,
                "aire": aire,
                "error": str(error),
            })
            raise HTTPException(
                status_code=502,
                detail="No se pudo leer el historial directamente desde Firebase.",
            ) from error

    cliente = obtener_cliente()
    consulta = (
        cliente.table("registros")
        .select("*")
        .limit(limite)
        .order("fecha_sync", desc=True)
    )
    if pabellon:
        consulta = consulta.eq("pabellon", pabellon)
    if aire:
        consulta = consulta.eq("aire", aire)
    respuesta = consulta.execute()
    return respuesta.data or []


@enrutador.get("/registros/agregado")
async def obtener_agregado(pabellon: str | None = None, aire: str | None = None):
    cliente = obtener_cliente()
    consulta = cliente.table("registros").select("*")
    if pabellon:
        consulta = consulta.eq("pabellon", pabellon)
    if aire:
        consulta = consulta.eq("aire", aire)
    respuesta = consulta.execute()
    return agregar_lecturas(respuesta.data)


@enrutador.get("/registros/reciente", response_model=RegistroRespuesta)
async def obtener_registro_reciente(
    sala_id: UUID | None = None,
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    cliente = obtener_cliente()
    consulta_base = (
        cliente.table("registros")
        .select("*")
        .order("fecha_sync", desc=True)
        .limit(1)
    )

    if sala_id:
        respuesta = consulta_base.eq("sala_id", str(sala_id)).execute()
        if respuesta.data:
            registro = respuesta.data[0]
            if _es_registro_antiguo(registro.get("fecha_sync")):
                sala_respuesta = (
                    cliente.table("rooms")
                    .select("nombre,pabellon,edificio")
                    .eq("id", str(sala_id))
                    .limit(1)
                    .execute()
                )
                if sala_respuesta.data:
                    sala = sala_respuesta.data[0]
                    pabellon_sala = sala.get("pabellon") or sala.get("edificio")
                    aire_sala = sala.get("nombre")
                    if pabellon_sala and aire_sala:
                        lectura_firebase = _obtener_lectura_firebase_directa(
                            pabellon_sala,
                            aire_sala,
                            sala_id=str(sala_id),
                        )
                        if lectura_firebase:
                            return lectura_firebase
            return registro

        sala_respuesta = (
            cliente.table("rooms")
            .select("nombre,pabellon,edificio")
            .eq("id", str(sala_id))
            .limit(1)
            .execute()
        )
        if sala_respuesta.data:
            sala = sala_respuesta.data[0]
            pabellon_sala = sala.get("pabellon") or sala.get("edificio")
            aire_sala = sala.get("nombre")
            if pabellon_sala and aire_sala:
                lectura_firebase = _obtener_lectura_firebase_directa(
                    pabellon_sala,
                    aire_sala,
                    sala_id=str(sala_id),
                )
                if lectura_firebase:
                    return lectura_firebase
    else:
        respuesta = consulta_base.eq("pabellon", pabellon).eq("aire", aire).execute()
        if respuesta.data:
            registro = respuesta.data[0]
            if _es_registro_antiguo(registro.get("fecha_sync")):
                lectura_firebase = _obtener_lectura_firebase_directa(pabellon, aire)
                if lectura_firebase:
                    return lectura_firebase
            return registro

        lectura_firebase = _obtener_lectura_firebase_directa(pabellon, aire)
        if lectura_firebase:
            return lectura_firebase

    raise HTTPException(status_code=404, detail="No hay registros sincronizados")


@enrutador.post("/registros", response_model=RegistroRespuesta, status_code=201)
async def crear_registro(registro: RegistroCrear):
    cliente = obtener_cliente()
    respuesta = cliente.table("registros").insert(registro.model_dump()).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar el registro")
    return respuesta.data[0]


@limitador.limit("30/minute")
@enrutador.post("/firebase/sincronizar")
def sincronizar_registros_firebase(
    request: Request,
    x_atmos_token: Annotated[Optional[str], Header()] = None,
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    if not configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="ATMOS_DEVICE_TOKEN no está configurado en el servidor",
        )

    if x_atmos_token != configuracion.ATMOS_DEVICE_TOKEN:
        raise HTTPException(status_code=401, detail="Token ATMOS inválido")

    try:
        resultado = sincronizar_firebase_supabase(
            pabellon_objetivo=pabellon,
            aire_objetivo=aire,
        )
        resultado["alertas"] = ServicioAlertas().verificar_alertas_registros_atmos(
            pabellon=pabellon,
            aire=aire,
        )
        return resultado
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Error sincronizando Firebase con Supabase: {error}",
        )


@enrutador.get("/firebase/ultima")
def obtener_ultima_lectura_firebase(
    pabellon: str = "robotica",
    aire: str = "Aire_1",
):
    try:
        return {
            "pabellon": pabellon,
            "aire": aire,
            "lecturas": leer_ultimas_lecturas_firebase_rest(
                pabellon=pabellon,
                aire=aire,
                limite=1,
            ),
        }
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Error leyendo Firebase por REST: {error}",
        )


@enrutador.post("/firebase/sincronizar-rapido")
def sincronizar_registros_firebase_rapido(
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
        resultado = sincronizar_firebase_supabase(
            pabellon_objetivo=pabellon,
            aire_objetivo=aire,
        )
        resultado["alertas"] = ServicioAlertas().verificar_alertas_registros_atmos(
            pabellon=pabellon,
            aire=aire,
        )
        return resultado
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Error sincronizando Firebase con Supabase: {error}",
        )
