# backend/routers/wa_admin.py
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field
from typing import Optional, List, Any, Dict
import os, json, logging, httpx

from db import Session, get_session, select, Brand, WAConfig, BrandDataSource
from security import check_api_key
from rag import build_context_from_datasources
from agents.sales import run_sales
from agents.reservas import run_reservas
from agents.mc import try_admin_command
from common.pwhash import hash_password, verify_password

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "")
ENV_SUPER_PASS = os.getenv("WA_SUPERADMIN_PASSWORD", "")  # opcional
EVO_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVO_KEY  = os.getenv("EVOLUTION_API_KEY", "")

# ------- helpers evo -------
def _evo_send_text(instance: str, number: str, text: str):
    """
    Intento estándar Evolution: POST /message/sendText/{instance}
    Body: { "number": "<msisdn>", "text": "..." }
    Headers: { "apikey": "..." }
    """
    if not EVO_BASE or not EVO_KEY:
        raise RuntimeError("EVOLUTION_BASE_URL/EVOLUTION_API_KEY no configurados")
    url = f"{EVO_BASE}/message/sendText/{instance}"
    with httpx.Client(timeout=15) as c:
        r = c.post(url, headers={"apikey": EVO_KEY}, json={"number": number, "text": text})
        if r.status_code >= 400:
            raise HTTPException(502, f"Evolution sendText error ({r.status_code}) {r.text}")

# ------- modelos UI -------
class WAConfigIn(BaseModel):
    brand_id: int
    agent_mode: str = Field(pattern="^(ventas|reservas|auto)$")
    model_name: Optional[str] = None
    temperature: float = 0.2
    rules_md: Optional[str] = None
    rules_json: Optional[str] = None
    super_enabled: bool = True
    super_keyword: Optional[str] = "#admin"
    super_allow_list_json: Optional[str] = None

class WAConfigOut(WAConfigIn):
    id: int
    model_config = {"from_attributes": True}

class DataSourceIn(BaseModel):
    id: Optional[int] = None
    brand_id: int
    name: str
    kind: str = Field(pattern="^(postgres|http)$")
    url: str
    headers_json: Optional[str] = None
    enabled: bool = True
    read_only: bool = True

class DataSourceOut(DataSourceIn):
    id: int
    model_config = {"from_attributes": True}

class PasswordSetIn(BaseModel):
    brand_id: int
    new_password: str
    current_password: Optional[str] = None

# ------- endpoints -------
@router.get("/config", response_model=Dict[str, Any], dependencies=[Depends(check_api_key)])
def get_config(brand_id: int, session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    if not brand: raise HTTPException(404, "Brand no encontrada")
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    dss = session.exec(select(BrandDataSource).where(BrandDataSource.brand_id == brand_id)).all()
    has_pw = bool((cfg and cfg.super_password_hash) or ENV_SUPER_PASS)
    return {
        "brand": {"id": brand.id, "name": brand.name},
        "config": cfg,
        "datasources": dss,
        "has_password": has_pw,
        "webhook_example": f"{os.getenv('PUBLIC_BASE_URL','')}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance=brand_{brand_id}",
    }

@router.post("/config/save", response_model=WAConfigOut, dependencies=[Depends(check_api_key)])
def save_config(payload: WAConfigIn, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand: raise HTTPException(404, "Brand no encontrada")
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == payload.brand_id)).first()
    if not cfg:
        cfg = WAConfig(**payload.model_dump())
        session.add(cfg)
    else:
        for k, v in payload.model_dump().items():
            setattr(cfg, k, v)
        session.add(cfg)
    session.commit(); session.refresh(cfg)
    return cfg

@router.post("/config/set_password", dependencies=[Depends(check_api_key)])
def set_password(payload: PasswordSetIn, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand: raise HTTPException(404, "Brand no encontrada")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "El password nuevo debe tener al menos 6 caracteres")

    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == payload.brand_id)).first()
    if not cfg:
        cfg = WAConfig(brand_id=payload.brand_id)
        session.add(cfg); session.commit(); session.refresh(cfg)

    if cfg.super_password_hash or ENV_SUPER_PASS:
        if not payload.current_password:
            raise HTTPException(400, "Debes ingresar el password actual")
        ok = False
        if cfg.super_password_hash:
            ok = verify_password(payload.current_password, cfg.super_password_hash)
        if not ok and ENV_SUPER_PASS:
            ok = (payload.current_password == ENV_SUPER_PASS)
        if not ok:
            raise HTTPException(401, "Password actual incorrecto")

    cfg.super_password_hash = hash_password(payload.new_password)
    session.add(cfg); session.commit()
    return {"ok": True}

