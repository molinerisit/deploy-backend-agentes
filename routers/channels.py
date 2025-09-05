import os, logging, io, base64, json, time
from typing import Optional, Dict, Any, List, Tuple

import httpx
from fastapi import APIRouter, HTTPException, Query, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import get_session, session_cm, Session, select, WAConfig, Brand, WAChatMeta, WAMessage
from wa_evolution import EvolutionClient  # opcional (compat)

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"

# -------------------------------------------------------------------
# HTTP helpers crudos contra Evolution 2.3.0 (evitan métodos ausentes)
# -------------------------------------------------------------------

def _evo_headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if EVOLUTION_API_KEY:
        # Evolution 2.3.0 suele aceptar Authorization Bearer; algunas builds X-API-KEY/apikey
        h["Authorization"] = f"Bearer {EVOLUTION_API_KEY}"
        h["apikey"] = EVOLUTION_API_KEY
        h["X-API-KEY"] = EVOLUTION_API_KEY
    return h

def _evo_get(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    if not EVOLUTION_BASE_URL:
        return 500, {"error": "EVOLUTION_BASE_URL not set"}
    url = f"{EVOLUTION_BASE_URL}{path}"
    try:
        r = httpx.get(url, params=params, headers=_evo_headers(), timeout=20.0)
        log.info("HTTP GET %s -> %s", r.request.url, r.status_code)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as e:
        log.warning("HTTP GET %s error: %s", url, e)
        return 500, {"error": str(e)}

def _evo_post(path: str, body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    if not EVOLUTION_BASE_URL:
        return 500, {"error": "EVOLUTION_BASE_URL not set"}
    url = f"{EVOLUTION_BASE_URL}{path}"
    try:
        r = httpx.post(url, params=params, json=body or {}, headers=_evo_headers(), timeout=20.0)
        log.info("HTTP POST %s -> %s", r.request.url, r.status_code)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text}
    except Exception as e:
        log.warning("HTTP POST %s error: %s", url, e)
        return 500, {"error": str(e)}

# ---------------- Utilidades de números / JID ----------------

def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _canonical_msisdn_ar(d: str) -> str:
    """
    Canonicaliza números de Argentina:
    - '54911xxxx...' -> '5411xxxx...'  (quita el '9' móvil)
    - deja todo lo demás igual
    """
    d = _digits_only(d)
    if d.startswith("549") and len(d) >= 5:
        return "54" + d[3:]
    return d

def _canonical_jid(jid_or_num: str) -> str:
    """
    Devuelve jid canónico en @s.whatsapp.net (AR: saca '9' luego de 54).
    """
    if "@s.whatsapp.net" in (jid_or_num or ""):
        num = jid_or_num.split("@", 1)[0]
    else:
        num = _digits_only(jid_or_num)
    num = _canonical_msisdn_ar(num)
    return f"{num}@s.whatsapp.net"

def _normalize_jid(j: str) -> str:
    """
    Legacy helper (mantengo por compat). Preferir _canonical_jid.
    """
    j = (j or "").strip()
    if not j:
        return ""
    if "@s.whatsapp.net" in j:
        return j
    digits = _digits_only(j)
    if not digits:
        return j
    return f"{digits}@s.whatsapp.net"

def _number_from_jid(jid: str) -> str:
    return (jid or "").split("@", 1)[0]

def _is_connected_state_payload(js: Dict[str, Any]) -> bool:
    """
    Chequea distintos formatos:
    - { instance: { state: 'open' } }
    - { body: { instance: { state: 'open' } } }
    - { state: 'open' }
    """
    try:
        b = js.get("body", js) or {}
        s = (
            (b.get("instance") or {}).get("state")
            or b.get("state")
            or js.get("state")
            or ""
        )
        return str(s).lower() in ("open", "connected")
    except Exception:
        return False

def _qr_data_url_from_text(text: str) -> str:
    if not text:
        return ""
    if isinstance(text, str) and text.startswith("data:image"):
        return text
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(text).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.warning("qr render failed: %s", e)
        return ""

def _brand_id_from_instance(s: Optional[str]) -> Optional[int]:
    """
    Acepta 'brand_1', 'brand-1', '1' y devuelve 1.
    """
    if not s:
        return None
    s = str(s)
    if s.isdigit():
        return int(s)
    for sep in ("_", "-"):
        if sep in s:
            try:
                return int(s.split(sep, 1)[1])
            except Exception:
                pass
    return None

def _extract_text(msg: Dict[str, Any]) -> str:
    """
    Extrae texto desde diferentes formatos de Baileys/Evolution.
    """
    if not isinstance(msg, dict):
        return ""
    body = (msg.get("message") or msg.get("body") or msg) or {}

    t = body.get("conversation")
    if isinstance(t, str) and t:
        return t
    ext = body.get("extendedTextMessage") or {}
    t = ext.get("text")
    if isinstance(t, str) and t:
        return t
    t = body.get("caption")
    if isinstance(t, str) and t:
        return t
    t = msg.get("text") or msg.get("body")
    return t if isinstance(t, str) else ""

# ---------------- /config para el front ----------------

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

# ---------------- Conexión / Start ----------------

def _ensure_started(instance: str, webhook_url: str) -> Dict[str, Any]:
    detail: Dict[str, Any] = {}

    # 1) create/add/init (no todas existen en 2.3.0)
    for path in ("/instance/create", "/instance/add", "/instance/init", f"/instance/create/{instance}"):
        sc, js = _evo_post(path, body={"instanceName": instance, "integration": "WHATSAPP", "webhook": webhook_url})
        detail["create"] = {"http_status": sc, "body": js}
        # 200-299 ok; 400/403/409 suele ser "ya existe": continuamos
        if 200 <= sc < 300 or sc in (400, 403, 409):
            break

    # 2) set webhook (variantes 2.3.0)
    wh_done = None
    for p in ("/instance/setWebhook", "/webhook/set", "/webhook"):
        sc_g, js_g = _evo_get(p, params={"instanceName": instance, "webhook": webhook_url})
        if 200 <= sc_g < 300:
            wh_done = (p, "GET", sc_g, js_g); break
        sc_p, js_p = _evo_post(p, body={"instanceName": instance, "webhook": webhook_url})
        if 200 <= sc_p < 300:
            wh_done = (p, "POST", sc_p, js_p); break
    detail["webhook"] = {
        "http_status": (wh_done[2] if wh_done else 404),
        "body": (wh_done[3] if wh_done else {"error": "webhook endpoint not found"})
    }

    # 3) connect (sí existe)
    sc_c, js_c = _evo_get(f"/instance/connect/{instance}")
    detail["connect"] = {"http_status": sc_c, "body": js_c}

    return {"ok": True, "detail": detail}

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    try:
        detail = _ensure_started(instance, webhook_url)
    except Exception as e:
        log.warning("ensure_started fallo: %s", e)
        raise HTTPException(404, "No se pudo iniciar/conectar la instancia")

    return {"ok": True, "instance": instance, "ts": int(time.time()), **detail}

# ---------------- QR / Estado ----------------

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"

    # 1) estado
    sc_s, js_s = _evo_get(f"/instance/connectionState/{instance}")
    connected = _is_connected_state_payload(js_s)

    qr_data_url: Optional[str] = ""
    pairing: Optional[str] = ""
    raw_dump: Dict[str, Any] = {"state": js_s}

    if not connected:
        # 2) intentar conectar (devuelve a veces pairingCode o code/base64)
        sc_c, js_c = _evo_get(f"/instance/connect/{instance}")
        raw_dump["connect"] = {"http_status": sc_c, "body": js_c}

        body_c = js_c.get("body", js_c) if isinstance(js_c, dict) else {}
        pairing = (
            body_c.get("pairingCode")
            or body_c.get("pairing_code")
            or body_c.get("pin")
            or body_c.get("code_short")
            or ""
        )

        code_txt = (
            body_c.get("base64")
            or body_c.get("qr")
            or body_c.get("qrcode")
            or body_c.get("qrCode")
            or body_c.get("dataUrl")
            or body_c.get("code")
            or ""
        )
        if code_txt:
            qr_data_url = _qr_data_url_from_text(code_txt) or qr_data_url

        # 3) si todavía no tenemos QR, probamos endpoints de QR típicos
        if not qr_data_url:
            sc_q1, js_q1 = _evo_get(f"/instance/qr/{instance}")
            raw_dump["qr_try1"] = {"http_status": sc_q1, "body": js_q1}
            b1 = js_q1.get("body", js_q1)
            if isinstance(b1, dict):
                cand = b1.get("base64") or b1.get("qr") or b1.get("dataUrl")
                if cand:
                    qr_data_url = _qr_data_url_from_text(cand)

        if not qr_data_url:
            sc_q2, js_q2 = _evo_get("/instance/qr", params={"instanceName": instance})
            raw_dump["qr_try2"] = {"http_status": sc_q2, "body": js_q2}
            b2 = js_q2.get("body", js_q2)
            if isinstance(b2, dict):
                cand = b2.get("base64") or b2.get("qr") or b2.get("dataUrl")
                if cand:
                    qr_data_url = _qr_data_url_from_text(cand)

    out = {
        "connected": connected,
        "qr": qr_data_url or "",
        "pairingCode": pairing or "",
        "raw": raw_dump,  # útil para debug en el <details> del front
    }
    # nunca explotar: devolvemos 200 con payload consistente
    return JSONResponse(out)

# ---- Estado simple (para UI)
@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    sc, js = _evo_get(f"/instance/connectionState/{instance}")
    return {"ok": (200 <= sc < 400), "instance": instance, "state": js}

# ---------------- Test envío ----------------

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
        to = _digits_only(to_raw)
    # canonizar AR (549 -> 54)
    to = _canonical_msisdn_ar(to)

    text = str(pick("text", "message", "body", default="Hola desde API"))

    if not brand_id or not to:
        raise HTTPException(422, "Se requieren brand_id y to")

    instance = f"brand_{brand_id}"
    sc, js = _evo_post(f"/message/sendText/{instance}", body={"number": to, "text": text})
    if sc >= 400:
        raise HTTPException(sc, str(js))

    # persistimos saliente para UI
    try:
        with session_cm() as s:
            jid = _canonical_jid(to)
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

    return {"ok": True, "result": js}

# ---------------- Metadatos / Board / Mensajes (DB) ----------------

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
    jid = _canonical_jid(payload.jid)
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
        jid = _canonical_jid(raw)
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
def wa_messages(
    brand_id: int = Query(...),
    jid: str = Query(...),
    limit: int = Query(60, ge=1, le=300),
    session: Session = Depends(get_session)
):
    jid = _canonical_jid(jid)
    if not jid:
        return {"ok": True, "messages": []}
    q = select(WAMessage).where(WAMessage.brand_id == brand_id, WAMessage.jid == jid)
    rows = session.exec(q).all()
    out = []
    for r in sorted(rows, key=lambda x: (getattr(x, "ts", None) or 0), reverse=True)[:limit]:
        from_me = bool(getattr(r, "from_me", False))
        text = getattr(r, "text", "") or ""
        out.append({
            "key": {"remoteJid": jid, "fromMe": from_me},
            "message": {"conversation": text}
        })
    out = list(reversed(out))
    return {"ok": True, "messages": out}

# ---------------- Webhook (incluye /webhook y /webhook/{event}) ----------------

@router.api_route("/webhook", methods=["POST", "GET"])
@router.api_route("/webhook/{event}", methods=["POST", "GET"])
async def wa_webhook(
    request: Request,
    event: Optional[str] = None,
    token: str = Query(""),
    instance: Optional[str] = Query(None),
    brand_id_qs: Optional[int] = Query(None, alias="brand_id"),
    session: Session = Depends(get_session),
):
    # 1) auth
    if token != (EVOLUTION_WEBHOOK_TOKEN or "evolution"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid token")

    # 2) ping
    if request.method == "GET":
        return {"ok": True, "ping": "ok", "instance": instance, "event": event}

    # 3) log liviano
    try:
        body_bytes = await request.body()
        log.info("[WEBHOOK] %s %s | len=%s | qs=%s",
                 request.method, str(request.url), len(body_bytes or b""),
                 dict(request.query_params))
    except Exception:
        pass

    # 4) payload
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    # Evolution puede mandar:
    #  A) {event:"MESSAGES_UPSERT", instanceName:"...", data:{ messages:[...], ...}}
    #  B) arreglo de objetos como A)
    #  C) objeto “crudo” estilo Baileys con key/message/...
    raw_events = payload if isinstance(payload, list) else [payload]

    def iter_messages(ev: dict):
        # caso A/B
        if isinstance(ev, dict) and "event" in ev and "data" in ev:
            d = ev.get("data") or {}
            msgs = d.get("messages") or d.get("data") or d.get("message") or []
            if isinstance(msgs, list):
                for m in msgs:
                    yield m
            elif isinstance(msgs, dict):
                yield msgs
            else:
                yield d
            return
        # caso C (crudo)
        yield ev

    # 6) deducir brand_id
    brand_id = brand_id_qs or _brand_id_from_instance(instance)
    if brand_id is None:
        try:
            maybe_inst = (
                payload.get("instanceName")
                or payload.get("instance")
                or (payload.get("body") or {}).get("instanceName")
            )
            brand_id = _brand_id_from_instance(maybe_inst)
        except Exception:
            brand_id = None
    _brand_id_final = brand_id if brand_id is not None else 0

    saved = 0
    with session_cm() as s:
        for ev in raw_events:
            for msg in iter_messages(ev):
                try:
                    key = msg.get("key") or {}
                    from_me = bool(key.get("fromMe"))
                    remote_jid = (
                        key.get("remoteJid")
                        or msg.get("remoteJid")
                        or msg.get("jid")
                        or ""
                    )

                    text = _extract_text(msg)

                    if not remote_jid:
                        num = _digits_only(str(msg.get("number") or ""))
                        if num:
                            remote_jid = f"{num}@s.whatsapp.net"
                    if not remote_jid:
                        continue

                    jid_norm = _canonical_jid(remote_jid)

                    ts = (
                        msg.get("messageTimestamp")
                        or msg.get("timestamp")
                        or int(time.time())
                    )

                    # guardamos solo entrantes
                    if from_me is False and text:
                        m = WAMessage(
                            brand_id=_brand_id_final,
                            jid=jid_norm,
                            from_me=False,
                            text=text,
                            ts=int(ts),
                        )
                        setattr(m, "instance", instance or f"brand_{_brand_id_final}" if _brand_id_final else None)
                        setattr(m, "raw_json", json.dumps(msg, ensure_ascii=False))
                        s.add(m)
                        saved += 1

                except Exception as e:
                    log.warning("webhook save error: %s | msg=%s", e, msg)

        s.commit()

    return {"ok": True, "saved": saved, "events": len(raw_events), "instance": instance, "event": event}

# ---------------- Forzar set_webhook (opcional, útil para debug) ----------------

@router.api_route("/set_webhook", methods=["GET", "POST", "OPTIONS"])
def wa_set_webhook(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    detail: Dict[str, Any] = {}
    for p in ("/webhook", "/webhook/set", "/instance/setWebhook"):
        sc_g, js_g = _evo_get(p, params={"instanceName": instance, "webhook": webhook_url})
        detail[f"{p}:GET"] = {"http_status": sc_g, "body": js_g}
        if 200 <= sc_g < 300:
            break
        sc_p, js_p = _evo_post(p, body={"instanceName": instance, "webhook": webhook_url})
        detail[f"{p}:POST"] = {"http_status": sc_p, "body": js_p}
        if 200 <= sc_p < 300:
            break

    sc_c, js_c = _evo_get(f"/instance/connect/{instance}")
    detail["connect"] = {"http_status": sc_c, "body": js_c}

    ok = any(200 <= (blk.get("http_status", 0)) < 300 for blk in detail.values())
    return {"ok": ok, "detail": detail, "webhook_url": webhook_url}

# ---------------- Board ----------------

@router.get("/board")
def wa_board(
    brand_id: int = Query(...),
    group: str = Query("column"),  # "column" | "priority" | "interest" | "tag"
    limit: int = Query(500, ge=1, le=5000),
    show_archived: bool = Query(False),
    q: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    if group not in ("column", "priority", "interest", "tag"):
        group = "column"

    sc, js = _evo_get(f"/instance/connectionState/brand_{brand_id}")
    connected = _is_connected_state_payload(js)

    rows = session.exec(select(WAMessage).where(WAMessage.brand_id == brand_id)).all()
    last_by_jid: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        jid = _canonical_jid(r.jid)
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
    meta_map: Dict[str, WAChatMeta] = { _canonical_jid(m.jid): m for m in metas }

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
            k = {3:"p3",2:"p2",1:"p1"}.get(int(it["priority"] or 0), "p0")
            title = {"p3":"Prioridad Alta","p2":"Prioridad Media","p1":"Prioridad Baja","p0":"Sin prioridad"}[k]
            ensure_col(k, title)
            columns[k]["chats"].append(it)
    elif group == "interest":
        kmap = {3:("hot","Interés Hot"),2:("warm","Interés Warm"),1:("cold","Interés Cold"),0:("unknown","Sin interés")}
        for it in enriched:
            k, title = kmap.get(int(it["interest"] or 0), ("unknown","Sin interés"))
            ensure_col(k, title)
            columns[k]["chats"].append(it)
    else:  # tag
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
