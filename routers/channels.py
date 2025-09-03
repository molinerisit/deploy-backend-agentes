import os, logging, io, base64, json, time
from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from wa_evolution import EvolutionClient
from db import get_session, Session, select, WAConfig, Brand, WAChatMeta, WAMessage

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")

# ---------- utils básicos ----------
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

def _is_connected(state_json: Dict[str, Any]) -> bool:
    try:
        b = state_json.get("body") if "body" in state_json else state_json
        s = (b or {}).get("instance", {}).get("state") or (b or {}).get("state") or ""
        return str(s).lower() in ("open", "connected")
    except Exception:
        return False

def _qr_data_url_from_code(code: str) -> str:
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(code).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return ""

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
        "instance_name": f"brand_{brand_id}",
    }

# ---------------- Conexión / QR ----------------
def _ensure_started(instance: str, webhook_url: str) -> Dict[str, Any]:
    evo = EvolutionClient()
    # Intentamos crear con webhook in-line (v2.3)
    created = evo.create_instance(instance, webhook_url=webhook_url)
    if created["http_status"] == 400 and "already" in json.dumps(created["body"]).lower():
        pass  # existente
    elif created["http_status"] >= 400 and created["http_status"] != 409:
        # si fue 4xx que no sea "ya existe", lo dejamos logueado pero seguimos conectando
        log.warning("ensure_started create_instance result: %s", created)

    # Asegurar webhook (si el create no lo tomó)
    evo.set_webhook(instance, webhook_url)

    # Conectar (idempotente)
    evo.connect_instance(instance)
    return created

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    try:
        created = _ensure_started(instance, webhook_url)
    except Exception as e:
        log.warning("ensure_started fallo: %s", e)
        raise HTTPException(404, "No se pudo iniciar/conectar la instancia")

    return {"ok": True, "instance": instance, "created": created}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    evo = EvolutionClient()
    st = evo.connection_state(instance)

    connected = _is_connected(st)
    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        # Algunos servers entregan el QR al llamar connect por segunda vez
        try:
            raw_dump = evo.connect_instance(instance) or {}
        except Exception:
            raw_dump = {}

        # buscar QR embebido en answers comunes
        body = raw_dump.get("body", {})
        pairing = body.get("pairingCode") or body.get("pairing_code")
        code_txt = body.get("code") or body.get("qrcode") or body.get("qrCode")
        if code_txt:
            qr_data_url = _qr_data_url_from_code(code_txt)

        if not qr_data_url:
            code, qj = evo.qr_by_param(instance)
            raw_dump = qj or raw_dump
            for k in ("base64", "qr", "image", "qrcode", "dataUrl"):
                v = (qj or {}).get(k)
                if isinstance(v, str) and v.startswith("data:image"):
                    qr_data_url = v
                    break

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    })

# ---- Estado
@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    return {"ok": True, "instance": instance, "state": st}

# ---------------- Test envío ----------------
@router.post("/test")
async def wa_test(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict): body = {}
    except Exception:
        body = {}
    qp = dict(request.query_params)

    def pick(*keys, default=None):
        for k in keys:
            if k in body and body[k] not in (None, ""):
                return body[k]
            if k in qp and qp[k] not in (None, ""):
                return qp[k]
        return default

    instance = pick("instance")
    brand_id_raw = pick("brand_id", "brandId", "brand")
    if not brand_id_raw and instance and str(instance).startswith("brand_"):
        try: brand_id_raw = str(instance).split("_", 1)[1]
        except Exception: brand_id_raw = None
    try:
        brand_id = int(brand_id_raw or 0)
    except Exception:
        brand_id = 0

    to_raw = str(pick("to", "phone", "number", "jid", "msisdn", default="")).strip()
    if "@s.whatsapp.net" in to_raw:
        to = _number_from_jid(to_raw)
    else:
        to = "".join(ch for ch in to_raw if ch.isdigit())

    text = str(pick("text", "message", "body", default="Hola desde API"))

    if not brand_id or not to:
        raise HTTPException(422, "Se requieren brand_id y to")

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    resp = evo.send_text(instance, to, text)
    if (resp.get("http_status") or 500) >= 400:
        raise HTTPException(resp.get("http_status") or 500, str(resp.get("body")))
    return {"ok": True, "result": resp.get("body")}

# ---------------- Webhook: guardar y auto-responder ----------------
def _save_message(session: Session, brand_id: int, jid: str, from_me: bool, text: str, raw: Dict[str, Any]) -> None:
    try:
        msg = WAMessage(
            brand_id=brand_id,
            jid=jid,
            from_me=from_me,
            text=text or "",
            ts=int(time.time()),
            raw_json=json.dumps(raw, ensure_ascii=False),
        )
        session.add(msg); session.commit()
    except Exception as e:
        log.warning("no se pudo guardar WAMessage: %s", e)

