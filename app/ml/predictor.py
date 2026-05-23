from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.core.database import obtener_cliente


class ServicioPredictor:

    # -----------------------------------------------------------------------
    # Características de entrenamiento
    # -----------------------------------------------------------------------

    def obtener_caracteristicas_entrenamiento(
        self, sala_id: UUID, dias_atras: int = 30
    ) -> list[dict]:
        cliente = obtener_cliente()
        desde = (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()

        respuesta = (
            cliente.table("hourly_aggregates")
            .select("*")
            .eq("sala_id", str(sala_id))
            .gte("cubo_hora", desde)
            .gt("cantidad_lecturas", 0)
            .order("cubo_hora", desc=False)
            .execute()
        )

        caracteristicas: list[dict] = []
        for fila in respuesta.data:
            caracteristicas.append({
                "cubo_hora":           fila["cubo_hora"],
                "temperatura_promedio": fila.get("temperatura_promedio") or 0.0,
                "humedad_promedio":     fila.get("humedad_promedio") or 0.0,
                "razon_presencia":      fila.get("razon_presencia") or 0.0,
                "potencia_promedio_w":  fila.get("potencia_promedio_w") or 0.0,
                "energia_total_kwh":    fila.get("energia_total_kwh") or 0.0,
                "dia_semana":           fila.get("dia_semana") or 0,
                "hora_del_dia":         fila.get("hora_del_dia") or 0,
                "cantidad_lecturas":    fila.get("cantidad_lecturas") or 0,
            })

        return caracteristicas

    # -----------------------------------------------------------------------
    # Guardar predicción
    # -----------------------------------------------------------------------

    def guardar_prediccion(self, sala_id: UUID, carga: dict) -> dict:
        setpoint_recomendado = carga["setpoint_recomendado"]
        if not (16 <= setpoint_recomendado <= 30):
            raise ValueError(
                f"setpoint_recomendado debe estar entre 16 y 30 °C, "
                f"recibido: {setpoint_recomendado}"
            )

        cliente = obtener_cliente()
        registro = {
            "sala_id":                  str(sala_id),
            "predicho_en":              datetime.now(timezone.utc).isoformat(),
            "setpoint_recomendado":     setpoint_recomendado,
            "ahorro_predicho_pct":      carga["ahorro_predicho_pct"],
            "puntaje_confianza":        carga["puntaje_confianza"],
            "version_modelo":           carga["version_modelo"],
            "instantanea_caracteristicas": carga["instantanea_caracteristicas"],
            "fue_aplicado":             False,
            "ahorro_real_pct":          None,
        }

        respuesta = cliente.table("ml_predictions").insert(registro).execute()
        return respuesta.data[0]

    # -----------------------------------------------------------------------
    # Evaluar predicciones pasadas (cierre del ciclo de retroalimentación)
    # -----------------------------------------------------------------------

    def evaluar_predicciones_pasadas(self, sala_id: UUID) -> list[dict]:
        cliente = obtener_cliente()
        limite_tiempo = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()

        # Predicciones aplicadas sin ahorro real calculado aún
        respuesta = (
            cliente.table("ml_predictions")
            .select("*")
            .eq("sala_id", str(sala_id))
            .eq("fue_aplicado", True)
            .is_("ahorro_real_pct", "null")
            .lte("predicho_en", limite_tiempo)
            .execute()
        )

        resultados: list[dict] = []

        for prediccion in respuesta.data:
            prediccion_id = prediccion["id"]
            predicho_en = datetime.fromisoformat(prediccion["predicho_en"])

            # Hora real (la hora siguiente a la predicción)
            hora_real = predicho_en.replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=1)

            # Línea base: misma hora, mismo día de semana, hace 7 días
            hora_base = hora_real - timedelta(days=7)

            def _buscar_agregado(cubo: datetime) -> dict | None:
                r = (
                    cliente.table("hourly_aggregates")
                    .select("potencia_promedio_w")
                    .eq("sala_id", str(sala_id))
                    .eq("cubo_hora", cubo.isoformat())
                    .limit(1)
                    .execute()
                )
                return r.data[0] if r.data else None

            datos_base = _buscar_agregado(hora_base)
            datos_real = _buscar_agregado(hora_real)

            if not datos_base or not datos_real:
                continue

            potencia_base = datos_base.get("potencia_promedio_w")
            potencia_real = datos_real.get("potencia_promedio_w")

            if potencia_base is None or potencia_real is None or potencia_base == 0:
                continue

            ahorro_real = ((potencia_base - potencia_real) / potencia_base) * 100

            # Actualizar en base de datos
            cliente.table("ml_predictions").update(
                {"ahorro_real_pct": ahorro_real}
            ).eq("id", prediccion_id).execute()

            resultados.append({
                "id_prediccion":       prediccion_id,
                "ahorro_predicho_pct": prediccion["ahorro_predicho_pct"],
                "ahorro_real_pct":     ahorro_real,
                "error_pct":           abs(prediccion["ahorro_predicho_pct"] - ahorro_real),
            })

        return resultados
