# --- backend/routers/wa_admin.py ---
import os, json, time, logging
from typing import Optional, Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel

from db import (
    Session,
    get_session,
    select,
    Brand,
    WAConfig,
    BrandDataSource,
    WAMessage,
)
from wa_evolution import EvolutionClient

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"

# ------------------ helpers ------------------

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

        if text and not from_me:
            out.append({"jid": jid, "text": text})

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
            ts=int(time.time()),
        )
        try:
            setattr(msg, "instance", instance)
        except Exception:
            pass
        try:
            setattr(msg, "raw_json", json.dumps(raw, ensure_ascii=False)[:20000])
        except Exception:
            setattr(msg, "raw_json", str(raw)[:20000])
        session.add(msg)
        session.commit()
    except Exception as e:
        log.warning("no se pudo guardar WAMessage: %s", e)

# ------------------ webhook ------------------

@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "token inválido")

    raw = await req.body()
    raw_txt = raw.decode("utf-8", errors="ignore")
    try:
        payload = json.loads(raw_txt or "{}")
    except Exception:
        payload = {}

    if not instance:
        instance = payload.get("instance") or payload.get("instanceName") or payload.get("session")

    brand_id = None
    if isinstance(instance, str) and instance.startswith("brand_"):
        try:
            brand_id = int(instance.split("_", 1)[1])
        except Exception:
            brand_id = None

    if not brand_id:
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

            # 1) Routing a agentes + reglas + RAG
            try:
                cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
                agent_mode = (cfg.agent_mode if cfg else "ventas").strip().lower()
                model_name = getattr(cfg, "model_name", None)
                temperature = float(getattr(cfg, "temperature", 0.2) or 0.2)

                dss = session.exec(select(BrandDataSource).where(
                    BrandDataSource.brand_id == brand_id,
                    BrandDataSource.enabled == True
                )).all()
                rag_context = ""
                try:
                    from rag import build_context_from_datasources
                    rag_context = build_context_from_datasources(dss, query=text, max_snippets=8)
                except Exception as e:
                    log.warning("RAG context error: %s", e)

                rules_md = getattr(cfg, "rules_md", None) or ""

                reply = None
                if agent_mode == "reservas":
                    from agents.reservas import run_reservas
                    reply = run_reservas(text, context=rules_md, rag_context=rag_context,
                                         model_name=model_name, temperature=temperature)
                elif agent_mode == "ventas":
                    from agents.sales import run_sales
                    reply = run_sales(text, context=rules_md, rag_context=rag_context,
                                      model_name=model_name, temperature=temperature)
                else:
                    # auto: heurística básica
                    if any(k in text.lower() for k in ("reserva", "turno", "agenda", "horario", "fecha")):
                        from agents.reservas import run_reservas
                        reply = run_reservas(text, context=rules_md, rag_context=rag_context,
                                             model_name=model_name, temperature=temperature)
                    else:
                        from agents.sales import run_sales
                        reply = run_sales(text, context=rules_md, rag_context=rag_context,
                                          model_name=model_name, temperature=temperature)

                if reply and reply.strip():
                    evo.send_text(instance, _number_from_jid(jid), reply[:3500])
                    _save_message(session, brand_id, instance, jid, from_me=True, text=reply, raw={"response": "agent"})
                else:
                    ack = f"🤖 Recibido: {text[:180]}"
                    evo.send_text(instance, _number_from_jid(jid), ack)
                    _save_message(session, brand_id, instance, jid, from_me=True, text=ack, raw={"response": "auto-ack"})
            except Exception as e:
                log.warning("routing/agent error: %s", e)

    return {"ok": True, "count": saved}

# ---- Config vía API
class ConfigIn(BaseModel):
    brand_id: int
    agent_mode: Optional[str] = None
    rules_md: Optional[str] = None
    rules_json: Optional[str] = None
    model_name: Optional[str] = None
    temperature: Optional[float] = None

@router.post("/config/save")
def wa_config_save(payload: ConfigIn, session: Session = Depends(get_session)):
    brand_id = payload.brand_id
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    if not cfg:
        cfg = WAConfig(brand_id=brand_id)
    if payload.agent_mode is not None: cfg.agent_mode = (payload.agent_mode or "ventas").lower()
    if payload.rules_md   is not None: cfg.rules_md   = payload.rules_md or ""
    if payload.rules_json is not None: cfg.rules_json = payload.rules_json or ""
    if payload.model_name is not None: cfg.model_name = payload.model_name or None
    if payload.temperature is not None: cfg.temperature = float(payload.temperature or 0.2)
    session.add(cfg); session.commit(); session.refresh(cfg)
    return {"ok": True, "config": cfg}
