"""Guardas puras y configuración fail-closed para el control infrarrojo."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.config import configuracion


ACCIONES_IR_EXPLICITAS = {
    "apagar",
    "ahorro_24",
    "encender_22",
    "encender_23",
    "enfriar_fuerte",
}
ESTADOS_COMANDO_EJECUTABLES = {"pendiente"}


def normalizar_accion_ir(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def control_ir_habilitado() -> bool:
    """Control automático: únicamente disponible en modo active."""
    return (
        str(configuracion.ATMOS_CONTROL_MODE).strip().lower() == "active"
        and configuracion.ATMOS_IR_CONTROL_ENABLED is True
    )


def control_manual_ir_habilitado() -> bool:
    """Órdenes humanas explícitas, sin habilitar predictor ni horarios."""
    return (
        str(configuracion.ATMOS_CONTROL_MODE).strip().lower()
        in {"active", "manual_only"}
        and configuracion.ATMOS_IR_CONTROL_ENABLED is True
    )


def control_solo_manual() -> bool:
    return str(configuracion.ATMOS_CONTROL_MODE).strip().lower() == "manual_only"


def motivo_bloqueo_control_ir() -> str | None:
    modo = str(configuracion.ATMOS_CONTROL_MODE).strip().lower()
    if modo != "active":
        return f"control_en_modo_{modo or 'no_configurado'}"
    if configuracion.ATMOS_IR_CONTROL_ENABLED is not True:
        return "control_ir_deshabilitado"
    return None


def motivo_bloqueo_control_manual_ir() -> str | None:
    modo = str(configuracion.ATMOS_CONTROL_MODE).strip().lower()
    if modo not in {"active", "manual_only"}:
        return f"control_en_modo_{modo or 'no_configurado'}"
    if configuracion.ATMOS_IR_CONTROL_ENABLED is not True:
        return "control_ir_deshabilitado"
    return None


def accion_es_no_op(accion: Any) -> bool:
    return normalizar_accion_ir(accion) in {"", "mantener", "no_op", "ninguno"}


def estado_deseado_desde_accion(accion: Any) -> str | None:
    accion_normalizada = normalizar_accion_ir(accion)
    if accion_normalizada == "apagar":
        return "apagado"
    if accion_normalizada in ACCIONES_IR_EXPLICITAS:
        return "encendido"
    return None


def evaluar_autorizacion_automatica(
    accion: Any,
    estado_electrico_observado: str | None,
) -> dict:
    accion_normalizada = normalizar_accion_ir(accion)
    if accion_es_no_op(accion_normalizada):
        return {"autorizada": False, "motivo": "decision_no_op"}
    if accion_normalizada not in ACCIONES_IR_EXPLICITAS:
        return {"autorizada": False, "motivo": "accion_no_explicita"}
    if estado_electrico_observado not in {"encendido", "apagado"}:
        return {"autorizada": False, "motivo": "estado_electrico_no_confirmado"}
    estado_deseado = estado_deseado_desde_accion(accion_normalizada)
    if estado_deseado != estado_electrico_observado:
        return {"autorizada": False, "motivo": "discrepancia_estado_deseado_observado"}
    if accion_normalizada == "apagar":
        return {"autorizada": False, "motivo": "equipo_ya_apagado"}
    return {"autorizada": True, "motivo": "accion_explicita_sobre_equipo_encendido"}


def construir_comando_ir(
    *,
    pabellon: str,
    aire: str,
    accion: str,
    ahora: datetime | None = None,
    ttl_segundos: int | None = None,
) -> dict:
    accion_normalizada = normalizar_accion_ir(accion)
    if accion_normalizada not in ACCIONES_IR_EXPLICITAS:
        raise ValueError("La acción no es una orden IR explícita y ejecutable.")
    pabellon_real = str(pabellon or "").strip()
    aire_real = str(aire or "").strip()
    if not pabellon_real or not aire_real:
        raise ValueError("Todo comando IR requiere pabellon y aire explícitos.")

    creado = ahora or datetime.now(timezone.utc)
    if creado.tzinfo is None:
        creado = creado.replace(tzinfo=timezone.utc)
    ttl = ttl_segundos if ttl_segundos is not None else configuracion.ATMOS_COMMAND_TTL_SECONDS
    if ttl <= 0:
        raise ValueError("La vigencia del comando debe ser mayor que cero.")

    return {
        "command_id": str(uuid4()),
        "created_at": creado.isoformat(),
        "expires_at": (creado + timedelta(seconds=ttl)).isoformat(),
        "estado": "pendiente",
        "pabellon": pabellon_real,
        "aire": aire_real,
        "accion": accion_normalizada,
        "estado_deseado": estado_deseado_desde_accion(accion_normalizada),
    }


def _fecha_iso(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        fecha = valor
    elif valor:
        try:
            fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha


def evaluar_comando_para_ejecucion(
    comando: dict,
    *,
    pabellon: str,
    aire: str,
    ultimo_command_id: str | None = None,
    ahora: datetime | None = None,
) -> dict:
    """Valida el sobre que debe comprobar el ESP32 antes de emitir IR."""
    command_id = str(comando.get("command_id") or "").strip()
    if not command_id:
        return {"ejecutable": False, "motivo": "command_id_ausente"}
    if ultimo_command_id and command_id == str(ultimo_command_id):
        return {"ejecutable": False, "motivo": "command_id_ya_ejecutado"}
    if str(comando.get("pabellon") or "").strip() != str(pabellon).strip():
        return {"ejecutable": False, "motivo": "pabellon_no_coincide"}
    if str(comando.get("aire") or "").strip() != str(aire).strip():
        return {"ejecutable": False, "motivo": "aire_no_coincide"}
    if normalizar_accion_ir(comando.get("accion")) not in ACCIONES_IR_EXPLICITAS:
        return {"ejecutable": False, "motivo": "accion_no_ejecutable"}
    if str(comando.get("estado") or "").strip().lower() not in ESTADOS_COMANDO_EJECUTABLES:
        return {"ejecutable": False, "motivo": "estado_no_ejecutable"}

    creado = _fecha_iso(comando.get("created_at"))
    expira = _fecha_iso(comando.get("expires_at"))
    if creado is None:
        return {"ejecutable": False, "motivo": "created_at_ausente_o_invalido"}
    if expira is None:
        return {"ejecutable": False, "motivo": "expires_at_ausente_o_invalido"}
    instante = ahora or datetime.now(timezone.utc)
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=timezone.utc)
    if creado > instante:
        return {"ejecutable": False, "motivo": "comando_futuro"}
    if expira <= instante:
        return {"ejecutable": False, "motivo": "comando_vencido"}
    return {"ejecutable": True, "motivo": "comando_vigente"}
