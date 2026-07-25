from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from datetime import datetime
from typing import Optional, Literal, List
from uuid import UUID
import re


# ---------------------------------------------------------------------------
# Registros (tabla principal sincronizada desde Firebase)
# ---------------------------------------------------------------------------

class RegistroCrear(BaseModel):
    firebase_key: str
    sala_id: Optional[UUID] = None
    nodo_id: Optional[UUID] = None
    pabellon: Optional[str] = None
    aire: Optional[str] = None
    temperatura_ambiente: Optional[float] = None
    humedad: Optional[float] = None
    temperatura_salida_aire: Optional[float] = None
    delta_t: Optional[float] = None
    movimiento: Optional[int] = None
    potencia_w: Optional[float] = None
    corriente_rms: Optional[float] = None
    corriente_rms_cruda: Optional[float] = None
    corriente_rms_instantanea: Optional[float] = None
    corriente_calculada_vpp: Optional[float] = None
    factor_calibracion_sct: Optional[float] = None
    corriente_retenida_por_filtro: Optional[bool] = None
    ceros_consecutivos_sct: Optional[int] = None
    voltaje_red_v: Optional[float] = None
    factor_potencia: Optional[float] = None
    potencia_aparente_va: Optional[float] = None
    potencia_activa_w: Optional[float] = None
    potencia_activa_kw: Optional[float] = None
    consumo_intervalo_kwh: Optional[float] = None
    consumo_acumulado_sesion_kwh: Optional[float] = None
    tarifa_kwh: Optional[float] = None
    costo_intervalo: Optional[float] = None
    costo_acumulado_sesion: Optional[float] = None
    dht_ok: Optional[bool] = None
    ds18b20_ok: Optional[bool] = None
    fallos_dht: Optional[int] = None
    fallos_ds18b20: Optional[int] = None
    energia_kwh: Optional[float] = None
    estado_ocupacion: Optional[bool] = None
    recomendacion_local: Optional[str] = None
    control_ir_activo: Optional[bool] = None
    aire_encendido_atmos: Optional[bool] = None
    estado_deseado: Optional[str] = None
    ultimo_comando_enviado: Optional[str] = None
    estado_reportado_por_software: Optional[str] = None
    estado_electrico_observado: Optional[str] = None
    compresor_confirmado: Optional[bool] = None
    ultima_accion_ejecutada: Optional[str] = None
    fecha_sync: Optional[datetime] = None


class RegistroRespuesta(RegistroCrear):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None


# ---------------------------------------------------------------------------
# Salas
# ---------------------------------------------------------------------------

