import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.database import obtener_cliente, obtener_firebase


ZONA_HORARIA_PANAMA = timezone(timedelta(hours=-5), "America/Panama")


def _a_booleano(valor: Any) -> bool:
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    return texto in {"1", "true", "si", "sí", "ocupado", "detectado", "presente"}


def _a_numero(valor: Any, defecto: float = 0.0) -> float:
    if valor is None or valor == "":
        return defecto
    try:
        return float(valor)
    except (TypeError, ValueError):
        return defecto


def _a_entero(valor: Any, defecto: int = 0) -> int:
    if valor is None or valor == "":
        return defecto
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def preparar_registro_supabase(
    pabellon: str,
    aire: str,
    firebase_key: str,
    valor: dict,
) -> dict:
    temperatura_ambiente = _a_numero(
        valor.get("temperatura_ambiente", valor.get("temperatura"))
    )
    temperatura_salida_aire = _a_numero(
        valor.get("temperatura_salida_aire", valor.get("temperatura_ac"))
    )
    delta_t = _a_numero(
        valor.get("delta_t"),
        temperatura_ambiente - temperatura_salida_aire,
    )

    return {
        "firebase_key": f"{pabellon}_{aire}_{firebase_key}",
        "pabellon": valor.get("pabellon", pabellon),
        "aire": valor.get("aire", aire),
        "temperatura_ambiente": temperatura_ambiente,
        "humedad": _a_numero(valor.get("humedad")),
        "temperatura_salida_aire": temperatura_salida_aire,
        "delta_t": delta_t,
        "movimiento": _a_entero(valor.get("movimiento")),
        "estado_ocupacion": _a_booleano(
            valor.get("estado_ocupacion", valor.get("presencia", valor.get("ocupado", 0)))
        ),
        "recomendacion_local": valor.get("recomendacion_local", ""),
        "control_ir_activo": _a_booleano(valor.get("control_ir_activo", False)),
        "aire_encendido_atmos": (
            None
            if valor.get("aire_encendido_atmos") is None
            else _a_booleano(valor.get("aire_encendido_atmos"))
        ),
        "ultima_accion_ejecutada": valor.get("ultima_accion_ejecutada", ""),
        "fecha_sync": datetime.now(ZONA_HORARIA_PANAMA).isoformat(),
    }


def sincronizar_firebase_supabase() -> dict:
    firebase_db = obtener_firebase()
    supabase = obtener_cliente()
    datos = firebase_db.child("Atmos").child("registro").get().val()

    if not datos:
        return {"sincronizados": 0, "errores": 0, "mensaje": "No hay datos en Firebase."}

    sincronizados = 0
    errores = 0

    for pabellon, aires in datos.items():
        if not isinstance(aires, dict):
            continue

        for aire, contenido in aires.items():
            if not isinstance(contenido, dict):
                continue

            lecturas = contenido.get("lecturas", {})
            if not isinstance(lecturas, dict):
                continue

            for firebase_key, valor in lecturas.items():
                if not isinstance(valor, dict):
                    errores += 1
                    continue

                registro = preparar_registro_supabase(
                    pabellon=pabellon,
                    aire=aire,
                    firebase_key=firebase_key,
                    valor=valor,
                )

                try:
                    supabase.table("registros").upsert(
                        registro,
                        on_conflict="firebase_key",
                    ).execute()
                    sincronizados += 1
                except Exception:
                    errores += 1

    return {"sincronizados": sincronizados, "errores": errores}


def sincronizar(intervalo_segundos: int = 15):
    print("Sincronizando ATMOS Firebase -> Supabase...")

    while True:
        resultado = sincronizar_firebase_supabase()
        print("Resultado sincronización:", resultado)
        time.sleep(intervalo_segundos)


if __name__ == "__main__":
    sincronizar()
