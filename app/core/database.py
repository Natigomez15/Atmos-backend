import pyrebase
from supabase import create_client, Client
from app.config import configuracion

_cliente_supabase: Client | None = None
_cliente_firebase = None


def obtener_cliente() -> Client:
    global _cliente_supabase
    if _cliente_supabase is None:
        supabase_url = configuracion.SUPABASE_URL.strip().strip('"').strip("'")
        supabase_key = (
            configuracion.SUPABASE_KEY
            .strip()
            .strip('"')
            .strip("'")
            .removeprefix("Bearer ")
            .strip()
        )
        _cliente_supabase = create_client(
            supabase_url,
            supabase_key,
        )
    return _cliente_supabase


def obtener_firebase():
    global _cliente_firebase
    if _cliente_firebase is None:
        app = pyrebase.initialize_app(configuracion.firebase_config())
        _cliente_firebase = app.database()
    return _cliente_firebase
