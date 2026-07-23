from fastapi import Header, HTTPException

from app.config import configuracion
from app.core.database import obtener_cliente


def verificar_api_key(api_key: str | None) -> bool:
    if not api_key:
        return False
    return api_key == configuracion.CRON_SECRET


def obtener_usuario_actual(
    authorization: str | None = Header(default=None),
) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No autenticado")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        cliente = obtener_cliente()
        respuesta = cliente.auth.get_user(token)
        usuario = respuesta.user
        if not usuario:
            raise HTTPException(status_code=401, detail="Token inválido")

        perfil = (
            cliente.table("profiles")
            .select("id, correo, nombre, rol, esta_activo")
            .eq("id", str(usuario.id))
            .single()
            .execute()
        )
        if not perfil.data:
            raise HTTPException(status_code=403, detail="Perfil no encontrado")
        if not perfil.data.get("esta_activo"):
            raise HTTPException(status_code=403, detail="Usuario inactivo")

        return perfil.data
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")


def obtener_usuario_opcional(
    authorization: str | None = Header(default=None),
) -> dict | None:
    if not authorization:
        return None
    try:
        return obtener_usuario_actual(authorization)
    except Exception:
        return None


def requerir_admin(
    authorization: str | None = Header(default=None),
) -> dict:
    usuario = obtener_usuario_actual(authorization)
    if usuario.get("rol") != "admin":
        raise HTTPException(status_code=403, detail="Se requiere rol admin")
    return usuario


def requerir_mantenimiento_o_admin(
    authorization: str | None = Header(default=None),
) -> dict:
    usuario = obtener_usuario_actual(authorization)
    if usuario.get("rol") not in ("admin", "mantenimiento"):
        raise HTTPException(
            status_code=403,
            detail="Se requiere rol mantenimiento o admin",
        )
    return usuario
