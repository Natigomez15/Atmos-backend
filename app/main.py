from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import rooms, nodes, readings

aplicacion = FastAPI(title="ATMOS API")

aplicacion.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

aplicacion.include_router(rooms.enrutador, prefix="/api/v1")
aplicacion.include_router(nodes.enrutador, prefix="/api/v1")
aplicacion.include_router(readings.enrutador, prefix="/api/v1")


@aplicacion.get("/health")
async def estado_servicio():
    return {"estado": "ok", "servicio": "atmos-api"}
