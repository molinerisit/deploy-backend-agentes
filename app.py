import os
import re
import logging
import importlib
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Response
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
app = FastAPI(title="Marketing PRO v2 API", version="0.3.5")

# ---------------- CORS -----------------
raw_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
allow_all = os.getenv("CORS_ALLOW_ALL", "false").lower() == "true"
origin_regex_str: Optional[str] = os.getenv("CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app$")
origin_regex = re.compile(origin_regex_str) if origin_regex_str else None

if not raw_origins and not allow_all:
    raw_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://deploy-frontend-agentes.vercel.app",
    ]

# CORSMiddleware estándar
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all else raw_origins,
    allow_origin_regex=None if allow_all else origin_regex_str,
    allow_credentials=not allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _origin_allowed(origin: Optional[str]) -> bool:
    # cuando allow_all es True, permitimos SIEMPRE (aunque no haya header Origin)
    if allow_all:
        return True
    if not origin:
        return False
    if origin in raw_origins:
        return True
    if origin_regex and origin_regex.match(origin):
        return True
    return False

# ---- Failsafe CORS: agrega headers SIEMPRE, incluso en 500 ----
@app.middleware("http")
async def ensure_cors_headers(request: Request, call_next):
    # Preflight manual (por si el de Starlette no cubre algún path/500)
    if request.method == "OPTIONS":
        origin = request.headers.get("origin")
        acrm = request.headers.get("access-control-request-method", "*")
        acrh = request.headers.get("access-control-request-headers", "*")
        resp = Response(status_code=204)
        # Con allow_all True devolvemos siempre los headers CORS
        if _origin_allowed(origin):
            resp.headers["Access-Control-Allow-Origin"] = "*" if allow_all else (origin or "")
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Credentials"] = "false" if allow_all else "true"
            resp.headers["Access-Control-Allow-Methods"] = acrm or "*"
            resp.headers["Access-Control-Allow-Headers"] = acrh or "*"
            resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    # Flujo normal
    try:
        resp = await call_next(request)
    except Exception as e:
        log.exception("Unhandled error: %s", e)
        resp = Response("Internal Server Error", status_code=500)

    # Inyectar CORS SIEMPRE cuando allow_all=True; si no, validar origen
    origin = request.headers.get("origin")
    if allow_all:
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        # con "*" no se permiten credenciales
        resp.headers.setdefault("Access-Control-Allow-Credentials", "false")
    elif _origin_allowed(origin):
        resp.headers.setdefault("Access-Control-Allow-Origin", origin or "")
        resp.headers.setdefault("Vary", "Origin")
        resp.headers.setdefault("Access-Control-Allow-Credentials", "true")
    return resp

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
    log.info("CORS allow_origins: %s", ['*'] if allow_all else raw_origins)
    log.info("CORS allow_origin_regex: %s", origin_regex_str if not allow_all else None)
    init_db()
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
    return {"ok": True, "version": "0.3.5"}

@app.get("/api/debug/cors")
def debug_cors():
    return {
        "allow_all": allow_all,
        "allow_origins": ["*"] if allow_all else raw_origins,
        "allow_origin_regex": origin_regex_str if not allow_all else None,
    }
