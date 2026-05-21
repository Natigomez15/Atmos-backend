import firebase_admin
from firebase_admin import credentials, db
import os
from dotenv import load_dotenv

# Cargamos el archivo .env donde está guardada tu DATABASE_URL
load_dotenv()

# Usamos el nombre del archivo que guardamos en la raíz
# Si el archivo se llama "serviceAccountKey.json", esto funcionará
cred = credentials.Certificate("serviceAccountKey.json")

# Inicializamos la app con el certificado y la URL de la base de datos
firebase_admin.initialize_app(cred, {
    'databaseURL': os.getenv("DATABASE_URL")
})

print("Conexión con Firebase establecida exitosamente 🚀")