# --- backend/routers/wa_admin.py ---
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import Optional, List, Any, Dict, Tuple
import os, json, logging

from db import (
    Session, get_session, select,
    Brand, WAConfig, BrandDataSource,
    ConversationThread, ChatMessage, WAMessage
)
from rag import build_context_from_datasources
from agents.sales import run_sales
from agents.reservas import run_reservas
from agents.mc import try_admin_command
from wa_evolution import EvolutionClient
# NOTE: hash/verify importados si tenés endpoints de admin para setear password en el mismo archivo
# from common.pwhash import hash_password, verify_password  # <- usalos donde corresponda

log = logging.getLogger("wa_admin")
router = APIRouter(prefix="/api/wa", tags=["wa-admin"])

EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "")
ENV_SUPER_PASS = os.getenv("WA_SUPERADMIN_PASSWORD", "")

# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())

def _normalize_jid(j: str) -> str:
    j = (j or "").strip()
    if not j:
        return ""
    if "@s.whatsapp.net" in j:
        return j
    return f"{_digits_only(j)}@s.whatsapp.net" if _digits_only(j) else j

def _sanitize_wa_number(x: str) -> str:
    """
    Devuelve solo dígitos (para Evolution.send_text).
    """
    if not x:
        return ""
    if "@" in x:
        x = x.split("@", 1)[0]
    return _digits_only(x)

def _extract_text_from_message(msg: Dict[str, Any]) -> str:
    """
    Extrae texto/caption de distintas variantes de Baileys/Evolution.
    """
    # directos
    for k in ("text", "body", "messageText"):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # estructuras anidadas
    m = msg.get("message") or {}
    if isinstance(m, dict):
        # conversation
        if isinstance(m.get("conversation"), str) and m["conversation"].strip():
            return m["conversation"].strip()

        ext = m.get("extendedTextMessage") or {}
        if isinstance(ext, dict):
            t = ext.get("text")
            if isinstance(t, str) and t.strip():
                return t.strip()

        # captions
        for key in ("imageMessage", "videoMessage", "documentMessage"):
            blob = m.get(key) or {}
            if isinstance(blob, dict):
                cap = blob.get("caption")
                if isinstance(cap, str) and cap.strip():
                    return cap.strip()

    return ""

def _extract_sender_from_message(msg: Dict[str, Any]) -> str:
    """
    Obtiene el remitente desde variantes típicas (from, key.remoteJid, message.key.remoteJid).
    """
    raw = msg.get("from") or msg.get("sender") or ""
    if not raw:
        key = msg.get("key") or {}
        if isinstance(key, dict):
            raw = key.get("remoteJid") or ""
    if not raw:
        inner = msg.get("message") or {}
        if isinstance(inner, dict):
            inner_key = inner.get("key") or {}
            if isinstance(inner_key, dict):
                raw = inner_key.get("remoteJid") or ""
    return _normalize_jid(raw)

def _is_from_me(msg: Dict[str, Any]) -> bool:
    """
    Determina si el mensaje es 'fromMe' para evitar loops.
    """
    # variantes
    v = msg.get("fromMe")
    if isinstance(v, bool):
        return v
    key = msg.get("key") or {}
    if isinstance(key, dict) and isinstance(key.get("fromMe"), bool):
        return key["fromMe"]
    # algunos providers usan isMe, me, etc.
    for k in ("isMe", "me", "own"):
        if isinstance(msg.get(k), bool) and msg[k]:
            return True
    return False

