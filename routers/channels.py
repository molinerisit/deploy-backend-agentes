import os, logging, io, base64, json
from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import qrcode

from wa_evolution import EvolutionClient
from db import get_session, Session, select, WAConfig, Brand, WAChatMeta

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")
EVOLUTION_INTEGRATION = os.getenv("EVOLUTION_INTEGRATION", "WHATSAPP").strip()

def _qr_data_url_from_code(code: str) -> str:
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

def _is_connected(state_json: Dict[str, Any]) -> bool:
    try:
        s = (state_json or {}).get("instance", {}).get("state", "")
        if isinstance(s, str) and s.lower() in ("open", "connected"):
            return True
        st = (state_json or {}).get("status") or (state_json or {}).get("state")
        if isinstance(st, str) and st.lower() in ("open","connected","online"):
            return True
    except Exception:
        pass
    return False

def _normalize_jid(j: str) -> str:
    j = (j or "").strip()
    if not j:
        return ""
    if "@s.whatsapp.net" in j or "@g.us" in j:
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
    candidates = []
    for k in ("data", "chats", "items", "list", "results", "response"):
        v = js.get(k)
        if isinstance(v, list):
            candidates = v; break
        if isinstance(v, dict):
            vv = v.get("chats") or v.get("items") or v.get("list") or v.get("results")
            if isinstance(vv, list):
                candidates = vv; break

    out = []
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
        out.append({
            "jid": jid,
            "number": _number_from_jid(jid),
            "name": name,
            "unread": int(unread) if isinstance(unread, (int, float)) else 0,
            "lastMessageText": last_txt,
            "lastMessageAt": last_at
        })
    return out

@router.get("/config")
def wa_config(brand_id: int = Query(...), session: Session = Depends(get_session)):
    brand = session.get(Brand, brand_id)
    cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    has_pw = bool(getattr(cfg, "super_password_hash", None))
    instance = f"brand_{brand_id}"
    return {
        "brand": {"id": brand.id if brand else brand_id, "name": (brand.name if brand else f"brand_{brand_id}")},
        "config": cfg,
        "has_password": has_pw,
        "webhook_example": f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}",
        "instance_name": instance,
    }

# --------- START (idempotente) ----------
@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    # 1) Si ya existe, solo aseguramos webhook y connect
    try:
        if evo.instance_exists(instance):
            sc, _ = evo.set_webhook(instance, webhook_url)
            if sc >= 400:
                log.warning("start: set_webhook devolvio %s (tolerado)", sc)
            conn = evo.connect_instance(instance)
            # aunque falle connect, devolvemos 200 para no romper el flujo del front
            return {"ok": True, "instance": instance, "detail": {"webhook_status": sc, "connect": conn}}
    except Exception as e:
        log.warning("start: instance_exists error (continuo): %s", e)

    # 2) Crear + conectar (robusto a variantes)
    res = evo.ensure_started(instance, webhook_url, integration=EVOLUTION_INTEGRATION)
    http_status = res.get("http_status") or 500
    if http_status >= 400:
        # Último intento: intentar solo conectar
        last_conn = evo.connect_instance(instance)
        if (last_conn.get("http_status") or 500) < 400:
            return {"ok": True, "instance": instance, "detail": {"fallback_connect": last_conn}}
        detail = res.get("body") or {"error": "unknown"}
        log.warning("ensure_started fallo: %s", detail)
        raise HTTPException(500, {"error": "No se pudo iniciar/conectar la instancia", "detail": detail})

    return {"ok": True, "webhook": webhook_url, "instance": instance, "detail": res.get("body")}

# --------- QR ----------
@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    st = evo.connection_state(instance)
    st_body = st.get("body") if isinstance(st, dict) else st
    connected = _is_connected(st_body if isinstance(st_body, dict) else {})

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        sc, qj = evo.qr_by_param(instance)
        raw_dump = qj or {}
        if sc and sc < 400 and isinstance(qj, dict):
            from wa_evolution import EvolutionClient as _EC
            flds = _EC.extract_qr_fields(qj)
            qr_data_url = flds.get("qr_data_url")
            pairing = flds.get("pairing_code") or flds.get("link_code")

    if not connected and not (qr_data_url or pairing):
        conn = evo.connect_instance(instance)
        raw_dump = {"connect": conn, "qr": raw_dump}
        body = conn.get("body") if isinstance(conn, dict) else {}
        if isinstance(body, dict):
            from wa_evolution import EvolutionClient as _EC
            flds = _EC.extract_qr_fields(body)
            qr_data_url = flds.get("qr_data_url") or qr_data_url
            pairing = pairing or flds.get("pairing_code") or flds.get("link_code")
            code_txt = flds.get("link_code") or flds.get("pairing_code")
            if not qr_data_url and isinstance(code_txt, str) and code_txt.strip():
                try:
                    qr_data_url = _qr_data_url_from_code(code_txt.strip())
                except Exception as e:
                    log.warning("QR local error: %s", e)

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    })

