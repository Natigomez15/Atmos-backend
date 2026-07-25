from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from uuid import UUID

import csv
import io
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.config import configuracion
from app.core.database import obtener_cliente, obtener_firebase
from app.core.logger import log
from app.core.security import obtener_usuario_opcional, requerir_mantenimiento_o_admin
from app.ml.impacto import TARIFA_KWH
from app.models.schemas import SalaActualizar, SalaCrear
from app.api.ac_commands import comando_desde_prediccion
from app.services.alert_service import ServicioAlertas
from app.services.dashboard_energy import construir_resumen_dashboard, rango_dashboard, ahora_panama
from app.services.sincronizador_firebase import leer_ultima_lectura_valida_firebase_rest
from app.core.aires import es_aire_ignorado
from app.core.control_ir import (
    accion_es_no_op,
    construir_comando_ir,
    control_manual_ir_habilitado,
    control_solo_manual,
    motivo_bloqueo_control_manual_ir,
)


enrutador = APIRouter(tags=["compatibilidad"])


def _mapear_registro_a_reading(registro: dict) -> dict:
    return {
        **registro,
        "room_id": registro.get("sala_id"),
        "sala_id": registro.get("sala_id"),
        "temperature": registro.get("temperatura_ambiente"),
        "temperatura": registro.get("temperatura_ambiente"),
        "humidity": registro.get("humedad"),
        "humedad": registro.get("humedad"),
        "presence": registro.get("estado_ocupacion"),
        "presencia": registro.get("estado_ocupacion"),
        "ac_is_on": registro.get("aire_encendido_atmos"),
        "ac_encendido": registro.get("aire_encendido_atmos"),
        "power_w": registro.get("potencia_w"),
        "potencia_w": registro.get("potencia_w"),
        "energy_kwh": registro.get("energia_kwh"),
        "energia_kwh": registro.get("energia_kwh"),
        "recorded_at": registro.get("fecha_sync"),
        "registrado_en": registro.get("fecha_sync"),
    }


def _mapear_sala(sala: dict) -> dict:
    return {
        **sala,
        "room_id": sala.get("id"),
        "sala_id": sala.get("id"),
        "name": sala.get("nombre"),
        "pavilion": sala.get("pabellon") or sala.get("edificio"),
        "floor": sala.get("piso"),
        "capacity": sala.get("capacidad"),
        "aires": sala.get("aires") or [],
        "tipo": sala.get("tipo", "laboratorio"),
        "activo": sala.get("activo", True),
    }


def _resolver_pabellon_y_aire(sala: dict, aire_param: Optional[str] = None) -> tuple[str | None, str | None]:
    pabellon = sala.get("pabellon") or sala.get("edificio")
    aires = sala.get("aires") or []
    if aire_param:
        aire = aire_param
    elif aires:
        aire = aires[0]
    else:
        # legado: nombre del salón era el nombre del aire
        aire = sala.get("nombre")
    return pabellon, aire


def _inicializar_comando_sala_firebase(sala: dict) -> None:
    # Una sala nueva no debe mutar el canal de órdenes. Mantener es no-op.
    return


def _preparar_sobre_comando_manual(
    payload: dict,
    raw_payload: dict,
    cliente=None,
) -> dict:
    pabellon = str(
        raw_payload.get("pabellon")
        or raw_payload.get("pavilion")
        or raw_payload.get("edificio")
        or ""
    ).strip()
    aire = str(
        raw_payload.get("aire")
        or raw_payload.get("ac")
        or raw_payload.get("air")
        or raw_payload.get("nombre_aire")
        or ""
    ).strip()

    # Compatibilidad con clientes antiguos. Si el destino ya viene explícito,
    # el canal operativo no depende de una consulta a Supabase.
    if (not pabellon or not aire) and cliente is not None:
        sala = _resolver_sala_para_comando(cliente, payload["sala_id"])
        if sala:
            pabellon_resuelto, aire_resuelto = _resolver_destino_firebase_comando(sala, raw_payload)
            pabellon = pabellon or str(pabellon_resuelto or "").strip()
            aire = aire or str(aire_resuelto or "").strip()
    if not pabellon or not aire:
        raise ValueError("pabellon y aire son obligatorios para enviar el comando a Firebase.")
    accion = _traducir_comando_manual_a_accion_esp32(payload)
    if accion_es_no_op(accion):
        raise ValueError("La solicitud no contiene una acción IR explícita.")
    return construir_comando_ir(
        pabellon=pabellon,
        aire=aire,
        accion=accion,
    )


def _control_ir_inactivo_http() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "estado": "control_ir_bloqueado",
            "motivo": motivo_bloqueo_control_manual_ir(),
        },
    )


def _valor_ac_encendido_reporte(registro: dict):
    ac_encendido = registro.get("ac_encendido")
    if ac_encendido is not None:
        return ac_encendido
    return registro.get("aire_encendido_atmos", "")


