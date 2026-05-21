from app.core.firebase_config import get_db

def guardar_datos_aula(aula_id: str, temp: float, humedad: float, presencia: bool):
    ref = get_db()
    data = {
        "temperatura": temp,
        "humedad": humedad,
        "presencia": presencia
    }
    # Esto creará una estructura tipo: /aulas/aula_1/
    ref.child(f'aulas/{aula_id}').update(data)
    return {"status": "datos guardados"}