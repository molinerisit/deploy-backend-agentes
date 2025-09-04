from fastapi import APIRouter, Depends
from pydantic import BaseModel
from db import Session, get_session, ConversationThread, ChatMessage, Brand, select

router = APIRouter(prefix="/api", tags=["chat"])

class ChatIn(BaseModel):
    brand_id: int; agent: str; text: str

@router.get("/chat/thread")
def chat_thread(brand_id: int, session: Session = Depends(get_session)):
    thread = session.exec(select(ConversationThread).where(ConversationThread.brand_id == brand_id)).first()
    if not thread:
        thread = ConversationThread(brand_id=brand_id, topic="general")
        session.add(thread); session.commit(); session.refresh(thread)
    msgs = session.exec(select(ChatMessage).where(ChatMessage.thread_id==thread.id)).all()
    brand = session.get(Brand, brand_id)
    return {"thread_id": thread.id, "context": (brand.context if brand else ""), "messages": msgs}

@router.post("/chat")
def chat_post(payload: ChatIn, session: Session = Depends(get_session)):
    thread = session.exec(select(ConversationThread).where(ConversationThread.brand_id==payload.brand_id)).first()
    if not thread:
        thread = ConversationThread(brand_id=payload.brand_id, topic="general")
        session.add(thread); session.commit(); session.refresh(thread)
    m = ChatMessage(thread_id=thread.id, sender="user", agent=payload.agent, text=payload.text)
    session.add(m); session.commit()
    msgs = session.exec(select(ChatMessage).where(ChatMessage.thread_id==thread.id)).all()
    brand = session.get(Brand, payload.brand_id)
    return {"thread_id": thread.id, "context": (brand.context if brand else ""), "messages": msgs}