def _extract_incoming(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Devuelve [{'jid': str, 'text': str}] para cada mensaje recibido (no fromMe).
    Soporta estructuras típicas de Evolution/Baileys.
    """
    out = []
    body = payload.get("body") or payload.get("data") or payload
    # array messages
    msgs = body.get("messages") if isinstance(body, dict) else None
    if isinstance(msgs, list):
        for m in msgs:
            from_me = bool(m.get("fromMe") or m.get("key", {}).get("fromMe"))
            if from_me: continue
            jid = m.get("chatId") or m.get("remoteJid") or m.get("key", {}).get("remoteJid") or ""
            inner = m.get("message") if isinstance(m.get("message"), dict) else {}
            text = m.get("text") or m.get("body") or inner.get("conversation") or inner.get("extendedTextMessage", {}).get("text")
            if jid and text:
                out.append({"jid": _normalize_jid(jid), "text": str(text).strip()})
    # single message fallback
    else:
        m = body if isinstance(body, dict) else {}
        from_me = bool(m.get("fromMe") or m.get("key", {}).get("fromMe"))
        if not from_me:
            jid = m.get("chatId") or m.get("remoteJid") or m.get("key", {}).get("remoteJid") or ""
            inner = m.get("message") if isinstance(m.get("message"), dict) else {}
            text = m.get("text") or m.get("body") or inner.get("conversation") or inner.get("extendedTextMessage", {}).get("text")
            if jid and text:
                out.append({"jid": _normalize_jid(jid), "text": str(text).strip()})
    return out

@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    if token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "token inválido")

    try:
        payload = await req.json()
    except Exception:
        try:
            raw = await req.body()
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}

    # deducir brand
    if not instance:
        instance = (payload.get("instance") or payload.get("instanceName") or payload.get("session") or "")
    brand_id = None
    if isinstance(instance, str) and instance.startswith("brand_"):
        try: brand_id = int(instance.split("_", 1)[1])
        except Exception: brand_id = None
    if not brand_id:
        # ignoramos si no sabemos a qué brand pertenece
        return {"ok": True, "ignored": "no brand"}

    evo = EvolutionClient()
    incoming = _extract_incoming(payload)

    with get_session() as session:
        for m in incoming:
            jid = m["jid"]; text = m["text"]
            _save_message(session, brand_id, jid, from_me=False, text=text, raw=payload)

            # auto-respuesta simple (para validar ida/vuelta)
            try:
                evo.send_text(instance, _number_from_jid(jid), f"🤖 Recibido: {text[:180]}")
            except Exception as e:
                log.warning("no se pudo responder: %s", e)

    return {"ok": True, "count": len(incoming)}

# ---------------- Board (desde DB + metadatos) ----------------
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

    if payload.title is not None: meta.title = (payload.title or "").strip()
    if payload.color is not None: meta.color = (payload.color or "").strip() or None
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

@router.get("/messages")
def wa_messages(brand_id: int = Query(...), jid: str = Query(...), limit: int = Query(60, ge=1, le=300), session: Session = Depends(get_session)):
    jid = _normalize_jid(jid)
    if not jid:
        return {"ok": True, "messages": []}
    q = select(WAMessage).where(WAMessage.brand_id == brand_id, WAMessage.jid == jid)
    rows = session.exec(q).all()
    # devolvemos en formato similar a Evolution message array
    out = []
    for r in sorted(rows, key=lambda x: x.ts, reverse=True)[:limit]:
        from_me = bool(getattr(r, "from_me", False))
        if from_me:
            out.append({"key": {"remoteJid": jid, "fromMe": True}, "message": {"conversation": r.text}})
        else:
            out.append({"key": {"remoteJid": jid, "fromMe": False}, "message": {"conversation": r.text}})
    return {"ok": True, "messages": list(reversed(out))}

@router.get("/board")
def wa_board(
    brand_id: int = Query(...),
    group: str = Query("column", pattern="^(column|priority|interest|tag)$"),
    limit: int = Query(500, ge=1, le=5000),
    show_archived: bool = Query(False),
    q: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    # Estado (conectado o no)
    try:
        evo = EvolutionClient()
        st = evo.connection_state(f"brand_{brand_id}")
        connected = _is_connected(st)
    except Exception:
        connected = False

    # Construimos board con último mensaje por JID desde nuestra DB
    # (no dependemos de /chat/list de Evolution)
    rows = session.exec(select(WAMessage).where(WAMessage.brand_id == brand_id)).all()
    last_by_jid: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        jid = _normalize_jid(r.jid)
        if not jid: continue
        cur = last_by_jid.get(jid)
        if (not cur) or (r.ts or 0) > (cur.get("ts") or 0):
            last_by_jid[jid] = {
                "jid": jid,
                "number": _number_from_jid(jid),
                "lastMessageText": r.text,
                "lastMessageAt": r.ts,
                "unread": 0,  # si querés, podés calcular esto más adelante
            }

    metas = session.exec(select(WAChatMeta).where(WAChatMeta.brand_id == brand_id)).all()
    meta_map: Dict[str, WAChatMeta] = {m.jid: m for m in metas}

    def _match_search(item: Dict[str, Any], meta: Optional[WAChatMeta]) -> bool:
        if not q:
            return True
        term = q.lower().strip()
        fields = [item.get("number") or "", (meta.title if meta else "") or ""]
        if meta and meta.tags_json:
            try: fields += json.loads(meta.tags_json)
            except Exception: pass
        return term in " ".join(str(x) for x in fields).lower()

    enriched = []
    for jid, base in last_by_jid.items():
        m = meta_map.get(jid)
        if m and m.archived and not show_archived:
            continue
        if not _match_search(base, m):
            continue
        enriched.append({
            "jid": base["jid"],
            "number": base["number"],
            "name": (m.title if m and m.title else base["number"]),
            "unread": base.get("unread", 0),
            "lastMessageText": base.get("lastMessageText"),
            "lastMessageAt": base.get("lastMessageAt"),
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
