from pydantic_settings import BaseSettings


class Configuracion(BaseSettings):
    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Cron
    CRON_SECRET: str

    # Firebase
    FIREBASE_API_KEY: str
    FIREBASE_AUTH_DOMAIN: str
    FIREBASE_DATABASE_URL: str
    FIREBASE_STORAGE_BUCKET: str

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    WORKING_HOURS_START: int = 7
    WORKING_HOURS_END: int = 18
    WORKING_DAYS: str = "0,1,2,3,4,5"

    model_config = {"env_file": ".env"}

    def firebase_config(self) -> dict:
        return {
            "apiKey": self.FIREBASE_API_KEY,
            "authDomain": self.FIREBASE_AUTH_DOMAIN,
            "databaseURL": self.FIREBASE_DATABASE_URL,
            "storageBucket": self.FIREBASE_STORAGE_BUCKET,
        }


configuracion = Configuracion()
