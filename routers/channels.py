# routers/channels.py
import os, logging, io, base64, json, time
from typing import Optional, Dict, Any, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query, Depends, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import get_session, session_cm, Session, select, WAConfig, Brand, WAChatMeta, WAMessage

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

# ====== ENV ======
EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"  # unificado

# ====== HTTP helpers contra Evolution (tolerantes a versiones) ======
def _evo_headers() -> Dict[str, str]:
    h = {}
    if EVOLUTION_API_KEY:
        # distintas builds usan uno u otro
        h["apikey"] = EVOLUTION_API_KEY
        h["Authorization"] = f"Bearer {EVOLUTION_API_KEY}"
    return h

def _evo_req(method: str, path: str, params: Dict[str, Any] | None = None, json_body: Any | None = None):
    if not EVOLUTION_BASE_URL:
        return {"http_status": 500, "body": {"error": "EVOLUTION_BASE_URL not set"}}
    url = f"{EVOLUTION_BASE_URL}{path}"
    try:
        with httpx.Client(timeout=30) as cli:
            resp = cli.request(method, url, params=params, json=json_body, headers=_evo_headers())
        try:
            data = resp.json()
        except Exception:
            data = {"text": resp.text}
        log.info("HTTP %s %s -> %s", method, url, resp.status_code)
        return {"http_status": resp.status_code, "body": data}
    except Exception as e:
        log.warning("evo %s %s fail: %s", method, path, e)
        return {"http_status": 599, "body": {"error": str(e)}}

def _evo_get(path: str, params: Dict[str, Any] | None = None):
    return _evo_req("GET", path, params=params)

def _evo_post(path: str, json_body: Any | None = None):
    return _evo_req("POST", path, json_body=json_body)

# ====== Utils internos ======
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
        if not isinstance(b, dict): 
            return False
        inst = b.get("instance") or {}
        s = inst.get("state") or b.get("state") or ""
        return str(s).lower() in ("open", "connected", "online")
    except Exception:
        return False

def _qr_data_url_from_text(text: str) -> str:
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.warning("qr build failed: %s", e)
        return ""

def _save_msg(session: Session, brand_id: int, jid: str, text: str, from_me: bool, ts: int | None = None):
    try:
        m = WAMessage(
            brand_id=brand_id,
            jid=_normalize_jid(jid),
            from_me=bool(from_me),
            text=text or "",
            ts=int(ts or time.time()),
        )
        session.add(m)
        session.commit()
    except Exception as e:
        log.warning("save_msg fail: %s", e)

# ====== Evolution endpoints (compat) ======
def evo_connection_state(instance: str) -> Dict[str, Any]:
    # prueba varias rutas de estado
    for path in (
        f"/instance/connectionState/{instance}",
        f"/instance/state/{instance}",
        f"/instance/connect/{instance}",  # algunas devuelven state aquí también
    ):
        r = _evo_get(path)
        if r["http_status"] != 404:
            return r
    return {"http_status": 404, "body": {"error": "no state endpoint"}}

def evo_connect(instance: str) -> Dict[str, Any]:
    # conecta o refresca QR/código
    for path in (
        f"/instance/connect/{instance}",
        f"/instance/open/{instance}",
    ):
        r = _evo_get(path)
        if r["http_status"] != 404:
            return r
    return {"http_status": 404, "body": {"message": "Cannot connect"}}

def evo_create_instance(instance: str, integration: str | None = "WHATSAPP"):
    # distintos paths aceptados
    payloads = [
        {"instanceName": instance, "integration": integration or "WHATSAPP"},
        {"instanceName": instance},
    ]
    for body in payloads:
        for path in ("/instance/create", "/instance/add", "/instance/init"):
            r = _evo_post(path, json_body=body)
            if r["http_status"] != 404:
                return r
    # algunos tienen "/instance/create/{instance}"
    r = _evo_post(f"/instance/create/{instance}?integration={integration or 'WHATSAPP'}")
    return r

