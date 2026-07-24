import os

from pydantic import Field
from pydantic_settings import BaseSettings


class Configuracion(BaseSettings):
    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""

    # Cron
    CRON_SECRET: str = ""
    ATMOS_DEVICE_TOKEN: str = ""

    # Firebase
    FIREBASE_API_KEY: str = ""
    FIREBASE_AUTH_DOMAIN: str = ""
    FIREBASE_DATABASE_URL: str = ""
    FIREBASE_STORAGE_BUCKET: str = ""
    FIREBASE_SYNC_AUTOSTART: bool = False
    # La sincronización consulta Firebase por REST. Un mínimo de un minuto evita
    # descargar repetidamente la misma ventana de lecturas cuando el ESP32 no
    # ha publicado datos nuevos.
    FIREBASE_SYNC_INTERVAL_SECONDS: int = Field(default=60, ge=60)

    # Control físico del AC: seguro por defecto. Solo ``active`` + ``true``
    # permite crear o publicar una orden ejecutable.
    ATMOS_CONTROL_MODE: str = "dry_run"
    ATMOS_IR_CONTROL_ENABLED: bool = False
    ATMOS_COMMAND_TTL_SECONDS: int = 120

    # Dashboard energetico
    # Baseline validado pre-ATMOS usado para calcular ahorro estimado.
    DASHBOARD_BASELINE_KWH_DIA: float = 36.0
    DASHBOARD_TARIFA_USD_KWH: float = 0.17
    # Histéresis para inferir el estado físico del AC desde potencia activa.
    AC_POWER_ON_THRESHOLD_W: float = 700.0
    AC_POWER_OFF_THRESHOLD_W: float = 700.0
    AC_COMPRESSOR_ON_THRESHOLD_W: float = 700.0
    AC_POWER_THRESHOLDS_CALIBRATED: bool = True

    # VAPID — notificaciones push
    vapid_clave_privada: str = ""
    vapid_clave_publica: str = ""
    vapid_correo:        str = ""

    model_config = {
        "env_file": None
        if os.getenv("SUPABASE_URL")
        else (".env", ".env.runtime"),
    }

    def firebase_config(self) -> dict:
        return {
            "apiKey": self.FIREBASE_API_KEY,
            "authDomain": self.FIREBASE_AUTH_DOMAIN,
            "databaseURL": self.FIREBASE_DATABASE_URL,
            "storageBucket": self.FIREBASE_STORAGE_BUCKET,
        }


configuracion = Configuracion()
