from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.core.database import obtener_cliente
from app.core.logger import log


# ---------------------------------------------------------------------------
# Función auxiliar — usada por el router de registros Firebase
# ---------------------------------------------------------------------------

def agregar_lecturas(registros: list[dict[str, Any]]) -> dict[str, Any]:
    if not registros:
        return {"cantidad": 0, "temperatura_promedio": None, "humedad_promedio": None}

    temperaturas = [r["temperatura_dht11"] for r in registros if r.get("temperatura_dht11") is not None]
    humedades = [r["humedad"] for r in registros if r.get("humedad") is not None]
    con_movimiento = sum(1 for r in registros if r.get("movimiento"))

    return {
        "cantidad": len(registros),
        "temperatura_promedio": sum(temperaturas) / len(temperaturas) if temperaturas else None,
        "temperatura_minima": min(temperaturas) if temperaturas else None,
        "temperatura_maxima": max(temperaturas) if temperaturas else None,
        "humedad_promedio": sum(humedades) / len(humedades) if humedades else None,
        "registros_con_movimiento": con_movimiento,
    }


# ---------------------------------------------------------------------------
# Servicio de agregación por hora
# ---------------------------------------------------------------------------

class ServicioAgregacion:

    def agregar_ultima_hora(self, sala_id: UUID) -> dict | None:
        ahora_utc = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        cubo_hora = ahora_utc - timedelta(hours=1)
        fin_cubo = cubo_hora + timedelta(hours=1)

        cliente = obtener_cliente()
        respuesta = (
            cliente.table("sensor_readings")
            .select("*")
            .eq("sala_id", str(sala_id))
            .gte("registrado_en", cubo_hora.isoformat())
            .lt("registrado_en", fin_cubo.isoformat())
            .execute()
        )
        filas: list[dict] = respuesta.data

        if not filas:
            log.warning({
                "evento":  "agregacion_omitida",
                "sala_id": str(sala_id),
                "razon":   "sin_datos",
            })
            return None

        # Temperatura
        temperaturas = [f["temperatura"] for f in filas if f.get("temperatura") is not None]
        temperatura_promedio = sum(temperaturas) / len(temperaturas) if temperaturas else None
        temperatura_minima = min(temperaturas) if temperaturas else None
        temperatura_maxima = max(temperaturas) if temperaturas else None

        # Humedad
        humedades = [f["humedad"] for f in filas if f.get("humedad") is not None]
        humedad_promedio = sum(humedades) / len(humedades) if humedades else None

        # Presencia: proporción de filas con presencia=True
        total_filas = len(filas)
        filas_con_presencia = sum(1 for f in filas if f.get("presencia") is True)
        razon_presencia = filas_con_presencia / total_filas

        # Potencia
        potencias = [f["potencia_w"] for f in filas if f.get("potencia_w") is not None]
        potencia_promedio_w = sum(potencias) / len(potencias) if potencias else None

        # Energía acumulada (contador incremental en el ESP32)
        energias = [f["energia_kwh"] for f in filas if f.get("energia_kwh") is not None]
        energia_total_kwh = (max(energias) - min(energias)) if len(energias) >= 2 else None

        agregado = {
            "sala_id": str(sala_id),
            "cubo_hora": cubo_hora.isoformat(),
            "temperatura_promedio": temperatura_promedio,
            "temperatura_minima": temperatura_minima,
            "temperatura_maxima": temperatura_maxima,
            "humedad_promedio": humedad_promedio,
            "razon_presencia": razon_presencia,
            "potencia_promedio_w": potencia_promedio_w,
            "energia_total_kwh": energia_total_kwh,
            "cantidad_lecturas": total_filas,
            "dia_semana": cubo_hora.weekday(),   # 0=lunes, 6=domingo
            "hora_del_dia": cubo_hora.hour,       # 0–23
        }

        resultado = (
            cliente.table("hourly_aggregates")
            .upsert(agregado, on_conflict="sala_id,cubo_hora")
            .execute()
        )
        log.info({
            "evento":           "agregacion_completada",
            "sala_id":          str(sala_id),
            "cantidad_lecturas": total_filas,
        })
        return resultado.data[0] if resultado.data else agregado

    def agregar_todas_las_salas(self) -> dict:
        cliente = obtener_cliente()

        # Salas con al menos un nodo activo
        respuesta = (
            cliente.table("nodes")
            .select("sala_id")
            .eq("esta_activo", True)
            .execute()
        )
        salas_ids: list[UUID] = list({
            UUID(fila["sala_id"]) for fila in respuesta.data if fila.get("sala_id")
        })

        procesadas = 0
        omitidas = 0
        errores: list[str] = []

        for sala_id in salas_ids:
            try:
                resultado = self.agregar_ultima_hora(sala_id)
                if resultado is None:
                    omitidas += 1
                else:
                    procesadas += 1
            except Exception as error:
                errores.append(f"{sala_id}: {error}")

        return {
            "procesadas": procesadas,
            "omitidas": omitidas,
            "errores": errores,
        }
