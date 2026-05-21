import time
from datetime import datetime
from app.core.database import obtener_cliente, obtener_firebase


def sincronizar():
    """Sincroniza registros nuevos de Firebase RTDB → Supabase."""
    supabase = obtener_cliente()
    firebase_db = obtener_firebase()
    registros_enviados: set[str] = set()

    print("Sincronizando Atmos...")

    while True:
        try:
            datos = firebase_db.child("Atmos").get().val()

            if datos:
                for aire, contenido in datos.items():
                    if "registros" not in contenido:
                        continue

                    for clave, valor in contenido["registros"].items():
                        registro_id = f"{aire}_{clave}"
                        if registro_id in registros_enviados:
                            continue

                        registro = {
                            "sensor": valor.get("sensor", aire),
                            "temperatura_dht11": valor.get("temperatura"),
                            "temperatura_ds18b20": valor.get("temperatura_ds18b20"),
                            "humedad": valor.get("humedad"),
                            "movimiento": valor.get("movimiento"),
                            "fecha": datetime.now().isoformat(),
                        }

                        supabase.table("registros").insert(registro).execute()
                        registros_enviados.add(registro_id)
                        print("Registro enviado:", registro)

            time.sleep(15)

        except Exception as error:
            print("Error en sincronización:", error)
            time.sleep(10)


if __name__ == "__main__":
    sincronizar()
