import os, logging, io, base64, json
from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import qrcode
from datetime import datetime

from wa_evolution import EvolutionClient
from db import get_session, Session, select, WAChatMeta

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

# --------- Evolución: helpers para extraer lista cruda ---------
def _extract_chats_payload(js: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normaliza la lista de chats que devuelve Evolution (puede variar).
    Retorna elementos con al menos: { jid, name?, unread?, lastMessageText?, lastMessageAt? }
    """
    if not isinstance(js, dict):
        return []
    data = []
    # a veces viene en "data" o "chats" o raíz
    candidates = []
    for k in ("data", "chats", "items", "list", "results"):
        v = js.get(k)
        if isinstance(v, list):
            candidates = v
            break
    if not candidates and isinstance(js.get("0"), dict):
        # respuesta tipo { "0": {...}, "1": {...} }
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

        # last message may vary
        last_txt = ""
        last_at = None
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

# ----------------- rutas básicas (ya las tenías) -----------------

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    ci = evo.create_instance(instance)
    if ci.get("status") == 401:
        raise HTTPException(502, "Evolution 401 (verifica EVOLUTION_API_KEY)")

    try:
        evo.connect_instance(instance)  # pairing/qr/reconnect
    except Exception as e:
        log.warning("Evolution connect_instance(%s) error: %s", instance, e)

    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    wsc, wjs = evo.set_webhook(instance, webhook_url)
    if (wsc or 0) >= 400:
        log.warning("No pude setear webhook %s -> %s %s", webhook_url, wsc, wjs)

    return {"ok": True, "webhook": webhook_url}

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
            cj = evo.connect(instance)
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

@router.get("/chats")
def wa_chats(brand_id: int = Query(...), limit: int = Query(200, ge=1, le=2000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    st = evo.connection_state(instance)
    if not _is_connected(st):
        return {"ok": True, "connected": False, "chats": []}
    status, js = evo.list_chats(instance, limit=limit)
    return {"ok": status < 400, "status": status, "chats": js}

@router.get("/messages")
def wa_messages(brand_id: int = Query(...), jid: str = Query(...), limit: int = Query(50, ge=1, le=1000)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    jid = _normalize_jid(jid)
    st = evo.connection_state(instance)
    if not _is_connected(st):
        return {"ok": True, "connected": False, "messages": []}
    status, js = evo.get_chat_messages(instance, jid=jid, limit=limit)
    return {"ok": status < 400, "status": status, "messages": js}

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

    js = evo.send_text(instance, to, text)
    if (js.get("status") or 500) >= 400:
        raise HTTPException(js.get("status") or 500, js.get("error") or js.get("message") or "sendText error")
    return {"ok": True, "result": js}

# ----------------- NUEVO: META + BOARD -----------------

class ChatMetaIn(BaseModel):
    brand_id: int
    jid: str
    title: Optional[str] = None
    color: Optional[str] = None
    column: Optional[str] = None
    priority: Optional[int] = None   # 0..3
    interest: Optional[int] = None   # 0..3
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    tags: Optional[List[str]] = None
    notes: Optional[str] = None

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

    meta.updated_at = datetime.utcnow().isoformat()  # (si querés, actualizá a ahora)
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

# ---- tablero ----
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
    q: Optional[str] = Query(None),  # búsqueda por nombre/número/tag
    session: Session = Depends(get_session)
):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    st = evo.connection_state(instance)
    connected = _is_connected(st)
    status, js = evo.list_chats(instance, limit=limit)
    chats_raw = _extract_chats_payload(js if status < 400 else {})

    # metas por brand
    metas = session.exec(select(WAChatMeta).where(WAChatMeta.brand_id == brand_id)).all()
    meta_map: Dict[str, WAChatMeta] = {m.jid: m for m in metas}

    def _match_search(item: Dict[str, Any], meta: Optional[WAChatMeta]) -> bool:
        if not q:
            return True
        term = q.lower().strip()
        fields = [
            item.get("name") or "",
            item.get("number") or "",
            (meta.title if meta else "") or "",
        ]
        if meta:
            try:
                tags = json.loads(meta.tags_json or "[]")
                fields += tags
            except Exception:
                pass
        txt = " ".join(str(x) for x in fields).lower()
        return term in txt

    # fusion chats + meta
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
            # meta:
            "column": (m.column if m else "inbox"),
            "priority": (m.priority if m else 0),
            "interest": (m.interest if m else 0),
            "color": (m.color if m else None),
            "pinned": (m.pinned if m else False),
            "archived": (m.archived if m else False),
            "tags": (json.loads(m.tags_json or "[]") if m and m.tags_json else []),
            "notes": (m.notes if m else None),
        })

    # ordenar: primero pinned, luego unread desc, luego último mensaje
    enriched.sort(key=lambda x: (
        not x["pinned"],
        -(x.get("unread") or 0),
        -(int(x.get("lastMessageAt") or 0))
    ))

    # armar columnas
    columns: Dict[str, Dict[str, Any]] = {}

    def ensure_col(key: str, title: str, color: Optional[str] = None):
        if key not in columns:
            columns[key] = {"key": key, "title": title, "color": color, "chats": []}

    if group == "column":
        for it in enriched:
            key = it["column"] or "inbox"
            title = key.capitalize()
            ensure_col(key, title, it.get("color"))
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

    # serializar columnas en lista ordenada (key asc salvo inbox/pN/hot)
    ordered_keys = list(columns.keys())
    # pequeña preferencia
    ordered_keys.sort(key=lambda k: (0 if k in ("inbox","p3","hot") else 1, k))
    out_cols = []
    for k in ordered_keys:
        cols = columns[k]
        out_cols.append({
            "key": cols["key"],
            "title": cols["title"],
            "color": cols.get("color"),
            "count": len(cols["chats"]),
            "chats": cols["chats"],
        })

    return {
        "ok": True,
        "connected": connected,
        "group": group,
        "columns": out_cols
    }
