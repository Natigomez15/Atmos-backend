from datetime import datetime, timezone
from typing import Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.database import obtener_cliente
from app.models.schemas import SalaActualizar, SalaCrear


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
async def obtener_reading_reciente(sala_id: UUID):
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
        .select("nombre,pabellon,edificio")
        .eq("id", str(sala_id))
        .limit(1)
        .execute()
    )
    if sala_respuesta.data:
        sala = sala_respuesta.data[0]
        pabellon = sala.get("pabellon") or sala.get("edificio")
        aire = sala.get("nombre")
        if pabellon and aire:
            respuesta = (
                cliente.table("registros")
                .select("*")
                .eq("pabellon", pabellon)
                .eq("aire", aire)
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
    return [_mapear_registro_a_reading(registro) for registro in respuesta.data]


@enrutador.get("/alerts")
async def listar_alerts(
    is_resolved: Optional[bool] = None,
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
    respuesta = consulta.execute()
    return respuesta.data


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
    }


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

    return {
        "total_energy_kwh": total_energy_kwh,
        "total_savings_usd": 0,
        "avg_savings_pct": 0,
        "rooms_count": 0,
    }