def _valor_movimiento_reporte(registro: dict):
    movimiento = registro.get("movimiento")
    if movimiento is not None:
        try:
            return 1 if int(movimiento) > 0 else 0
        except (TypeError, ValueError):
            return movimiento

    estado_ocupacion = registro.get("estado_ocupacion")
    if estado_ocupacion is not None:
        return 1 if estado_ocupacion is True else 0 if estado_ocupacion is False else estado_ocupacion

    presencia = registro.get("presencia")
    if presencia is not None:
        return 1 if presencia is True else 0 if presencia is False else presencia

    return ""


def _promedio_valores(filas: list[dict], campo: str) -> float | None:
    valores = [fila[campo] for fila in filas if fila.get(campo) is not None]
    if not valores:
        return None
    return round(sum(valores) / len(valores), 4)


def _razon_presencia(filas: list[dict]) -> float | None:
    valores = []
    for fila in filas:
        valor = fila.get("estado_ocupacion")
        if valor is None:
            valor = fila.get("presencia")
        if valor is None:
            movimiento = fila.get("movimiento")
            if movimiento is not None:
                try:
                    valor = int(movimiento) > 0
                except (TypeError, ValueError):
                    valor = None
        if valor is not None:
            valores.append(bool(valor))
    if not valores:
        return None
    return round(sum(1 for valor in valores if valor) / len(valores), 4)


def _horas_ac_encendido(filas: list[dict]) -> float | None:
    fechas_encendido = []
    for fila in filas:
        encendido = fila.get("aire_encendido_atmos")
        if encendido is None:
            encendido = fila.get("ac_encendido")
        if encendido and fila.get("fecha_sync"):
            fechas_encendido.append(fila["fecha_sync"])
    if not fechas_encendido:
        return None
    return round(len(fechas_encendido), 2)


def _calcular_energia_kwh(filas: list[dict]) -> float:
    energias = [fila["energia_kwh"] for fila in filas if fila.get("energia_kwh") is not None]
    if len(energias) >= 2:
        return max(energias) - min(energias)
    potencias = [fila["potencia_w"] for fila in filas if fila.get("potencia_w") is not None]
    if potencias:
        return sum(potencias) / len(potencias) / 1000
    return 0


def _resumir_consumo_total_aires(filas: list[dict]) -> dict:
    """Suma consumo y costo por intervalo, sin mezclar acumulados entre aires."""
    por_aire: dict[tuple[str, str], list[dict]] = {}
    for fila in filas:
        clave = (str(fila.get("pabellon") or ""), str(fila.get("aire") or ""))
        por_aire.setdefault(clave, []).append(fila)

    consumo_total = 0.0
    costo_total = 0.0
    equipos_contabilizados = 0
    for lecturas in por_aire.values():
        lecturas_ordenadas = sorted(
            lecturas,
            key=lambda fila: str(fila.get("fecha_sync") or ""),
        )
        anterior = None
        equipo_con_consumo = False
        for lectura in lecturas_ordenadas:
            consumo = lectura.get("consumo_intervalo_kwh")
            try:
                consumo_kwh = max(0.0, float(consumo)) if consumo is not None else None
            except (TypeError, ValueError):
                consumo_kwh = None

            if consumo_kwh is None and anterior is not None:
                try:
                    inicio = datetime.fromisoformat(
                        str(anterior.get("fecha_sync")).replace("Z", "+00:00")
                    )
                    fin = datetime.fromisoformat(
                        str(lectura.get("fecha_sync")).replace("Z", "+00:00")
                    )
                    segundos = max(0.0, min((fin - inicio).total_seconds(), 10 * 60))
                    potencia_w = float(
                        anterior.get("potencia_activa_w")
                        if anterior.get("potencia_activa_w") is not None
                        else anterior.get("potencia_w")
                    )
                    consumo_kwh = potencia_w / 1000 * (segundos / 3600)
                except (TypeError, ValueError):
                    consumo_kwh = None

            if consumo_kwh is not None:
                equipo_con_consumo = True
                consumo_total += consumo_kwh
                costo_intervalo = lectura.get("costo_intervalo")
                try:
                    costo = max(0.0, float(costo_intervalo)) if costo_intervalo is not None else None
                except (TypeError, ValueError):
                    costo = None
                if costo is None:
                    try:
                        tarifa = float(lectura.get("tarifa_kwh"))
                    except (TypeError, ValueError):
                        tarifa = configuracion.DASHBOARD_TARIFA_USD_KWH
                    costo = consumo_kwh * tarifa
                costo_total += costo
            anterior = lectura
        if equipo_con_consumo:
            equipos_contabilizados += 1

    return {
        "total_energy_kwh": round(consumo_total, 6),
        "total_cost_usd": round(costo_total, 6),
        "rooms_count": equipos_contabilizados,
    }