def _pick_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Devuelve una lista de "mensajes" a procesar.
    Acepta payloads con: data/messages/entry/events/... y también arrays crudos.
    """
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []

    # Evolution a veces manda { messages: [...] }, otras { data: [...] }, otras { entry: [...] }
    for k in ("messages", "data", "entry", "events", "items", "list"):
        v = payload.get(k)
        if isinstance(v, list) and v:
            return [m for m in v if isinstance(m, dict)]

    # payload plano con un mensaje directo
    # o un objeto con "message" adentro
    one = payload.get("message") or payload
    return [one] if isinstance(one, dict) else []

def _brand_id_from_instance(instance: Optional[str], body: Dict[str, Any]) -> Optional[int]:
    inst = instance or body.get("instance") or body.get("instanceName") or body.get("session")
    if isinstance(inst, str) and inst.startswith("brand_"):
        try:
            return int(inst.split("_", 1)[1])
        except Exception:
            return None
    return None

def _ensure_thread(session: Session, brand_id: int, topic: str = "inbox") -> ConversationThread:
    th = session.exec(select(ConversationThread).where(ConversationThread.brand_id == brand_id)).first()
    if not th:
        th = ConversationThread(brand_id=brand_id, topic=topic)
        session.add(th); session.commit(); session.refresh(th)
    return th

def _save_wa_message(
    session: Session, *, brand_id: int, instance: str, jid: str,
    from_me: bool, text: Optional[str], ts: Optional[int], raw: Dict[str, Any]
) -> bool:
    """
    Guarda el mensaje en WAMessage con idempotencia básica.
    Retorna True si insertó, False si detectó duplicado y no insertó.
    """
    # idempotencia por (brand_id, jid, ts, text)
    if ts:
        existing = session.exec(
            select(WAMessage).where(
                WAMessage.brand_id == brand_id,
                WAMessage.jid == jid,
                WAMessage.ts == ts,
                WAMessage.text == (text or None),
                WAMessage.from_me == from_me,
            )
        ).first()
        if existing:
            return False

    wm = WAMessage(
        brand_id=brand_id,
        instance=instance,
        jid=jid,
        from_me=from_me,
        text=(text or None),
        ts=ts,
        raw_json=json.dumps(raw, ensure_ascii=False)
    )
    session.add(wm); session.commit()
    return True

def _clean_markdown_for_wa(s: str) -> str:
    """
    WhatsApp soporta algo de markdown, pero para evitar sorpresas
    podés simplificar si querés: por ahora devolvemos tal cual.
    """
    return (s or "").strip()

# --------------------------------------------------------------------
# Webhook principal
# --------------------------------------------------------------------
@router.post("/webhook")
async def webhook(req: Request, token: str = Query(""), instance: Optional[str] = Query(None)):
    # Seguridad: token por query o por header (si lo configuraste así al setear el webhook)
    hdr = req.headers.get("X-Webhook-Token") or req.headers.get("x-webhook-token") or ""
    if EVOLUTION_WEBHOOK_TOKEN:
        if token != EVOLUTION_WEBHOOK_TOKEN and hdr != EVOLUTION_WEBHOOK_TOKEN:
            raise HTTPException(401, "token inválido")

    try:
        body = await req.json()
    except Exception:
        body = {}

    brand_id = _brand_id_from_instance(instance, body)
    if not brand_id:
        log.warning("Webhook sin brand_id deducible: %s", body)
        return {"ok": True}

    # recolectar mensajes a procesar
    events = _pick_messages(body)
    if not events:
        # no romper reintentos: devolvemos 200
        return {"ok": True, "ignored": True}

    evo = EvolutionClient()
    instance_name = f"brand_{brand_id}"

    with get_session() as session:
        # Cargar config y contexto
        cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
        brand = session.get(Brand, brand_id)
        dss = session.exec(
            select(BrandDataSource).where(
                BrandDataSource.brand_id == brand_id,
                BrandDataSource.enabled == True
            )
        ).all()

        # Preparar contexto (rules/context)
        extra_ctx: List[str] = []
        if brand and (brand.context or ""):
            extra_ctx.append(f"Contexto de marca:\n{brand.context}\n")
        if cfg and cfg.rules_md:
            extra_ctx.append(f"Reglas de negocio (MD):\n{cfg.rules_md}\n")
        if cfg and cfg.rules_json:
            try:
                j = json.loads(cfg.rules_json)
                extra_ctx.append("Reglas (JSON):\n" + json.dumps(j, ensure_ascii=False, indent=2))
            except Exception:
                extra_ctx.append("Reglas (JSON - crudo):\n" + cfg.rules_json)
        context_str = "\n".join(extra_ctx)

        # Parámetros de LLM
        agent_mode = (cfg.agent_mode if cfg else "ventas").lower()
        model_name = (cfg.model_name if cfg and cfg.model_name else None)
        temperature = (cfg.temperature if cfg else 0.2)

        # Thread de conversación
        thread = _ensure_thread(session, brand_id, topic="inbox")

        results: List[Dict[str, Any]] = []

        for msg in events:
            try:
                # Evitar loops
                if _is_from_me(msg):
                    # opcional: persistir from_me como auditoría
                    _save_wa_message(
                        session, brand_id=brand_id, instance=instance_name,
                        jid=_normalize_jid(msg.get("from") or msg.get("sender") or ""),
                        from_me=True, text=_extract_text_from_message(msg),
                        ts=msg.get("timestamp") or msg.get("messageTimestamp") or msg.get("ts"),
                        raw=msg
                    )
                    results.append({"skipped": "from_me"})
                    continue

                # Extraer datos
                text = _extract_text_from_message(msg)
                sender_jid = _extract_sender_from_message(msg)
                sender_num = _sanitize_wa_number(sender_jid)
                ts = msg.get("timestamp") or msg.get("messageTimestamp") or msg.get("ts")

                if not sender_num:
                    results.append({"error": "no_sender"})
                    continue
                if not text:
                    # ignoramos mensajes sin texto (stickers/reacciones)
                    # igual podemos persistir crudo
                    _save_wa_message(
                        session, brand_id=brand_id, instance=instance_name,
                        jid=sender_jid, from_me=False, text=None, ts=ts, raw=msg
                    )
                    results.append({"ignored": "no_text"})
                    continue

                # Persistir entrante
                _save_wa_message(
                    session, brand_id=brand_id, instance=instance_name,
                    jid=sender_jid, from_me=False, text=text, ts=ts, raw=msg
                )

                # Primero: comandos admin (#admin ...)
                handled, admin_resp = try_admin_command(brand_id, sender_num, text)
                if handled:
                    reply = _clean_markdown_for_wa(admin_resp)
                    try:
                        evo.send_text(instance_name, sender_num, reply)
                    except Exception as e:
                        log.warning("No se pudo responder admin a %s: %s", sender_num, e)
                    # también guardamos en ChatMessage
                    session.add(ChatMessage(thread_id=thread.id, sender="bot", agent="admin", text=reply))
                    session.commit()
                    results.append({"admin": True})
                    continue

                # RAG por datasource (opcional)
                try:
                    rag_ctx = build_context_from_datasources(dss, text, max_snippets=12)
                except Exception as e:
                    rag_ctx = f"(RAG error: {e})"

                # Elección de agente
                chosen = agent_mode
                if agent_mode == "auto":
                    t = (text or "").lower()
                    if any(k in t for k in ["reserv", "turno", "hora", "agenda", "disponibilidad"]):
                        chosen = "reservas"
                    elif any(k in t for k in ["precio", "costo", "promo", "comprar", "venta", "stock", "cotiza"]):
                        chosen = "ventas"
                    else:
                        chosen = "ventas"

                # Guardar entrada de usuario en ChatMessage
                session.add(ChatMessage(thread_id=thread.id, sender=sender_num, agent="wa", text=text))
                session.commit()

                # Generar respuesta
                if chosen == "reservas":
                    md = run_reservas(text, context=context_str, rag_context=rag_ctx,
                                      model_name=model_name, temperature=temperature)
                else:
                    md = run_sales(text, context=context_str, rag_context=rag_ctx,
                                   model_name=model_name, temperature=temperature)

                reply = _clean_markdown_for_wa(md)

                # Enviar por WA
                try:
                    evo.send_text(instance_name, sender_num, reply)
                except Exception as e:
                    log.warning("No se pudo enviar respuesta a %s: %s", sender_num, e)
                    results.append({"sent": False, "error": str(e)})
                else:
                    # Persistir saliente
                    _save_wa_message(
                        session, brand_id=brand_id, instance=instance_name,
                        jid=sender_jid, from_me=True, text=reply, ts=None, raw={"out": True, "text": reply}
                    )
                    session.add(ChatMessage(thread_id=thread.id, sender="bot", agent=chosen, text=md))
                    session.commit()
                    results.append({"sent": True, "agent": chosen})

            except Exception as e:
                log.exception("Error procesando evento: %s", e)
                results.append({"error": str(e)})

    return {"ok": True, "processed": len(results), "results": results}
