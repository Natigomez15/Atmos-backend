# ATMOS Backend

API REST desarrollada en FastAPI para el sistema de
optimización energética ATMOS.

## Requisitos

- Python 3.11
- Cuenta en Supabase
- Cuenta en Firebase

## Instalación local

1. Clonar el repositorio:
```bash
git clone https://github.com/natigomez15/atmos-backend.git
cd atmos-backend
```

2. Crear entorno virtual con Python 3.11:
```bash
py -3.11 -m venv .venv311
.venv311\Scripts\activate  # Windows
source .venv311/bin/activate  # Linux/Mac
```

3. Instalar dependencias:
```bash
pip install -r requirements.txt
```

4. Crear archivo `.env` en la raíz:
```env
SUPABASE_URL=tu_supabase_url
SUPABASE_KEY=tu_supabase_key
FIREBASE_API_KEY=tu_firebase_key
FIREBASE_DATABASE_URL=tu_firebase_url
FIREBASE_AUTH_DOMAIN=tu_firebase_domain
FIREBASE_STORAGE_BUCKET=tu_firebase_bucket
CRON_SECRET=tu_secret
SUPABASE_JWT_SECRET=tu_jwt_secret
```

5. Correr el servidor:
```bash
uvicorn app.main:app --reload
```

6. Verificar en el navegador:
http://localhost:8000/health
http://localhost:8000/docs

## Estructura del proyecto
atmos-backend/
├── app/
│   ├── api/
│   │   ├── ac_commands.py   # Comandos IR al AC
│   │   ├── ajustes.py       # Configuración del sistema
│   │   ├── alertas.py       # Sistema de alertas
│   │   ├── auth.py          # Autenticación y usuarios
│   │   ├── ml.py            # Predicciones ML
│   │   ├── nodos.py         # Gestión de ESP32
│   │   ├── lecturas.py      # Ingestión de datos
│   │   ├── reportes.py      # Reportes energéticos
│   │   ├── salas.py         # Gestión de salones
│   │   ├── telegram.py      # Notificaciones
│   │   └── websockets.py    # Tiempo real
│   ├── core/
│   │   ├── database.py      # Cliente Supabase
│   │   ├── limiter.py       # Rate limiting
│   │   ├── logger.py        # Logging estructurado
│   │   ├── security.py      # JWT y roles
│   │   └── websocket_manager.py
│   ├── ml/
│   │   └── predictor.py     # Servicio ML
│   ├── models/
│   │   └── schemas.py       # Modelos Pydantic
│   ├── services/
│   │   ├── aggregation.py   # Agregación horaria
│   │   ├── alert_service.py # Verificación alertas
│   │   └── telegram_service.py
│   ├── config.py            # Variables de entorno
│   └── main.py              # App principal
├── tests/                   # Suite de tests
├── .env.example
├── Procfile
├── requirements.txt
└── runtime.txt

## Endpoints principales

### Salud del sistema
GET  /health

### Salones
GET  /api/v1/salas
POST /api/v1/salas
GET  /api/v1/salas/{id}
PATCH /api/v1/salas/{id}

### Nodos ESP32
GET   /api/v1/nodos
POST  /api/v1/nodos
PATCH /api/v1/nodos/{id}/heartbeat
PATCH /api/v1/nodos/{id}/desactivar

### Lecturas de sensores
POST /api/v1/lecturas
POST /api/v1/lecturas/lote
GET  /api/v1/lecturas
GET  /api/v1/lecturas/ultima/{sala_id}

### Comandos AC
POST  /api/v1/ac-commands
GET   /api/v1/ac-commands/pendientes/{nodo_id}
PATCH /api/v1/ac-commands/{id}/confirmar

### Alertas
GET   /api/v1/alertas
GET   /api/v1/alertas/resumen
PATCH /api/v1/alertas/{id}/resolver
POST  /api/v1/alertas/verificar

### ML
GET  /api/v1/ml/caracteristicas/{sala_id}
POST /api/v1/ml/predicciones
GET  /api/v1/ml/predicciones/{sala_id}/ultima

### Reportes
POST /api/v1/reportes/energia
POST /api/v1/reportes/sala/{sala_id}
GET  /api/v1/reportes/resumen/pabellon
GET  /api/v1/reportes/comparar

### Autenticación
POST /api/v1/auth/usuarios
GET  /api/v1/auth/usuarios
GET  /api/v1/auth/yo

## Deploy en Render

1. Conectar repositorio en render.com
2. Configurar:
Runtime:         Python
Build Command:   pip install -r requirements.txt
Start Command:   uvicorn app.main:app --host 0.0.0.0 --port $PORT
3. Agregar variables de entorno en el panel de Render
4. Agregar variable: PYTHON_VERSION = 3.11.0

## Tests

```bash
pytest
pytest --cov=app --cov-report=term-missing
```

## Documentación interactiva

Con el servidor corriendo:
http://localhost:8000/docs      # Swagger UI
http://localhost:8000/redoc     # ReDoc
