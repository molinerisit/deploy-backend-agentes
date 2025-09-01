# backend/app.py
import os
import logging
import importlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from db import init_db
from scheduler import start_scheduler

# ---------------- .env ----------------
here = Path(__file__).parent
for env_path in (here / ".env", here.parent / ".env"):
    if env_path.exists():
        load_dotenv(env_path, override=False)

# ---------------- logging --------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("app")

# ---------------- app ------------------
app = FastAPI(title="Marketing PRO v2 API", version="0.3.1")

# CORS
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()] or [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- include routers (seguros + opcionales) --------
ROUTER_MODULES_REQUIRED = [
    "routers.brands",
    "routers.context",
    "routers.chat",
    "routers.leads",
    "routers.channels",
    "routers.meta",
    "routers.agent_mc",
    "routers.agent_reservas",
    "routers.agent_sales",
]
ROUTER_MODULES_OPTIONAL = [
    "routers.reservas",  # CRUD clásico de reservas (opcional)
]

def _include(modname: str, required: bool = True):
    try:
        m = importlib.import_module(modname)
        app.include_router(m.router)
        log.info("Router cargado: %s", modname)
    except Exception as e:
        msg = f"Router no cargado ({modname}): {e}"
        if required:
            log.error(msg)
            raise
        else:
            log.warning(msg)

for m in ROUTER_MODULES_REQUIRED:
    _include(m, required=True)

for m in ROUTER_MODULES_OPTIONAL:
    _include(m, required=False)

# ---------------- lifecycle -------------
@app.on_event("startup")
def on_startup():
    log.info("CORS origins efectivos: %s", origins)
    init_db()
    try:
        start_scheduler()
        log.info("Scheduler iniciado.")
    except Exception as e:
        log.warning("No se pudo iniciar el scheduler: %s", e)
    log.info("Backend listo.")

# ---------------- health ----------------
@app.get("/api/health")
def health():
    return {"ok": True}