class SalaCrear(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    nombre: str = Field(validation_alias=AliasChoices("nombre", "name"))
    pabellon: Optional[str] = Field(default=None, validation_alias=AliasChoices("pabellon", "pavilion", "edificio"))
    capacidad: Optional[int] = Field(default=None, validation_alias=AliasChoices("capacidad", "capacity"))
    area_m2: Optional[float] = None
    piso: Optional[int] = None
    marca_ac: Optional[str] = Field(default=None, validation_alias=AliasChoices("marca_ac", "ac_brand"))
    modelo_ac: Optional[str] = Field(default=None, validation_alias=AliasChoices("modelo_ac", "ac_model"))
    aires: Optional[List[str]] = Field(default_factory=list)
    tipo: Literal["laboratorio", "oficina", "salon"] = "laboratorio"
    activo: bool = True


class SalaActualizar(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    nombre: Optional[str] = Field(default=None, validation_alias=AliasChoices("nombre", "name"))
    pabellon: Optional[str] = Field(default=None, validation_alias=AliasChoices("pabellon", "pavilion", "edificio"))
    capacidad: Optional[int] = Field(default=None, validation_alias=AliasChoices("capacidad", "capacity"))
    area_m2: Optional[float] = None
    piso: Optional[int] = None
    marca_ac: Optional[str] = Field(default=None, validation_alias=AliasChoices("marca_ac", "ac_brand"))
    modelo_ac: Optional[str] = Field(default=None, validation_alias=AliasChoices("modelo_ac", "ac_model"))
    aires: Optional[List[str]] = None
    tipo: Optional[Literal["laboratorio", "oficina", "salon"]] = None
    activo: Optional[bool] = None


class SalaRespuesta(SalaCrear):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    creado_en: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Nodos
# ---------------------------------------------------------------------------

class NodoCrear(BaseModel):
    sala_id: UUID
    direccion_mac: str
    tipo_nodo: Literal["master", "sensor"]
    version_firmware: Optional[str] = None

    @field_validator("direccion_mac")
    @classmethod
    def validar_mac(cls, v: str) -> str:
        patron = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
        if not re.match(patron, v):
            raise ValueError("Formato de MAC inválido. Use XX:XX:XX:XX:XX:XX")
        return v.upper()


class NodoActualizar(BaseModel):
    esta_activo: Optional[bool] = None
    version_firmware: Optional[str] = None
    ultima_vez_visto: Optional[datetime] = None


class NodoRespuesta(NodoCrear):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    esta_activo: bool
    ultima_vez_visto: Optional[datetime] = None
    creado_en: datetime


# ---------------------------------------------------------------------------
# Lecturas de sensores (ESP32 → sensor_readings)
# ---------------------------------------------------------------------------

class LecturaSensorCrear(BaseModel):
    nodo_id: UUID
    sala_id: UUID
    temperatura: Optional[float] = None
    humedad: Optional[float] = None
    presencia: Optional[bool] = None
    setpoint_ac: Optional[int] = None
    ac_encendido: Optional[bool] = None
    voltaje: Optional[float] = None
    corriente_a: Optional[float] = None
    potencia_w: Optional[float] = None
    energia_kwh: Optional[float] = None


class LecturaSensorRespuesta(LecturaSensorCrear):
    model_config = ConfigDict(from_attributes=True)

    id: int
    registrado_en: datetime


# ---------------------------------------------------------------------------
# Comandos AC
# ---------------------------------------------------------------------------

class ComandoACCrear(BaseModel):
    sala_id: UUID
    nodo_id: Optional[UUID] = None
    tipo_comando: Literal["on", "off", "setpoint", "mode", "fan_speed"]
    setpoint: Optional[int] = Field(None, ge=16, le=30)
    modo: Optional[str] = None
    origen: Literal["ml_model", "manual", "schedule", "emergency"]
    pabellon: Optional[str] = None
    aire: Optional[str] = None
    accion: Optional[Literal[
        "apagar", "ahorro_24", "encender_22", "encender_23", "enfriar_fuerte"
    ]] = None


class ComandoACRespuesta(ComandoACCrear):
    model_config = ConfigDict(from_attributes=True)

    id: int
    enviado_en: datetime
    command_id: Optional[UUID] = None
    created_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    estado: Optional[str] = None
    ejecutado_en: Optional[datetime] = None
    fue_ejecutado: bool


class ComandoPendienteRespuesta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    command_id: UUID
    created_at: datetime
    expires_at: datetime
    estado: str
    pabellon: str
    aire: str
    accion: str
    tipo_comando: str
    setpoint: Optional[int] = None
    modo: Optional[str] = None
    enviado_en: datetime


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

class AlertaRespuesta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sala_id: Optional[UUID] = None
    nodo_id: Optional[UUID] = None
    tipo_alerta: str
    severidad: str
    mensaje: str
    detalle: Optional[dict] = None
    esta_resuelta: bool
    creado_en: datetime
    resuelto_en: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Suscripciones push (PWA)
# ---------------------------------------------------------------------------

class SuscripcionPushCrear(BaseModel):
    endpoint: str
    p256dh: Optional[str] = None
    auth: Optional[str] = None
    keys: Optional[dict] = None
    clave_p256dh: Optional[str] = None
    clave_auth: Optional[str] = None
    permiso: str = "granted"
    user_agent: Optional[str] = None
    # días separados por coma: 0=lunes, 6=domingo
    dias_activos: str = "0,1,2,3,4"
    hora_inicio: int = Field(7, ge=0, le=23)
    hora_fin: int = Field(18, ge=0, le=23)

    @property
    def llave_p256dh(self) -> str:
        return self.p256dh or self.clave_p256dh or (self.keys or {}).get("p256dh") or ""

    @property
    def llave_auth(self) -> str:
        return self.auth or self.clave_auth or (self.keys or {}).get("auth") or ""


class SuscripcionPushRespuesta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[UUID | int] = None
    profile_id: Optional[UUID] = None
    usuario_id: Optional[UUID] = None
    endpoint: str
    p256dh: Optional[str] = None
    auth: Optional[str] = None
    permiso: Optional[str] = None
    user_agent: Optional[str] = None
    activa: Optional[bool] = None
    creado_en: Optional[datetime] = None
    actualizado_en: Optional[datetime] = None


class ActualizarHorarioNotificaciones(BaseModel):
    dias_activos: Optional[str]  = None
    hora_inicio:  Optional[int]  = None
    hora_fin:     Optional[int]  = None
    esta_activa:  Optional[bool] = None
