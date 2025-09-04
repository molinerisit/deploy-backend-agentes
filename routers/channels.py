# --- backend/routers/channels.py ---
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
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN") or "evolution"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

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
    out = {
        "brand": {"id": brand.id if brand else brand_id, "name": (brand.name if brand else f"brand_{brand_id}")},
        "config": cfg,
        "datasources": [],
        "webhook_example": f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance=brand_{brand_id}",
        "instance_name": f"brand_{brand_id}",
    }
    return out

# ---------------- Conexión / QR ----------------
def _ensure_started(instance: str, webhook_url: str) -> Dict[str, Any]:
    evo = EvolutionClient()
    created = evo.ensure_started(instance, webhook_url=webhook_url)
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
        try:
            raw_dump = evo.connect_instance(instance) or {}
        except Exception:
            raw_dump = {}

        body = raw_dump.get("body", {}) if isinstance(raw_dump, dict) else {}
        pairing = (body.get("pairingCode") or body.get("pairing_code") or
                   body.get("pin") or body.get("code_short"))
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

    out = {
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    }
    return JSONResponse(out)

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

    # Persistimos salida para UI
    try:
        with get_session() as s:
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
    # Orden tolerante a ts None
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
        evo = EvolutionClient()
        st = evo.connection_state(f"brand_{brand_id}")
        connected = True if str(st).lower() else True
    except Exception:
        connected = False

    # Board desde nuestra DB
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
            k = f"p{it['priority']}"
            ensure_col(k, f"Prioridad {it['priority']}")
            columns[k]["chats"].append(it)
    elif group == "interest":
        for it in enriched:
            ensure_col("interest", "Interés")
            columns["interest"]["chats"].append(it)
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

    out = {"ok": True, "connected": connected, "group": group,
           "columns": [{"key": k, "title": v["title"], "count": len(v["chats"]), "chats": v["chats"]} for k, v in columns.items()]}
    return out