def _guardar_historial_reporte(cliente, *, tipo: str, carga: dict | None, resultado: dict, usuario: dict | None = None):
    periodo = (carga or {}).get("period", {})
    datos = {
        "report_type": tipo,
        "period_start": periodo.get("start"),
        "period_end": periodo.get("end"),
        "room_count": len(resultado.get("rooms") or []),
        "generated_at": resultado.get("generated_at"),
        "profile_id": usuario.get("id") if usuario else None,
        "metadata": {
            "room_ids": (carga or {}).get("room_ids"),
            "total_energy_kwh": resultado.get("total_energy_kwh"),
            "total_cost_usd": resultado.get("total_cost_usd"),
        },
    }
    try:
        cliente.table("report_history").insert(datos).execute()
    except Exception as error:
        log.warning({
            "evento": "historial_reporte_no_guardado",
            "tabla": "report_history",
            "error": str(error),
        })


def _mapear_alerta(alerta: dict) -> dict:
    return {
        **alerta,
        "room_id": alerta.get("sala_id"),
        "node_id": alerta.get("nodo_id"),
        "alert_type": alerta.get("tipo_alerta"),
        "severity": alerta.get("severidad"),
        "message": alerta.get("mensaje"),
        "detail": alerta.get("detalle"),
        "is_resolved": alerta.get("esta_resuelta"),
        "created_at": alerta.get("creado_en"),
        "resolved_at": alerta.get("resuelto_en"),
    }


def _mapear_comando(comando: dict) -> dict:
    return {
        **comando,
        "command_id": comando.get("id"),
        "comando_id": comando.get("id"),
        "room_id": comando.get("sala_id"),
        "command_type": comando.get("tipo_comando"),
        "mode": comando.get("modo"),
        "source": comando.get("origen"),
        "was_executed": comando.get("fue_ejecutado"),
        "commanded_at": comando.get("enviado_en"),
        "executed_at": comando.get("ejecutado_en"),
    }


def _normalizar_payload_comando(payload: dict) -> dict:
    return {
        "sala_id": payload.get("sala_id") or payload.get("room_id"),
        "nodo_id": payload.get("nodo_id") or payload.get("node_id"),
        "tipo_comando": payload.get("tipo_comando") or payload.get("command_type"),
        "setpoint": payload.get("setpoint"),
        "modo": payload.get("modo") or payload.get("mode"),
        "origen": payload.get("origen") or payload.get("source") or "manual",
    }


def _traducir_comando_manual_a_accion_esp32(payload: dict) -> str:
    tipo = str(payload.get("tipo_comando") or "").strip().lower()
    setpoint = payload.get("setpoint")

    try:
        setpoint_int = int(setpoint) if setpoint is not None else None
    except (TypeError, ValueError):
        setpoint_int = None

    if tipo == "off":
        return "apagar"
    if tipo == "on":
        if setpoint_int == 23:
            return "encender_23"
        return "encender_22"
    if tipo == "setpoint":
        if setpoint_int is None:
            return "mantener"
        if setpoint_int <= 21:
            return "enfriar_fuerte"
        if setpoint_int == 22:
            return "encender_22"
        if setpoint_int == 23:
            return "encender_23"
        if setpoint_int == 24:
            return "ahorro_24"
        return "mantener"
    return "mantener"


def _resolver_sala_para_comando(cliente, sala_id: str) -> dict | None:
    respuesta = (
        cliente.table("rooms")
        .select("*")
        .eq("id", str(sala_id))
        .single()
        .execute()
    )
    return respuesta.data


def _resolver_destino_firebase_comando(
    sala: dict,
    raw_payload: dict,
) -> tuple[str | None, str | None]:
    pabellon = (
        sala.get("pabellon")
        or sala.get("building")
        or sala.get("area")
        or sala.get("edificio")
    )
    aire_param = (
        raw_payload.get("aire")
        or raw_payload.get("ac")
        or raw_payload.get("air")
        or raw_payload.get("nombre_aire")
    )
    aires = [str(valor).strip() for valor in (sala.get("aires") or []) if str(valor).strip()]
    if aire_param:
        aire = str(aire_param).strip()
        if aires and aire not in aires:
            raise ValueError(
                f"El aire '{aire}' no pertenece a la sala solicitada."
            )
    elif len(aires) > 1:
        raise ValueError(
            "La sala tiene varios aires; el campo 'aire' es obligatorio."
        )
    elif aires:
        aire = aires[0]
    else:
        aire = sala.get("aire") or sala.get("name") or sala.get("nombre")
    return pabellon, aire


