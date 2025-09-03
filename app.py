# backend/app.py
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
app = FastAPI(title="Marketing PRO v2 API", version="0.3.2")

# ---------------- CORS -----------------
# Preferí configurar por ENV:
#   CORS_ORIGINS="https://deploy-frontend-agentes.vercel.app,http://localhost:5173"
#   CORS_ORIGIN_REGEX="https://.*\.vercel\.app$"
origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if not origins:
    origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://deploy-frontend-agentes.vercel.app",
    ]

origin_regex = os.getenv("CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app$")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,          # lista explícita
    allow_origin_regex=origin_regex,# y regex para subdominios (Vercel, etc.)
    allow_credentials=True,
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
    log.info("CORS allow_origins: %s", origins)
    log.info("CORS allow_origin_regex: %s", origin_regex)
    init_db()
    # Scheduler opcional
    try:
        from scheduler import start_scheduler  # import lazy para no fallar si no existe
        start_scheduler()
        log.info("Scheduler iniciado.")
    except Exception as e:
        log.warning("No se pudo iniciar el scheduler: %s", e)
    log.info("Backend listo.")

# ---------------- health ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.3.2"}
