from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db import Brand, Session, get_session
from security import check_api_key

router = APIRouter(prefix="/api", tags=["context"])

class ContextSetIn(BaseModel):
    brand_id: int; context: str

@router.post("/context/set", dependencies=[Depends(check_api_key)])
def set_context(payload: ContextSetIn, session: Session = Depends(get_session)):
    b = session.get(Brand, payload.brand_id)
    if not b: raise HTTPException(404, "Brand no encontrada")
    b.context = payload.context; session.add(b); session.commit()
    return {"ok": True, "brand_id": b.id, "context": b.context}