def _escribir_accion_manual_en_firebase(
    *,
    payload: dict,
    sobre_comando: dict,
) -> dict:
    accion = sobre_comando["accion"]
    resultado = {
        "accion_firebase": accion,
        "ruta_firebase": None,
        "firebase_escrito": False,
        "error_firebase": None,
        "advertencia_firebase": None,
    }

    try:
        pabellon = sobre_comando["pabellon"]
        aire = sobre_comando["aire"]

        ruta = f"/Atmos/comandos/{pabellon}/{aire}/accion"
        resultado["ruta_firebase"] = ruta

        firebase_db = obtener_firebase()
        firebase_db.child("Atmos").child("comandos").child(pabellon).child(aire).update({
            **sobre_comando,
            "actualizado_en": sobre_comando["created_at"],
            "resultado": "pendiente",
        })
        resultado["firebase_escrito"] = True
        log.info({
            "evento": "comando_manual_firebase_escrito",
            "sala_id": str(payload["sala_id"]),
            "pabellon": pabellon,
            "aire": aire,
            "accion": accion,
            "ruta": ruta,
        })
    except Exception as error:
        resultado["error_firebase"] = str(error)
        log.error({
            "evento": "comando_manual_firebase_error",
            "sala_id": str(payload.get("sala_id")),
            "accion": accion,
            "error": str(error),
        })

    return resultado


@enrutador.get("/rooms")
async def listar_rooms(incluir_inactivos: bool = False):
    try:
        cliente = obtener_cliente()
        consulta = cliente.table("rooms").select("*")
        if not incluir_inactivos:
            consulta = consulta.eq("activo", True)
        respuesta = consulta.order("nombre", desc=False).execute()
        return [_mapear_sala(sala) for sala in respuesta.data or []]
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo leer rooms desde Supabase: {error}",
        )


@enrutador.post("/rooms", status_code=201)
async def crear_room(sala: SalaCrear):
    cliente = obtener_cliente()
    respuesta = cliente.table("rooms").insert(sala.model_dump(exclude_none=True)).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    sala_creada = respuesta.data[0]
    _inicializar_comando_sala_firebase(sala_creada)
    return _mapear_sala(sala_creada)


@enrutador.get("/rooms/{sala_id}")
async def obtener_room(sala_id: UUID):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("rooms")
        .select("*")
        .eq("id", str(sala_id))
        .single()
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return _mapear_sala(respuesta.data)


@enrutador.patch("/rooms/{sala_id}")
async def actualizar_room(sala_id: UUID, cambios: SalaActualizar):
    cliente = obtener_cliente()
    datos = {
        campo: valor
        for campo, valor in cambios.model_dump(exclude_unset=True).items()
        if valor is not None
    }
    if not datos:
        raise HTTPException(status_code=400, detail="No se proporcionaron cambios")
    respuesta = cliente.table("rooms").update(datos).eq("id", str(sala_id)).execute()
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return _mapear_sala(respuesta.data[0])


@enrutador.delete("/rooms/{sala_id}")
async def eliminar_room(sala_id: UUID):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("rooms")
        .update({"activo": False})
        .eq("id", str(sala_id))
        .execute()
    )
    if not respuesta.data:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return _mapear_sala(respuesta.data[0])


@enrutador.get("/readings/latest/{sala_id}")
async def obtener_reading_reciente(sala_id: UUID, aire: Optional[str] = None):
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("registros")
        .select("*")
        .eq("sala_id", str(sala_id))
        .order("fecha_sync", desc=True)
        .limit(1)
        .execute()
    )
    if respuesta.data:
        return _mapear_registro_a_reading(respuesta.data[0])

    sala_respuesta = (
        cliente.table("rooms")
        .select("nombre,pabellon,edificio,aires")
        .eq("id", str(sala_id))
        .limit(1)
        .execute()
    )
    if sala_respuesta.data:
        sala = sala_respuesta.data[0]
        pabellon, aire_a_usar = _resolver_pabellon_y_aire(sala, aire)
        if pabellon and aire_a_usar:
            respuesta = (
                cliente.table("registros")
                .select("*")
                .eq("pabellon", pabellon)
                .eq("aire", aire_a_usar)
                .order("fecha_sync", desc=True)
                .limit(1)
                .execute()
            )
            if respuesta.data:
                registro = {**respuesta.data[0], "sala_id": str(sala_id)}
                return _mapear_registro_a_reading(registro)

    raise HTTPException(status_code=404, detail="No hay lecturas para esta sala")


@enrutador.get("/readings")
async def listar_readings(
    room_id: UUID,
    aire: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
):
    cliente = obtener_cliente()
    consulta = (
        cliente.table("registros")
        .select("*")
        .eq("sala_id", str(room_id))
        .order("fecha_sync", desc=False)
        .limit(limit)
    )
    if start:
        consulta = consulta.gte("fecha_sync", start.isoformat())
    if end:
        consulta = consulta.lte("fecha_sync", end.isoformat())
    respuesta = consulta.execute()
    if respuesta.data:
        return [_mapear_registro_a_reading(registro) for registro in respuesta.data]

    sala_respuesta = (
        cliente.table("rooms")
        .select("nombre,pabellon,edificio,aires")
        .eq("id", str(room_id))
        .limit(1)
        .execute()
    )
    if sala_respuesta.data:
        sala = sala_respuesta.data[0]
        pabellon, aire_a_usar = _resolver_pabellon_y_aire(sala, aire)
        if pabellon and aire_a_usar:
            consulta = (
                cliente.table("registros")
                .select("*")
                .eq("pabellon", pabellon)
                .eq("aire", aire_a_usar)
                .order("fecha_sync", desc=False)
                .limit(limit)
            )
            if start:
                consulta = consulta.gte("fecha_sync", start.isoformat())
            if end:
                consulta = consulta.lte("fecha_sync", end.isoformat())
            respuesta = consulta.execute()
            return [
                _mapear_registro_a_reading({**registro, "sala_id": str(room_id)})
                for registro in respuesta.data or []
            ]

    return [_mapear_registro_a_reading(registro) for registro in respuesta.data]


