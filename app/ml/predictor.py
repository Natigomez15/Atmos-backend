from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import joblib

from app.core.database import obtener_cliente
from app.core.database import obtener_firebase
from app.ml.atmos_logic import ejecutar_atmos
from app.services.sincronizador_firebase import leer_ultima_lectura_valida_firebase_rest


MODELO_ATMOS_PATH = Path(__file__).with_name("modelo_atmos (1).pkl")
ZONA_HORARIA_ATMOS = timezone(timedelta(hours=-5), "America/Panama")
HORA_INICIO_OPERACION = time(7, 0)
HORA_FIN_OPERACION = time(22, 45)


@lru_cache(maxsize=1)
def cargar_modelo_atmos():
    return joblib.load(MODELO_ATMOS_PATH)


class ServicioPredictor:

    # -----------------------------------------------------------------------
    # Modelo ATMOS en tiempo real
    # -----------------------------------------------------------------------

    def decidir_atmos(self, datos: dict) -> dict:
        sala_id = datos.pop("sala_id", None)
        nodo_id = datos.pop("nodo_id", None)

        if datos.get("presencia") == 1:
            datos["minutos_sin_presencia"] = 0
        elif sala_id or nodo_id:
            minutos = self.calcular_minutos_sin_presencia(sala_id, nodo_id)
            if minutos is not None:
                datos["minutos_sin_presencia"] = minutos

        modelo = cargar_modelo_atmos()
        return ejecutar_atmos(modelo, **datos)

    def decidir_atmos_desde_firebase(
        self, area: str = "robotica", aire: str = "Aire_1"
    ) -> dict:
        firebase_db = obtener_firebase()
        seleccion = self.obtener_ultima_lectura_firebase(firebase_db, area, aire)
        if not seleccion["valida"]:
            horario = self.obtener_estado_horario_operacion()
            accion = "mantener"
            firebase_db.child("Atmos").child("comandos").child(area).child(aire).update({
                "accion": accion,
            })
            actualizacion_supabase = self.guardar_decision_en_registro(
                pabellon=area,
                aire=aire,
                accion=accion,
                resultado=None,
                horario=horario,
            )
            return {
                "lectura_firebase": None,
                "lectura_valida": False,
                "firebase_key_usado": None,
                "lecturas_invalidas_ignoradas": seleccion["lecturas_invalidas_ignoradas"],
                "advertencias": seleccion["advertencias"],
                "entrada_modelo": None,
                "resultado_modelo": None,
                "accion": accion,
                "horario": horario,
                "ruta_accion": f"/Atmos/comandos/{area}/{aire}/accion",
                "actualizacion_supabase": actualizacion_supabase,
                "mensaje": "No habia lectura valida suficiente. Se mantiene accion segura.",
            }

        lectura = seleccion["lectura"]
        datos_atmos = self.preparar_lectura_firebase(lectura)
        if datos_atmos.get("presencia") == 0:
            datos_atmos["minutos_sin_presencia"] = self.calcular_minutos_sin_presencia(
                pabellon=area,
                aire=aire,
            ) or 0
        horario = self.obtener_estado_horario_operacion()

        if horario["dentro_horario"]:
            resultado = self.decidir_atmos(datos_atmos)
            accion = (
                self.traducir_accion_esp32(resultado)
                if resultado.get("valido")
                else "mantener"
            )
        else:
            resultado = None
            accion = "apagar"

        firebase_db.child("Atmos").child("comandos").child(area).child(aire).update({
            "accion": accion,
        })
        actualizacion_supabase = self.guardar_decision_en_registro(
            pabellon=area,
            aire=aire,
            accion=accion,
            resultado=resultado,
            horario=horario,
        )

        return {
            "lectura_firebase": lectura,
            "lectura_valida": True,
            "firebase_key_usado": (
                f"{area}_{aire}_{seleccion['firebase_key']}"
                if seleccion.get("firebase_key")
                else None
            ),
            "lecturas_invalidas_ignoradas": seleccion["lecturas_invalidas_ignoradas"],
            "advertencias": seleccion["advertencias"],
            "entrada_modelo": datos_atmos,
            "resultado_modelo": resultado,
            "accion": accion,
            "horario": horario,
            "ruta_accion": f"/Atmos/comandos/{area}/{aire}/accion",
            "actualizacion_supabase": actualizacion_supabase,
        }

    def obtener_estado_horario_operacion(self, ahora: datetime | None = None) -> dict:
        ahora = ahora or datetime.now(ZONA_HORARIA_ATMOS)
        if ahora.tzinfo is None:
            ahora = ahora.replace(tzinfo=ZONA_HORARIA_ATMOS)
        else:
            ahora = ahora.astimezone(ZONA_HORARIA_ATMOS)

        hora_actual = ahora.time().replace(microsecond=0)
        dentro_horario = HORA_INICIO_OPERACION <= hora_actual < HORA_FIN_OPERACION

        return {
            "zona_horaria": "America/Panama",
            "hora_actual": hora_actual.isoformat(timespec="minutes"),
            "hora_inicio": HORA_INICIO_OPERACION.isoformat(timespec="minutes"),
            "hora_fin": HORA_FIN_OPERACION.isoformat(timespec="minutes"),
            "dentro_horario": dentro_horario,
            "motivo": (
                "Horario permitido para ejecutar el modelo ML."
                if dentro_horario
                else "Fuera de horario permitido. Se fuerza accion apagar."
            ),
        }

    def obtener_ultima_lectura_firebase(self, firebase_db, area: str, aire: str) -> dict:
        respuesta = (
            firebase_db.child("Atmos")
            .child("registro")
            .child(area)
            .child(aire)
            .child("lecturas")
            .order_by_key()
            .limit_to_last(1)
            .get()
            .val()
        )

        if not respuesta:
            raise ValueError(f"No hay lecturas en /Atmos/registro/{area}/{aire}/lecturas")

        if isinstance(respuesta, dict):
            return list(respuesta.values())[-1]

        if isinstance(respuesta, list):
            lecturas = [lectura for lectura in respuesta if lectura]
            if lecturas:
                return lecturas[-1]

        raise ValueError("La última lectura de Firebase no tiene un formato válido")

    def obtener_ultima_lectura_firebase(self, firebase_db, area: str, aire: str) -> dict:
        return leer_ultima_lectura_valida_firebase_rest(
            pabellon=area,
            aire=aire,
            limite=50,
        )

    def preparar_lectura_firebase(self, lectura: dict) -> dict:
        def buscar(*claves, requerido: bool = True, defecto=None):
            for clave in claves:
                if clave in lectura and lectura[clave] is not None:
                    return lectura[clave]
            if requerido:
                raise ValueError(f"Falta un campo requerido en Firebase: {claves[0]}")
            return defecto

        presencia_raw = buscar(
            "presencia",
            "ocupacion",
            "ocupado",
            "estado_ocupacion",
            "movimiento",
            requerido=False,
            defecto=0,
        )
        presencia_texto = str(presencia_raw).strip().lower()
        presencia = 1 if presencia_raw is True or presencia_texto in {
            "1",
            "true",
            "si",
            "sí",
            "ocupado",
            "detectado",
            "presente",
        } else 0

        temp_ambiente = float(buscar(
            "temp_ambiente",
            "temperatura_ambiente",
            "temperatura",
            "temperatura_dht11",
        ))

        temp_ac_raw = buscar(
            "temp_ac",
            "temperatura_ac",
            "temperatura_salida",
            "temperatura_salida_aire",
            "temperatura_ds18b20",
            requerido=False,
            defecto=None,
        )
        if temp_ac_raw is None:
            delta_t = float(buscar("delta_t"))
            temp_ac = temp_ambiente - delta_t
        else:
            temp_ac = float(temp_ac_raw)

        return {
            "presencia": presencia,
            "temp_ambiente": temp_ambiente,
            "temp_ac": temp_ac,
            "humedad": float(buscar("humedad")),
            "minutos_sin_presencia": int(buscar(
                "minutos_sin_presencia",
                requerido=False,
                defecto=0,
            )),
            "minutos_enfriando": int(buscar(
                "minutos_enfriando",
                requerido=False,
                defecto=0,
            )),
            "temp_inicio": buscar("temp_inicio", requerido=False, defecto=None),
            "temp_actual": buscar("temp_actual", requerido=False, defecto=None),
            "temp_ac_actual": buscar("temp_ac_actual", requerido=False, defecto=None),
            "usar_capa_seguridad": True,
        }

    def traducir_accion_esp32(self, resultado: dict) -> str:
        decision = resultado["control"]["decision_final"]
        accion_ac = resultado["control"]["accion_ac"]
        temperatura_objetivo = accion_ac.get("temperatura_objetivo")

        if decision == "apagar":
            return "apagar"

        if decision == "esperar_apagado":
            return "mantener"

        if decision == "mantener":
            return "ahorro_24" if temperatura_objetivo == 24 else "mantener"

        if decision == "enfriar_fuerte":
            return "encender_22" if temperatura_objetivo == 22 else "enfriar_fuerte"

        return "mantener"

    def calcular_minutos_sin_presencia(
        self,
        sala_id: UUID | str | None = None,
        nodo_id: UUID | str | None = None,
        pabellon: str | None = None,
        aire: str | None = None,
    ) -> int | None:
        cliente = obtener_cliente()

        # La fuente principal actual del prototipo es registros, sincronizada
        # desde Firebase. sensor_readings se mantiene como fallback legado.
        consulta_registros = (
            cliente.table("registros")
            .select("fecha_sync")
            .eq("estado_ocupacion", True)
            .order("fecha_sync", desc=True)
            .limit(1)
        )
        if sala_id:
            consulta_registros = consulta_registros.eq("sala_id", str(sala_id))
        if nodo_id:
            consulta_registros = consulta_registros.eq("nodo_id", str(nodo_id))
        if pabellon:
            consulta_registros = consulta_registros.eq("pabellon", pabellon)
        if aire:
            consulta_registros = consulta_registros.eq("aire", aire)

        try:
            respuesta_registros = consulta_registros.execute()
            if respuesta_registros.data:
                ultimo_registro = respuesta_registros.data[0].get("fecha_sync")
                if ultimo_registro:
                    ultima_presencia = datetime.fromisoformat(
                        ultimo_registro.replace("Z", "+00:00")
                    )
                    if ultima_presencia.tzinfo is None:
                        ultima_presencia = ultima_presencia.replace(tzinfo=timezone.utc)
                    diferencia = datetime.now(timezone.utc) - ultima_presencia
                    return max(0, int(diferencia.total_seconds() // 60))
        except Exception:
            pass

        consulta = (
            cliente.table("sensor_readings")
            .select("registrado_en")
            .eq("presencia", True)
            .order("registrado_en", desc=True)
            .limit(1)
        )

        if sala_id:
            consulta = consulta.eq("sala_id", str(sala_id))
        if nodo_id:
            consulta = consulta.eq("nodo_id", str(nodo_id))

        respuesta = consulta.execute()
        if not respuesta.data:
            return None

        ultimo_registro = respuesta.data[0].get("registrado_en")
        if not ultimo_registro:
            return None

        ultima_presencia = datetime.fromisoformat(
            ultimo_registro.replace("Z", "+00:00")
        )
        if ultima_presencia.tzinfo is None:
            ultima_presencia = ultima_presencia.replace(tzinfo=timezone.utc)

        diferencia = datetime.now(timezone.utc) - ultima_presencia
        return max(0, int(diferencia.total_seconds() // 60))

    def guardar_decision_en_registro(
        self,
        pabellon: str,
        aire: str,
        accion: str,
        resultado: dict | None,
        horario: dict,
    ) -> dict:
        cliente = obtener_cliente()
        respuesta = (
            cliente.table("registros")
            .select("id,firebase_key")
            .eq("pabellon", pabellon)
            .eq("aire", aire)
            .order("fecha_sync", desc=True)
            .limit(1)
            .execute()
        )
        if not respuesta.data:
            return {"actualizado": False, "motivo": "sin_registros"}

        decision_ml = None
        decision_final = None
        if resultado:
            decision_ml = (resultado.get("modelo") or {}).get("decision_ml")
            decision_final = (resultado.get("control") or {}).get("decision_final")

        datos = {
            "ultima_accion_ejecutada": accion,
            "recomendacion_local": (
                decision_final
                or decision_ml
                or ("fuera_horario_apagar" if not horario["dentro_horario"] else accion)
            ),
        }
        registro_id = respuesta.data[0]["id"]
        cliente.table("registros").update(datos).eq("id", registro_id).execute()
        return {"actualizado": True, "registro_id": registro_id, **datos}

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
