# --- dentro de backend/routers/wa_admin.py ---
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import Optional, Any, Dict
import os, json, logging

from db import get_session, select, Brand, WAConfig, BrandDataSource, WAMessage, Session
from rag import build_context_from_datasources
from agents.sales import run_sales
from agents.reservas import run_reservas
from agents.mc import try_admin_command
from wa_evolution import EvolutionClient

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "")

def _sanitize_wa_number(x: str) -> str:
    if not x:
        return ""
    if "@" in x:
        x = x.split("@", 1)[0]
    return "".join(ch for ch in x if ch.isdigit())

def _extract_text(payload: Dict[str, Any]) -> str:
    for k in ("text", "body", "messageText"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    msg = payload.get("message") or payload.get("data") or {}
    if isinstance(msg, dict):
        for k in ("text", "body", "messageText"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        m = msg.get("message") or {}
        if isinstance(m, dict):
            if isinstance(m.get("conversation"), str) and m["conversation"].strip():
                return m["conversation"].strip()
            ext = m.get("extendedTextMessage") or {}
            if isinstance(ext, dict):
                t = ext.get("text")
                if isinstance(t, str) and t.strip():
                    return t.strip()
            img = m.get("imageMessage") or {}
            if isinstance(img, dict):
                cap = img.get("caption")
                if isinstance(cap, str) and cap.strip():
                    return cap.strip()
            doc = m.get("documentMessage") or {}
            if isinstance(doc, dict):
                cap = doc.get("caption")
                if isinstance(cap, str) and cap.strip():
                    return cap.strip()
    return ""

def _extract_sender(payload: Dict[str, Any]) -> str:
    raw = payload.get("from") or payload.get("sender") or ""
    if not raw:
        msg = payload.get("message") or payload.get("data") or {}
        if isinstance(msg, dict):
            raw = msg.get("from") or ""
            if not raw:
                key = msg.get("key") or {}
                if isinstance(key, dict):
                    raw = key.get("remoteJid") or ""
            if not raw:
                inner = msg.get("message") or {}
                if isinstance(inner, dict):
                    key2 = inner.get("key") or {}
                    if isinstance(key2, dict):
                        raw = key2.get("remoteJid") or ""
    return _sanitize_wa_number(raw)

def _extract_ts(payload: Dict[str, Any]) -> Optional[int]:
    # intenta distintos lugares comunes para timestamp
    for k in ("timestamp","ts","messageTimestamp"):
        v = payload.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    msg = payload.get("message") or payload.get("data") or {}
    if isinstance(msg, dict):
        for k in ("timestamp","ts","messageTimestamp"):
            v = msg.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        m = msg.get("message") or {}
        if isinstance(m, dict):
            for k in ("messageTimestamp",):
                v = m.get(k)
                if isinstance(v, int):
                    return v
                if isinstance(v, str) and v.isdigit():
                    return int(v)
    return None

@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "token inválido")

    try:
        body = await req.json()
    except Exception:
        body = {}

    # Deducir instance y brand
    if not instance:
        instance = body.get("instance") or body.get("instanceName") or body.get("session")
    brand_id: Optional[int] = None
    if instance and isinstance(instance, str) and instance.startswith("brand_"):
        try:
            brand_id = int(instance.split("_", 1)[1])
        except Exception:
            brand_id = None
    if not brand_id:
        log.warning("Webhook sin brand_id deducible: %s", body)
        return {"ok": True}

    text = _extract_text(body)
    sender_num = _extract_sender(body)
    if not sender_num:
        log.warning("Webhook sin sender deducible: %s", body)
        return {"ok": True}
    jid = f"{sender_num}@s.whatsapp.net"
    ts = _extract_ts(body)

    # Persistimos el mensaje en DB (para Inbox fallback/ auditoría)
    try:
        with get_session() as session:
            wm = WAMessage(
                brand_id=brand_id,
                instance=instance,
                jid=jid,
                from_me=False,
                text=text or None,
                ts=ts,
                raw_json=json.dumps(body, ensure_ascii=False)
            )
            session.add(wm)
            session.commit()
    except Exception as e:
        log.warning("No se pudo persistir WAMessage: %s", e)

    evo = EvolutionClient()

    # --- ADMIN primero ---
    handled, admin_resp = try_admin_command(brand_id, sender_num, text)
    if handled:
        try:
            evo.send_text(instance, sender_num, admin_resp)
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
    try:
        rag_ctx = build_context_from_datasources(dss, text, max_snippets=12)
    except Exception as e:
        rag_ctx = f"(RAG error: {e})"

    # Heurística auto
    chosen = agent_mode
    if agent_mode == "auto":
        t = (text or "").lower()
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

    # Responder
    try:
        evo.send_text(instance, sender_num, md)
    except Exception as e:
        log.warning("No se pudo enviar respuesta a %s: %s", sender_num, e)

    return {"ok": True, "agent": chosen}