# --------- Estado ----------
@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    return {"ok": True, "instance": instance, "state": st}

# --------- Envío de prueba ----------
class TestIn(BaseModel):
    brand_id: int
    to: str
    text: str

@router.post("/test")
def wa_test(body: TestIn):
    evo = EvolutionClient()
    instance = f"brand_{body.brand_id}"
    to = "".join(ch for ch in body.to if ch.isdigit())
    if not to:
        raise HTTPException(400, "Destino inválido")
    res = evo.send_text(instance, to, body.text or "")
    st = res.get("http_status") or 500
    if st >= 400:
        raise HTTPException(st, res.get("body") or {"error": "send_failed"})
    return {"ok": True, "response": res}

# --------- Chats / Mensajes ----------
@router.get("/chats")
def wa_chats(brand_id: int = Query(...), limit: int = Query(200, ge=1, le=2000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    st_body = st.get("body") if isinstance(st, dict) else st
    if not _is_connected(st_body if isinstance(st_body, dict) else {}):
        return {"ok": True, "connected": False, "chats": []}
    status, js = evo.list_chats(instance, limit=limit)
    return {"ok": status < 400, "status": status, "raw": js, "chats": _extract_chats_payload(js if status < 400 else {})}

@router.get("/messages")
def wa_messages(brand_id: int = Query(...), jid: str = Query(...), limit: int = Query(50, ge=1, le=1000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    jid = _normalize_jid(jid)
    st = evo.connection_state(instance)
    st_body = st.get("body") if isinstance(st, dict) else st
    if not _is_connected(st_body if isinstance(st_body, dict) else {}):
        return {"ok": True, "connected": False, "messages": []}
    status, js = evo.get_chat_messages(instance, jid=jid, limit=limit)
    return {"ok": status < 400, "status": status, "messages": js}

# --------- Board (kanban) ----------
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
    st_body = st.get("body") if isinstance(st, dict) else st
    connected = _is_connected(st_body if isinstance(st_body, dict) else {})

    status, js = evo.list_chats(instance, limit=limit)
    if status >= 400:
        log.warning("board: list_chats status=%s body=%s", status, js)
    chats_raw = _extract_chats_payload(js if status < 400 else {})

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

    return {"ok": True, "connected": connected, "group": group, "columns": out_cols}

# --------- Webhook (autoresponder mínimo) ----------
@router.post("/webhook")
def wa_webhook(req: Request, token: str = Query(""), instance: str = Query("")):
    if token != EVOLUTION_WEBHOOK_TOKEN:
        raise HTTPException(401, "invalid token")
    try:
        payload = json.loads((await req.body()).decode("utf-8") or "{}")
    except Exception:
        payload = {}
    # Detectar mensaje entrante sencillo
    try:
        body = payload.get("body") or payload.get("data") or payload
        messages = body.get("messages") if isinstance(body, dict) else None
        if isinstance(messages, list):
            for m in messages:
                from_me = bool(m.get("fromMe") or m.get("key", {}).get("fromMe"))
                if from_me:
                    continue
                jid = m.get("chatId") or m.get("remoteJid") or m.get("key", {}).get("remoteJid")
                text = (
                    m.get("text") or
                    (m.get("message", {}).get("conversation") if isinstance(m.get("message"), dict) else None) or
                    m.get("body")
                )
                if jid and text:
                    number = _number_from_jid(_normalize_jid(jid))
                    evo = EvolutionClient()
                    reply = f"🤖 Gracias! Recibí: “{text[:120]}”. Pronto te respondemos."
                    evo.send_text(instance or "brand_1", number, reply)
    except Exception as e:
        log.warning("webhook parse error: %s", e)
    return {"ok": True}
