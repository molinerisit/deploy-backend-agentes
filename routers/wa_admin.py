# routers/wa_admin.py
import os
import io
import json
import time
import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import (
    get_session,
    session_cm,
    Session,
    select,
    Brand,
    WAConfig,
    WAMessage,
)

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

# ====== ENV ======
EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"

# ====== HTTP helpers contra Evolution ======
def _evo_headers() -> Dict[str, str]:
    h = {}
    if EVOLUTION_API_KEY:
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

# ====== Utils ======
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

def _qr_data_url_from_text(text: str) -> str:
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.warning("qr build failed: %s", e)
        return ""

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

# ====== Evolution compat calls ======
def evo_connection_state(instance: str) -> Dict[str, Any]:
    for path in (
        f"/instance/connectionState/{instance}",
        f"/instance/state/{instance}",
        f"/instance/connect/{instance}",
    ):
        r = _evo_get(path)
        if r["http_status"] != 404:
            return r
    return {"http_status": 404, "body": {"error": "no state endpoint"}}

def evo_connect(instance: str) -> Dict[str, Any]:
    for path in (
        f"/instance/connect/{instance}",
        f"/instance/open/{instance}",
    ):
        r = _evo_get(path)
        if r["http_status"] != 404:
            return r
    return {"http_status": 404, "body": {"message": "Cannot connect"}}

def evo_create_instance(instance: str, integration: str | None = "WHATSAPP"):
    payloads = [
        {"instanceName": instance, "integration": integration or "WHATSAPP"},
        {"instanceName": instance},
    ]
    for body in payloads:
        for path in ("/instance/create", "/instance/add", "/instance/init"):
            r = _evo_post(path, json_body=body)
            if r["http_status"] != 404:
                return r
    r = _evo_post(f"/instance/create/{instance}?integration={integration or 'WHATSAPP'}")
    return r

def evo_set_webhook(instance: str, webhook_url: str):
    tries = [
        ("POST", "/webhook", {"instanceName": instance, "webhook": webhook_url}),
        ("GET",  "/webhook/set", {"instanceName": instance, "webhook": webhook_url}),
        ("GET",  "/webhook",     {"instanceName": instance, "webhook": webhook_url}),
        ("POST", "/instance/setWebhook", {"instanceName": instance, "webhook": webhook_url}),
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
    r = _evo_post(f"/messages/send/{instance}", json_body={"to": number, "text": text})
    if r["http_status"] == 404:
        r = _evo_post(f"/message/send/{instance}", json_body={"to": number, "text": text})
    return r

def evo_qr_image_or_code(instance: str) -> Dict[str, Any]:
    out = {"base64": None, "pairingCode": None, "code": None, "raw": {}}
    rc = evo_connect(instance)
    out["raw"] = rc
    body = rc.get("body") or {}
    if isinstance(body, dict):
        out["pairingCode"] = body.get("pairingCode") or body.get("pin") or body.get("code_short")
        out["code"] = body.get("code") or body.get("qrcode") or body.get("qrCode")
        for k in ("base64", "dataUrl", "qr", "image"):
            v = body.get(k)
            if isinstance(v, str) and v.startswith("data:image"):
                out["base64"] = v
                break
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

# ====== Normalizadores ======
def _parse_evo_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normaliza Evolution -> lista de {jid,text,from_me,ts}
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

# ====== Endpoints mínimos usados por WhatsAppAdmin del front ======
@router.get("/config")
def wa_config(brand_id: int = Query(...), session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    has_pw = bool(getattr(cfg, "super_password_hash", None))
    out = {
        "brand": {"id": brand.id if brand else brand_id, "name": (brand.name if brand else f"brand_{brand_id}")},
        "config": cfg,
        "has_password": has_pw,
        "instance_name": f"brand_{brand_id}",
        "webhook_example": f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance=brand_{brand_id}",
    }
    return out

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    create = evo_create_instance(instance, integration="WHATSAPP")
    setwh = evo_set_webhook(instance, webhook_url)
    conn = evo_connect(instance)

    ok = any(200 <= d.get("http_status", 0) < 400 for d in (create, setwh, conn))
    if not ok:
        raise HTTPException(404, "No se pudo iniciar/conectar la instancia")
    return {"ok": True, "detail": {"create": create, "webhook": setwh, "connect": conn}}

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

    out = {"connected": connected, "qr": base64_img, "pairingCode": pairing, "state": st, "raw": raw_dump}
    return JSONResponse(out)

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

    if not brand_id or not to:
        raise HTTPException(422, "Se requieren brand_id y to")

    instance = f"brand_{brand_id}"
    resp = evo_send_text(instance, to, text)
    if (resp.get("http_status") or 500) >= 400:
        raise HTTPException(resp.get("http_status") or 500, str(resp.get("body")))

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
    except Exception as e:
        log.warning("no se pudo guardar mensaje saliente wa_test: %s", e)

    return {"ok": True, "result": resp.get("body")}

# ====== SET WEBHOOK (manual) ======
@router.api_route("/set_webhook", methods=["GET", "POST", "OPTIONS"])
def wa_set_webhook(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    wr = evo_set_webhook(instance, webhook_url)
    detail = {"webhook": wr}

    if not (200 <= wr.get("http_status", 0) < 400):
        # intenta create+connect también
        cr = evo_create_instance(instance, integration="WHATSAPP")
        cn = evo_connect(instance)
        detail.update({"create": cr, "connect": cn})

    ok = any(200 <= d.get("http_status", 0) < 400 for d in detail.values())
    sc = 200 if ok else 500
    out = {"ok": ok, "status": sc, "body": {"ok": ok, "detail": detail}, "webhook_url": webhook_url}
    return JSONResponse(out, status_code=sc)

# ====== SYNC PULL (sin webhook) ======
@router.api_route("/sync_pull", methods=["GET", "POST"])
def wa_sync_pull(brand_id: int = Query(...), limit: int = Query(200, ge=10, le=1000)):
    """
    Jala mensajes recientes desde Evolution y los persiste en la DB.
    Compatible con front que llama POST /api/wa/sync_pull?brand_id=1
    """
    instance = f"brand_{brand_id}"
    res = evo_list_messages(instance, limit=limit)
    if (res.get("http_status") or 500) >= 400:
        return {"ok": False, "status": res.get("http_status"), "error": res.get("body")}

    body = res.get("body") or {}
    items: List[Dict[str, Any]] = []
    if isinstance(body, list):
        items = body
    elif isinstance(body, dict):
        # tolera varios formatos: {messages: [...]}, {data: [...]}, {items: [...]}
        for key in ("messages", "data", "items"):
            if isinstance(body.get(key), list):
                items = body[key]
                break

    count = 0
    with session_cm() as s:
        # algunos endpoints devuelven una lista "plana" de mensajes
        if items and items and isinstance(items[0], dict) and ("key" in items[0] or "message" in items[0]):
            for m in _parse_evo_payload({"messages": items}):
                _save_msg(s, brand_id, m["jid"], m["text"], m["from_me"], m["ts"])
                count += 1
        else:
            # o devuelven objetos que contienen mensajes
            for obj in items:
                try:
                    for m in _parse_evo_payload(obj):
                        _save_msg(s, brand_id, m["jid"], m["text"], m["from_me"], m["ts"])
                        count += 1
                except Exception as e:
                    log.debug("skip item parse: %s", e)

    return {"ok": True, "saved": count, "source_status": res.get("http_status")}
