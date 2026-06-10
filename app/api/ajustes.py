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
