import os
import logging
import importlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from db import init_db

# ---------------- .env ----------------
here = Path(__file__).parent
for env_path in (here / ".env", here.parent / ".env"):
    if env_path.exists():
        load_dotenv(env_path, override=False)

# ---------------- logging --------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("app")

# ---------------- app ------------------
app = FastAPI(title="Marketing PRO v2 API", version="0.3.3")

# ---------------- CORS -----------------
# Config por ENV:
#   CORS_ORIGINS="https://deploy-frontend-agentes.vercel.app,https://otro-dominio.com"
#   CORS_ORIGIN_REGEX="https://.*\\.vercel\\.app$"
#   CORS_ALLOW_ALL="false"   # si pones "true", habilita * (útil para pruebas)
#
raw_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
allow_all = os.getenv("CORS_ALLOW_ALL", "false").lower() == "true"
origin_regex = os.getenv("CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app$")

# Defaults seguros si no hay ENV:
if not raw_origins and not allow_all:
    raw_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://deploy-frontend-agentes.vercel.app",
    ]

# Nota: si usas allow_all=True no podés usar allow_credentials=True (limitación CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all else raw_origins,
    allow_origin_regex=None if allow_all else origin_regex,
    allow_credentials=not allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------- include routers (cargar en orden estable) --------
ROUTER_MODULES_REQUIRED = [
    "routers.brands",
    "routers.context",
    "routers.chat",
    "routers.leads",
    "routers.channels",        # 👈 WA + webhook + board
    "routers.meta",
    "routers.agent_mc",
    "routers.agent_reservas",
    "routers.agent_sales",
    "routers.wa_admin",        # 👈 Config de WhatsApp (GET /api/wa/config)
]
ROUTER_MODULES_OPTIONAL = [
    "routers.reservas",        # CRUD reservas (opcional)
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
    log.info("CORS allow_all=%s", allow_all)
    log.info("CORS allow_origins: %s", ["*"] if allow_all else raw_origins)
    log.info("CORS allow_origin_regex: %s", None if allow_all else origin_regex)
    init_db()
    # Scheduler opcional
    try:
        from scheduler import start_scheduler
        start_scheduler()
        log.info("Scheduler iniciado.")
    except Exception as e:
        log.warning("No se pudo iniciar el scheduler: %s", e)
    log.info("Backend listo.")

# ---------------- health ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.3.3"}

# ---- util para verificar qué ve el front (opcional) ----
@app.get("/api/debug/cors")
def debug_cors():
    return {
        "allow_all": allow_all,
        "allow_origins": ["*"] if allow_all else raw_origins,
        "allow_origin_regex": None if allow_all else origin_regex,
    }
