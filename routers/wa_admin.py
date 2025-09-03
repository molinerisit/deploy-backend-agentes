# --- backend/routers/wa_admin.py ---
import os
import json
import time
import logging
from typing import Optional, Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request, Query

from db import (
    Session,
    get_session,
    select,
    Brand,
    WAConfig,
    BrandDataSource,
    WAMessage,
)
from security import check_api_key  # opcional
from rag import build_context_from_datasources  # opcional
from agents.sales import run_sales              # opcional
from agents.reservas import run_reservas       # opcional
from agents.mc import try_admin_command        # comandos admin por chat
from common.pwhash import hash_password, verify_password  # opcional
from wa_evolution import EvolutionClient

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"  # <-- unificado
ENV_SUPER_PASS = os.getenv("WA_SUPERADMIN_PASSWORD", "")

# ------------------ helpers ------------------

def _sanitize_wa_number(x: str) -> str:
    if not x:
        return ""
    if "@" in x:
        x = x.split("@", 1)[0]
    return "".join(ch for ch in x if ch.isdigit())

def _normalize_jid(j: str) -> str:
    j = (j or "").strip()
    if not j:
        return ""
    if "@s.whatsapp.net" in j:
        return j
    digits = "".join(ch for ch in j if ch.isdigit())
    if not digits:
        return j
    return f"{digits}@s.whatsapp.net"

def _number_from_jid(jid: str) -> str:
    return (jid or "").split("@", 1)[0]

def _extract_text(payload: Dict[str, Any]) -> str:
    # Intenta extraer texto rápido para logs
    try:
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
                for media_key in ("imageMessage", "videoMessage", "documentMessage"):
                    md = m.get(media_key) or {}
                    if isinstance(md, dict):
                        cap = md.get("caption")
                        if isinstance(cap, str) and cap.strip():
                            return cap.strip()
    except Exception:
        pass
    return ""

def _extract_incoming(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Devuelve [{'jid': str, 'text': str}] para cada mensaje NO fromMe.
    Soporta:
    - data.messages: [ ... ]
    - cuerpo simple con key/remoteJid y message/extendedTextMessage, etc.
    """
    out: List[Dict[str, str]] = []
    body = payload.get("data") or payload.get("body") or payload

    msgs = body.get("messages") if isinstance(body, dict) else None
    if isinstance(msgs, list) and msgs:
        cand = msgs
    else:
        cand = [body] if isinstance(body, dict) else []

    for m in cand:
        key = m.get("key") if isinstance(m.get("key"), dict) else {}
        jid = m.get("chatId") or m.get("remoteJid") or key.get("remoteJid") or ""
        jid = _normalize_jid(jid)
        if not jid:
            continue

        inner = m.get("message") if isinstance(m.get("message"), dict) else {}
        text = (
            m.get("text") or m.get("body") or
            inner.get("conversation") or
            (inner.get("extendedTextMessage") or {}).get("text") or
            (inner.get("imageMessage") or {}).get("caption") or
            (inner.get("videoMessage") or {}).get("caption") or
            (inner.get("documentMessage") or {}).get("caption")
        )
        text = (text or "").strip()
        from_me = bool(m.get("fromMe") or key.get("fromMe"))

        # Log de cada mensaje crudo reducido
        log.debug("incoming raw: fromMe=%s jid=%s text=%.60s", from_me, jid, text)

        if text and not from_me:
            out.append({"jid": jid, "text": text})

    log.debug("_extract_incoming count=%s", len(out))
    return out

def _save_message(
    session: Session,
    brand_id: int,
    instance: Optional[str],
    jid: str,
    from_me: bool,
    text: str,
    raw: Dict[str, Any],
) -> None:
    try:
        msg = WAMessage(
            brand_id=brand_id,
            jid=jid,
            from_me=from_me,
            text=text or "",
            ts=int(time.time()),  # <-- ahora siempre guarda ts
        )
        # instance (si existe la columna)
        try:
            setattr(msg, "instance", instance)
        except Exception:
            pass
        # raw_json como string
        try:
            setattr(msg, "raw_json", json.dumps(raw, ensure_ascii=False)[:20000])
        except Exception:
            setattr(msg, "raw_json", str(raw)[:20000])

        session.add(msg)
        session.commit()
        log.debug("_save_message ok: brand=%s jid=%s from_me=%s", brand_id, jid, from_me)
    except Exception as e:
        log.warning("no se pudo guardar WAMessage (1er intento): %s", e)
        try:
            msg2 = WAMessage(
                brand_id=brand_id,
                jid=jid,
                from_me=from_me,
                text=text or "",
                ts=int(time.time()),
            )
            session.add(msg2)
            session.commit()
            log.debug("_save_message fallback ok")
        except Exception as e2:
            log.error("no se pudo guardar WAMessage (fallback): %s", e2)

# ------------------ webhook ------------------

@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    token_ok = (EVOLUTION_WEBHOOK_TOKEN and token == EVOLUTION_WEBHOOK_TOKEN)
    log.info("WA WEBHOOK HIT: token_ok=%s instance_qs=%s", token_ok, instance)

    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        log.warning("invalid token in webhook")
        raise HTTPException(401, "token inválido")

    # Lee body crudo para debug
    raw = await req.body()
    raw_txt = raw.decode("utf-8", errors="ignore")
    # Log recortado para no inundar
    log.debug("WEBHOOK RAW (first 4KB): %s", raw_txt[:4096])

    try:
        payload = json.loads(raw_txt or "{}")
    except Exception:
        payload = {}

    # deducir instance/brand
    if not instance:
        instance = payload.get("instance") or payload.get("instanceName") or payload.get("session")

    brand_id = None
    if isinstance(instance, str) and instance.startswith("brand_"):
        try:
            brand_id = int(instance.split("_", 1)[1])
        except Exception:
            brand_id = None

    if not brand_id:
        log.warning("Webhook sin brand_id deducible: %s", (payload.keys() if isinstance(payload, dict) else type(payload)))
        return {"ok": True, "ignored": "no brand"}

    incoming = _extract_incoming(payload)
    evo = EvolutionClient()

    saved = 0
    with get_session() as session:
        for m in incoming:
            jid = m["jid"]
            text = m["text"]
            _save_message(session, brand_id, instance, jid, from_me=False, text=text, raw=payload)
            saved += 1

            # 1) comandos admin primero
            try:
                handled, admin_resp = try_admin_command(brand_id, _number_from_jid(jid), text)
            except Exception as e:
                log.warning("try_admin_command error: %s", e)
                handled, admin_resp = False, None

            if handled and admin_resp:
                try:
                    evo.send_text(instance, _number_from_jid(jid), admin_resp)
                    _save_message(session, brand_id, instance, jid, from_me=True, text=admin_resp, raw={"response": "admin"})
                except Exception as e:
                    log.warning("no se pudo responder admin: %s", e)
                continue

            # 2) auto-ack simple (para validar ida/vuelta)
            try:
                ack = f"🤖 Recibido: {text[:180]}"
                evo.send_text(instance, _number_from_jid(jid), ack)
                _save_message(session, brand_id, instance, jid, from_me=True, text=ack, raw={"response": "auto-ack"})
            except Exception as e:
                log.warning("no se pudo responder: %s", e)

    log.info("WA WEBHOOK processed: brand=%s instance=%s count=%s", brand_id, instance, saved)
    return {"ok": True, "count": saved}
