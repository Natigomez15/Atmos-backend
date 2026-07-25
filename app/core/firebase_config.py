import os
from pathlib import Path

import firebase_admin
from dotenv import load_dotenv
from firebase_admin import credentials, db

load_dotenv()


def _ruta_credencial() -> Path:
    """
    Obtiene la ruta segura de la credencial de Firebase Admin.

    Puede definirse FIREBASE_ADMIN_CREDENTIALS en .env.
    Si no existe, usa la carpeta privada del usuario:
    C:\\Users\\TU_USUARIO\\.atmos-secrets\\atmos-firebase-admin.json
    """
    ruta_env = os.getenv("FIREBASE_ADMIN_CREDENTIALS", "").strip().strip('"').strip("'")

    if ruta_env:
        return Path(os.path.expandvars(os.path.expanduser(ruta_env)))

    return Path.home() / ".atmos-secrets" / "atmos-firebase-admin.json"


def inicializar_firebase_admin():
    """
    Inicializa Firebase Admin una sola vez.
    """
    if firebase_admin._apps:
        return firebase_admin.get_app()

    ruta_credencial = _ruta_credencial()

    if not ruta_credencial.exists():
        raise FileNotFoundError(
            f"No se encontró la credencial de Firebase Admin en: {ruta_credencial}"
        )

    database_url = (
        os.getenv("FIREBASE_DATABASE_URL", "")
        .strip()
        .strip('"')
        .strip("'")
    )

    if not database_url:
        raise RuntimeError(
            "FIREBASE_DATABASE_URL no está configurada en el archivo .env."
        )

    credencial = credentials.Certificate(str(ruta_credencial))

    return firebase_admin.initialize_app(
        credencial,
        {
            "databaseURL": database_url,
        },
    )


def obtener_referencia_admin(ruta: str = "/"):
    """
    Devuelve una referencia autenticada de Firebase Realtime Database.
    """
    app = inicializar_firebase_admin()
    return db.reference(ruta, app=app)