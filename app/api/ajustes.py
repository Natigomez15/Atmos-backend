from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.database import obtener_cliente
from app.core.security import obtener_usuario_actual, requerir_admin


enrutador = APIRouter(prefix="/ajustes", tags=["ajustes"])

CONFIGURACION_SISTEMA = {
    "nombre_pabellon": "robotica",
    "temperatura_confort_min": 20,
    "temperatura_confort_max": 24,
    "costo_kwh_usd": 0.17,
    "minutos_sin_senal": 10,
    "exceso_consumo_pct": 50,
    "temperatura_max_alerta": 32,
    "minutos_sin_enfriamiento": 30,
    "actualizado_en": None,
}


@enrutador.get("/perfil")
async def obtener_perfil(usuario_actual: dict = Depends(obtener_usuario_actual)):
    return usuario_actual


@enrutador.patch("/perfil")
async def actualizar_perfil(
    cambios: dict[str, Any],
    usuario_actual: dict = Depends(obtener_usuario_actual),
):
    if cambios.get("contrasenia_actual") or cambios.get("contrasenia_nueva"):
        raise HTTPException(
            status_code=422,
            detail="El cambio de contrasena no esta habilitado desde ATMOS.",
        )

    nombre = str(cambios.get("nombre") or "").strip()
    if not nombre:
        return usuario_actual

    cliente = obtener_cliente()
    respuesta = (
        cliente.table("profiles")
        .update({"nombre": nombre})
        .eq("id", usuario_actual["id"])
        .execute()
    )
    return respuesta.data[0] if respuesta.data else {**usuario_actual, "nombre": nombre}


@enrutador.get("/sistema")
async def obtener_configuracion_sistema(_=Depends(requerir_admin)):
    return CONFIGURACION_SISTEMA


@enrutador.patch("/sistema")
async def actualizar_configuracion_sistema(
    cambios: dict[str, Any],
    _=Depends(requerir_admin),
):
    return {**CONFIGURACION_SISTEMA, **cambios}

ROLES_USUARIO = {"admin", "mantenimiento", "usuario"}


@enrutador.get("/usuarios")
async def listar_usuarios(_=Depends(requerir_admin)):
    cliente = obtener_cliente()

    respuesta = (
        cliente.table("profiles")
        .select("id, correo, nombre, rol, esta_activo")
        .order("nombre")
        .execute()
    )

    return respuesta.data or []


@enrutador.patch("/usuarios/{usuario_id}")
async def actualizar_usuario(
    usuario_id: str,
    cambios: dict[str, Any],
    administrador: dict = Depends(requerir_admin),
):
    cliente = obtener_cliente()

    usuario_objetivo = (
        cliente.table("profiles")
        .select("id, correo, nombre, rol, esta_activo")
        .eq("id", usuario_id)
        .execute()
    )

    if not usuario_objetivo.data:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado",
        )

    actualizaciones: dict[str, Any] = {}

    if "rol" in cambios:
        rol = str(cambios["rol"] or "").strip().lower()

        if rol not in ROLES_USUARIO:
            raise HTTPException(
                status_code=422,
                detail="Rol no valido",
            )

        actualizaciones["rol"] = rol

    if "esta_activo" in cambios:
        esta_activo = cambios["esta_activo"]

        if not isinstance(esta_activo, bool):
            raise HTTPException(
                status_code=422,
                detail="esta_activo debe ser verdadero o falso",
            )

        actualizaciones["esta_activo"] = esta_activo

    if not actualizaciones:
        raise HTTPException(
            status_code=422,
            detail="No hay cambios validos para aplicar",
        )

    # Evitar que el administrador se bloquee accidentalmente.
    if str(administrador["id"]) == usuario_id:
        if actualizaciones.get("esta_activo") is False:
            raise HTTPException(
                status_code=422,
                detail="No puedes desactivar tu propia cuenta",
            )

        if (
            "rol" in actualizaciones
            and actualizaciones["rol"] != "admin"
        ):
            raise HTTPException(
                status_code=422,
                detail="No puedes quitarte tu propio rol de administrador",
            )

    respuesta = (
        cliente.table("profiles")
        .update(actualizaciones)
        .eq("id", usuario_id)
        .execute()
    )

    if not respuesta.data:
        raise HTTPException(
            status_code=500,
            detail="No se pudo actualizar el usuario",
        )

    return respuesta.data[0]
