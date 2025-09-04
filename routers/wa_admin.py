# --- backend/routers/wa_admin.py ---
import os, json, time, logging
from typing import Optional, Any, Dict, List
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel
from db import session_cm  # 👈 usaremos el context manager correcto

from db import (
    Session,
    get_session,
    session_cm,   # 👈 agregar
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
    token_ok = (EVOLUTION_WEBHOOK_TOKEN and token == EVOLUTION_WEBHOOK_TOKEN)
    log.info("WA WEBHOOK HIT: token_ok=%s instance_qs=%s", token_ok, instance)

    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        log.warning("invalid token in webhook")
        raise HTTPException(401, "token inválido")

    raw = await req.body()
    raw_txt = raw.decode("utf-8", errors="ignore")
    log.debug("WEBHOOK RAW (first 4KB): %s", raw_txt[:4096])

    try:
        payload = json.loads(raw_txt or "{}")
    except Exception:
        payload = {}

    # Deducir instancia/brand
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

    # ---- Normalización de entrantes (Evolution v2.x) ----
    def _extract_incoming_evolution(p: dict) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        body = p.get("data") or p.get("body") or p

        msgs = body.get("messages") if isinstance(body, dict) else None
        if isinstance(msgs, list):
            for m in msgs:
                key = m.get("key") if isinstance(m.get("key"), dict) else {}
                jid = (
                    m.get("chatId") or m.get("remoteJid") or key.get("remoteJid") or
                    ((m.get("id") or {}).get("remote") if isinstance(m.get("id"), dict) else None)
                ) or ""
                if "@s.whatsapp.net" not in str(jid):
                    jid = f"{''.join(ch for ch in str(jid) if ch.isdigit())}@s.whatsapp.net"

                inner = m.get("message") if isinstance(m.get("message"), dict) else {}
                text = (
                    m.get("text") or m.get("body") or
                    inner.get("conversation") or
                    (inner.get("extendedTextMessage") or {}).get("text") or
                    (inner.get("imageMessage") or {}).get("caption") or
                    (inner.get("videoMessage") or {}).get("caption") or
                    (inner.get("documentMessage") or {}).get("caption") or
                    ""
                ).strip()
                from_me = bool(m.get("fromMe") or key.get("fromMe"))
                if text and not from_me and jid:
                    out.append({"jid": jid, "text": text})

        elif isinstance(body, dict):
            key = body.get("key") if isinstance(body.get("key"), dict) else {}
            jid = (
                body.get("chatId") or body.get("remoteJid") or key.get("remoteJid") or
                ((body.get("id") or {}).get("remote") if isinstance(body.get("id"), dict) else None)
            ) or ""
            if jid and "@s.whatsapp.net" not in str(jid):
                jid = f"{''.join(ch for ch in str(jid) if ch.isdigit())}@s.whatsapp.net"

            inner = body.get("message") if isinstance(body.get("message"), dict) else {}
            text = (
                body.get("text") or body.get("body") or
                inner.get("conversation") or
                (inner.get("extendedTextMessage") or {}).get("text") or
                (inner.get("imageMessage") or {}).get("caption") or
                (inner.get("videoMessage") or {}).get("caption") or
                (inner.get("documentMessage") or {}).get("caption") or
                ""
            ).strip()
            from_me = bool(body.get("fromMe") or key.get("fromMe"))
            if text and not from_me and jid:
                out.append({"jid": jid, "text": text})

        return out

    incoming = _extract_incoming_evolution(payload)
    if not incoming:
        log.info("Webhook sin entrantes útiles (puede ser ack/salida).")
        return {"ok": True, "count": 0, "note": "no inbound"}

    from wa_evolution import EvolutionClient
    evo = EvolutionClient()

    saved = 0
    with session_cm() as session:
        for m in incoming:
            jid = m["jid"]; text = m["text"]

            # Guardar entrante
            try:
                msg = WAMessage(
                    brand_id=brand_id,
                    jid=jid,
                    from_me=False,
                    text=text,
                    ts=int(time.time()),
                )
                setattr(msg, "instance", instance)
                setattr(msg, "raw_json", json.dumps(payload, ensure_ascii=False)[:20000])
                session.add(msg)
                session.commit()
                saved += 1
            except Exception as e:
                log.warning("no se pudo guardar WAMessage inbound: %s", e)

            # Auto-ACK (opcional)
            try:
                ack = f"🤖 Recibido: {text[:180]}"
                evo.send_text(instance, jid.split("@", 1)[0], ack)
                try:
                    msg2 = WAMessage(
                        brand_id=brand_id,
                        jid=jid,
                        from_me=True,
                        text=ack,
                        ts=int(time.time()),
                    )
                    setattr(msg2, "instance", instance)
                    setattr(msg2, "raw_json", json.dumps({"response": "auto-ack"}, ensure_ascii=False))
                    session.add(msg2)
                    session.commit()
                except Exception:
                    pass
            except Exception as e:
                log.warning("no se pudo responder ACK: %s", e)

    log.info("WA WEBHOOK processed: brand=%s instance=%s count=%s", brand_id, instance, saved)
    return {"ok": True, "count": saved}

@router.post("/sync_pull")
def wa_sync_pull(brand_id: int = Query(...)):
    from wa_evolution import EvolutionClient
    import time, json

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    sc, chats = evo.list_chats(instance, limit=200)
    if sc < 200 or sc >= 400:
        raise HTTPException(502, f"Evolution list_chats fallo: {sc} {chats}")

    items = chats.get("chats") or chats.get("data") or chats.get("response") or chats
    if isinstance(items, dict):
        items = items.get("chats") or items.get("items") or []
    if not isinstance(items, list):
        items = []

    saved = 0
    with session_cm() as s:
        for ch in items[:200]:
            jid = ch.get("jid") or ch.get("id") or ch.get("remoteJid") or ch.get("chatId") or ""
            if not isinstance(jid, str):
                continue
            if "@s.whatsapp.net" not in jid:
                jid = f"{''.join(c for c in jid if c.isdigit())}@s.whatsapp.net"

            sc2, msgs = evo.get_chat_messages(instance, jid, limit=20)
            arr = msgs.get("messages") or msgs.get("data") or msgs.get("response") or []
            if isinstance(arr, dict):
                arr = arr.get("messages") or []
            if not isinstance(arr, list):
                arr = []

            for m in arr:
                key = m.get("key") if isinstance(m.get("key"), dict) else {}
                inner = m.get("message") if isinstance(m.get("message"), dict) else {}
                text = (
                    m.get("text") or m.get("body") or
                    inner.get("conversation") or
                    (inner.get("extendedTextMessage") or {}).get("text") or
                    (inner.get("imageMessage") or {}).get("caption") or
                    (inner.get("videoMessage") or {}).get("caption") or
                    (inner.get("documentMessage") or {}).get("caption") or
                    ""
                ).strip()
                if not text:
                    continue
                from_me = bool(m.get("fromMe") or key.get("fromMe"))
                ts = int(m.get("messageTimestamp") or m.get("timestamp") or time.time())

                try:
                    rec = WAMessage(
                        brand_id=brand_id,
                        jid=jid,
                        from_me=from_me,
                        text=text,
                        ts=ts,
                    )
                    setattr(rec, "instance", instance)
                    setattr(rec, "raw_json", json.dumps(m, ensure_ascii=False)[:20000])
                    s.add(rec)
                    saved += 1
                except Exception:
                    pass
        s.commit()

    return {"ok": True, "saved": saved}

@router.post("/sync_pull")
def wa_sync_pull(brand_id: int = Query(...)):
    """
    Pull del histórico desde Evolution y persistencia en WAMessage,
    para poblar el tablero/inbox aunque no funcione el webhook.
    """
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    # 1) Listar chats
    sc, js = evo.list_chats(instance, limit=200)
    if sc >= 400:
        raise HTTPException(sc, f"Evolution list_chats error: {js}")

    # Cada build trae el array con claves distintas
    chats = []
    raw = js if isinstance(js, dict) else {}
    for k in ("chats", "data", "response", "items", "list"):
        v = raw.get(k)
        if isinstance(v, list):
            chats = v
            break
    if not isinstance(chats, list):
        chats = []

    saved = 0
    with session_cm() as s:
        for ch in chats:
            # intentamos extraer el jid/number
            jid = _normalize_jid(
                ch.get("jid") or ch.get("chatId") or ch.get("remoteJid") or ch.get("id") or ""
            )
            if not jid:
                num = "".join(c for c in str(ch.get("number") or ch.get("name") or "") if c.isdigit())
                if num:
                    jid = f"{num}@s.whatsapp.net"
            if not jid:
                continue

            # 2) Mensajes por chat
            msc, mjs = evo.get_chat_messages(instance, jid, limit=60)
            if msc >= 400:
                continue

            msgs = None
            if isinstance(mjs, dict):
                for k in ("messages", "data", "items", "list", "response"):
                    v = mjs.get(k)
                    if isinstance(v, list):
                        msgs = v; break
            if not isinstance(msgs, list):
                msgs = []

            # 3) Guardado (dedupe naive por (brand_id, jid, from_me, text, ts))
            for m in msgs:
                try:
                    key = m.get("key") if isinstance(m.get("key"), dict) else {}
                    inner = m.get("message") if isinstance(m.get("message"), dict) else {}

                    from_me = bool(m.get("fromMe") or key.get("fromMe"))
                    text = (
                        m.get("text") or m.get("body") or
                        inner.get("conversation") or
                        (inner.get("extendedTextMessage") or {}).get("text") or
                        (inner.get("imageMessage") or {}).get("caption") or
                        (inner.get("videoMessage") or {}).get("caption") or
                        (inner.get("documentMessage") or {}).get("caption") or
                        ""
                    ).strip()

                    # timestamp si existe
                    ts = None
                    for tk in ("timestamp", "messageTimestamp", "ts", "t", "date"):
                        v = m.get(tk)
                        if isinstance(v, int): ts = v; break
                        if isinstance(v, str) and v.isdigit(): ts = int(v); break

                    # dedupe rápido
                    dupe = s.exec(
                        select(WAMessage).where(
                            WAMessage.brand_id == brand_id,
                            WAMessage.jid == jid,
                            WAMessage.from_me == from_me,
                            (WAMessage.text == text),
                            (WAMessage.ts == ts)
                        )
                    ).first()
                    if dupe:
                        continue

                    _save_message(s, brand_id, instance, jid, from_me, text, raw=m)
                    saved += 1
                except Exception as e:
                    log.warning("sync_pull skip msg: %s", e)

    return {"ok": True, "saved": saved}