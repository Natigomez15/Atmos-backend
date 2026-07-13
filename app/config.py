import os

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
    FIREBASE_SYNC_AUTOSTART: bool = True
    FIREBASE_SYNC_INTERVAL_SECONDS: int = 10

    # Dashboard energetico
    # Baseline validado pre-ATMOS usado para calcular ahorro estimado.
    DASHBOARD_BASELINE_KWH_DIA: float = 36.0
    DASHBOARD_TARIFA_USD_KWH: float = 0.17

    # VAPID — notificaciones push
    vapid_clave_privada: str = ""
    vapid_clave_publica: str = ""
    vapid_correo:        str = ""

    model_config = {"env_file": None if os.getenv("SUPABASE_URL") else ".env"}

    def firebase_config(self) -> dict:
        return {
            "apiKey": self.FIREBASE_API_KEY,
            "authDomain": self.FIREBASE_AUTH_DOMAIN,
            "databaseURL": self.FIREBASE_DATABASE_URL,
            "storageBucket": self.FIREBASE_STORAGE_BUCKET,
        }


configuracion = Configuracion()
