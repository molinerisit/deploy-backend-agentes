import os, logging, io, base64, json
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
        return str(s).lower() in ("open", "connected", "online")
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

def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _extract_chats_payload(js: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(js, dict):
        return []
    data = []
    candidates = []
    for k in ("data", "chats", "items", "list", "results", "response"):
        v = js.get(k)
        if isinstance(v, list):
            candidates = v
            break
        if isinstance(v, dict):
            vv = v.get("chats") or v.get("items") or v.get("results")
            if isinstance(vv, list):
                candidates = vv
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
        "instance_name": f"brand_{brand_id}",
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

    res = evo.ensure_started(instance, webhook_url)
    http_status = res.get("http_status") or 500
    if http_status >= 400:
        # devolvemos el detalle real para poder depurar desde el front
        detail = res.get("body") or {"error": "unknown"}
        log.warning("ensure_started fallo: %s", detail)
        raise HTTPException(http_status, detail)

    return {"ok": True, "webhook": webhook_url, "instance": instance, "detail": res.get("body")}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    # 1) Estado actual
    st = evo.connection_state(instance)
    connected = _is_connected(st.get("body") if "body" in st else st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    link_code: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        # 2) Pedimos QR por las rutas conocidas
        sc, qj = evo.qr_by_param(instance)
        raw_dump = qj if isinstance(qj, dict) else {"raw": qj}

        # Normalizamos posibles claves
        norm = EvolutionClient.extract_qr_fields(qj if isinstance(qj, dict) else {})
        qr_data_url = norm.get("qr_data_url") or None
        link_code   = norm.get("link_code") or None
        pairing     = norm.get("pairing_code") or None

        # 3) Si seguimos sin nada, intentar reconectar para forzar QR nuevo
        if not qr_data_url and not link_code and not pairing:
            conn = evo.connect_instance(instance)
            # reintenta leer QR tras conexión
            sc2, qj2 = evo.qr_by_param(instance)
            raw_dump = qj2 if isinstance(qj2, dict) else {"raw": qj2}
            norm2 = EvolutionClient.extract_qr_fields(qj2 if isinstance(qj2, dict) else {})
            qr_data_url = qr_data_url or norm2.get("qr_data_url")
            link_code   = link_code   or norm2.get("link_code")
            pairing     = pairing     or norm2.get("pairing_code")

    # 4) Si el "qr" que vino no es data-url pero sí un "code", generamos la imagen local
    if not qr_data_url and link_code:
        try:
            qr_data_url = _qr_data_url_from_code(link_code)
        except Exception as e:
            log.warning("QR local error: %s", e)

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "linkCode": link_code,
        "state": st,
        "raw": raw_dump,
    })

# ---- Estado y rotación ----
@router.get("/instance/status")
def wa_instance_status(brand_id: int = Query(...)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    caps = {"chat_list": False, "messages_list": False}
    try:
        sc, _ = evo.list_chats(instance, limit=1)
        caps["chat_list"] = sc < 400
    except Exception:
        pass
    try:
        sc2, _ = evo.get_chat_messages(instance, jid="000@s.whatsapp.net", limit=1)
        caps["messages_list"] = sc2 < 400
    except Exception:
        pass
    return {"ok": True, "instance": instance, "state": st, "server_caps": caps}

@router.post("/instance/rotate")
def wa_instance_rotate(brand_id: int = Query(...)):
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    try:
        evo.delete_instance(instance)
    except Exception as e:
        log.warning("rotate: delete_instance fallo (tolerado): %s", e)

    conn = evo.ensure_started(instance, webhook_url)
    if (conn.get("http_status") or 500) >= 400:
        log.warning("ensure_started fallo en rotate: %s", conn)
        raise HTTPException(conn.get("http_status") or 500, "No se pudo reiniciar/conectar la instancia")

    return {"ok": True, "instance": instance, "webhook": webhook_url}

# ---------------- Chats / Mensajes ----------------
@router.get("/chats")
def wa_chats(brand_id: int = Query(...), limit: int = Query(200, ge=1, le=2000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    connected = _is_connected(st.get("body") if "body" in st else st)
    if not connected:
        return {"ok": True, "connected": False, "chats": []}
    status, js = evo.list_chats(instance, limit=limit)
    return {"ok": status < 400, "status": status, "raw": js, "chats": _extract_chats_payload(js if status < 400 else {})}

@router.get("/messages")
def wa_messages(brand_id: int = Query(...), jid: str = Query(...), limit: int = Query(50, ge=1, le=1000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    jid = _normalize_jid(jid)
    st = evo.connection_state(instance)
    connected = _is_connected(st.get("body") if "body" in st else st)
    if not connected:
        return {"ok": True, "connected": False, "messages": []}
    status, js = evo.get_chat_messages(instance, jid=jid, limit=limit)
    return {"ok": status < 400, "status": status, "messages": js}

# ---------------- Board (agrupado) + Meta ----------------
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
    connected = _is_connected(st.get("body") if "body" in st else st)

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
