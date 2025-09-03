from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from db import get_session, Session, Brand, WAConfig, select
from security import check_api_key

router = APIRouter(prefix="/api", tags=["context"])

class ContextIn(BaseModel):
    tone: Optional[str] = None
    context: Optional[str] = None

@router.get("/context", dependencies=[Depends(check_api_key)])
def get_context(brand_id: int, session: Session = Depends(get_session)):
    b = session.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand no encontrada")
    wac = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    return {
        "brand": {"id": b.id, "name": b.name, "tone": b.tone, "context": b.context},
        "wa_config": wac
    }

@router.put("/context", dependencies=[Depends(check_api_key)])
def update_context(brand_id: int, payload: ContextIn, session: Session = Depends(get_session)):
    b = session.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand no encontrada")
    if payload.tone is not None:
        b.tone = payload.tone
    if payload.context is not None:
        b.context = payload.context
    session.add(b); session.commit(); session.refresh(b)

    wac = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
    if not wac:
        wac = WAConfig(brand_id=brand_id)
        session.add(wac); session.commit(); session.refresh(wac)

    return {"ok": True, "brand": {"id": b.id, "name": b.name, "tone": b.tone, "context": b.context}, "wa_config": wac}