def evo_set_webhook(instance: str, webhook_url: str):
    # intenta varias firmas
    tries = [
        ("POST", "/webhook", {"instanceName": instance, "webhook": webhook_url}),
        ("GET",  f"/webhook/set", {"instanceName": instance, "webhook": webhook_url}),
        ("GET",  f"/webhook",     {"instanceName": instance, "webhook": webhook_url}),
        ("POST", f"/instance/setWebhook", {"instanceName": instance, "webhook": webhook_url}),
    ]
    last = {"http_status": 404, "body": {"error": "webhook endpoint not found"}}
    for m, p, data in tries:
        r = _evo_req(m, p, params=(data if m == "GET" else None), json_body=(data if m == "POST" else None))
        if r["http_status"] != 404:
            return r
        last = r
    return last

def evo_send_text(instance: str, number: str, text: str):
    bodies = [
        {"number": number, "text": text},
        {"phone": number,  "text": text},
        {"to": number,     "text": text},
    ]
    for body in bodies:
        r = _evo_post(f"/message/sendText/{instance}", json_body=body)
        if r["http_status"] != 404:
            return r
    # alternativos
    r = _evo_post(f"/messages/send/{instance}", json_body={"to": number, "text": text})
    if r["http_status"] == 404:
        r = _evo_post(f"/message/send/{instance}", json_body={"to": number, "text": text})
    return r

def evo_qr_image_or_code(instance: str) -> Dict[str, Any]:
    """
    Intenta retornar dict {"base64": dataURL?, "pairingCode": str|None, "code": str|None, "raw": {...}}
    """
    out = {"base64": None, "pairingCode": None, "code": None, "raw": {}}

    # 1) muchos backends retornan code/base64 en connect
    rc = evo_connect(instance)
    out["raw"] = rc
    body = rc.get("body") or {}
    if isinstance(body, dict):
        out["pairingCode"] = body.get("pairingCode") or body.get("pin") or body.get("code_short")
        out["code"] = body.get("code") or body.get("qrcode") or body.get("qrCode")
        # dataURL directo
        for k in ("base64", "dataUrl", "qr", "image"):
            v = body.get(k)
            if isinstance(v, str) and v.startswith("data:image"):
                out["base64"] = v
                break

    # 2) si no hay base64 pero hay "code" => generar dataURL local
    if not out["base64"] and out["code"]:
        out["base64"] = _qr_data_url_from_text(out["code"])

    return out

def evo_list_messages(instance: str, limit: int = 200) -> Dict[str, Any]:
    params = {"limit": str(limit)}
    for path in (
        f"/messages/{instance}",
        f"/instance/{instance}/messages",
        f"/chat/messages/{instance}",
        f"/message/list/{instance}",
    ):
        r = _evo_get(path, params=params)
        if r["http_status"] != 404:
            return r
    return {"http_status": 404, "body": {"error": "no messages endpoint"}}

def ensure_started_and_webhook(instance: str, webhook_url: str) -> Dict[str, Any]:
    detail = {"create": None, "webhook": None, "connect": None}
    # create
    cr = evo_create_instance(instance, integration="WHATSAPP")
    detail["create"] = cr
    # 403 por "already in use" es aceptable
    # set webhook
    wr = evo_set_webhook(instance, webhook_url)
    detail["webhook"] = wr
    # connect
    cn = evo_connect(instance)
    detail["connect"] = cn
    ok = any(200 <= (d or {}).get("http_status", 0) < 400 for d in detail.values())
    return {"ok": ok, "detail": detail}

