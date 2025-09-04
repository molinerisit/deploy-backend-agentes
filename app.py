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
app = FastAPI(title="WA Orchestrator (Evolution API)", version="0.4.0")

# ---------------- CORS -----------------
raw_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
allow_all = os.getenv("CORS_ALLOW_ALL", "false").lower() == "true"
origin_regex_str: Optional[str] = os.getenv("CORS_ORIGIN_REGEX", r"https://.*\.vercel\.app$")

if not raw_origins and not allow_all:
    raw_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://deploy-frontend-agentes.vercel.app",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if allow_all else raw_origins,
    allow_origin_regex=None if allow_all else origin_regex_str,
    allow_credentials=not allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

def _origin_allowed(origin: Optional[str]) -> bool:
    if allow_all:
        return True
    if not origin:
        return False
    if origin in raw_origins:
        return True
    try:
        if origin_regex_str and re.compile(origin_regex_str).match(origin):
            return True
    except Exception:
        pass
    return False

@app.middleware("http")
async def ensure_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        origin = request.headers.get("origin")
        acrm = request.headers.get("access-control-request-method", "*")
        acrh = request.headers.get("access-control-request-headers", "*")
        resp = Response(status_code=204)
        if _origin_allowed(origin):
            resp.headers["Access-Control-Allow-Origin"] = "*" if allow_all else (origin or "")
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Credentials"] = "false" if allow_all else "true"
            resp.headers["Access-Control-Allow-Methods"] = acrm or "*"
            resp.headers["Access-Control-Allow-Headers"] = acrh or "*"
            resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    try:
        resp = await call_next(request)
    except Exception as e:
        log.exception("Unhandled error: %s", e)
        resp = Response("Internal Server Error", status_code=500)

    origin = request.headers.get("origin")
    if allow_all:
        resp.headers.setdefault("Access-Control-Allow-Origin", "*")
        resp.headers.setdefault("Access-Control-Allow-Credentials", "false")
        resp.headers.setdefault("Access-Control-Expose-Headers", "*")
    elif _origin_allowed(origin):
        resp.headers.setdefault("Access-Control-Allow-Origin", origin or "")
        resp.headers.setdefault("Vary", "Origin")
        resp.headers.setdefault("Access-Control-Allow-Credentials", "true")
        resp.headers.setdefault("Access-Control-Expose-Headers", "*")
    return resp

# -------- include routers --------
ROUTER_MODULES = [
    "routers.brands",    # 👈 AÑADIDO
    "routers.chat",
    "routers.channels",
    "routers.wa_admin",
]

for m in ROUTER_MODULES:
    mod = importlib.import_module(m)
    app.include_router(mod.router)
    log.info("Router cargado: %s", m)

@app.on_event("startup")
def on_startup():
    init_db()
    log.info("CORS allow_all=%s", allow_all)
    log.info("CORS allow_origins: %s", ['*'] if allow_all else raw_origins)
    log.info("CORS allow_origin_regex: %s", origin_regex_str if not allow_all else None)
    log.info("Backend listo.")

@app.get("/api/health")
def health():
    return {"ok": True, "version": "0.4.0"}
