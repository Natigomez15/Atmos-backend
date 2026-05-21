from typing import Any


def predecir_consumo(historial: list[dict[str, Any]]) -> dict[str, Any]:
    """Predicción de consumo energético basada en historial de lecturas."""
    if not historial:
        return {"valor_predicho": None, "confianza": 0.0}

    valores = [r["potencia_w"] for r in historial if r.get("potencia_w") is not None]
    prediccion = sum(valores) / len(valores) if valores else 0.0

    return {"valor_predicho": prediccion, "confianza": 0.5}