# ====== CONFIG ======
@router.get("/config")
def wa_config(brand_id: int = Query(...), session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    has_pw = bool(getattr(cfg, "super_password_hash", None))
    out = {
        "brand": {"id": brand.id if brand else brand_id, "name": (brand.name if brand else f"brand_{brand_id}")},
        "config": cfg,
        "datasources": [],
        "has_password": has_pw,
        "webhook_example": f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance=brand_{brand_id}",
        "instance_name": f"brand_{brand_id}",
    }
    log.debug("/config -> %s", out)
    return out

# ====== START / QR / STATUS ======
@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    res = ensure_started_and_webhook(instance, webhook_url)
    if not res.get("ok"):
        raise HTTPException(404, "No se pudo iniciar/conectar la instancia")
    return {"ok": True, "instance": instance, "created": res}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    st = evo_connection_state(instance)
    connected = _is_connected(st)

    base64_img: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        qr = evo_qr_image_or_code(instance)
        base64_img = qr.get("base64")
        pairing = qr.get("pairingCode")
        raw_dump = qr.get("raw") or {}

    out = {
        "connected": connected,
        "qr": base64_img,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    }
    log.debug("/qr out: %s", {**out, "raw": "...truncated..."})
    return JSONResponse(out)

@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    st = evo_connection_state(instance)
    return {"ok": True, "instance": instance, "state": st}

# ====== TEST ENVÍO ======
@router.post("/test")
async def wa_test(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
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
        try:
            brand_id_raw = str(instance).split("_", 1)[1]
        except Exception:
            brand_id_raw = None
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

    log.info("/test brand=%s to=%s text=%s", brand_id, to, text)

    if not brand_id or not to:
        raise HTTPException(422, "Se requieren brand_id y to")

    instance = f"brand_{brand_id}"
    resp = evo_send_text(instance, to, text)
    if (resp.get("http_status") or 500) >= 400:
        raise HTTPException(resp.get("http_status") or 500, str(resp.get("body")))

    # persistimos salida para ver en UI aunque no entre el webhook
    try:
        with session_cm() as s:
            jid = f"{to}@s.whatsapp.net"
            msg = WAMessage(
                brand_id=brand_id,
                jid=jid,
                from_me=True,
                text=text,
                ts=int(time.time()),
            )
            setattr(msg, "instance", instance)
            setattr(msg, "raw_json", json.dumps({"source": "wa_test"}, ensure_ascii=False))
            s.add(msg)
            s.commit()
        log.debug("/test saved outgoing to DB jid=%s", jid)
    except Exception as e:
        log.warning("no se pudo guardar mensaje saliente wa_test: %s", e)

    return {"ok": True, "result": resp.get("body")}

# ====== WEBHOOK (tolerante) ======
def _parse_evo_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normaliza formatos comunes de Evolution -> lista de {jid,text,from_me,ts}
    """
    out = []
    if not isinstance(payload, dict):
        return out

    def _one(obj):
        jid = (obj.get("key") or {}).get("remoteJid") or obj.get("jid") or ""
        from_me = bool((obj.get("key") or {}).get("fromMe") or obj.get("fromMe"))
        ts = obj.get("messageTimestamp") or obj.get("timestamp") or int(time.time())
        msg = obj.get("message") or {}
        text = (
            msg.get("conversation")
            or (msg.get("extendedTextMessage") or {}).get("text")
            or obj.get("text")
            or obj.get("body")
            or ""
        )
        if jid and text is not None:
            out.append({"jid": jid, "text": str(text), "from_me": from_me, "ts": int(ts)})

    if "messages" in payload and isinstance(payload["messages"], list):
        for m in payload["messages"]:
            if isinstance(m, dict):
                _one(m)
    else:
        _one(payload)
    return out

@router.api_route("/webhook", methods=["GET", "POST"])
async def wa_webhook(request: Request, token: str = Query(""), instance: str = Query("")):
    if EVOLUTION_WEBHOOK_TOKEN and token != EVOLUTION_WEBHOOK_TOKEN:
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)

    if request.method == "GET":
        return {"ok": True, "instance": instance or None}

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # inferir brand_id
    brand_id = 0
    try:
        if instance and instance.startswith("brand_"):
            brand_id = int(instance.split("_", 1)[1])
    except Exception:
        brand_id = 0
    if not brand_id:
        try:
            inst = payload.get("instanceName") or payload.get("instance", "")
            if isinstance(inst, str) and inst.startswith("brand_"):
                brand_id = int(inst.split("_", 1)[1])
        except Exception:
            brand_id = 0

    msgs = _parse_evo_payload(payload)
    saved = 0
    if brand_id and msgs:
        with session_cm() as s:
            for m in msgs:
                _save_msg(s, brand_id, m["jid"], m["text"], m["from_me"], m["ts"])
                saved += 1

    log.info("/webhook instance=%s saved=%s", instance, saved)
    return {"ok": True, "saved": saved}

# ====== SYNC PULL (sin webhook) ======
@router.get("/sync_pull")
def wa_sync_pull(brand_id: int = Query(...), limit: int = Query(200, ge=10, le=1000)):
    instance = f"brand_{brand_id}"
    res = evo_list_messages(instance, limit=limit)
    if (res.get("http_status") or 500) >= 400:
        return {"ok": False, "status": res.get("http_status"), "error": res.get("body")}

    body = res.get("body") or {}
    items = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        for key in ("messages", "data", "items"):
            if isinstance(body.get(key), list):
                items = body[key]
                break

    count = 0
    with session_cm() as s:
        for obj in items:
            try:
                for m in _parse_evo_payload(obj):
                    _save_msg(s, brand_id, m["jid"], m["text"], m["from_me"], m["ts"])
                    count += 1
            except Exception as e:
                log.debug("skip item parse: %s", e)

    return {"ok": True, "saved": count, "source_status": res.get("http_status")}

# ====== META / BOARD / MESSAGES ======
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
    out = {"ok": True, "meta": {
        "jid": meta.jid, "title": meta.title, "color": meta.color, "column": meta.column,
        "priority": meta.priority, "interest": meta.interest, "pinned": meta.pinned,
        "archived": meta.archived, "tags": json.loads(meta.tags_json or "[]"),
        "notes": meta.notes
    }}
    log.debug("/chat/meta -> %s", out)
    return out

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
    out = {"ok": True, "updated": updated, "column": column}
    log.info("/chat/bulk_move -> %s", out)
    return out

@router.get("/messages")
def wa_messages(
    brand_id: int = Query(...),
    jid: str = Query(...),
    limit: int = Query(60, ge=1, le=300),
    session: Session = Depends(get_session)
):
    jid = _normalize_jid(jid)
    if not jid:
        return {"ok": True, "messages": []}
    q = select(WAMessage).where(WAMessage.brand_id == brand_id, WAMessage.jid == jid)
    rows = session.exec(q).all()
    out = []
    for r in sorted(rows, key=lambda x: (getattr(x, "ts", None) or 0), reverse=True)[:limit]:
        from_me = bool(getattr(r, "from_me", False))
        text = getattr(r, "text", "") or ""
        if from_me:
            out.append({"key": {"remoteJid": jid, "fromMe": True}, "message": {"conversation": text}})
        else:
            out.append({"key": {"remoteJid": jid, "fromMe": False}, "message": {"conversation": text}})
    out = list(reversed(out))
    return {"ok": True, "messages": out}

@router.get("/board")
def wa_board(
    brand_id: int = Query(...),
    group: str = Query("column", pattern="^(column|priority|interest|tag)$"),
    limit: int = Query(500, ge=1, le=5000),
    show_archived: bool = Query(False),
    q: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    try:
        st = evo_connection_state(f"brand_{brand_id}")
        connected = _is_connected(st)
    except Exception as e:
        log.warning("/board state error: %s", e)
        connected = False

    rows = session.exec(select(WAMessage).where(WAMessage.brand_id == brand_id)).all()
    last_by_jid: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        jid = _normalize_jid(r.jid)
        if not jid:
            continue
        cur = last_by_jid.get(jid)
        tsv = getattr(r, "ts", None) or 0
        if (not cur) or tsv > (cur.get("ts") or 0):
            last_by_jid[jid] = {
                "jid": jid,
                "number": _number_from_jid(jid),
                "lastMessageText": r.text,
                "lastMessageAt": tsv,
                "unread": 0,
                "ts": tsv,
            }

    metas = session.exec(select(WAChatMeta).where(WAChatMeta.brand_id == brand_id)).all()
    meta_map: Dict[str, WAChatMeta] = {m.jid: m for m in metas}

    def _match_search(item: Dict[str, Any], meta: Optional[WAChatMeta]) -> bool:
        if not q:
            return True
        term = q.lower().strip()
        fields = [item.get("number") or "", (meta.title if meta else "") or ""]
        if meta and meta.tags_json:
            try:
                fields += json.loads(meta.tags_json)
            except Exception:
                pass
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
    out = {"ok": True, "connected": connected, "group": group, "columns": out_cols}
    return out

# ====== SET WEBHOOK manual ======
@router.api_route("/set_webhook", methods=["GET", "POST", "OPTIONS"])
def wa_set_webhook(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    wr = evo_set_webhook(instance, webhook_url)
    if not (200 <= wr.get("http_status", 0) < 400):
        ensure = ensure_started_and_webhook(instance, webhook_url)
        body = ensure
        sc = 200 if ensure.get("ok") else 500
    else:
        sc = wr.get("http_status", 200)
        body = {"ok": True, "detail": wr}

    out = {"ok": sc < 400, "status": sc, "body": body, "webhook_url": webhook_url}
    log.info("/set_webhook -> %s", out)
    return out