@enrutador.get("/alerts")
async def listar_alerts(
    is_resolved: Optional[bool] = None,
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    room_id: Optional[UUID] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    cliente = obtener_cliente()
    consulta = (
        cliente.table("alerts")
        .select("*")
        .order("creado_en", desc=True)
        .limit(limit)
    )
    if is_resolved is not None:
        consulta = consulta.eq("esta_resuelta", is_resolved)
    if severity is not None:
        consulta = consulta.eq("severidad", severity)
    if alert_type is not None:
        consulta = consulta.eq("tipo_alerta", alert_type)
    if room_id is not None:
        consulta = consulta.eq("sala_id", str(room_id))
    respuesta = consulta.execute()
    return [
        _mapear_alerta(alerta)
        for alerta in (respuesta.data or [])
        if not es_aire_ignorado((alerta.get("detalle") or {}).get("aire"))
    ]


@enrutador.get("/alerts/summary")
async def resumen_alerts():
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("alerts")
        .select("severidad,tipo_alerta,detalle")
        .eq("esta_resuelta", False)
        .execute()
    )
    filas = [
        alerta for alerta in (respuesta.data or [])
        if not es_aire_ignorado((alerta.get("detalle") or {}).get("aire"))
    ]
    return {
        "total_unresolved": len(filas),
        "total_sin_resolver": len(filas),
        "by_severity": {
            "high": sum(1 for fila in filas if fila.get("severidad") == "high"),
            "medium": sum(1 for fila in filas if fila.get("severidad") == "medium"),
            "low": sum(1 for fila in filas if fila.get("severidad") == "low"),
        },
        "by_type": {
            tipo: sum(1 for fila in filas if fila.get("tipo_alerta") == tipo)
            for tipo in {
                "node_offline",
                "power_anomaly",
                "temperature_stuck",
                "sensor_datos_invalidos",
                "temperatura_alta",
                "temperatura_fuera_rango",
                "humedad_alta",
                "humedad_invalida",
                "control_ir_inactivo",
                "aire_sin_datos",
            }
        },
    }


@enrutador.patch("/alerts/{alerta_id}/resolve")
async def resolver_alert_alias(alerta_id: int, _=Depends(requerir_mantenimiento_o_admin)):
    cliente = obtener_cliente()
    existente = (
        cliente.table("alerts").select("*").eq("id", alerta_id).single().execute()
    )
    if not existente.data:
        raise HTTPException(status_code=404, detail="Alerta no encontrada")
    if existente.data.get("esta_resuelta"):
        raise HTTPException(status_code=409, detail="La alerta ya esta resuelta")

    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = (
        cliente.table("alerts")
        .update({"esta_resuelta": True, "resuelto_en": ahora})
        .eq("id", alerta_id)
        .execute()
    )
    return _mapear_alerta(respuesta.data[0])


