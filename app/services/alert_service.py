import asyncio
from datetime import datetime, timedelta, timezone

from app.core.database import obtener_cliente
from app.core.logger import log
from app.core.websocket_manager import gestor


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class ServicioAlertas:

    # -----------------------------------------------------------------------
    # Helpers internos
    # -----------------------------------------------------------------------

    def _alerta_sin_resolver_existe(
        self,
        cliente,
        tipo_alerta: str,
        sala_id: str | None = None,
        nodo_id: str | None = None,
    ) -> bool:
        consulta = (
            cliente.table("alerts")
            .select("id")
            .eq("tipo_alerta", tipo_alerta)
            .eq("esta_resuelta", False)
        )
        if sala_id:
            consulta = consulta.eq("sala_id", sala_id)
        if nodo_id:
            consulta = consulta.eq("nodo_id", nodo_id)
        return bool(consulta.execute().data)

    def _insertar_alerta(self, cliente, alerta: dict) -> dict:
        respuesta = cliente.table("alerts").insert(alerta).execute()
        fila = respuesta.data[0]
        log.warning({
            "evento":     "alerta_creada",
            "tipo":       alerta.get("tipo_alerta"),
            "severidad":  alerta.get("severidad"),
            "sala_id":    str(alerta.get("sala_id", "")),
        })
        # Broadcast a todos los clientes WebSocket conectados
        asyncio.create_task(gestor.transmitir_a_todos({
            "tipo":        "nueva_alerta",
            "tipo_alerta": alerta.get("tipo_alerta"),
            "severidad":   alerta.get("severidad"),
            "sala_id":     str(alerta.get("sala_id", "")),
            "mensaje":     alerta.get("mensaje"),
        }))
        return fila

    def _resolver_alertas(self, cliente, ids: list[int]) -> None:
        if not ids:
            return
        ahora = _iso(_ahora())
        cliente.table("alerts").update(
            {"esta_resuelta": True, "resuelto_en": ahora}
        ).in_("id", ids).execute()

    # -----------------------------------------------------------------------
    # Verificar nodos sin conexión
    # -----------------------------------------------------------------------

    def verificar_nodos_desconectados(self) -> list[dict]:
        cliente = obtener_cliente()
        hace_10_min = _iso(_ahora() - timedelta(minutes=10))

        resp = cliente.table("nodes").select("*").eq("esta_activo", True).execute()
        nodos_offline = [
            n for n in resp.data
            if n.get("ultima_vez_visto") is None
            or n["ultima_vez_visto"] <= hace_10_min
        ]

        alertas_creadas: list[dict] = []
        for nodo in nodos_offline:
            if self._alerta_sin_resolver_existe(
                cliente, "node_offline", nodo_id=str(nodo["id"])
            ):
                continue

            mensaje_alerta = (
                f"El nodo {nodo['direccion_mac']} no ha enviado datos "
                "en más de 10 minutos"
            )
            detalle_alerta = {
                "ultima_vez_visto": nodo.get("ultima_vez_visto"),
                "version_firmware": nodo.get("version_firmware"),
            }
            alerta = self._insertar_alerta(cliente, {
                "sala_id":     nodo["sala_id"],
                "nodo_id":     nodo["id"],
                "tipo_alerta": "node_offline",
                "severidad":   "high",
                "mensaje":     mensaje_alerta,
                "detalle":     detalle_alerta,
            })
            notificar_alerta(
                tipo_alerta="node_offline",
                severidad="high",
                mensaje=mensaje_alerta,
                sala_id=str(nodo["sala_id"]),
                detalle=detalle_alerta,
            )
            alertas_creadas.append(alerta)

        return alertas_creadas

    # -----------------------------------------------------------------------
    # Verificar anomalías de consumo eléctrico
    # -----------------------------------------------------------------------

    def verificar_anomalias_potencia(self) -> list[dict]:
        cliente = obtener_cliente()
        ahora = _ahora()
        hace_30_dias = _iso(ahora - timedelta(days=30))
        hace_15_min = _iso(ahora - timedelta(minutes=15))

        # Salas con nodos activos
        resp_nodos = (
            cliente.table("nodes").select("sala_id").eq("esta_activo", True).execute()
        )
        salas_ids = list({n["sala_id"] for n in resp_nodos.data if n.get("sala_id")})

        alertas_creadas: list[dict] = []
        dia_semana = ahora.weekday()
        hora = ahora.hour

        for sala_id in salas_ids:
            # Línea base histórica: mismo día y hora, últimos 30 días
            resp_base = (
                cliente.table("hourly_aggregates")
                .select("potencia_promedio_w")
                .eq("sala_id", sala_id)
                .eq("dia_semana", dia_semana)
                .eq("hora_del_dia", hora)
                .gte("cubo_hora", hace_30_dias)
                .execute()
            )
            valores_base = [
                f["potencia_promedio_w"]
                for f in resp_base.data
                if f.get("potencia_promedio_w") is not None
            ]
            if not valores_base:
                continue
            potencia_base = sum(valores_base) / len(valores_base)
            if potencia_base == 0:
                continue

            # Consumo actual: promedio últimos 15 minutos
            resp_actual = (
                cliente.table("sensor_readings")
                .select("potencia_w")
                .eq("sala_id", sala_id)
                .gte("registrado_en", hace_15_min)
                .execute()
            )
            valores_actuales = [
                f["potencia_w"]
                for f in resp_actual.data
                if f.get("potencia_w") is not None
            ]
            if not valores_actuales:
                continue
            potencia_actual = sum(valores_actuales) / len(valores_actuales)

            if potencia_actual <= potencia_base * 1.5:
                continue

            if self._alerta_sin_resolver_existe(
                cliente, "power_anomaly", sala_id=sala_id
            ):
                continue

            exceso_pct = ((potencia_actual - potencia_base) / potencia_base) * 100

            # Obtener nombre de sala para el mensaje
            resp_sala = (
                cliente.table("rooms").select("nombre").eq("id", sala_id).single().execute()
            )
            nombre_sala = resp_sala.data["nombre"] if resp_sala.data else sala_id

            mensaje_alerta = (
                f"La sala {nombre_sala} consume {exceso_pct:.1f}% "
                "por encima de su promedio histórico"
            )
            detalle_alerta = {
                "potencia_actual_w": round(potencia_actual, 2),
                "potencia_base_w":   round(potencia_base, 2),
                "exceso_pct":        round(exceso_pct, 2),
            }
            alerta = self._insertar_alerta(cliente, {
                "sala_id":     sala_id,
                "nodo_id":     None,
                "tipo_alerta": "power_anomaly",
                "severidad":   "medium",
                "mensaje":     mensaje_alerta,
                "detalle":     detalle_alerta,
            })
            notificar_alerta(
                tipo_alerta="power_anomaly",
                severidad="medium",
                mensaje=mensaje_alerta,
                sala_id=str(sala_id),
                detalle=detalle_alerta,
            )
            alertas_creadas.append(alerta)

        return alertas_creadas

    # -----------------------------------------------------------------------
    # Verificar temperatura estancada
    # -----------------------------------------------------------------------

    def verificar_temperatura_estancada(self) -> list[dict]:
        cliente = obtener_cliente()
        hace_30_min = _iso(_ahora() - timedelta(minutes=30))

        resp_nodos = (
            cliente.table("nodes").select("sala_id").eq("esta_activo", True).execute()
        )
        salas_ids = list({n["sala_id"] for n in resp_nodos.data if n.get("sala_id")})

        alertas_creadas: list[dict] = []

        for sala_id in salas_ids:
            resp = (
                cliente.table("sensor_readings")
                .select("temperatura, presencia, ac_encendido, setpoint_ac, registrado_en")
                .eq("sala_id", sala_id)
                .eq("ac_encendido", True)
                .gte("registrado_en", hace_30_min)
                .order("registrado_en", desc=False)
                .execute()
            )
            filas = resp.data

            if len(filas) < 10:
                continue

            # Presencia > 50%
            razon_presencia = sum(
                1 for f in filas if f.get("presencia")
            ) / len(filas)
            if razon_presencia <= 0.5:
                continue

            temperaturas = [
                f["temperatura"] for f in filas if f.get("temperatura") is not None
            ]
            if not temperaturas or min(temperaturas) <= 26.0:
                continue

            # Tendencia: promedio primeras 10 vs últimas 10 lecturas
            promedio_inicial = sum(temperaturas[:10]) / 10
            promedio_final = sum(temperaturas[-10:]) / 10
            if promedio_final < promedio_inicial:
                continue

            if self._alerta_sin_resolver_existe(
                cliente, "temperature_stuck", sala_id=sala_id
            ):
                continue

            setpoint = next(
                (f["setpoint_ac"] for f in reversed(filas) if f.get("setpoint_ac")),
                None,
            )
            resp_sala = (
                cliente.table("rooms").select("nombre").eq("id", sala_id).single().execute()
            )
            nombre_sala = resp_sala.data["nombre"] if resp_sala.data else sala_id

            mensaje_alerta = (
                f"El AC de la sala {nombre_sala} está encendido "
                "pero la temperatura no baja"
            )
            detalle_alerta = {
                "temperatura_promedio": round(
                    sum(temperaturas) / len(temperaturas), 2
                ),
                "setpoint_ac":     setpoint,
                "ventana_minutos": 30,
                "razon_presencia": round(razon_presencia, 2),
            }
            alerta = self._insertar_alerta(cliente, {
                "sala_id":     sala_id,
                "nodo_id":     None,
                "tipo_alerta": "temperature_stuck",
                "severidad":   "medium",
                "mensaje":     mensaje_alerta,
                "detalle":     detalle_alerta,
            })
            notificar_alerta(
                tipo_alerta="temperature_stuck",
                severidad="medium",
                mensaje=mensaje_alerta,
                sala_id=str(sala_id),
                detalle=detalle_alerta,
            )
            alertas_creadas.append(alerta)

        return alertas_creadas

    # -----------------------------------------------------------------------
    # Resolver alertas obsoletas
    # -----------------------------------------------------------------------

    def resolver_alertas_obsoletas(self) -> int:
        cliente = obtener_cliente()
        ahora = _ahora()
        hace_10_min = _iso(ahora - timedelta(minutes=10))
        hace_15_min = _iso(ahora - timedelta(minutes=15))
        total_resueltas = 0

        # --- node_offline: el nodo volvió a conectarse ---
        resp = (
            cliente.table("alerts")
            .select("id, nodo_id")
            .eq("tipo_alerta", "node_offline")
            .eq("esta_resuelta", False)
            .execute()
        )
        ids_resolver: list[int] = []
        for alerta in resp.data:
            nodo_resp = (
                cliente.table("nodes")
                .select("ultima_vez_visto")
                .eq("id", alerta["nodo_id"])
                .single()
                .execute()
            )
            if nodo_resp.data and nodo_resp.data.get("ultima_vez_visto", "") >= hace_10_min:
                ids_resolver.append(alerta["id"])
        self._resolver_alertas(cliente, ids_resolver)
        total_resueltas += len(ids_resolver)

        # --- power_anomaly: consumo volvió a la normalidad ---
        resp = (
            cliente.table("alerts")
            .select("id, sala_id, detalle")
            .eq("tipo_alerta", "power_anomaly")
            .eq("esta_resuelta", False)
            .execute()
        )
        ids_resolver = []
        for alerta in resp.data:
            sala_id = alerta["sala_id"]
            potencia_base = (alerta.get("detalle") or {}).get("potencia_base_w", 0)
            if potencia_base == 0:
                continue
            resp_actual = (
                cliente.table("sensor_readings")
                .select("potencia_w")
                .eq("sala_id", sala_id)
                .gte("registrado_en", hace_15_min)
                .execute()
            )
            valores = [
                f["potencia_w"]
                for f in resp_actual.data
                if f.get("potencia_w") is not None
            ]
            if valores and sum(valores) / len(valores) < potencia_base * 1.3:
                ids_resolver.append(alerta["id"])
        self._resolver_alertas(cliente, ids_resolver)
        total_resueltas += len(ids_resolver)

        # --- temperature_stuck: temperatura bajó de 25.5 °C ---
        resp = (
            cliente.table("alerts")
            .select("id, sala_id")
            .eq("tipo_alerta", "temperature_stuck")
            .eq("esta_resuelta", False)
            .execute()
        )
        ids_resolver = []
        for alerta in resp.data:
            resp_temp = (
                cliente.table("sensor_readings")
                .select("temperatura")
                .eq("sala_id", alerta["sala_id"])
                .gte("registrado_en", hace_10_min)
                .execute()
            )
            temps = [
                f["temperatura"]
                for f in resp_temp.data
                if f.get("temperatura") is not None
            ]
            if temps and min(temps) < 25.5:
                ids_resolver.append(alerta["id"])
        self._resolver_alertas(cliente, ids_resolver)
        total_resueltas += len(ids_resolver)

        return total_resueltas

    # -----------------------------------------------------------------------
    # Orquestador principal
    # -----------------------------------------------------------------------

    def ejecutar_todas_las_verificaciones(self) -> dict:
        resueltas = self.resolver_alertas_obsoletas()
        nuevas_offline = self.verificar_nodos_desconectados()
        nuevas_potencia = self.verificar_anomalias_potencia()
        nuevas_temperatura = self.verificar_temperatura_estancada()

        total_nuevas = len(nuevas_offline) + len(nuevas_potencia) + len(nuevas_temperatura)

        return {
            "resueltas":          resueltas,
            "nuevas_desconexion": len(nuevas_offline),
            "nuevas_potencia":    len(nuevas_potencia),
            "nuevas_temperatura": len(nuevas_temperatura),
            "total_nuevas":       total_nuevas,
        }
