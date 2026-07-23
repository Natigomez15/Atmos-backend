from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from math import isfinite
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import joblib

from app.core.database import obtener_cliente
from app.core.database import obtener_firebase
from app.core.logger import log
from app.ml.atmos_logic import dentro_de_horario_operacion, ejecutar_atmos
from app.ml.metadata_modelo import construir_panel_modelo
from app.services.sincronizador_firebase import leer_ultima_lectura_valida_firebase_rest


MODELO_ATMOS_PATH = Path(__file__).with_name("modelo_atmos (1).pkl")
VERSION_MODELO_ATMOS = "modelo_atmos_rf_v1"
FEATURES_MODELO_ATMOS = ["presencia", "temp_ambiente", "temp_ac", "delta_t", "humedad"]
FEATURES_PUBLICAS_ATMOS = [
    "presencia",
    "temp_ambiente",
    "temperatura_salida_aire",
    "delta_t",
    "humedad",
]
ZONA_HORARIA_ATMOS = ZoneInfo("America/Panama")
HORA_INICIO_OPERACION = time(6, 0)
HORA_FIN_OPERACION = time(23, 0)
ACCIONES_IR_RECONOCIDAS = {
    "apagar",
    "ahorro_24",
    "encender_22",
    "encender_23",
    "enfriar_fuerte",
}


@lru_cache(maxsize=1)
def cargar_modelo_atmos():
    return joblib.load(MODELO_ATMOS_PATH)


