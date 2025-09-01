from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from db import Brand, Session, select, get_session
from security import check_api_key

router = APIRouter(prefix="/api", tags=["brands"])

class BrandCreate(BaseModel):
    name: str = Field(..., min_length=1)
    tone: Optional[str] = None
    context: Optional[str] = None

class BrandOut(BaseModel):
    id: int; name: str; tone: Optional[str] = None; context: Optional[str] = None
    class Config: from_attributes = True

@router.get("/brands", response_model=List[BrandOut], dependencies=[Depends(check_api_key)])
def list_brands(session: Session = Depends(get_session)):
    return session.exec(select(Brand)).all()

@router.post("/brands", response_model=BrandOut, status_code=201, dependencies=[Depends(check_api_key)])
def create_brand(payload: BrandCreate, session: Session = Depends(get_session)):
    b = Brand(name=payload.name, tone=payload.tone, context=payload.context or "")
    session.add(b); session.commit(); session.refresh(b)
    return b
