from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from db import Session, get_session, Brand

router = APIRouter(prefix="/api/brands", tags=["brands"])

class BrandIn(BaseModel):
    name: str
    tone: Optional[str] = None
    context: Optional[str] = None

class BrandUpdate(BaseModel):
    name: Optional[str] = None
    tone: Optional[str] = None
    context: Optional[str] = None

@router.get("", response_model=List[Brand])
def list_brands(session: Session = Depends(get_session)):
    return session.exec(select(Brand)).all()

@router.post("", response_model=Brand)
def create_brand(payload: BrandIn, session: Session = Depends(get_session)):
    b = Brand(name=payload.name, tone=payload.tone, context=payload.context)
    session.add(b)
    session.commit()
    session.refresh(b)
    return b

@router.get("/{brand_id}", response_model=Brand)
def get_brand(brand_id: int, session: Session = Depends(get_session)):
    b = session.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand no encontrada")
    return b

@router.put("/{brand_id}", response_model=Brand)
def update_brand(brand_id: int, payload: BrandUpdate, session: Session = Depends(get_session)):
    b = session.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand no encontrada")
    if payload.name is not None:
        b.name = payload.name
    if payload.tone is not None:
        b.tone = payload.tone
    if payload.context is not None:
        b.context = payload.context
    session.add(b)
    session.commit()
    session.refresh(b)
    return b

@router.delete("/{brand_id}")
def delete_brand(brand_id: int, session: Session = Depends(get_session)):
    b = session.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand no encontrada")
    session.delete(b)
    session.commit()
    return {"ok": True}
