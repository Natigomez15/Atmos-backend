from pydantic_settings import BaseSettings


class Configuracion(BaseSettings):
    # Supabase
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Firebase
    FIREBASE_API_KEY: str
    FIREBASE_AUTH_DOMAIN: str
    FIREBASE_DATABASE_URL: str
    FIREBASE_STORAGE_BUCKET: str

    model_config = {"env_file": ".env"}

    def firebase_config(self) -> dict:
        return {
            "apiKey": self.FIREBASE_API_KEY,
            "authDomain": self.FIREBASE_AUTH_DOMAIN,
            "databaseURL": self.FIREBASE_DATABASE_URL,
            "storageBucket": self.FIREBASE_STORAGE_BUCKET,
        }


configuracion = Configuracion()