@enrutador.post("/alerts/run-checks")
async def ejecutar_checks_alerts_alias(
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
    return ServicioAlertas().verificar_alertas_atmos(
        pabellon=pabellon,
        aire=aire,
        diagnostico=seleccion.get("diagnostico"),
    )


@enrutador.get("/reports/summary/pavilion")
async def resumen_pabellon(period_days: int = 1):
    cliente = obtener_cliente()
    desde = datetime.now(timezone.utc).timestamp() - period_days * 24 * 60 * 60
    desde_iso = datetime.fromtimestamp(desde, tz=timezone.utc).isoformat()

    respuesta = (
        cliente.table("registros")
        .select(
            "pabellon,aire,fecha_sync,potencia_w,potencia_activa_w,"
            "consumo_intervalo_kwh,tarifa_kwh,costo_intervalo"
        )
        .gte("fecha_sync", desde_iso)
        .execute()
    )
    filas = respuesta.data
    resumen = _resumir_consumo_total_aires(filas)

    return {
        "total_energy_kwh": resumen["total_energy_kwh"],
        "total_cost_usd": resumen["total_cost_usd"],
        "total_savings_usd": 0,
        "avg_savings_pct": 0,
        "rooms_count": resumen["rooms_count"],
    }


@enrutador.get("/dashboard/energy")
async def dashboard_energia(range: Annotated[str, Query(alias="range")] = "24h"):
    clave_rango, config_rango = rango_dashboard(range)
    fin_local = ahora_panama()
    inicio_hoy_local = fin_local.replace(hour=0, minute=0, second=0, microsecond=0)
    dias_consulta = max(config_rango.dias, 30)
    inicio_rango_local = fin_local - timedelta(days=dias_consulta)
    inicio_semana_local = inicio_hoy_local - timedelta(days=7)
    desde_iso = min(inicio_rango_local, inicio_semana_local).astimezone(timezone.utc).isoformat()

    respuesta = (
        obtener_cliente()
        .table("registros")
        .select("*")
        .gte("fecha_sync", desde_iso)
        # El volumen puede superar 10,000 filas en 30 dias. Se solicitan las
        # mas recientes para que el limite no deje fuera las lecturas de hoy;
        # el servicio de dashboard las ordena cronologicamente despues.
        .order("fecha_sync", desc=True)
        .limit(10000)
        .execute()
    )
    filas = [
        fila
        for fila in (respuesta.data or [])
        if not es_aire_ignorado(fila.get("aire"))
    ]
    resumen = construir_resumen_dashboard(filas, clave_rango)
    resumen["phase2"]["activity"] = _obtener_actividad_sistema_dashboard()
    resumen["source"] = "registros"
    return resumen


def _obtener_actividad_sistema_dashboard() -> dict:
    cliente = obtener_cliente()
    desde_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    try:
        respuesta = (
            cliente.table("atmos_decision_events")
            .select("*")
            .gte("timestamp_utc", desde_iso)
            .order("timestamp_utc", desc=True)
            .limit(300)
            .execute()
        )
    except Exception as error:
        log.warning({
            "evento": "dashboard_decision_events_no_disponible",
            "error": str(error),
        })
        return {
            "events": [],
            "counts": [],
            "table_available": False,
            "empty_message": "Sin eventos registrados todavía",
        }

    eventos = respuesta.data or []
    conteos: dict[str, int] = {}
    for evento in eventos:
        tipo = evento.get("tipo") or "sin_tipo"
        conteos[tipo] = conteos.get(tipo, 0) + 1

    return {
        "events": [
            {
                "id": evento.get("id"),
                "timestamp_utc": evento.get("timestamp_utc"),
                "tipo": evento.get("tipo"),
                "motivo": evento.get("motivo"),
                "pabellon": evento.get("pabellon"),
                "aire": evento.get("aire"),
                "decision_final": evento.get("decision_final"),
            }
            for evento in eventos[:10]
        ],
        "counts": [
            {"tipo": tipo, "count": cantidad}
            for tipo, cantidad in sorted(conteos.items(), key=lambda item: item[1], reverse=True)
        ],
        "table_available": True,
        "empty_message": "Sin eventos registrados todavía",
    }


@enrutador.get("/ac-commands")
async def listar_ac_commands(
    room_id: Optional[UUID] = None,
    sala_id: Optional[UUID] = None,
    only_pending: Optional[bool] = None,
    solo_pendientes: Optional[bool] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    id_sala = sala_id or room_id

    cliente = obtener_cliente()
    consulta = (
        cliente.table("ac_commands")
        .select("*")
        .order("enviado_en", desc=True)
        .limit(limit)
    )
    if id_sala:
        consulta = consulta.eq("sala_id", str(id_sala))
    if only_pending is True or solo_pendientes is True:
        consulta = consulta.eq("fue_ejecutado", False)

    respuesta = consulta.execute()
    return [_mapear_comando(comando) for comando in respuesta.data or []]


@enrutador.post("/ac-commands", status_code=201)
async def crear_ac_command(request: Request):
    if not control_manual_ir_habilitado():
        raise _control_ir_inactivo_http()
    raw_payload = await request.json()
    payload = _normalizar_payload_comando(raw_payload)
    if control_solo_manual() and payload.get("origen") != "manual":
        raise HTTPException(status_code=403, detail="Solo se permiten comandos manuales explícitos.")
    if not payload.get("sala_id") or not payload.get("tipo_comando"):
        raise HTTPException(status_code=422, detail="Faltan room_id o command_type")
    if payload["tipo_comando"] == "setpoint" and payload.get("setpoint") is None:
        raise HTTPException(status_code=422, detail="setpoint es obligatorio")

    try:
        sobre_comando = _preparar_sobre_comando_manual(payload, raw_payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    # Firebase es el canal operativo consumido por el ESP32. Se escribe antes
    # del registro auxiliar y nunca depende de que Supabase esté disponible.
    resultado_firebase = _escribir_accion_manual_en_firebase(
        payload=payload,
        sobre_comando=sobre_comando,
    )
    if not resultado_firebase["firebase_escrito"]:
        raise HTTPException(
            status_code=502,
            detail={
                "estado": "firebase_no_escrito",
                "mensaje": "Firebase rechazó el comando; no se envió ninguna orden al ESP32.",
                "motivo": resultado_firebase["error_firebase"],
                "ruta": resultado_firebase["ruta_firebase"],
            },
        )

    datos = {
        **payload,
        **sobre_comando,
        "fue_ejecutado": False,
        "ejecutado_en": None,
    }
    comando_mapeado = _mapear_comando(datos)
    supabase_guardado = False
    advertencia_supabase = None
    try:
        cliente = obtener_cliente()
        respuesta = cliente.table("ac_commands").insert(datos).execute()
        if respuesta.data:
            comando_mapeado = _mapear_comando(respuesta.data[0])
            supabase_guardado = True
        else:
            advertencia_supabase = "Supabase no devolvió el registro insertado."
    except Exception as error:
        advertencia_supabase = str(error)
        log.warning({
            "evento": "comando_manual_supabase_no_guardado",
            "command_id": sobre_comando["command_id"],
            "error": str(error),
        })
    log.info({
        "evento": "comando_manual_guardado",
        "sala_id": str(payload["sala_id"]),
        "tipo_comando": payload["tipo_comando"],
        "setpoint": payload.get("setpoint"),
        "supabase_guardado": supabase_guardado,
        "firebase_escrito": resultado_firebase["firebase_escrito"],
        "accion_firebase": resultado_firebase["accion_firebase"],
    })
    return {
        **comando_mapeado,
        "command_saved_in_supabase": supabase_guardado,
        "supabase_guardado": supabase_guardado,
        "advertencia_supabase": advertencia_supabase,
        **resultado_firebase,
    }


@enrutador.post("/ac-commands/from-prediction/{prediccion_id}", status_code=201)
async def ac_command_from_prediction(prediccion_id: int):
    comando = await comando_desde_prediccion(prediccion_id)
    return _mapear_comando(comando)


@enrutador.post("/reports/energy")
async def reporte_energia(carga: dict | None = None):
    cliente = obtener_cliente()

    # Obtener nombres de salones por pabellon
    salas_resp = cliente.table("rooms").select("nombre,pabellon").execute()
    pabellon_a_nombre = {
        r["pabellon"]: r["nombre"]
        for r in (salas_resp.data or [])
        if r.get("pabellon") and r.get("nombre")
    }

    query = cliente.table("registros").select("*")
    period = (carga or {}).get("period", {})
    if period.get("start"):
        query = query.gte("fecha_sync", period["start"])
    if period.get("end"):
        query = query.lte("fecha_sync", period["end"])
    respuesta = query.order("fecha_sync", desc=True).limit(5000).execute()
    registros = respuesta.data or []
    por_salon: dict[tuple[str, str], list[dict]] = {}
    for registro in registros:
        clave = (registro.get("pabellon") or "", registro.get("aire") or "")
        por_salon.setdefault(clave, []).append(registro)

    rooms = []
    for (pabellon, aire), filas in por_salon.items():
        energias = [f["energia_kwh"] for f in filas if f.get("energia_kwh") is not None]
        potencias = [f["potencia_w"] for f in filas if f.get("potencia_w") is not None]
        energia = (
            max(energias) - min(energias)
            if len(energias) >= 2
            else (sum(potencias) / len(potencias) / 1000 if potencias else 0)
        )
        nombre_salon = pabellon_a_nombre.get(pabellon, pabellon)
        rooms.append({
            "room_name": f"{nombre_salon} — {aire}",
            "salon": nombre_salon,
            "aire": aire,
            "pavilion": pabellon,
            "total_energy_kwh": round(energia, 6),
            "total_cost": round(energia * TARIFA_KWH, 6),
            "savings_pct": None,
            "savings_cost": 0,
            "recommendations": [],
        })

    generado_en = datetime.now(timezone.utc).isoformat()

    if carga and carga.get("format") == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "fecha",
                "salon",
                "aire",
                "temperatura_c",
                "temperatura_salida_aire_c",
                "humedad_pct",
                "delta_t_c",
                "movimiento",
                "estado_ocupacion",
                "corriente_rms_a",
                "corriente_rms_cruda_a",
                "corriente_rms_instantanea_a",
                "corriente_calculada_vpp",
                "factor_calibracion_sct",
                "ceros_consecutivos_sct",
                "voltaje_red_v",
                "factor_potencia",
                "potencia_aparente_va",
                "potencia_activa_w",
                "potencia_activa_kw",
                "potencia_w",
                "consumo_intervalo_kwh",
                "consumo_acumulado_sesion_kwh",
                "energia_kwh",
                "tarifa_kwh",
                "costo_intervalo",
                "costo_acumulado_sesion",
                "ac_encendido",
                "estado_electrico_observado",
                "compresor_confirmado",
                "dht_ok",
                "ds18b20_ok",
                "corriente_retenida_por_filtro",
                "fallos_dht",
                "fallos_ds18b20",
                "control_ir_activo",
                "recomendacion_local",
                "ultima_accion_ejecutada",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        for registro in registros:
            pabellon = registro.get("pabellon") or ""
            aire = registro.get("aire") or ""
            nombre_salon = pabellon_a_nombre.get(pabellon, pabellon)
            writer.writerow({
                "fecha":          registro.get("fecha_sync", ""),
                "salon":          nombre_salon,
                "aire":           aire,
                "temperatura_c":  registro.get("temperatura_ambiente", ""),
                "temperatura_salida_aire_c": registro.get("temperatura_salida_aire", ""),
                "humedad_pct":    registro.get("humedad", ""),
                "delta_t_c":      registro.get("delta_t", ""),
                "movimiento":     _valor_movimiento_reporte(registro),
                "estado_ocupacion": registro.get("estado_ocupacion", ""),
                "corriente_rms_a": registro.get("corriente_rms", ""),
                "corriente_rms_cruda_a": registro.get("corriente_rms_cruda", ""),
                "corriente_rms_instantanea_a": registro.get("corriente_rms_instantanea", ""),
                "corriente_calculada_vpp": registro.get("corriente_calculada_vpp", ""),
                "factor_calibracion_sct": registro.get("factor_calibracion_sct", ""),
                "ceros_consecutivos_sct": registro.get("ceros_consecutivos_sct", ""),
                "voltaje_red_v": registro.get("voltaje_red_v", ""),
                "factor_potencia": registro.get("factor_potencia", ""),
                "potencia_aparente_va": registro.get("potencia_aparente_va", ""),
                "potencia_activa_w": registro.get("potencia_activa_w", ""),
                "potencia_activa_kw": registro.get("potencia_activa_kw", ""),
                "potencia_w":     registro.get("potencia_w", ""),
                "consumo_intervalo_kwh": registro.get("consumo_intervalo_kwh", ""),
                "consumo_acumulado_sesion_kwh": registro.get("consumo_acumulado_sesion_kwh", ""),
                "energia_kwh":    registro.get("energia_kwh", ""),
                "tarifa_kwh": registro.get("tarifa_kwh", ""),
                "costo_intervalo": registro.get("costo_intervalo", ""),
                "costo_acumulado_sesion": registro.get("costo_acumulado_sesion", ""),
                "ac_encendido":   _valor_ac_encendido_reporte(registro),
                "estado_electrico_observado": registro.get("estado_electrico_observado", ""),
                "compresor_confirmado": registro.get("compresor_confirmado", ""),
                "dht_ok": registro.get("dht_ok", ""),
                "ds18b20_ok": registro.get("ds18b20_ok", ""),
                "corriente_retenida_por_filtro": registro.get("corriente_retenida_por_filtro", ""),
                "fallos_dht": registro.get("fallos_dht", ""),
                "fallos_ds18b20": registro.get("fallos_ds18b20", ""),
                "control_ir_activo": registro.get("control_ir_activo", ""),
                "recomendacion_local": registro.get("recomendacion_local", ""),
                "ultima_accion_ejecutada": (
                    registro.get("ultima_accion_ejecutada")
                    or registro.get("recomendacion_local", "")
                ),
            })
        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=reporte_atmos.csv"},
        )

    return {"type": "energy", "rooms": rooms, "generated_at": generado_en}


@enrutador.get("/reports/energy")
async def reporte_energia_get():
    return await reporte_energia()


@enrutador.post("/reports/room/{sala_id}")
async def reporte_room(sala_id: UUID, carga: dict | None = None):
    room = await obtener_room(sala_id)
    pabellon, aire = _resolver_pabellon_y_aire(room)
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("registros")
        .select("*")
        .eq("pabellon", pabellon)
        .eq("aire", aire)
        .order("fecha_sync", desc=True)
        .limit(500)
        .execute()
    )
    return {"room": room, "readings": respuesta.data or []}


@enrutador.get("/reports/compare")
async def comparar_room(room_id: UUID, period_days: int = 30):
    room = await obtener_room(room_id)
    pabellon, aire = _resolver_pabellon_y_aire(room)
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("registros")
        .select("energia_kwh,potencia_w,fecha_sync")
        .eq("pabellon", pabellon)
        .eq("aire", aire)
        .order("fecha_sync", desc=True)
        .limit(500)
        .execute()
    )
    filas = respuesta.data or []
    energias = [f["energia_kwh"] for f in filas if f.get("energia_kwh") is not None]
    energia = max(energias) - min(energias) if len(energias) >= 2 else 0
    return {
        "room_id": str(room_id),
        "total_energy_kwh": energia,
        "baseline_energy_kwh": energia,
        "energy_change_pct": 0,
        "cost_change_usd": 0,
        "period_days": period_days,
    }


@enrutador.get("/reports/history")
async def historial_reports(limit: Annotated[int, Query(ge=1, le=100)] = 10):
    return []
