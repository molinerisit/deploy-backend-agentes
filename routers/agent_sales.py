# backend/routers/agent_sales.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db import get_session, Brand, ConversationThread, ChatMessage, Lead, select, Session
from agents.sales import run_sales, extract_contact
import json

router = APIRouter(prefix="/api/chat/agent", tags=["chat/agent"])

class AgentPayload(BaseModel):
    brand_id: int
    text: str

def _get_or_create_thread(session: Session, brand_id: int) -> ConversationThread:
    thread = session.exec(
        select(ConversationThread).where(ConversationThread.brand_id == brand_id)
    ).first()
    if not thread:
        thread = ConversationThread(brand_id=brand_id, topic="ventas")
        session.add(thread)
        session.commit()
        session.refresh(thread)
    return thread

@router.post("/ventas")
def agent_ventas(payload: AgentPayload, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand no encontrada")

    thread = _get_or_create_thread(session, payload.brand_id)

    # Guardar mensaje del usuario
    session.add(ChatMessage(thread_id=thread.id, sender="user", agent="ventas", text=payload.text))
    session.commit()

    # Crear/actualizar Lead sencillo (opcional)
    contact = extract_contact(payload.text)
    profile = {k: v for k, v in contact.items() if v}
    lead = Lead(
        brand_id=payload.brand_id,
        name=None,
        channel="inbox",
        status="new",
        notes=payload.text[:500],
        profile_json=json.dumps(profile) if profile else None,
    )
    session.add(lead)
    session.commit()
    session.refresh(lead)

    try:
        summary_md = run_sales(payload.text, context=brand.context or "")
        # Guardar respuesta del agente
        session.add(ChatMessage(thread_id=thread.id, sender="agent", agent="ventas", text=summary_md))
        session.commit()
        return {"ok": True, "summary_md": summary_md, "lead_id": lead.id}
    except Exception as e:
        return {"ok": False, "summary_md": "", "error": str(e)}
