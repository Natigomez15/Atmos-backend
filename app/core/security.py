from app.config import configuracion


def verificar_api_key(api_key: str | None) -> bool:
    """Valida que la api_key coincida con CRON_SECRET del entorno."""
    if not api_key:
        return False
    return api_key == configuracion.CRON_SECRET
