# backend/routers/agent_mc.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db import Session, get_session, Brand, ConversationThread, ChatMessage, select
from security import check_api_key
from agents.mc import run_mc

router = APIRouter(prefix="/api/chat/agent", tags=["chat-agent"])

class McIn(BaseModel):
    brand_id: int
    text: str

@router.post("/mc", dependencies=[Depends(check_api_key)])
def agent_mc(payload: McIn, session: Session = Depends(get_session)):
    brand = session.get(Brand, payload.brand_id)
    if not brand:
        raise HTTPException(404, "Brand no encontrada")

    thread = session.exec(
        select(ConversationThread).where(ConversationThread.brand_id == payload.brand_id)
    ).first()
    if not thread:
        thread = ConversationThread(brand_id=payload.brand_id, topic="general")
        session.add(thread); session.commit(); session.refresh(thread)

    summary_md = run_mc(payload.text, context=brand.context or "", model_name=None, temperature=0.2)

    m = ChatMessage(thread_id=thread.id, sender="bot", agent="mc", text=summary_md)
    session.add(m); session.commit()

    msgs = session.exec(select(ChatMessage).where(ChatMessage.thread_id == thread.id)).all()
    return {"thread_id": thread.id, "context": brand.context or "", "messages": msgs}