class ServicioPredictor:
    @staticmethod
    def normalizar_accion_comando(accion: str | None) -> str:
        return str(accion or "").strip().lower().replace(" ", "_").replace("-", "_")

    def comando_desde_firma_ejecutada(self, firma: str | None) -> dict | None:
        texto = str(firma or "")
        separador = "|actualizado_en:"
        if not texto.startswith("accion:") or separador not in texto:
            return None
        accion, actualizado_en = texto.removeprefix("accion:").split(separador, 1)
        accion = self.normalizar_accion_comando(accion)
        if accion not in ACCIONES_IR_RECONOCIDAS or not actualizado_en:
            return None
        return {"accion": accion, "actualizado_en": actualizado_en}

    def publicar_comando_si_cambio(
        self,
        firebase_db,
        area: str,
        aire: str,
        accion: str,
        metadata: dict | None = None,
        permitir_publicacion: bool = True,
    ) -> dict:
        """Publica un comando únicamente cuando cambia la acción efectiva.

        El ESP32 deduplica usando ``accion`` + ``actualizado_en``. Reescribir
        la misma acción con una fecha nueva provoca otro envío IR, aunque el
        equipo ya esté en ese modo.
        """
        referencia = (
            firebase_db.child("Atmos").child("comandos").child(area).child(aire)
        )
        comando_actual = referencia.get().val() or {}
        if not isinstance(comando_actual, dict):
            comando_actual = {}
        accion_actual = self.normalizar_accion_comando(comando_actual.get("accion"))
        accion_nueva = self.normalizar_accion_comando(accion)
        if not permitir_publicacion:
            ejecutado = self.comando_desde_firma_ejecutada(
                comando_actual.get("firma_ejecutada")
            )
            if accion_actual == "mantener" and ejecutado:
                # Repara nodos antiguos donde "mantener" quedó como una orden
                # pendiente. Se restaura exactamente la firma ya ejecutada; al
                # coincidir, el ESP32 no vuelve a transmitir el código IR.
                try:
                    referencia.update({
                        **ejecutado,
                        "resultado": "ejecutado",
                    })
                except Exception as error:
                    log.error({
                        "evento": "reconciliacion_comando_firebase_sin_permiso",
                        "area": area,
                        "aire": aire,
                        "error": str(error),
                    })
                    return {
                        "publicado": False,
                        "motivo": "reconciliacion_requiere_permiso_firebase",
                        "accion_actual": comando_actual.get("accion"),
                        "actualizado_en": comando_actual.get("actualizado_en"),
                        "resultado": comando_actual.get("resultado"),
                        "reparacion_requerida": ejecutado,
                    }
                return {
                    "publicado": False,
                    "motivo": "estado_reconciliado_con_firma_ejecutada",
                    "accion_actual": ejecutado["accion"],
                    "actualizado_en": ejecutado["actualizado_en"],
                    "resultado": "ejecutado",
                }
            return {
                "publicado": False,
                "motivo": "control_sin_envio_ir",
                "accion_actual": comando_actual.get("accion"),
                "actualizado_en": comando_actual.get("actualizado_en"),
                "resultado": comando_actual.get("resultado"),
            }
        if accion_actual == accion_nueva:
            return {
                "publicado": False,
                "motivo": "accion_sin_cambios",
                "accion_actual": comando_actual.get("accion"),
                "actualizado_en": comando_actual.get("actualizado_en"),
                "resultado": comando_actual.get("resultado"),
            }

        actualizado_en = datetime.now(timezone.utc).isoformat()
        try:
            referencia.update({
                "accion": accion,
                **(metadata or {}),
                "actualizado_en": actualizado_en,
                "resultado": "pendiente",
            })
        except Exception as error:
            log.error({
                "evento": "publicacion_comando_firebase_sin_permiso",
                "area": area,
                "aire": aire,
                "accion": accion,
                "error": str(error),
            })
            return {
                "publicado": False,
                "motivo": "publicacion_requiere_permiso_firebase",
                "accion_actual": comando_actual.get("accion"),
                "actualizado_en": comando_actual.get("actualizado_en"),
                "resultado": comando_actual.get("resultado"),
            }
        return {
            "publicado": True,
            "motivo": "accion_cambiada",
            "accion_anterior": comando_actual.get("accion"),
            "accion_actual": accion,
            "actualizado_en": actualizado_en,
        }


    # -----------------------------------------------------------------------
    # Modelo ATMOS en tiempo real
    # -----------------------------------------------------------------------

    def decidir_atmos(self, datos: dict) -> dict:
        sala_id = datos.pop("sala_id", None)
        nodo_id = datos.pop("nodo_id", None)
        pabellon = datos.pop("pabellon", None)
        aire = datos.pop("aire", None)
        horario = self.obtener_estado_horario_operacion()
        if not horario["dentro_horario"]:
            return self.respuesta_fuera_horario(horario)

        if datos.get("temp_ac") is None and datos.get("temperatura_salida_aire") is not None:
            datos["temp_ac"] = datos["temperatura_salida_aire"]
        datos.pop("temperatura_salida_aire", None)

        estado_control = self.obtener_estado_control_registro(
            sala_id=sala_id,
            nodo_id=nodo_id,
            pabellon=pabellon,
            aire=aire,
        )
        self.completar_datos_control_atmos(datos, estado_control)

        if datos.get("presencia") == 1:
            datos["minutos_sin_presencia"] = 0
        elif sala_id or nodo_id:
            minutos = self.calcular_minutos_sin_presencia(sala_id, nodo_id)
            if minutos is not None:
                datos["minutos_sin_presencia"] = minutos

        modelo = cargar_modelo_atmos()
        resultado = ejecutar_atmos(modelo, **datos)
        resultado["modelo_ml"] = self.construir_trazabilidad_modelo(resultado, datos)
        self.persistir_estado_control_registro(
            sala_id=sala_id,
            nodo_id=nodo_id,
            pabellon=pabellon,
            aire=aire,
            resultado=resultado,
            datos_atmos=datos,
            estado_anterior=estado_control,
        )
        self.registrar_evento_decision_atmos(
            sala_id=sala_id,
            nodo_id=nodo_id,
            pabellon=pabellon,
            aire=aire,
            resultado=resultado,
            origen="decidir_atmos",
        )
        return resultado

    def decidir_atmos_desde_firebase(
        self, area: str = "robotica", aire: str = "Aire_1"
    ) -> dict:
        horario = self.obtener_estado_horario_operacion()
        if not horario["dentro_horario"]:
            return self.respuesta_fuera_horario(horario)

        firebase_db = obtener_firebase()
        seleccion = self.obtener_ultima_lectura_firebase(firebase_db, area, aire)
        if not seleccion["valida"]:
            horario = self.obtener_estado_horario_operacion()
            accion = "mantener"
            publicacion_comando = self.publicar_comando_si_cambio(
                firebase_db, area, aire, accion, permitir_publicacion=False
            )
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
                "diagnostico": seleccion.get("diagnostico"),
                "advertencias": seleccion["advertencias"],
                "entrada_modelo": None,
                "resultado_modelo": None,
                "modelo_ml": self.trazabilidad_modelo_no_usado(
                    "no hay lectura valida suficiente para ejecutar el modelo"
                ),
                "accion": accion,
                "publicacion_comando": publicacion_comando,
                "horario": horario,
                "ruta_accion": f"/Atmos/comandos/{area}/{aire}/accion",
                "actualizacion_supabase": actualizacion_supabase,
                "mensaje": "No habia lectura valida suficiente. Se mantiene accion segura.",
            }

        lectura = seleccion["lectura"]
        datos_atmos = self.preparar_lectura_firebase(lectura)
        datos_atmos["pabellon"] = area
        datos_atmos["aire"] = aire
        if datos_atmos.get("presencia") == 0:
            datos_atmos["minutos_sin_presencia"] = self.calcular_minutos_sin_presencia(
                pabellon=area,
                aire=aire,
            ) or 0
        resultado = self.decidir_atmos(datos_atmos)
        accion = (
            self.traducir_accion_esp32(resultado)
            if resultado.get("valido") and resultado.get("procesado", True)
            else "mantener"
        )
        modelo_ml = (
            resultado.get("modelo_ml")
            if resultado
            else self.trazabilidad_modelo_no_usado(
                "fuera de horario permitido; se fuerza accion apagar"
            )
        )
        probabilidades = (modelo_ml or {}).get("probabilidades") or {}
        confianza_ml = max(probabilidades.values()) if probabilidades else None
        ruta_comando = f"/Atmos/comandos/{area}/{aire}"
        control = (resultado or {}).get("control") or {}
        permitir_publicacion = bool(
            control.get("ejecutar_ir") and accion != "mantener"
        )

        publicacion_comando = self.publicar_comando_si_cambio(
            firebase_db,
            area,
            aire,
            accion,
            {
                "origen": "modelo_ml",
                "modelo_usado": bool((modelo_ml or {}).get("modelo_usado")),
                "tipo_modelo": (modelo_ml or {}).get("tipo_modelo"),
                "version_modelo": (modelo_ml or {}).get("version_modelo"),
                "prediccion_modelo": (modelo_ml or {}).get("prediccion_modelo"),
                "recomendacion_ml": accion,
                "confianza_ml": confianza_ml,
            },
            permitir_publicacion=permitir_publicacion,
        )
        if resultado and resultado.get("control"):
            resultado["control"]["accion_enviada"] = bool(
                resultado["control"].get("ejecutar_ir") and accion != "mantener"
                and publicacion_comando["publicado"]
            )
        actualizacion_supabase = self.guardar_decision_en_registro(
            pabellon=area,
            aire=aire,
            accion=accion,
            resultado=resultado,
            horario=horario,
        )
        prediccion_guardada = self.guardar_prediccion_modelo_valida(
            pabellon=area,
            aire=aire,
            accion=accion,
            resultado=resultado,
            modelo_ml=modelo_ml,
            actualizacion_supabase=actualizacion_supabase,
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
            "diagnostico": seleccion.get("diagnostico"),
            "advertencias": seleccion["advertencias"],
            "entrada_modelo": self.publicar_entrada_modelo(datos_atmos),
            "resultado_modelo": self.publicar_resultado_modelo(resultado),
            "modelo_ml": modelo_ml,
            "accion": accion,
            "publicacion_comando": publicacion_comando,
            "horario": horario,
            "ruta_comando": ruta_comando,
            "ruta_accion": f"/Atmos/comandos/{area}/{aire}/accion",
            "actualizacion_supabase": actualizacion_supabase,
            "prediccion_guardada": prediccion_guardada,
        }

    def obtener_estado_control_registro(
        self,
        sala_id: UUID | str | None = None,
        nodo_id: UUID | str | None = None,
        pabellon: str | None = None,
        aire: str | None = None,
    ) -> dict:
        cliente = obtener_cliente()
        columnas = (
            "id,fecha_sync,recomendacion_local,ultima_accion_ejecutada,"
            "ultima_accion_ir,ciclo_enfriamiento_temp_inicio,ciclo_enfriamiento_inicio"
        )
        consulta = (
            cliente.table("registros")
            .select(columnas)
            .order("fecha_sync", desc=True)
            .limit(1)
        )
        if sala_id:
            consulta = consulta.eq("sala_id", str(sala_id))
        if nodo_id:
            consulta = consulta.eq("nodo_id", str(nodo_id))
        if pabellon:
            consulta = consulta.eq("pabellon", pabellon)
        if aire:
            consulta = consulta.eq("aire", aire)

        try:
            respuesta = consulta.execute()
        except Exception as error:
            log.warning({
                "evento": "estado_control_atmos_no_disponible",
                "motivo": "No se pudo leer estado persistente de control; se continua sin memoria previa.",
                "error": str(error),
                "sala_id": str(sala_id) if sala_id else None,
                "nodo_id": str(nodo_id) if nodo_id else None,
                "pabellon": pabellon,
                "aire": aire,
            })
            return {}

        return respuesta.data[0] if respuesta.data else {}

    def completar_datos_control_atmos(self, datos: dict, estado_control: dict | None) -> None:
        estado_control = estado_control or {}
        datos.setdefault("modo_control", "experimental")
        if datos.get("ultima_accion_ir") is None and estado_control.get("ultima_accion_ir"):
            datos["ultima_accion_ir"] = estado_control["ultima_accion_ir"]

        inicio_raw = estado_control.get("ciclo_enfriamiento_inicio")
        temp_inicio = estado_control.get("ciclo_enfriamiento_temp_inicio")
        if inicio_raw and temp_inicio is not None:
            try:
                inicio = datetime.fromisoformat(str(inicio_raw).replace("Z", "+00:00"))
                if inicio.tzinfo is None:
                    inicio = inicio.replace(tzinfo=timezone.utc)
                datos["minutos_enfriando"] = max(
                    0,
                    int((datetime.now(timezone.utc) - inicio.astimezone(timezone.utc)).total_seconds() // 60),
                )
                if datos.get("temp_inicio") is None:
                    datos["temp_inicio"] = float(temp_inicio)
                if datos.get("temp_actual") is None:
                    datos["temp_actual"] = datos.get("temp_ambiente")
                if datos.get("temp_ac_actual") is None:
                    datos["temp_ac_actual"] = datos.get("temp_ac")
            except (TypeError, ValueError) as error:
                log.warning({
                    "evento": "estado_control_atmos_ciclo_invalido",
                    "motivo": "No se pudo interpretar ciclo_enfriamiento persistido; se continua sin ciclo previo.",
                    "error": str(error),
                    "ciclo_enfriamiento_inicio": inicio_raw,
                    "ciclo_enfriamiento_temp_inicio": temp_inicio,
                })

    def persistir_estado_control_registro(
        self,
        sala_id: UUID | str | None,
        nodo_id: UUID | str | None,
        pabellon: str | None,
        aire: str | None,
        resultado: dict,
        datos_atmos: dict,
        estado_anterior: dict | None,
    ) -> None:
        if not resultado or not resultado.get("valido"):
            return

        registro_id = (estado_anterior or {}).get("id")
        if not registro_id:
            estado_anterior = self.obtener_estado_control_registro(
                sala_id=sala_id,
                nodo_id=nodo_id,
                pabellon=pabellon,
                aire=aire,
            )
            registro_id = (estado_anterior or {}).get("id")
        if not registro_id:
            return

        datos = self.campos_estado_control_atmos(resultado, datos_atmos, estado_anterior or {})
        if not datos:
            return

        try:
            obtener_cliente().table("registros").update(datos).eq("id", registro_id).execute()
        except Exception as error:
            log.warning({
                "evento": "estado_control_atmos_no_persistido",
                "motivo": "No se pudo guardar estado de control ATMOS; la decision actual ya fue calculada.",
                "error": str(error),
                "registro_id": registro_id,
                "campos": sorted(datos.keys()),
            })

    def campos_estado_control_atmos(
        self,
        resultado: dict | None,
        datos_atmos: dict,
        estado_anterior: dict,
    ) -> dict:
        control = (resultado or {}).get("control") or {}
        decision_final = control.get("decision_final")
        datos: dict = {}

        if control.get("ejecutar_ir") is True and control.get("comando_ir"):
            datos["ultima_accion_ir"] = control["comando_ir"]

        decision_anterior = (
            estado_anterior.get("recomendacion_local")
            or estado_anterior.get("ultima_accion_ejecutada")
        )
        ciclo_activo = estado_anterior.get("ciclo_enfriamiento_inicio")
        if decision_final == "enfriar_fuerte":
            if decision_anterior != "enfriar_fuerte" or not ciclo_activo:
                datos["ciclo_enfriamiento_temp_inicio"] = datos_atmos.get("temp_ambiente")
                datos["ciclo_enfriamiento_inicio"] = datetime.now(timezone.utc).isoformat()
        elif decision_final:
            datos["ciclo_enfriamiento_temp_inicio"] = None
            datos["ciclo_enfriamiento_inicio"] = None

        return datos

    def informacion_modelo(self) -> dict:
        try:
            modelo = cargar_modelo_atmos()
            tipo_modelo = type(modelo).__name__
        except Exception:
            tipo_modelo = None

        return {
            "modelo_disponible": MODELO_ATMOS_PATH.exists(),
            "tipo_modelo": tipo_modelo,
            "version_modelo": VERSION_MODELO_ATMOS,
            "features_requeridas": FEATURES_PUBLICAS_ATMOS,
        }

    def panel_modelo(self) -> dict:
        """Introspección del modelo para el panel 'Sobre el motor':
        importancia REAL de variables + métricas de validación persistidas
        (o su ausencia). No usa la base de datos."""
        modelo = cargar_modelo_atmos()
        return construir_panel_modelo(
            modelo, MODELO_ATMOS_PATH, VERSION_MODELO_ATMOS
        )

    def trazabilidad_modelo_no_usado(
        self,
        motivo: str,
        features_usadas: dict | None = None,
    ) -> dict:
        return {
            **self.informacion_modelo(),
            "modelo_usado": False,
            "motivo_no_usado": motivo,
            "features_usadas": self.publicar_features_modelo(features_usadas),
            "prediccion_modelo": None,
            "probabilidades": None,
        }

    def construir_trazabilidad_modelo(self, resultado: dict, datos_atmos: dict) -> dict:
        features = self.construir_features_modelo(datos_atmos)
        if not resultado.get("valido"):
            errores = resultado.get("errores") or ["lectura invalida"]
            return self.trazabilidad_modelo_no_usado(
                "; ".join(errores),
                features_usadas=features,
            )

        probabilidades_pct = (resultado.get("modelo") or {}).get("probabilidades") or {}
        probabilidades = {
            clase: round(float(probabilidad) / 100, 4)
            for clase, probabilidad in probabilidades_pct.items()
        }

        return {
            **self.informacion_modelo(),
            "modelo_usado": True,
            "motivo_no_usado": None,
            "features_usadas": self.publicar_features_modelo(features),
            "prediccion_modelo": (resultado.get("modelo") or {}).get("decision_ml"),
            "probabilidades": probabilidades,
        }

    def construir_features_modelo(self, datos_atmos: dict) -> dict:
        temp_ambiente = datos_atmos.get("temp_ambiente")
        temp_ac = datos_atmos.get("temp_ac", datos_atmos.get("temperatura_salida_aire"))
        try:
            delta_t = float(temp_ambiente) - float(temp_ac)
        except (TypeError, ValueError):
            delta_t = datos_atmos.get("delta_t")

        return {
            "presencia": datos_atmos.get("presencia"),
            "temp_ambiente": temp_ambiente,
            "temp_ac": temp_ac,
            "delta_t": round(delta_t, 2) if isinstance(delta_t, (int, float)) else delta_t,
            "humedad": datos_atmos.get("humedad"),
        }

    def publicar_features_modelo(self, features: dict | None) -> dict | None:
        if features is None:
            return None

        publicas = dict(features)
        if "temp_ac" in publicas:
            publicas["temperatura_salida_aire"] = publicas.pop("temp_ac")
        return publicas

    def publicar_entrada_modelo(self, datos_atmos: dict | None) -> dict | None:
        if datos_atmos is None:
            return None

        entrada = dict(datos_atmos)
        if "temp_ac" in entrada:
            entrada["temperatura_salida_aire"] = entrada.pop("temp_ac")
        return entrada

    def publicar_resultado_modelo(self, resultado: dict | None) -> dict | None:
        if resultado is None:
            return None

        publicado = dict(resultado)
        lectura = publicado.get("lectura")
        if isinstance(lectura, dict):
            publicado["lectura"] = self.publicar_entrada_modelo(lectura)
        return publicado

    def obtener_estado_horario_operacion(self, ahora: datetime | None = None) -> dict:
        if ahora is None:
            ahora = datetime.now(ZONA_HORARIA_ATMOS)
        elif ahora.tzinfo is None:
            ahora = ahora.replace(tzinfo=ZONA_HORARIA_ATMOS)
        else:
            ahora = ahora.astimezone(ZONA_HORARIA_ATMOS)

        hora_actual = ahora.time().replace(microsecond=0)
        dentro_horario = dentro_de_horario_operacion(ahora)

        return {
            "zona_horaria": "America/Panama",
            "dia_semana": ahora.weekday(),
            "hora_actual": hora_actual.isoformat(timespec="minutes"),
            "hora_inicio": HORA_INICIO_OPERACION.isoformat(timespec="minutes"),
            "hora_fin": HORA_FIN_OPERACION.isoformat(timespec="minutes"),
            "dias_operativos": "lunes-sabado",
            "dentro_horario": dentro_horario,
            "motivo": (
                "Horario operativo de ATMOS."
                if dentro_horario
                else "Fuera de horario operativo de ATMOS (L-S 6:00am-11:00pm, hora de Panama)."
            ),
        }

    def respuesta_fuera_horario(self, horario: dict | None = None) -> dict:
        horario = horario or self.obtener_estado_horario_operacion()
        return {
            "valido": True,
            "procesado": False,
            "motivo": (
                "Fuera de horario operativo de ATMOS (L-S 6:00am-11:00pm, "
                "hora de Panama). Lectura recibida pero no procesada."
            ),
            "horario": horario,
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
        delta_t_raw = buscar("delta_t", requerido=False, defecto=None)
        temp_ac = None
        if temp_ac_raw is not None:
            temp_ac = float(temp_ac_raw)

        if (temp_ac is None or temp_ac < 5 or temp_ac > 35) and delta_t_raw is not None:
            delta_t = float(delta_t_raw)
            temp_ac_desde_delta = temp_ambiente - delta_t
            if 5 <= temp_ac_desde_delta <= 35:
                temp_ac = temp_ac_desde_delta

        if temp_ac is None:
            temp_ac = float(temp_ac_raw)

        return {
            "presencia": presencia,
            "temp_ambiente": temp_ambiente,
            "temp_ac": temp_ac,
            "temperatura_salida_aire": temp_ac,
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
        control = resultado["control"]
        if control.get("ejecutar_ir") is False:
            return "mantener"

        comando_ir = control.get("comando_ir")
        if comando_ir == "APAGAR":
            return "apagar"
        if comando_ir == "TEMP_24":
            return "ahorro_24"
        if comando_ir == "TEMP_23":
            return "encender_23"
        if comando_ir == "TEMP_22":
            return "encender_22"

        decision = control["decision_final"]
        accion_ac = control["accion_ac"]
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
            .select("id,firebase_key,sala_id,fecha_sync,energia_kwh,potencia_w,aire_encendido_atmos")
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

        registro_actual = respuesta.data[0]
        registro_id = registro_actual["id"]
        cliente.table("registros").update(datos).eq("id", registro_id).execute()
        return {
            "actualizado": True,
            "registro_id": registro_id,
            "sala_id": registro_actual.get("sala_id"),
            **datos,
        }

    def registrar_evento_decision_atmos(
        self,
        *,
        sala_id=None,
        nodo_id=None,
        pabellon: str | None = None,
        aire: str | None = None,
        resultado: dict | None = None,
        origen: str = "atmos_logic",
    ) -> None:
        if not resultado or not resultado.get("procesado", True):
            return

        control = resultado.get("control") or {}
        seguridad = resultado.get("seguridad") or {}
        fallas = resultado.get("deteccion_fallas") or {}
        decision_final = control.get("decision_final")
        if not decision_final:
            return

        comando_ir = control.get("comando_ir") or control.get("comando_ir_sugerido")
        tipo = decision_final
        if fallas.get("estado_falla") == "posible_falla":
            tipo = "posible_falla_ac"
        elif decision_final == "apagar":
            tipo = "apagado_automatico"
        elif decision_final == "esperar_apagado":
            tipo = "espera_apagado"
        elif decision_final == "enfriar_fuerte":
            tipo = "enfriamiento"
        elif decision_final == "mantener":
            tipo = "mantener"

        motivo = (
            seguridad.get("mensaje")
            or control.get("motivo_no_ejecucion")
            or fallas.get("mensaje")
            or "Decisión generada por atmos_logic"
        )
        datos = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "tipo": tipo,
            "motivo": motivo,
            "pabellon": pabellon,
            "aire": aire,
            "sala_id": str(sala_id) if sala_id else None,
            "nodo_id": str(nodo_id) if nodo_id else None,
            "decision_final": decision_final,
            "comando_ir": comando_ir,
            "metadata": {
                "origen": origen,
                "ejecutar_ir": control.get("ejecutar_ir"),
                "accion_enviada": control.get("accion_enviada"),
                "estado_falla": fallas.get("estado_falla"),
            },
        }
        try:
            obtener_cliente().table("atmos_decision_events").insert(datos).execute()
        except Exception as error:
            log.warning({
                "evento": "atmos_decision_event_no_guardado",
                "error": str(error),
                "tipo": tipo,
                "pabellon": pabellon,
                "aire": aire,
            })

    def resolver_sala_id_por_pabellon_aire(self, pabellon: str, aire: str) -> str | None:
        cliente = obtener_cliente()
        respuesta = (
            cliente.table("rooms")
            .select("id,nombre,pabellon,edificio,aires")
            .or_(f"pabellon.eq.{pabellon},edificio.eq.{pabellon}")
            .execute()
        )
        salas = respuesta.data or []
        aire_normalizado = aire.strip().lower()

        # La interfaz muestra salas compuestas (por ejemplo, "Robótica") y
        # oculta las salas técnicas (por ejemplo, "Aire_1"). Asociamos la
        # predicción a la sala compuesta que declara ese aire para que pueda
        # consultarse desde la pantalla de Predicciones ATMOS.
        for sala in salas:
            nombre_normalizado = str(sala.get("nombre") or "").strip().lower()
            aires = {
                str(item).strip().lower()
                for item in (sala.get("aires") or [])
                if str(item).strip()
            }
            if aire_normalizado in aires and nombre_normalizado != aire_normalizado:
                return sala.get("id")

        for sala in salas:
            if str(sala.get("nombre") or "").strip().lower() == aire_normalizado:
                return sala.get("id")
        if len(salas) == 1:
            return salas[0].get("id")
        return None

    def setpoint_desde_accion(self, accion: str | None) -> int | None:
        accion_normalizada = str(accion or "").strip().lower()
        if accion_normalizada in {"encender_22", "enfriar_fuerte"}:
            return 22
        if accion_normalizada == "ahorro_24":
            return 24
        return None

    def guardar_prediccion_modelo_valida(
        self,
        pabellon: str,
        aire: str,
        accion: str,
        resultado: dict | None,
        modelo_ml: dict | None,
        actualizacion_supabase: dict,
    ) -> dict:
        if not modelo_ml or not modelo_ml.get("modelo_usado"):
            return {
                "guardada": False,
                "motivo": (modelo_ml or {}).get("motivo_no_usado") or "modelo no usado",
            }

        sala_id = self.resolver_sala_id_por_pabellon_aire(pabellon, aire) or (
            actualizacion_supabase.get("sala_id")
        )
        if not sala_id:
            return {"guardada": False, "motivo": "no se pudo resolver sala_id"}

        probabilidades = modelo_ml.get("probabilidades") or {}
        confianza = max(probabilidades.values()) if probabilidades else None
        instantanea = {
            "features_usadas": modelo_ml.get("features_usadas"),
            "prediccion_modelo": modelo_ml.get("prediccion_modelo"),
            "probabilidades": probabilidades,
            "accion_final": accion,
            "fuente": "modelo_pkl",
            "motivo_reglas_seguridad": (
                (resultado or {}).get("seguridad") or {}
            ).get("mensaje"),
        }
        registro = {
            "sala_id": str(sala_id),
            "setpoint_recomendado": self.setpoint_desde_accion(accion),
            "ahorro_predicho_pct": None,
            "puntaje_confianza": confianza,
            "version_modelo": VERSION_MODELO_ATMOS,
            "instantanea_caracteristicas": instantanea,
            "ahorro_real_pct": None,
            "fue_aplicado": actualizacion_supabase.get("actualizado") is True,
            "predicho_en": datetime.now(timezone.utc).isoformat(),
        }
        respuesta = obtener_cliente().table("ml_predictions").insert(registro).execute()
        return {
            "guardada": True,
            "id": respuesta.data[0].get("id") if respuesta.data else None,
            "sala_id": str(sala_id),
        }

    # -----------------------------------------------------------------------
    # Aciertos en producción (Fase 2.3)
    # -----------------------------------------------------------------------
    #
    # REGLA DE ACIERTO (documentada):
    #   Una recomendación de APAGADO (decision_final == "apagar") se considera
    #   CORRECTA si en la ventana de VENTANA_ACIERTO_MIN minutos posteriores al
    #   evento NO se registró ocupación (estado_ocupacion = True) en el mismo
    #   pabellón/aire. Es decir: el sistema apagó y el espacio efectivamente
    #   siguió vacío. Si hubo ocupación en esa ventana, el apagado fue
    #   prematuro y cuenta como incorrecto.
    #
    #   "Precisión en producción" = correctas / evaluadas de las últimas N
    #   recomendaciones de apagado con ventana ya vencida.
    #
    # Fuente de recomendaciones: tabla atmos_decision_events (log de Fase 2).
    # Fuente de ocupación posterior: tabla registros (estado_ocupacion).
    VENTANA_ACIERTO_MIN = 45  # dentro del rango 30-60 min definido en la tarea

    def precision_apagados_produccion(
        self, limite: int = 100
    ) -> dict:
        """Calcula la precisión real de las recomendaciones de apagado contra
        la ocupación observada después. Ver REGLA DE ACIERTO arriba.

        Devuelve estado="disponible" con el porcentaje, o estado="sin_datos"
        si aún no hay suficientes eventos con ventana vencida. No lanza
        excepciones: ante cualquier problema de datos degrada a "pendiente".
        """
        cliente = obtener_cliente()
        ahora = datetime.now(timezone.utc)
        limite_ventana = (ahora - timedelta(minutes=self.VENTANA_ACIERTO_MIN)).isoformat()

        try:
            eventos = (
                cliente.table("atmos_decision_events")
                .select("timestamp_utc,pabellon,aire,decision_final")
                .eq("decision_final", "apagar")
                .lte("timestamp_utc", limite_ventana)
                .order("timestamp_utc", desc=True)
                .limit(limite)
                .execute()
            ).data or []
        except Exception as error:
            log.warning({
                "evento": "precision_apagados_no_disponible",
                "error": str(error),
            })
            return {
                "estado": "pendiente",
                "motivo": "No se pudo leer el log de decisiones (atmos_decision_events).",
                "ventana_min": self.VENTANA_ACIERTO_MIN,
            }

        evaluadas = 0
        correctas = 0
        for evento in eventos:
            inicio = self._parsear_fecha(evento.get("timestamp_utc"))
            if inicio is None:
                continue
            fin = inicio + timedelta(minutes=self.VENTANA_ACIERTO_MIN)
            try:
                ocupacion = (
                    cliente.table("registros")
                    .select("id")
                    .eq("pabellon", evento.get("pabellon"))
                    .eq("aire", evento.get("aire"))
                    .eq("estado_ocupacion", True)
                    .gte("fecha_sync", inicio.isoformat())
                    .lte("fecha_sync", fin.isoformat())
                    .limit(1)
                    .execute()
                ).data or []
            except Exception:
                continue
            evaluadas += 1
            if not ocupacion:  # no hubo ocupación → apagado correcto
                correctas += 1

        if evaluadas == 0:
            return {
                "estado": "sin_datos",
                "motivo": "Aún no hay recomendaciones de apagado con ventana vencida para evaluar.",
                "ventana_min": self.VENTANA_ACIERTO_MIN,
            }

        return {
            "estado": "disponible",
            "evaluadas": evaluadas,
            "correctas": correctas,
            "precision_pct": round(correctas / evaluadas * 100, 1),
            "ventana_min": self.VENTANA_ACIERTO_MIN,
        }

    @staticmethod
    def _parsear_fecha(valor) -> datetime | None:
        if not valor:
            return None
        try:
            fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
        return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)

    # -----------------------------------------------------------------------
    # Características de entrenamiento
    # -----------------------------------------------------------------------

    def obtener_sala(self, sala_id: UUID | str) -> dict | None:
        cliente = obtener_cliente()
        respuesta = (
            cliente.table("rooms")
            .select("id,nombre,pabellon,edificio,aires")
            .eq("id", str(sala_id))
            .limit(1)
            .execute()
        )
        return respuesta.data[0] if respuesta.data else None

    def _consulta_registros_sala(self, sala_id: UUID | str, dias_atras: int) -> list[dict]:
        cliente = obtener_cliente()
        desde = (datetime.now(timezone.utc) - timedelta(days=dias_atras)).isoformat()

        respuesta = (
            cliente.table("registros")
            .select("*")
            .eq("sala_id", str(sala_id))
            .gte("fecha_sync", desde)
            .order("fecha_sync", desc=False)
            .execute()
        )
        if respuesta.data:
            return respuesta.data

        sala = self.obtener_sala(sala_id)
        if not sala:
            return []

        pabellon = sala.get("pabellon") or sala.get("edificio")
        aires = sala.get("aires") or []
        aire = sala.get("nombre")
        if not pabellon or (not aire and not aires):
            return []

        consulta = (
            cliente.table("registros")
            .select("*")
            .eq("pabellon", pabellon)
            .gte("fecha_sync", desde)
            .order("fecha_sync", desc=False)
        )
        consulta = consulta.in_("aire", aires) if aires else consulta.eq("aire", aire)
        respuesta = consulta.execute()
        return respuesta.data or []

    def obtener_ultimo_registro_sala(self, sala_id: UUID | str) -> dict | None:
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
            return respuesta.data[0]

        sala = self.obtener_sala(sala_id)
        if not sala:
            return None

        pabellon = sala.get("pabellon") or sala.get("edificio")
        aires = sala.get("aires") or []
        aire = sala.get("nombre")
        if not pabellon or (not aire and not aires):
            return None

        consulta = (
            cliente.table("registros")
            .select("*")
            .eq("pabellon", pabellon)
            .order("fecha_sync", desc=True)
            .limit(1)
        )
        consulta = consulta.in_("aire", aires) if aires else consulta.eq("aire", aire)
        respuesta = consulta.execute()
        if respuesta.data:
            return {**respuesta.data[0], "sala_id": str(sala_id)}
        return None

    def obtener_caracteristicas_desde_registros(
        self, sala_id: UUID | str, dias_atras: int = 30
    ) -> list[dict]:
        registros = self._consulta_registros_sala(sala_id, dias_atras)
        cubos: dict[datetime, list[dict]] = {}

        for registro in registros:
            temperatura = registro.get("temperatura_ambiente")
            humedad = registro.get("humedad")
            fecha_raw = registro.get("fecha_sync")
            if temperatura in (None, 0) or humedad in (None, 0) or not fecha_raw:
                continue

            try:
                fecha = datetime.fromisoformat(str(fecha_raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if fecha.tzinfo is None:
                fecha = fecha.replace(tzinfo=timezone.utc)

            cubo = fecha.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            cubos.setdefault(cubo, []).append(registro)

        caracteristicas: list[dict] = []
        for cubo, filas in sorted(cubos.items()):
            temperaturas = [
                float(f["temperatura_ambiente"])
                for f in filas
                if f.get("temperatura_ambiente") is not None
            ]
            humedades = [
                float(f["humedad"])
                for f in filas
                if f.get("humedad") is not None
            ]
            potencias = []
            for fila in filas:
                try:
                    potencia = float(fila.get("potencia_w"))
                except (TypeError, ValueError):
                    continue
                if isfinite(potencia) and potencia > 0:
                    potencias.append(potencia)
            energias = [
                float(f["energia_kwh"])
                for f in filas
                if f.get("energia_kwh") is not None
            ]
            presencias = [1 if f.get("estado_ocupacion") is True else 0 for f in filas]
            fechas = [f.get("fecha_sync") for f in filas if f.get("fecha_sync")]
            energia_total = (
                max(energias) - min(energias)
                if len(energias) >= 2
                else (energias[-1] if energias else 0.0)
            )

            caracteristicas.append({
                "cubo_hora": cubo.isoformat(),
                "temperatura_promedio": round(sum(temperaturas) / len(temperaturas), 2) if temperaturas else 0.0,
                "humedad_promedio": round(sum(humedades) / len(humedades), 2) if humedades else 0.0,
                "razon_presencia": round(sum(presencias) / len(presencias), 4) if presencias else 0.0,
                "potencia_promedio_w": round(sum(potencias) / len(potencias), 2) if potencias else 0.0,
                "energia_total_kwh": round(max(0.0, energia_total), 6),
                "dia_semana": cubo.weekday(),
                "hora_del_dia": cubo.hour,
                "cantidad_lecturas": len(filas),
                "fecha_ultima_lectura": max(fechas) if fechas else None,
                "fuente": "registros",
            })

        return caracteristicas

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
                "fuente":               "hourly_aggregates",
            })

        # Las filas históricas agregadas con 0 W no deben ocultar mediciones
        # reales disponibles en registros para la misma hora.
        desde_registros = self.obtener_caracteristicas_desde_registros(
            sala_id, dias_atras
        )
        if not caracteristicas:
            return desde_registros
        if not desde_registros:
            return caracteristicas

        def clave_hora(valor: object) -> str:
            try:
                fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
                return fecha.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                ).isoformat()
            except (TypeError, ValueError):
                return str(valor or "")

        def potencia_utilizable(valor: object) -> float | None:
            try:
                potencia = float(valor)
            except (TypeError, ValueError):
                return None
            return potencia if isfinite(potencia) and potencia > 0 else None

        registros_por_hora = {
            clave_hora(fila["cubo_hora"]): fila
            for fila in desde_registros
            if fila.get("cubo_hora")
        }
        horas_agregadas = set()
        for fila in caracteristicas:
            cubo = clave_hora(fila.get("cubo_hora"))
            horas_agregadas.add(cubo)
            registro = registros_por_hora.get(cubo)
            potencia_agregada = potencia_utilizable(fila.get("potencia_promedio_w"))
            potencia_registros = potencia_utilizable(
                registro.get("potencia_promedio_w") if registro else None
            )
            if potencia_agregada is None and potencia_registros is not None:
                fila["potencia_promedio_w"] = potencia_registros
                fila["fuente"] = "hourly_aggregates+registros"

        caracteristicas.extend(
            fila for fila in desde_registros
            if clave_hora(fila.get("cubo_hora")) not in horas_agregadas
        )
        return sorted(caracteristicas, key=lambda fila: fila["cubo_hora"])


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
