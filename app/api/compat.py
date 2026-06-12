from datetime import datetime, timezone
from typing import Annotated, Optional
from uuid import UUID

import csv
import io
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.config import configuracion
from app.core.database import obtener_cliente
from app.core.security import requerir_mantenimiento_o_admin
from app.ml.impacto import TARIFA_KWH
from app.models.schemas import SalaActualizar, SalaCrear
from app.api.ac_commands import comando_desde_prediccion
from app.services.alert_service import ServicioAlertas
from app.services.sincronizador_firebase import leer_ultima_lectura_valida_firebase_rest


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


@enrutador.get("/rooms")
async def listar_rooms():
    try:
        cliente = obtener_cliente()
        respuesta = cliente.table("rooms").select("*").order("nombre", desc=False).execute()
        return [_mapear_sala(sala) for sala in respuesta.data or []]
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo leer rooms desde Supabase: {error}",
        )


@enrutador.post("/rooms", status_code=201)
async def crear_room(sala: SalaCrear):
    cliente = obtener_cliente()
    respuesta = cliente.table("rooms").insert(sala.model_dump()).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al insertar la sala")
    return _mapear_sala(respuesta.data[0])


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
        for campo, valor in cambios.model_dump().items()
        if valor is not None
    }
    if not datos:
        raise HTTPException(status_code=400, detail="No se proporcionaron cambios")
    respuesta = cliente.table("rooms").update(datos).eq("id", str(sala_id)).execute()
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
    return [_mapear_alerta(alerta) for alerta in respuesta.data]


@enrutador.get("/alerts/summary")
async def resumen_alerts():
    cliente = obtener_cliente()
    respuesta = (
        cliente.table("alerts")
        .select("severidad,tipo_alerta")
        .eq("esta_resuelta", False)
        .execute()
    )
    filas = respuesta.data
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
        .select("energia_kwh,potencia_w,fecha_sync")
        .gte("fecha_sync", desde_iso)
        .execute()
    )
    filas = respuesta.data
    energias = [fila["energia_kwh"] for fila in filas if fila.get("energia_kwh") is not None]
    potencias = [fila["potencia_w"] for fila in filas if fila.get("potencia_w") is not None]

    total_energy_kwh = (
        max(energias) - min(energias)
        if len(energias) >= 2
        else (sum(potencias) / len(potencias) / 1000 * 24 if potencias else 0)
    )
    total_cost_usd = total_energy_kwh * TARIFA_KWH

    return {
        "total_energy_kwh": total_energy_kwh,
        "total_cost_usd": total_cost_usd,
        "total_savings_usd": 0,
        "avg_savings_pct": 0,
        "rooms_count": 0,
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
    if not id_sala:
        return []

    cliente = obtener_cliente()
    consulta = (
        cliente.table("ac_commands")
        .select("*")
        .eq("sala_id", str(id_sala))
        .order("enviado_en", desc=True)
        .limit(limit)
    )
    if only_pending is True or solo_pendientes is True:
        consulta = consulta.eq("fue_ejecutado", False)

    respuesta = consulta.execute()
    return [_mapear_comando(comando) for comando in respuesta.data or []]


@enrutador.post("/ac-commands", status_code=201)
async def crear_ac_command(request: Request):
    payload = _normalizar_payload_comando(await request.json())
    if not payload.get("sala_id") or not payload.get("tipo_comando"):
        raise HTTPException(status_code=422, detail="Faltan room_id o command_type")
    if payload["tipo_comando"] == "setpoint" and payload.get("setpoint") is None:
        raise HTTPException(status_code=422, detail="setpoint es obligatorio")

    datos = {
        **payload,
        "fue_ejecutado": False,
        "ejecutado_en": None,
    }
    cliente = obtener_cliente()
    respuesta = cliente.table("ac_commands").insert(datos).execute()
    if not respuesta.data:
        raise HTTPException(status_code=400, detail="Error al crear comando")
    return _mapear_comando(respuesta.data[0])


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

    respuesta = (
        cliente.table("registros")
        .select("*")
        .order("fecha_sync", desc=True)
        .limit(1000)
        .execute()
    )
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
            fieldnames=["salon", "aire", "total_energy_kwh", "total_cost"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rooms)
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
