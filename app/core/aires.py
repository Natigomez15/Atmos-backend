AIRES_IGNORADOS: set[str] = set()


def normalizar_nombre_aire(valor: str | None) -> str:
    return str(valor or "").strip().lower().replace(" ", "_").replace("-", "_")


def es_aire_ignorado(aire: str | None) -> bool:
    return normalizar_nombre_aire(aire) in AIRES_IGNORADOS
