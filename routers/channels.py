import os, logging, io, base64, json, time
from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import qrcode

from wa_evolution import EvolutionClient
from db import get_session, Session, select, WAConfig, Brand, WAChatMeta, WAMessage

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")

def _qr_data_url_from_code(code: str) -> str:
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

def _is_connected(state_json: Dict[str, Any]) -> bool:
    try:
        s = (state_json or {}).get("instance", {}).get("state", "")
        return s.lower() in ("open", "connected")
    except Exception:
        return False

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

def _extract_chats_payload(js: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(js, dict):
        return []
    data = []
    candidates = []
    for k in ("data", "chats", "items", "list", "results"):
        v = js.get(k)
        if isinstance(v, list):
            candidates = v
            break
    if not candidates and isinstance(js.get("0"), dict):
        candidates = [v for v in js.values() if isinstance(v, dict)]

    for it in candidates:
        jid = it.get("jid") or it.get("id") or it.get("remoteJid") or ""
        if not jid and isinstance(it.get("user"), dict):
            jid = it["user"].get("jid") or ""
        jid = _normalize_jid(jid)
        if not jid:
            continue
        name = it.get("name") or it.get("pushName") or it.get("subject") or ""
        unread = it.get("unreadCount") or it.get("unread") or 0
        last_txt, last_at = "", None
        if isinstance(it.get("lastMessage"), dict):
            lm = it["lastMessage"]
            last_txt = lm.get("conversation") or lm.get("message") or lm.get("text") or ""
            last_at = lm.get("messageTimestamp") or lm.get("timestamp") or lm.get("ts")
        elif isinstance(it.get("last_message"), dict):
            lm = it["last_message"]
            last_txt = lm.get("text") or lm.get("body") or ""
            last_at = lm.get("timestamp")
        data.append({
            "jid": jid,
            "number": _number_from_jid(jid),
            "name": name,
            "unread": int(unread) if isinstance(unread, (int, float)) else 0,
            "lastMessageText": last_txt,
            "lastMessageAt": last_at
        })
    return data

# ---------------- Fallback /config para el front ----------------
@router.get("/config")
def wa_config(brand_id: int = Query(...), session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    has_pw = bool(getattr(cfg, "super_password_hash", None))
    return {
        "brand": {"id": brand.id if brand else brand_id, "name": (brand.name if brand else f"brand_{brand_id}")},
        "config": cfg,
        "datasources": [],
        "has_password": has_pw,
        "webhook_example": f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance=brand_{brand_id}",
    }

# ---------------- Conexión / QR ----------------
@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    # Garantizar webhook (intenta set, y si no, borrar y recrear con webhook)
    res = evo.ensure_webhook(instance, webhook_url)
    if not res.get("ok"):
        log.warning("ensure_webhook fallo: %s", res)

    # reconectar para gatillar pairing/webhook (no duele si ya está conectado)
    try:
        evo.connect_instance(instance)
    except Exception as e:
        log.warning("connect_instance(%s) error: %s", instance, e)

    return {"ok": True, "webhook": webhook_url, "ensure": res}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    st = evo.connection_state(instance)
    connected = _is_connected(st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        code, qj = evo.qr_by_param(instance)
        if code == 200 and isinstance(qj, dict):
            for k in ("base64","qr","image","qrcode","dataUrl"):
                val = qj.get(k)
                if isinstance(val, str) and val.startswith("data:image"):
                    qr_data_url = val
                    break
        if not qr_data_url:
            cj = evo.connect_instance(instance)
            raw_dump = cj or {}
            pairing = (cj or {}).get("pairingCode") or (cj or {}).get("pairing_code")
            code_txt = (cj or {}).get("code") or (cj or {}).get("qrcode") or (cj or {}).get("qrCode")
            if code_txt:
                try: qr_data_url = _qr_data_url_from_code(code_txt)
                except Exception as e: log.warning("QR local error: %s", e)

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    })

# ---------------- Chats / Mensajes ----------------
@router.get("/chats")
def wa_chats(brand_id: int = Query(...), limit: int = Query(200, ge=1, le=2000), session: Session = Depends(get_session)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    connected = _is_connected(st)
    status, js = evo.list_chats(instance, limit=limit)
    if status < 400:
        return {"ok": True, "connected": connected, "status": status, "raw": js, "chats": _extract_chats_payload(js)}
    # Fallback desde DB: últimos mensajes por JID
    rows = session.exec(
        select(WAMessage).where(WAMessage.brand_id == brand_id).order_by(WAMessage.ts.desc())
    ).all()
    seen = set()
    chats_raw: List[Dict[str, Any]] = []
    for r in rows:
        if r.jid in seen:
            continue
        seen.add(r.jid)
        chats_raw.append({
            "jid": r.jid,
            "number": r.jid.split("@", 1)[0],
            "name": r.jid.split("@", 1)[0],
            "unread": 0,
            "lastMessageText": r.text,
            "lastMessageAt": r.ts,
        })
    return {"ok": True, "connected": connected, "status": status, "raw": js, "chats": chats_raw}

@router.get("/messages")
def wa_messages(
    brand_id: int = Query(...),
    jid: str = Query(...),
    limit: int = Query(50, ge=1, le=1000),
    session: Session = Depends(get_session)
):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    jid = _normalize_jid(jid)
    st = evo.connection_state(instance)
    connected = _is_connected(st)
    status, js = evo.get_chat_messages(instance, jid=jid, limit=limit)
    if status < 400:
        return {"ok": True, "connected": connected, "status": status, "messages": js}
    # Fallback DB
    rows = session.exec(
        select(WAMessage).where(WAMessage.brand_id == brand_id, WAMessage.jid == jid).order_by(WAMessage.ts.desc())
    ).all()
    messages = [{"direction": r.direction, "text": r.text, "ts": r.ts} for r in rows[:limit]]
    return {"ok": True, "connected": connected, "status": status, "messages": messages}

# ---------------- Test envío ----------------
@router.post("/test")
def wa_test(body: Dict[str, Any]):
    brand_id = int(body.get("brand_id") or 0)
    to = (body.get("to") or "").strip()
    text = body.get("text") or "Hola desde API"
    if not brand_id or not to:
        raise HTTPException(400, "brand_id y to requeridos")

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    st = evo.connection_state(instance)
    if not _is_connected(st):
        stname = (st.get("instance", {}) or {}).get("state", "unknown")
        raise HTTPException(409, f"No conectado (state: {stname})")

    resp = evo.send_text(instance, to, text)   # {"http_status": int, "body": dict}
    if (resp.get("http_status") or 500) >= 400:
        raise HTTPException(resp.get("http_status") or 500, str(resp.get("body")))

    return {"ok": True, "result": resp.get("body")}

# ---------------- Board (agrupado) ----------------
class ChatMetaIn(BaseModel):
    brand_id: int
    jid: str
    title: Optional[str] = None
    color: Optional[str] = None
    column: Optional[str] = None
    priority: Optional[int] = None
    interest: Optional[int] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None

def _prio_bucket(p: int) -> Tuple[str, str]:
    p = int(p or 0)
    if p >= 3: return ("p3", "Alta")
    if p == 2: return ("p2", "Media")
    if p == 1: return ("p1", "Baja")
    return ("p0", "Sin prioridad")

def _interest_bucket(i: int) -> Tuple[str, str]:
    i = int(i or 0)
    if i >= 3: return ("hot", "Hot")
    if i == 2: return ("warm", "Warm")
    if i == 1: return ("cold", "Cold")
    return ("unknown", "Sin interés")

@router.get("/board")
def wa_board(
    brand_id: int = Query(...),
    group: str = Query("column", pattern="^(column|priority|interest|tag)$"),
    limit: int = Query(500, ge=1, le=5000),
    show_archived: bool = Query(False),
    q: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    connected = _is_connected(st)

    status, js = evo.list_chats(instance, limit=limit)
    chats_raw = _extract_chats_payload(js if status < 400 else {})

    # Fallback DB si no hay endpoint de lista
    if not chats_raw and status >= 400:
        rows = session.exec(
            select(WAMessage).where(WAMessage.brand_id == brand_id).order_by(WAMessage.ts.desc())
        ).all()
        seen = set()
        for r in rows:
            if r.jid in seen:
                continue
            seen.add(r.jid)
            chats_raw.append({
                "jid": r.jid,
                "number": r.jid.split("@", 1)[0],
                "name": r.jid.split("@", 1)[0],
                "unread": 0,
                "lastMessageText": r.text,
                "lastMessageAt": r.ts,
            })

    metas = session.exec(select(WAChatMeta).where(WAChatMeta.brand_id == brand_id)).all()
    meta_map: Dict[str, WAChatMeta] = {m.jid: m for m in metas}

    def _match_search(item: Dict[str, Any], meta: Optional[WAChatMeta]) -> bool:
        if not q:
            return True
        term = q.lower().strip()
        fields = [item.get("name") or "", item.get("number") or "", (meta.title if meta else "") or ""]
        if meta and meta.tags_json:
            try: fields += json.loads(meta.tags_json)
            except Exception: pass
        return term in " ".join(str(x) for x in fields).lower()

    enriched = []
    for c in chats_raw:
        m = meta_map.get(c["jid"])
        if m and m.archived and not show_archived:
            continue
        if not _match_search(c, m):
            continue
        enriched.append({
            "jid": c["jid"],
            "number": c["number"],
            "name": c.get("name") or (m.title if m and m.title else c["number"]),
            "unread": c.get("unread", 0),
            "lastMessageText": c.get("lastMessageText"),
            "lastMessageAt": c.get("lastMessageAt"),
            "column": (m.column if m else "inbox"),
            "priority": (m.priority if m else 0),
            "interest": (m.interest if m else 0),
            "color": (m.color if m else None),
            "pinned": (m.pinned if m else False),
            "archived": (m.archived if m else False),
            "tags": (json.loads(m.tags_json or "[]") if m and m.tags_json else []),
            "notes": (m.notes if m else None),
        })

    enriched.sort(key=lambda x: (
        not x["pinned"],
        -(x.get("unread") or 0),
        -(int(x.get("lastMessageAt") or 0) if x.get("lastMessageAt") else 0)
    ))

    columns: Dict[str, Dict[str, Any]] = {}
    def ensure_col(key: str, title: str, color: Optional[str] = None):
        if key not in columns:
            columns[key] = {"key": key, "title": title, "color": color, "chats": []}

    if group == "column":
        for it in enriched:
            key = it["column"] or "inbox"
            ensure_col(key, key.capitalize(), it.get("color"))
            columns[key]["chats"].append(it)
    elif group == "priority":
        for it in enriched:
            k, t = _prio_bucket(it["priority"])
            ensure_col(k, f"Prioridad {t}")
            columns[k]["chats"].append(it)
    elif group == "interest":
        for it in enriched:
            k, t = _interest_bucket(it["interest"])
            ensure_col(k, f"Interés {t}")
            columns[k]["chats"].append(it)
    elif group == "tag":
        untagged_key = "_untagged"
        ensure_col(untagged_key, "Sin tag")
        for it in enriched:
            tags = it.get("tags") or []
            if not tags:
                columns[untagged_key]["chats"].append(it)
            else:
                for tg in tags:
                    key = f"tag:{tg}"
                    ensure_col(key, f"#{tg}")
                    columns[key]["chats"].append(it)

    ordered_keys = list(columns.keys())
    ordered_keys.sort(key=lambda k: (0 if k in ("inbox","p3","hot") else 1, k))
    out_cols = [{
        "key": columns[k]["key"],
        "title": columns[k]["title"],
        "color": columns[k].get("color"),
        "count": len(columns[k]["chats"]),
        "chats": columns[k]["chats"],
    } for k in ordered_keys]

    return {"ok": True, "connected": connected, "group": group, "columns": out_cols}

# ---------------- META: columnas/flags/tags ----------------
@router.post("/chat/meta")
def wa_chat_meta(payload: ChatMetaIn, session: Session = Depends(get_session)):
    jid = _normalize_jid(payload.jid)
    if not jid:
        raise HTTPException(400, "jid inválido")

    q = select(WAChatMeta).where(WAChatMeta.brand_id == payload.brand_id, WAChatMeta.jid == jid)
    meta = session.exec(q).first()
    if not meta:
        meta = WAChatMeta(brand_id=payload.brand_id, jid=jid)
        session.add(meta)

    if payload.title is not None: meta.title = payload.title.strip()
    if payload.color is not None: meta.color = payload.color.strip() or None
    if payload.column is not None: meta.column = (payload.column or "inbox").strip().lower()
    if payload.priority is not None: meta.priority = max(0, min(3, int(payload.priority)))
    if payload.interest is not None: meta.interest = max(0, min(3, int(payload.interest)))
    if payload.pinned is not None: meta.pinned = bool(payload.pinned)
    if payload.archived is not None: meta.archived = bool(payload.archived)
    if payload.tags is not None:
        clean = [t.strip() for t in payload.tags if isinstance(t, str) and t.strip()]
        meta.tags_json = json.dumps(sorted(set(clean)))
    if payload.notes is not None: meta.notes = payload.notes

    session.add(meta); session.commit(); session.refresh(meta)

    return {"ok": True, "meta": {
        "jid": meta.jid, "title": meta.title, "color": meta.color, "column": meta.column,
        "priority": meta.priority, "interest": meta.interest, "pinned": meta.pinned,
        "archived": meta.archived, "tags": json.loads(meta.tags_json or "[]"),
        "notes": meta.notes
    }}

class BulkMoveIn(BaseModel):
    brand_id: int
    jids: List[str]
    column: str

@router.post("/chat/bulk_move")
def wa_chat_bulk_move(payload: BulkMoveIn, session: Session = Depends(get_session)):
    column = (payload.column or "inbox").strip().lower()
    updated = 0
    for raw in payload.jids:
        jid = _normalize_jid(raw)
        if not jid:
            continue
        q = select(WAChatMeta).where(WAChatMeta.brand_id == payload.brand_id, WAChatMeta.jid == jid)
        meta = session.exec(q).first()
        if not meta:
            meta = WAChatMeta(brand_id=payload.brand_id, jid=jid)
        meta.column = column
        session.add(meta)
        updated += 1
    session.commit()
    return {"ok": True, "updated": updated, "column": column}

# ---------------- WEBHOOK (persistencia + echo de prueba) ----------------
def _digits_only(x: str) -> str:
    return "".join(ch for ch in (x or "") if ch.isdigit())

def _extract_text_and_sender(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """
    Soporta varios layouts de Evolution/Baileys:
    - { message: { conversation: "txt" }, key: { remoteJid: "..."} }
    - { message: { extendedTextMessage: { text: "..." }}, from: "..." }
    - { text: "...", from: "..." }
    - { data: {...} }
    """
    msg = body.get("message") or body.get("data") or {}
    sender = (
        (msg.get("key") or {}).get("remoteJid") or
        msg.get("from") or
        body.get("from") or
        body.get("remoteJid")
    )
    text = (
        msg.get("conversation") or
        (msg.get("extendedTextMessage") or {}).get("text") or
        msg.get("text") or
        body.get("text")
    )
    return text, sender

@router.get("/webhook/ping")
def webhook_ping(token: str = Query("")):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "bad token")
    return {"ok": True, "pong": True}

@router.post("/webhook")
async def webhook(
    req: Request,
    token: str = Query(""),
    instance: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        # devolver 200 para no provocar reintentos ruidosos
        log.warning("Webhook con token invalido")
        return {"ok": True}

    try:
        body = await req.json()
    except Exception:
        body = {}
    log.info("WA webhook in: %s", json.dumps(body)[:2000])

    # deducir brand_id
    inst = instance or body.get("instance") or body.get("instanceName") or ""
    brand_id = None
    if isinstance(inst, str) and inst.startswith("brand_"):
        try:
            brand_id = int(inst.split("_", 1)[1])
        except:
            pass
    if not brand_id:
        maybe = body.get("brandId") or body.get("brand_id")
        try:
            brand_id = int(maybe)
        except:
            pass
    if not brand_id:
        return {"ok": True, "skip": "no brand_id"}

    text, sender = _extract_text_and_sender(body)
    if not sender:
        return {"ok": True, "skip": "no sender"}
    jid = _normalize_jid(sender)
    number = jid.split("@", 1)[0]

    if not text or not isinstance(text, str) or not text.strip():
        return {"ok": True, "skip": "no text"}

    # persistir mensaje entrante
    try:
        session.add(WAMessage(
            brand_id=brand_id,
            jid=jid,
            direction="in",
            text=text.strip(),
            ts=int(time.time())
        ))
        session.commit()
    except Exception as e:
        log.warning("Persistencia WAMessage fallo: %s", e)

    # echo de prueba para verificar end-to-end
    try:
        EvolutionClient().send_text(inst or f"brand_{brand_id}", number, f"Recibido: {text.strip()}")
        # también persistimos el out para tener hilo completo
        session.add(WAMessage(
            brand_id=brand_id,
            jid=jid,
            direction="out",
            text=f"Recibido: {text.strip()}",
            ts=int(time.time())
        ))
        session.commit()
    except Exception as e:
        log.warning("No pude responder echo: %s", e)

    return {"ok": True}
