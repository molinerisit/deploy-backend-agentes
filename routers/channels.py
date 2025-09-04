# routers/channels.py
import os, logging, io, base64, json, time
from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db import get_session, session_cm, Session, select, WAConfig, Brand, WAChatMeta, WAMessage

# EvolutionClient es opcional: si no tiene ciertos métodos, caemos a wrappers locales
try:
    from wa_evolution import EvolutionClient
except Exception:  # pragma: no cover
    EvolutionClient = None

import httpx

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = (os.getenv("EVOLUTION_BASE_URL", "")).rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = (os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"

# ---------------- helpers generales ----------------
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
        ok = str(s).lower() in ("open", "connected")
        return ok
    except Exception as e:
        log.warning("is_connected error: %s", e)
        return False

def _qr_data_url_from_code(code: str) -> str:
    try:
        import qrcode
        buf = io.BytesIO()
        qrcode.make(code).save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        log.warning("qr build failed: %s", e)
        return ""

# ---------------- wrappers HTTP a Evolution (tolerantes a versiones) ----------------
def _evo_headers() -> Dict[str, str]:
    # Evolution suele aceptar 'apikey' y algunos despliegues 'Authorization: Bearer ...'
    h = {"Accept": "application/json"}
    if EVOLUTION_API_KEY:
        h["apikey"] = EVOLUTION_API_KEY
        h["Authorization"] = f"Bearer {EVOLUTION_API_KEY}"
    return h

def _evo_get(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not EVOLUTION_BASE_URL:
        return {"http_status": 500, "body": {"error": "EVOLUTION_BASE_URL no configurado"}}
    url = f"{EVOLUTION_BASE_URL}{path}"
    try:
        with httpx.Client(timeout=15) as cli:
            r = cli.get(url, params=params, headers=_evo_headers())
        body = r.json() if r.headers.get("content-type","").startswith("application/json") else {"text": r.text}
        return {"http_status": r.status_code, "body": body}
    except Exception as e:
        return {"http_status": 599, "body": {"error": str(e)}}

def _evo_post(path: str, json_body: Dict[str, Any] | None = None, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not EVOLUTION_BASE_URL:
        return {"http_status": 500, "body": {"error": "EVOLUTION_BASE_URL no configurado"}}
    url = f"{EVOLUTION_BASE_URL}{path}"
    try:
        with httpx.Client(timeout=20) as cli:
            r = cli.post(url, json=json_body or {}, params=params, headers=_evo_headers())
        body = r.json() if r.headers.get("content-type","").startswith("application/json") else {"text": r.text}
        return {"http_status": r.status_code, "body": body}
    except Exception as e:
        return {"http_status": 599, "body": {"error": str(e)}}

def _evo_connection_state(instance: str) -> Dict[str, Any]:
    # probar varias rutas de Evolution
    for path in (f"/instance/connectionState/{instance}",
                 f"/instance/state/{instance}",
                 f"/instance/{instance}/connectionState"):
        res = _evo_get(path)
        if res["http_status"] != 404:
            return res
    return {"http_status": 404, "body": {"error": "connectionState not found"}}

def _evo_connect(instance: str) -> Dict[str, Any]:
    for path in (f"/instance/connect/{instance}",
                 f"/instance/{instance}/connect"):
        res = _evo_get(path)
        if res["http_status"] != 404:
            return res
    return {"http_status": 404, "body": {"error": "connect not found"}}

def _evo_try_qr(instance: str) -> Dict[str, Any]:
    # Devuelve dict con posibles claves: base64, qr, image, qrcode, dataUrl, code, pairingCode
    # Intentar varias rutas conocidas
    candidates = [
        f"/instance/qrCode/{instance}",
        f"/instance/{instance}/qrCode",
        f"/instance/qr/{instance}",
        f"/instance/{instance}/qr",
        f"/qr/base64/{instance}",
        f"/instance/{instance}/qr/base64",
    ]
    for path in candidates:
        res = _evo_get(path)
        if res["http_status"] != 404:
            return res
    return {"http_status": 404, "body": {"error": "qr endpoint not found"}}

def _evo_set_webhook(instance: str, webhook_url: str) -> Dict[str, Any]:
    # intentar variantes
    tries = [
        ("POST", f"/instance/webhook/{instance}", {"url": webhook_url}, None),
        ("GET",  "/webhook/set", None, {"instanceName": instance, "webhook": webhook_url}),
        ("GET",  f"/webhook", None, {"instanceName": instance, "webhook": webhook_url}),
        ("POST", f"/instance/{instance}/webhook", {"url": webhook_url}, None),
    ]
    for method, path, body, params in tries:
        res = _evo_post(path, body, params) if method == "POST" else _evo_get(path, params)
        if res["http_status"] not in (404, 405):
            return res
    return {"http_status": 404, "body": {"error": "webhook endpoint not found"}}

def _evo_create(instance: str, integration: str | None = "WHATSAPP", webhook_url: str | None = None) -> Dict[str, Any]:
    bodies = [
        {"instanceName": instance, "integration": integration, "webhook": webhook_url} if webhook_url else {"instanceName": instance, "integration": integration},
        {"name": instance, "integration": integration, "webhook": webhook_url} if webhook_url else {"name": instance, "integration": integration},
    ]
    for b in bodies:
        for path in ("/instance/create", "/instance/add", "/instance/init"):
            res = _evo_post(path, b)
            if res["http_status"] not in (404, 405):
                return res
    return {"http_status": 404, "body": {"error": "create endpoint not found"}}

def _evo_ensure_started(instance: str, webhook_url: str) -> Dict[str, Any]:
    detail: Dict[str, Any] = {}
    # 1) create si hace falta
    cr = _evo_create(instance, "WHATSAPP", webhook_url)
    detail["create"] = cr
    # 2) webhook
    wh = _evo_set_webhook(instance, webhook_url)
    detail["webhook"] = wh
    # 3) connect
    co = _evo_connect(instance)
    detail["connect"] = co
    ok = any((
        (cr.get("http_status") or 500) < 400,
        (co.get("http_status") or 500) < 400
    ))
    return {"ok": ok, "detail": detail}

# ---------------- /config para el front ----------------
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
    return out

# ---------------- Start / Connect ----------------
def _start_instance(instance: str, webhook_url: str) -> Dict[str, Any]:
    # Si existe EvolutionClient y tiene ensure_started, usarlo
    if EvolutionClient and hasattr(EvolutionClient, "ensure_started"):
        try:
            evo = EvolutionClient()
            detail = evo.ensure_started(instance, webhook_url=webhook_url)
            return {"ok": (detail or {}).get("http_status", 500) < 400, "detail": detail}
        except Exception as e:
            log.warning("EvolutionClient.ensure_started fallo: %s", e)
    # Fallback: wrappers HTTP
    return _evo_ensure_started(instance, webhook_url)

@router.api_route("/start", methods=["GET", "POST", "OPTIONS"])
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        return {"ok": False, "error": "EVOLUTION_BASE_URL no configurado"}
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "PUBLIC_BASE_URL no configurado"}

    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    out = _start_instance(instance, webhook_url)
    out.update({"instance": instance, "webhook_url": webhook_url})
    return out

@router.get("/ping")
def wa_ping(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    # EvolutionClient con connection_state si existe
    if EvolutionClient and hasattr(EvolutionClient, "connection_state"):
        try:
            evo = EvolutionClient()
            st = evo.connection_state(instance)
            return {"ok": True, "instance": instance, "state": st}
        except Exception as e:
            return {"ok": False, "instance": instance, "error": str(e)}
    # Fallback wrappers
    st = _evo_connection_state(instance)
    return {"ok": (st.get("http_status") or 500) < 400, "instance": instance, "state": st}

# ---------------- QR / Pairing ----------------
@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"

    # 1) estado
    if EvolutionClient and hasattr(EvolutionClient, "connection_state"):
        try:
            evo = EvolutionClient()
            st = evo.connection_state(instance)
        except Exception as e:
            log.warning("connection_state error via client: %s", e)
            st = {"http_status": 599, "body": {"error": str(e)}}
    else:
        st = _evo_connection_state(instance)

    connected = _is_connected(st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        # 2) conectar
        if EvolutionClient and hasattr(EvolutionClient, "connect_instance"):
            try:
                evo = EvolutionClient()
                raw_dump = evo.connect_instance(instance) or {}
            except Exception as e:
                log.warning("connect_instance error via client: %s", e)
                raw_dump = {}
        else:
            raw_dump = _evo_connect(instance)

        body = raw_dump.get("body", {}) if isinstance(raw_dump, dict) else {}
        pairing = (body.get("pairingCode") or body.get("pairing_code") or
                   body.get("pin") or body.get("code_short"))
        code_txt = body.get("code") or body.get("qrcode") or body.get("qrCode")
        if code_txt:
            qr_data_url = _qr_data_url_from_code(code_txt)

        # 3) pedir QR explícito si aún no lo tenemos
        if not qr_data_url:
            if EvolutionClient and hasattr(EvolutionClient, "qr_by_param"):
                try:
                    evo = EvolutionClient()
                    _, qj = evo.qr_by_param(instance)
                except Exception as e:
                    log.warning("qr_by_param error via client: %s", e)
                    qj = None
            else:
                qres = _evo_try_qr(instance)
                qj = qres.get("body") if isinstance(qres, dict) else None

            raw_dump = qj or raw_dump
            if isinstance(qj, dict):
                # admitir múltiples claves posibles
                for k in ("base64", "qr", "image", "qrcode", "dataUrl", "dataURL"):
                    v = qj.get(k)
                    if isinstance(v, str) and v.startswith("data:image"):
                        qr_data_url = v
                        break

    out = {
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    }
    return JSONResponse(out)

# ---- Estado compat
@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    if EvolutionClient and hasattr(EvolutionClient, "connection_state"):
        evo = EvolutionClient()
        st = evo.connection_state(instance)
    else:
        st = _evo_connection_state(instance)
    return {"ok": True, "instance": instance, "state": st}

# ---------------- Test envío (sin 500 en error Evolution) ----------------
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
        return {"ok": False, "error": "Se requieren brand_id y to", "status": 422}

    # enviar
    send_res = None
    if EvolutionClient and hasattr(EvolutionClient, "send_text"):
        try:
            evo = EvolutionClient()
            send_res = evo.send_text(f"brand_{brand_id}", to, text)
        except Exception as e:
            log.warning("send_text via client error: %s", e)
            send_res = {"http_status": 599, "body": {"error": str(e)}}
    else:
        send_res = _evo_post(f"/message/sendText/brand_{brand_id}", {"number": to, "text": text})

    if (send_res.get("http_status") or 500) >= 400:
        return {"ok": False, "status": send_res.get("http_status"), "error": send_res.get("body")}

    # persistir saliente
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
            setattr(msg, "instance", f"brand_{brand_id}")
            setattr(msg, "raw_json", json.dumps({"source": "wa_test"}, ensure_ascii=False))
            s.add(msg)
            s.commit()
    except Exception as e:
        log.warning("no se pudo guardar mensaje saliente wa_test: %s", e)

    return {"ok": True, "result": send_res.get("body")}

# ---------------- Board & Meta ----------------
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
    return {"ok": True, "updated": updated, "column": column}

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

# ---- Set webhook tolerante
@router.api_route("/set_webhook", methods=["GET", "POST", "OPTIONS"])
def wa_set_webhook(brand_id: int = Query(...)):
    instance = f"brand_{brand_id}"
    if not PUBLIC_BASE_URL:
        return {"ok": False, "error": "PUBLIC_BASE_URL no configurado", "instance": instance}
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    # EvolutionClient si tiene set_webhook, si no wrappers
    if EvolutionClient and hasattr(EvolutionClient, "set_webhook"):
        try:
            evo = EvolutionClient()
            sc, js = evo.set_webhook(instance, webhook_url)
        except Exception as e:
            log.warning("set_webhook via client fallo: %s", e)
            sc, js = 599, {"error": str(e)}
    else:
        res = _evo_set_webhook(instance, webhook_url)
        sc, js = res.get("http_status"), res.get("body")

    if not (200 <= (sc or 500) < 400):
        # intentar asegurar start
        ensure = _evo_ensure_started(instance, webhook_url)
        sc = (ensure.get("detail", {}).get("connect", {}) or {}).get("http_status", sc)
        js = ensure

    return {"ok": 200 <= (sc or 500) < 400, "status": sc, "body": js, "webhook_url": webhook_url}

# ---- Board (desde DB + metadatos)
@router.get("/board")
def wa_board(
    brand_id: int = Query(...),
    group: str = Query("column", pattern="^(column|priority|interest|tag)$"),
    limit: int = Query(500, ge=1, le=5000),
    show_archived: bool = Query(False),
    q: Optional[str] = Query(None),
    session: Session = Depends(get_session)
):
    # Estado conectado?
    if EvolutionClient and hasattr(EvolutionClient, "connection_state"):
        try:
            evo = EvolutionClient()
            st = evo.connection_state(f"brand_{brand_id}")
            connected = _is_connected(st)
        except Exception:
            connected = False
    else:
        st = _evo_connection_state(f"brand_{brand_id}")
        connected = _is_connected(st)

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
        def _prio_bucket(p: int) -> Tuple[str, str]:
            p = int(p or 0)
            if p >= 3: return ("p3", "Alta")
            if p == 2: return ("p2", "Media")
            if p == 1: return ("p1", "Baja")
            return ("p0", "Sin prioridad")
        for it in enriched:
            k, t = _prio_bucket(it["priority"])
            ensure_col(k, f"Prioridad {t}")
            columns[k]["chats"].append(it)
    elif group == "interest":
        def _interest_bucket(i: int) -> Tuple[str, str]:
            i = int(i or 0)
            if i >= 3: return ("hot", "Hot")
            if i == 2: return ("warm", "Warm")
            if i == 1: return ("cold", "Cold")
            return ("unknown", "Sin interés")
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
# a ver ahora