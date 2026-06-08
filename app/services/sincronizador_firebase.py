import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.core.database import obtener_cliente, obtener_firebase


ZONA_HORARIA_PANAMA = timezone(timedelta(hours=-5), "America/Panama")


def _a_booleano(valor: Any) -> bool:
    if isinstance(valor, bool):
        return valor
    texto = str(valor).strip().lower()
    return texto in {
        "1",
        "true",
        "si",
        "sí",
        "ocupado",
        "con_ocupacion",
        "detectado",
        "presente",
    }


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


def _a_uuid(valor: Any) -> str | None:
    if not valor:
        return None
    try:
        return str(UUID(str(valor)))
    except (TypeError, ValueError):
        return None


def _normalizar_texto(valor: Any) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def resolver_sala_id(supabase, pabellon: str, aire: str, valor: dict) -> str | None:
    sala_id = _a_uuid(valor.get("sala_id"))
    if sala_id:
        return sala_id

    nombre_sala = valor.get("sala", valor.get("nombre_sala", valor.get("room")))
    objetivo = _normalizar_texto(nombre_sala or aire)

    try:
        respuesta = (
            supabase.table("rooms")
            .select("id,nombre,pabellon,edificio")
            .or_(f"pabellon.eq.{pabellon},edificio.eq.{pabellon}")
            .execute()
        )
    except Exception:
        return None

    salas = respuesta.data or []
    for sala in salas:
        if _normalizar_texto(sala.get("nombre")) == objetivo:
            return sala.get("id")

    if len(salas) == 1:
        return salas[0].get("id")

    return None


def resolver_nodo_id(supabase, sala_id: str | None, valor: dict) -> str | None:
    nodo_id = _a_uuid(valor.get("nodo_id"))
    if nodo_id:
        return nodo_id

    direccion_mac = valor.get("direccion_mac", valor.get("mac"))
    try:
        if direccion_mac:
            respuesta = (
                supabase.table("nodes")
                .select("id")
                .eq("direccion_mac", str(direccion_mac).upper())
                .limit(1)
                .execute()
            )
            return respuesta.data[0]["id"] if respuesta.data else None

        if sala_id:
            respuesta = (
                supabase.table("nodes")
                .select("id")
                .eq("sala_id", sala_id)
                .eq("esta_activo", True)
                .limit(2)
                .execute()
            )
            if len(respuesta.data or []) == 1:
                return respuesta.data[0]["id"]
    except Exception:
        return None

    return None


def preparar_registro_supabase(
    pabellon: str,
    aire: str,
    firebase_key: str,
    valor: dict,
    sala_id: str | None = None,
    nodo_id: str | None = None,
) -> dict:
    temperatura_ambiente = _a_numero(
        valor.get(
            "temperatura_ambiente",
            valor.get("temperatura", valor.get("temperatura_dht11")),
        )
    )
    temperatura_salida_aire = _a_numero(
        valor.get(
            "temperatura_salida_aire",
            valor.get("temperatura_ac", valor.get("temperatura_ds18b20")),
        )
    )
    delta_t = _a_numero(
        valor.get("delta_t"),
        temperatura_ambiente - temperatura_salida_aire,
    )

    return {
        "firebase_key": f"{pabellon}_{aire}_{firebase_key}",
        "sala_id": sala_id,
        "nodo_id": nodo_id,
        "pabellon": valor.get("pabellon", pabellon),
        "aire": valor.get("aire", aire),
        "temperatura_ambiente": temperatura_ambiente,
        "humedad": _a_numero(valor.get("humedad")),
        "temperatura_salida_aire": temperatura_salida_aire,
        "delta_t": delta_t,
        "movimiento": _a_entero(valor.get("movimiento")),
        "potencia_w": _a_numero(
            valor.get("potencia_w", valor.get("power_w")),
            defecto=None,
        ),
        "energia_kwh": _a_numero(
            valor.get("energia_kwh", valor.get("energy_kwh")),
            defecto=None,
        ),
        "estado_ocupacion": _a_booleano(
            valor.get("estado_ocupacion", valor.get("presencia", valor.get("ocupado", 0)))
        ),
        "recomendacion_local": valor.get(
            "recomendacion_local",
            valor.get("recomendacion", ""),
        ),
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

                sala_id = resolver_sala_id(supabase, pabellon, aire, valor)
                nodo_id = resolver_nodo_id(supabase, sala_id, valor)
                registro = preparar_registro_supabase(
                    pabellon=pabellon,
                    aire=aire,
                    firebase_key=firebase_key,
                    valor=valor,
                    sala_id=sala_id,
                    nodo_id=nodo_id,
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
