# --- backend/routers/wa_admin.py ---
import os, json, logging
from typing import Optional, Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Request, Query

from db import Session, get_session, select, Brand, WAConfig, BrandDataSource, WAMessage
from security import check_api_key
from rag import build_context_from_datasources
from agents.sales import run_sales
from agents.reservas import run_reservas
from agents.mc import try_admin_command
from common.pwhash import hash_password, verify_password
from wa_evolution import EvolutionClient

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "")
ENV_SUPER_PASS = os.getenv("WA_SUPERADMIN_PASSWORD", "")

# ------------------ helpers ------------------

def _sanitize_wa_number(x: str) -> str:
    if not x:
        return ""
    if "@" in x:
        x = x.split("@", 1)[0]
    return "".join(ch for ch in x if ch.isdigit())

def _extract_text(payload: Dict[str, Any]) -> str:
    """
    Intenta extraer texto del webhook en varias variantes comunes de Baileys/Evolution.
    """
    # 1) directos
    for k in ("text", "body", "messageText"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    msg = payload.get("message") or payload.get("data") or {}
    if isinstance(msg, dict):
        # 2) dentro de message
        for k in ("text", "body", "messageText"):
            v = msg.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()

        # 3) estructura tipo Baileys
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
    """
    Busca el remitente en distintas variantes: from, key.remoteJid, etc.
    """
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

# ------------------ webhook ------------------

@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "token inválido")

    try:
        body = await req.json()
    except Exception:
        body = {}

    # deducir instance y brand
    if not instance:
        instance = body.get("instance") or body.get("instanceName") or body.get("session")
    brand_id = None
    if instance and isinstance(instance, str) and instance.startswith("brand_"):
        try:
            brand_id = int(instance.split("_", 1)[1])
        except Exception:
            pass
    if not brand_id:
        log.warning("Webhook sin brand_id deducible: %s", body)
        return {"ok": True}

    text = _extract_text(body)
    sender = _extract_sender(body)
    if not sender:
        log.warning("Webhook sin sender deducible: %s", body)
        return {"ok": True}

    evo = EvolutionClient()

    # --- guardar en DB (entrante) ---
    try:
        ts = None
        try:
            msg = body.get("message") or body.get("data") or {}
            ts = (msg.get("messageTimestamp") or msg.get("timestamp") or msg.get("ts"))
            if isinstance(ts, str) and ts.isdigit():
                ts = int(ts)
        except:
            ts = None
        with get_session() as s:
            wm = WAMessage(
                brand_id=brand_id,
                instance=instance,
                jid=f"{sender}@s.whatsapp.net",
                from_me=False,
                text=text or None,
                ts=ts,
                raw_json=json.dumps(body, ensure_ascii=False),
            )
            s.add(wm); s.commit()
    except Exception as e:
        log.warning("No se pudo guardar WAMessage: %s", e)

    # --- admin primero ---
    handled, admin_resp = try_admin_command(brand_id, sender, text)
    if handled:
        try:
            evo.send_text(instance, sender, admin_resp)
        except Exception as e:
            log.warning("No se pudo responder admin: %s", e)
        return {"ok": True, "admin": True}

    # --- config/datasources ---
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

    # contexto
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

    # rag
    try:
        rag_ctx = build_context_from_datasources(dss, text, max_snippets=12)
    except Exception as e:
        rag_ctx = f"(RAG error: {e})"

    # heurística auto
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

    # responder al usuario
    try:
        evo.send_text(instance, sender, md)
        # guardar en DB como saliente
        with get_session() as s:
            wm = WAMessage(
                brand_id=brand_id,
                instance=instance,
                jid=f"{sender}@s.whatsapp.net",
                from_me=True,
                text=md or None,
                ts=None,
                raw_json=json.dumps({"response": md}, ensure_ascii=False),
            )
            s.add(wm); s.commit()
    except Exception as e:
        log.warning("No se pudo enviar respuesta a %s: %s", sender, e)

    return {"ok": True, "agent": chosen}
