from datetime import datetime, timezone
from typing import Any


CONSUMO_AC_KW = 1.5
TARIFA_KWH = 0.18
CANTIDAD_AIRES = 1

ACCIONES_APAGADO = {"apagar", "off", "apagado"}
ACCIONES_ENCENDIDO = {
    "mantener",
    "encender_22",
    "ahorro_24",
    "enfriar_fuerte",
    "on",
    "encendido",
}


def _normalizar(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def _a_bool(valor: Any) -> bool | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return valor
    texto = _normalizar(valor)
    if texto in {"1", "true", "si", "sí", "on", "encendido", "ocupado"}:
        return True
    if texto in {"0", "false", "no", "off", "apagado", "sin_ocupacion"}:
        return False
    return None


def _fecha(valor: Any) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        fecha = valor
    else:
        try:
            fecha = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    if fecha.tzinfo is None:
        return fecha.replace(tzinfo=timezone.utc)
    return fecha


def inferir_accion(valor: dict[str, Any]) -> str:
    return _normalizar(
        valor.get("ultima_accion_ejecutada")
        or valor.get("accion")
        or valor.get("decision_final")
        or valor.get("recomendacion_local")
        or valor.get("recomendacion")
    )


def inferir_ac_encendido(valor: dict[str, Any], registro_anterior: dict[str, Any] | None = None) -> bool:
    estado_explicito = _a_bool(
        valor.get("aire_encendido_atmos", valor.get("ac_encendido", valor.get("estado_ac")))
    )
    if estado_explicito is not None:
        return estado_explicito

    accion = inferir_accion(valor)
    if accion in ACCIONES_APAGADO:
        return False
    if accion in ACCIONES_ENCENDIDO:
        return True

    if registro_anterior and registro_anterior.get("potencia_w") is not None:
        return float(registro_anterior["potencia_w"]) > 0

    return False


def estimar_consumo_registro(
    valor: dict[str, Any],
    registro_anterior: dict[str, Any] | None,
    fecha_actual: datetime,
) -> dict[str, float]:
    ac_encendido = inferir_ac_encendido(valor, registro_anterior)
    potencia_w = CONSUMO_AC_KW * CANTIDAD_AIRES * 1000 if ac_encendido else 0.0

    energia_anterior = 0.0
    if registro_anterior and registro_anterior.get("energia_kwh") is not None:
        energia_anterior = float(registro_anterior["energia_kwh"])

    fecha_anterior = _fecha(registro_anterior.get("fecha_sync")) if registro_anterior else None
    horas_transcurridas = 0.0
    if fecha_anterior:
        horas_transcurridas = max(
            0.0,
            (fecha_actual - fecha_anterior.astimezone(fecha_actual.tzinfo)).total_seconds() / 3600,
        )

    energia_kwh = energia_anterior + (potencia_w / 1000) * horas_transcurridas
    costo_estimado = energia_kwh * TARIFA_KWH

    return {
        "potencia_w": round(potencia_w, 2),
        "energia_kwh": round(energia_kwh, 6),
        "costo_estimado": round(costo_estimado, 4),
    }
