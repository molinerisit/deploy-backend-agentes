# backend/routers/agent_reservas.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db import get_session, Brand, ConversationThread, ChatMessage, select, Session
from agents.reservas import run_reservas

router = APIRouter(prefix="/api/chat/agent", tags=["chat/agent"])

class AgentPayload(BaseModel):
    brand_id: int
    text: str

def _get_or_create_thread(session: Session, brand_id: int) -> ConversationThread:
    thread = session.exec(
        select(ConversationThread).where(ConversationThread.brand_id == brand_id)
    ).first()
    if not thread:
        thread = ConversationThread(brand_id=brand_id, topic="reservas")
        session.add(thread)
        session.commit()
        session.refresh(thread)
    return thread

@router.post("/reservas")
def agent_reservas(payload: AgentPayload, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand no encontrada")

    thread = _get_or_create_thread(session, payload.brand_id)

    # Guardar mensaje del usuario
    session.add(ChatMessage(thread_id=thread.id, sender="user", agent="reservas", text=payload.text))
    session.commit()

    try:
        summary_md = run_reservas(payload.text, context=brand.context or "")
        # Guardar respuesta del agente
        session.add(ChatMessage(thread_id=thread.id, sender="agent", agent="reservas", text=summary_md))
        session.commit()
        return {"ok": True, "summary_md": summary_md}
    except Exception as e:
        return {"ok": False, "summary_md": "", "error": str(e)}
