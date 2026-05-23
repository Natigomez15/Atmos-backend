from pydantic import BaseModel, ConfigDict, Field, field_validator
from datetime import datetime
from typing import Optional, Literal
from uuid import UUID
import re


# ---------------------------------------------------------------------------
# Registros (tabla principal sincronizada desde Firebase)
# ---------------------------------------------------------------------------

class RegistroCrear(BaseModel):
    sensor: str
    temperatura_dht11: Optional[float] = None
    temperatura_ds18b20: Optional[float] = None
    humedad: Optional[float] = None
    movimiento: Optional[int] = None
    fecha: Optional[datetime] = None


class RegistroRespuesta(RegistroCrear):
    model_config = ConfigDict(from_attributes=True)

    id: int


# ---------------------------------------------------------------------------
# Salas
# ---------------------------------------------------------------------------

class SalaCrear(BaseModel):
    nombre: str
    pabellon: Optional[str] = None
    capacidad: Optional[int] = None
    area_m2: Optional[float] = None
    piso: Optional[int] = None
    marca_ac: Optional[str] = None
    modelo_ac: Optional[str] = None


class SalaActualizar(BaseModel):
    nombre: Optional[str] = None
    pabellon: Optional[str] = None
    capacidad: Optional[int] = None
    area_m2: Optional[float] = None
    piso: Optional[int] = None
    marca_ac: Optional[str] = None
    modelo_ac: Optional[str] = None


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


class ComandoACRespuesta(ComandoACCrear):
    model_config = ConfigDict(from_attributes=True)

    id: int
    enviado_en: datetime
    ejecutado_en: Optional[datetime] = None
    fue_ejecutado: bool


class ComandoPendienteRespuesta(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
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
    sala_id: UUID
    nodo_id: Optional[UUID] = None
    tipo_alerta: str
    severidad: str
    mensaje: str
    detalle: Optional[dict] = None
    esta_resuelta: bool
    creado_en: datetime
    resuelto_en: Optional[datetime] = None