@router.post("/datasource/upsert", response_model=DataSourceOut, dependencies=[Depends(check_api_key)])
def upsert_ds(payload: DataSourceIn, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand: raise HTTPException(404, "Brand no encontrada")
    if payload.id:
        ds = session.get(BrandDataSource, payload.id)
        if not ds: raise HTTPException(404, "DataSource no encontrado")
        for k, v in payload.model_dump().items():
            if k == "id": continue
            setattr(ds, k, v)
        session.add(ds)
    else:
        ds = BrandDataSource(**payload.model_dump(exclude_none=True))
        session.add(ds)
    session.commit(); session.refresh(ds)
    return ds

@router.delete("/datasource/delete", dependencies=[Depends(check_api_key)])
def delete_ds(id: int, session: Session = Depends(get_session)):
    ds = session.get(BrandDataSource, id)
    if not ds: raise HTTPException(404, "DataSource no encontrado")
    session.delete(ds); session.commit()
    return {"ok": True}

@router.post("/datasource/test", dependencies=[Depends(check_api_key)])
def test_ds(payload: DataSourceIn):
    if payload.kind == "http":
        try:
            with httpx.Client(timeout=10) as c:
                headers = {}
                if payload.headers_json:
                    try: headers = json.loads(payload.headers_json)
                    except: pass
                r = c.get(payload.url, headers=headers)
            return {"ok": r.status_code < 400, "status": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    elif payload.kind == "postgres":
        try:
            from sqlalchemy import create_engine, text as sqltext
            engine = create_engine(payload.url, future=True)
            with engine.connect() as conn:
                res = conn.execute(sqltext("SELECT 1")).scalar()
            return {"ok": True, "result": int(res)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    else:
        return {"ok": False, "error": "kind no soportado"}

# ---------- WEBHOOK (Evolution) ----------
@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "token inválido")
    try:
        body = await req.json()
    except Exception:
        body = {}

    msg = body.get("message") or body.get("data") or body
    text = (msg.get("text") if isinstance(msg, dict) else None) or body.get("text") or ""
    sender = (msg.get("from") if isinstance(msg, dict) else None) or body.get("from") or ""

    brand_id = None
    if not instance:
        instance = body.get("instance") or body.get("instanceName")
    if instance and instance.startswith("brand_"):
        try:
            brand_id = int(instance.split("_", 1)[1])
        except: pass
    if not brand_id:
        log.warning("Webhook sin brand_id deducible: %s", body)
        return {"ok": True}

    # --- ADMIN primero ---
    handled, admin_resp = try_admin_command(brand_id, sender, text)
    if handled:
        try:
            _evo_send_text(instance, sender, admin_resp)
        except Exception as e:
            log.warning("No se pudo responder admin: %s", e)
        return {"ok": True, "admin": True}

    # --- Cargar config/datasources ---
    with get_session() as session:
        cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
        brand = session.get(Brand, brand_id)
        dss = session.exec(
            select(BrandDataSource).where(
                BrandDataSource.brand_id == brand_id,
                BrandDataSource.enabled == True
            )
        ).all()

    agent_mode = cfg.agent_mode if cfg else "ventas"
    model_name = (cfg.model_name if cfg and cfg.model_name else None)
    temperature = (cfg.temperature if cfg else 0.2)

    # Reglas & contexto
    extra_ctx = []
    if brand and (brand.context or ""):
        extra_ctx.append(f"Contexto de marca:\n{brand.context}\n")
    if cfg and cfg.rules_md:
        extra_ctx.append(f"Reglas de negocio (MD):\n{cfg.rules_md}\n")
    if cfg and cfg.rules_json:
        try:
            j = json.loads(cfg.rules_json)
            extra_ctx.append("Reglas (JSON):\n" + json.dumps(j, ensure_ascii=False, indent=2))
        except Exception:
            extra_ctx.append("Reglas (JSON - crudo):\n" + cfg.rules_json)
    context_str = "\n".join(extra_ctx)

    # RAG
    rag_ctx = ""
    try:
        rag_ctx = build_context_from_datasources(dss, text, max_snippets=12)
    except Exception as e:
        rag_ctx = f"(RAG error: {e})"

    # Heurística auto
    chosen = agent_mode
    if agent_mode == "auto":
        t = text.lower()
        if any(k in t for k in ["reserv", "turno", "hora", "agenda", "disponibilidad"]):
            chosen = "reservas"
        elif any(k in t for k in ["precio", "costo", "promo", "comprar", "venta", "stock", "cotiza"]):
            chosen = "ventas"
        else:
            chosen = "ventas"

    if chosen == "reservas":
        md = run_reservas(text, context=context_str, rag_context=rag_ctx, model_name=model_name, temperature=temperature)
    else:
        md = run_sales(text, context=context_str, rag_context=rag_ctx, model_name=model_name, temperature=temperature)

    try:
        _evo_send_text(instance, sender, md)
    except Exception as e:
        log.warning("No se pudo enviar respuesta a %s: %s", sender, e)

    return {"ok": True, "agent": chosen}
