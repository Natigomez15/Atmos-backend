import asyncio
from datetime import datetime, timedelta, timezone

from app.core.database import obtener_cliente
from app.core.logger import log
from app.core.websocket_manager import gestor
from app.services.notificaciones_service import notificar_alerta_push


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


MINUTOS_SIN_DATOS_AIRE = 5


def _desde_iso(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


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

    def _emitir_alerta_ws(self, alerta: dict) -> None:
        payload = {
            "tipo": "nueva_alerta",
            "type": "new_alert",
            "alerta": alerta,
            "alert": {
                "id": alerta.get("id"),
                "room_id": alerta.get("sala_id"),
                "node_id": alerta.get("nodo_id"),
                "alert_type": alerta.get("tipo_alerta"),
                "severity": alerta.get("severidad"),
                "message": alerta.get("mensaje"),
                "detail": alerta.get("detalle"),
                "is_resolved": alerta.get("esta_resuelta"),
                "created_at": alerta.get("creado_en"),
                "resolved_at": alerta.get("resuelto_en"),
            },
            "tipo_alerta": alerta.get("tipo_alerta"),
            "alert_type": alerta.get("tipo_alerta"),
            "severidad": alerta.get("severidad"),
            "severity": alerta.get("severidad"),
            "sala_id": str(alerta.get("sala_id", "")),
            "room_id": str(alerta.get("sala_id", "")),
            "mensaje": alerta.get("mensaje"),
            "message": alerta.get("mensaje"),
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(gestor.transmitir_a_todos(payload))
        except RuntimeError:
            # El servicio puede ejecutarse desde un endpoint sync en threadpool.
            pass

    def _insertar_alerta(self, cliente, alerta: dict) -> dict:
        alerta = {
            "esta_resuelta": False,
            "creado_en": _iso(_ahora()),
            **alerta,
        }
        respuesta = cliente.table("alerts").insert(alerta).execute()
        fila = respuesta.data[0]
        log.warning({
            "evento":     "alerta_creada",
            "tipo":       alerta.get("tipo_alerta"),
            "severidad":  alerta.get("severidad"),
            "sala_id":    str(alerta.get("sala_id", "")),
        })
        self._emitir_alerta_ws(fila)
        return fila

    def _notificar_alerta_atmos_push(self, alerta: dict) -> None:
        tipo_alerta = alerta.get("tipo_alerta")
        if tipo_alerta not in {
            "sensor_datos_invalidos",
            "temperatura_alta",
            "temperatura_fuera_rango",
            "humedad_alta",
            "humedad_invalida",
            "control_ir_inactivo",
            "aire_sin_datos",
        }:
            return

        detalle = alerta.get("detalle") or {}
        nombre_sala = detalle.get("aire") or detalle.get("pabellon") or "ATMOS"
        try:
            notificar_alerta_push(
                tipo_alerta=tipo_alerta,
                severidad=alerta.get("severidad", "medium"),
                mensaje=alerta.get("mensaje", "Nueva alerta ATMOS"),
                sala_id=str(alerta.get("sala_id") or ""),
                nombre_sala=nombre_sala,
                detalle=detalle,
            )
        except Exception as error:
            log.warning({
                "evento": "push_alerta_atmos_fallido",
                "tipo": tipo_alerta,
                "error": str(error),
            })

    def _buscar_alerta_activa_atmos(
        self,
        cliente,
        tipo_alerta: str,
        sala_id: str | None,
        pabellon: str,
        aire: str,
    ) -> dict | None:
        consulta = (
            cliente.table("alerts")
            .select("*")
            .eq("tipo_alerta", tipo_alerta)
            .eq("esta_resuelta", False)
        )
        if sala_id:
            consulta = consulta.eq("sala_id", sala_id)

        filas = consulta.execute().data or []
        if sala_id:
            return filas[0] if filas else None

        for fila in filas:
            detalle = fila.get("detalle") or {}
            if detalle.get("pabellon") == pabellon and detalle.get("aire") == aire:
                return fila
        return None

    def _crear_o_actualizar_alerta_atmos(
        self,
        cliente,
        tipo_alerta: str,
        severidad: str,
        mensaje: str,
        detalle: dict,
        sala_id: str | None = None,
    ) -> tuple[str, dict]:
        existente = self._buscar_alerta_activa_atmos(
            cliente=cliente,
            tipo_alerta=tipo_alerta,
            sala_id=sala_id,
            pabellon=detalle.get("pabellon"),
            aire=detalle.get("aire"),
        )
        datos = {
            "sala_id": sala_id,
            "nodo_id": None,
            "tipo_alerta": tipo_alerta,
            "severidad": severidad,
            "mensaje": mensaje,
            "detalle": detalle,
            "esta_resuelta": False,
        }
        if existente:
            respuesta = (
                cliente.table("alerts")
                .update({
                    "severidad": severidad,
                    "mensaje": mensaje,
                    "detalle": detalle,
                })
                .eq("id", existente["id"])
                .execute()
            )
            return "actualizada", respuesta.data[0] if respuesta.data else existente

        alerta_creada = self._insertar_alerta(cliente, datos)
        self._notificar_alerta_atmos_push(alerta_creada)
        return "creada", alerta_creada

    def _resolver_alertas_atmos(
        self,
        cliente,
        tipos: list[str],
        sala_id: str | None,
        pabellon: str,
        aire: str,
    ) -> int:
        total = 0
        for tipo_alerta in tipos:
            alerta = self._buscar_alerta_activa_atmos(
                cliente=cliente,
                tipo_alerta=tipo_alerta,
                sala_id=sala_id,
                pabellon=pabellon,
                aire=aire,
            )
            if alerta:
                self._resolver_alertas(cliente, [alerta["id"]])
                total += 1
        return total

    def _resolver_sala_id(self, cliente, pabellon: str, aire: str) -> str | None:
        try:
            respuesta = (
                cliente.table("rooms")
                .select("id,nombre,pabellon,edificio")
                .or_(f"pabellon.eq.{pabellon},edificio.eq.{pabellon}")
                .execute()
            )
        except Exception:
            return None

        salas = respuesta.data or []
        for sala in salas:
            if str(sala.get("nombre") or "").strip().lower() == aire.strip().lower():
                return sala.get("id")
        if len(salas) == 1:
            return salas[0].get("id")
        return None

    def _ultimo_registro_atmos(self, cliente, pabellon: str, aire: str) -> dict | None:
        respuesta = (
            cliente.table("registros")
            .select("*")
            .eq("pabellon", pabellon)
            .eq("aire", aire)
            .order("fecha_sync", desc=True)
            .limit(1)
            .execute()
        )
        return respuesta.data[0] if respuesta.data else None

    def verificar_alertas_atmos(
        self,
        pabellon: str = "robotica",
        aire: str = "Aire_1",
        diagnostico: dict | None = None,
    ) -> dict:
        cliente = obtener_cliente()
        sala_id = self._resolver_sala_id(cliente, pabellon, aire)
        creadas = 0
        actualizadas = 0
        resueltas = 0
        tipos: list[str] = []
        errores: list[str] = []

        if diagnostico and diagnostico.get("posible_fallo_sensor") is True:
            detalle = {
                "pabellon": pabellon,
                "aire": aire,
                "lecturas_revisadas": diagnostico.get("lecturas_revisadas"),
                "lecturas_validas": diagnostico.get("lecturas_validas"),
                "lecturas_invalidas": diagnostico.get("lecturas_invalidas"),
                "porcentaje_invalidas": diagnostico.get("porcentaje_invalidas"),
                "estado_sensor": diagnostico.get("estado_sensor"),
                "ultima_lectura_recibida_key": diagnostico.get("ultima_lectura_recibida_key"),
                "ultima_lectura_valida_key": diagnostico.get("ultima_lectura_valida_key"),
                "fuente": "atmos_diagnostico",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente=cliente,
                tipo_alerta="sensor_datos_invalidos",
                severidad="high",
                mensaje=(
                    "Se detectaron lecturas invalidas recientes del ESP32 o sensor. "
                    "El sistema esta usando la ultima lectura valida disponible."
                ),
                detalle=detalle,
                sala_id=sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("sensor_datos_invalidos")
        else:
            resueltas += self._resolver_alertas_atmos(
                cliente,
                ["sensor_datos_invalidos"],
                sala_id,
                pabellon,
                aire,
            )

        registro = self._ultimo_registro_atmos(cliente, pabellon, aire)
        if not registro:
            return {
                "verificadas": True,
                "creadas": creadas,
                "actualizadas": actualizadas,
                "resueltas": resueltas,
                "tipos": tipos,
                "errores": errores,
                "mensaje": "No hay registros para evaluar alertas ATMOS.",
            }

        def _numero(valor):
            try:
                return float(valor)
            except (TypeError, ValueError):
                return None

        temperatura = _numero(registro.get("temperatura_ambiente"))
        humedad = _numero(registro.get("humedad"))
        fecha_sync = registro.get("fecha_sync")
        fecha_sync_dt = _desde_iso(fecha_sync)
        minutos_sin_datos = None

        if fecha_sync_dt:
            minutos_sin_datos = round(
                (_ahora() - fecha_sync_dt.astimezone(timezone.utc)).total_seconds() / 60,
                2,
            )
            if minutos_sin_datos >= MINUTOS_SIN_DATOS_AIRE:
                detalle = {
                    "minutos_sin_datos": minutos_sin_datos,
                    "umbral_minutos": MINUTOS_SIN_DATOS_AIRE,
                    "pabellon": pabellon,
                    "aire": aire,
                    "fecha_sync": fecha_sync,
                    "firebase_key": registro.get("firebase_key"),
                    "fuente": "registros",
                }
                estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                    cliente,
                    "aire_sin_datos",
                    "high",
                    f"{aire} lleva {minutos_sin_datos:.1f} minutos sin enviar datos.",
                    detalle,
                    sala_id,
                )
                creadas += 1 if estado == "creada" else 0
                actualizadas += 1 if estado == "actualizada" else 0
                tipos.append("aire_sin_datos")
            else:
                resueltas += self._resolver_alertas_atmos(
                    cliente,
                    ["aire_sin_datos"],
                    sala_id,
                    pabellon,
                    aire,
                )

        control_ir_activo = registro.get("control_ir_activo")

        if control_ir_activo is False:
            detalle = {
                "control_ir_activo": control_ir_activo,
                "ultima_accion_ejecutada": registro.get("ultima_accion_ejecutada"),
                "pabellon": pabellon,
                "aire": aire,
                "fecha_sync": fecha_sync,
                "firebase_key": registro.get("firebase_key"),
                "fuente": "registros",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente,
                "control_ir_inactivo",
                "high",
                f"Se dejo de enviar senal IR al {aire}.",
                detalle,
                sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("control_ir_inactivo")
        elif control_ir_activo is True:
            resueltas += self._resolver_alertas_atmos(
                cliente,
                ["control_ir_inactivo"],
                sala_id,
                pabellon,
                aire,
            )

        if temperatura is not None and temperatura != 0 and temperatura > 32:
            detalle = {
                "temperatura_ambiente": temperatura,
                "pabellon": pabellon,
                "aire": aire,
                "fecha_sync": fecha_sync,
                "fuente": "registros",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente,
                "temperatura_alta",
                "medium",
                f"Temperatura ambiente alta en {aire}.",
                detalle,
                sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("temperatura_alta")
        elif temperatura is not None and temperatura < 10:
            detalle = {
                "temperatura_ambiente": temperatura,
                "pabellon": pabellon,
                "aire": aire,
                "fecha_sync": fecha_sync,
                "fuente": "registros",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente,
                "temperatura_fuera_rango",
                "high",
                f"Temperatura ambiente fuera de rango en {aire}.",
                detalle,
                sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("temperatura_fuera_rango")
        else:
            resueltas += self._resolver_alertas_atmos(
                cliente,
                ["temperatura_alta", "temperatura_fuera_rango"],
                sala_id,
                pabellon,
                aire,
            )

        if humedad is not None and humedad > 85:
            detalle = {
                "humedad": humedad,
                "pabellon": pabellon,
                "aire": aire,
                "fecha_sync": fecha_sync,
                "fuente": "registros",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente,
                "humedad_alta",
                "medium",
                f"Humedad alta en {aire}.",
                detalle,
                sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("humedad_alta")
        elif humedad is not None and humedad <= 0 and not (
            diagnostico and diagnostico.get("posible_fallo_sensor") is True
        ):
            detalle = {
                "humedad": humedad,
                "pabellon": pabellon,
                "aire": aire,
                "fecha_sync": fecha_sync,
                "fuente": "registros",
            }
            estado, _alerta = self._crear_o_actualizar_alerta_atmos(
                cliente,
                "humedad_invalida",
                "high",
                f"Humedad invalida en {aire}.",
                detalle,
                sala_id,
            )
            creadas += 1 if estado == "creada" else 0
            actualizadas += 1 if estado == "actualizada" else 0
            tipos.append("humedad_invalida")
        else:
            resueltas += self._resolver_alertas_atmos(
                cliente,
                ["humedad_alta", "humedad_invalida"],
                sala_id,
                pabellon,
                aire,
            )

        return {
            "verificadas": True,
            "creadas": creadas,
            "actualizadas": actualizadas,
            "resueltas": resueltas,
            "tipos": sorted(set(tipos)),
            "errores": errores,
            "fuente": "registros_atmos_diagnostico",
        }

    def verificar_alertas_registros_atmos(
        self,
        pabellon: str | None = None,
        aire: str | None = None,
    ) -> dict:
        cliente = obtener_cliente()
        if pabellon and aire:
            pares = [(pabellon, aire)]
        else:
            consulta = cliente.table("registros").select("pabellon,aire")
            if pabellon:
                consulta = consulta.eq("pabellon", pabellon)
            if aire:
                consulta = consulta.eq("aire", aire)
            respuesta = consulta.execute()
            pares = sorted({
                (fila.get("pabellon"), fila.get("aire"))
                for fila in (respuesta.data or [])
                if fila.get("pabellon") and fila.get("aire")
            })

        resultados = []
        for pabellon_item, aire_item in pares:
            resultados.append(
                self.verificar_alertas_atmos(
                    pabellon=pabellon_item,
                    aire=aire_item,
                )
            )

        return {
            "verificadas": True,
            "pares_revisados": len(pares),
            "resultados": resultados,
            "creadas": sum(r.get("creadas", 0) for r in resultados),
            "actualizadas": sum(r.get("actualizadas", 0) for r in resultados),
            "resueltas": sum(r.get("resueltas", 0) for r in resultados),
            "tipos": sorted({
                tipo
                for resultado in resultados
                for tipo in resultado.get("tipos", [])
            }),
        }

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
            notificar_alerta_push(
                tipo_alerta="node_offline",
                severidad="high",
                mensaje=mensaje_alerta,
                sala_id=str(nodo["sala_id"]),
                nombre_sala=str(nodo["sala_id"]),
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
            notificar_alerta_push(
                tipo_alerta="power_anomaly",
                severidad="medium",
                mensaje=mensaje_alerta,
                sala_id=str(sala_id),
                nombre_sala=nombre_sala,
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
            notificar_alerta_push(
                tipo_alerta="temperature_stuck",
                severidad="medium",
                mensaje=mensaje_alerta,
                sala_id=str(sala_id),
                nombre_sala=nombre_sala,
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
