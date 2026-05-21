from typing import Any


def agregar_lecturas(registros: list[dict[str, Any]]) -> dict[str, Any]:
    if not registros:
        return {"cantidad": 0, "temperatura_promedio": None, "humedad_promedio": None}

    temperaturas = [r["temperatura_dht11"] for r in registros if r.get("temperatura_dht11") is not None]
    humedades = [r["humedad"] for r in registros if r.get("humedad") is not None]
    con_movimiento = sum(1 for r in registros if r.get("movimiento"))

    return {
        "cantidad": len(registros),
        "temperatura_promedio": sum(temperaturas) / len(temperaturas) if temperaturas else None,
        "temperatura_minima": min(temperaturas) if temperaturas else None,
        "temperatura_maxima": max(temperaturas) if temperaturas else None,
        "humedad_promedio": sum(humedades) / len(humedades) if humedades else None,
        "registros_con_movimiento": con_movimiento,
    }
