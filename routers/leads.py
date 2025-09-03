from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List
from db import Session, get_session, Lead, Brand, select
from agents.lead_sales import lead_qualify, sales_brief
from security import check_api_key
import json

router = APIRouter(prefix="/api", tags=["leads"])

class LeadIngest(BaseModel):
    brand_id: int
    raw_text: str
    channel: Optional[str] = "web"

@router.post("/leads/ingest", dependencies=[Depends(check_api_key)])
def leads_ingest(payload: LeadIngest, session: Session = Depends(get_session)):
    if not session.get(Brand, payload.brand_id):
        raise HTTPException(404, "Brand no encontrada")
    q = lead_qualify(payload.raw_text)
    brief = sales_brief(payload.raw_text, brand_context=session.get(Brand, payload.brand_id).context or "")
    l = Lead(
        brand_id=payload.brand_id,
        name=q.name,
        channel=payload.channel,
        status="qualified" if (q.interested is True) else ("disqualified" if (q.interested is False) else "new"),
        score=int(q.intent_strength or 0),
        notes=q.notes,
        profile_json=json.dumps({"qualification": q.model_dump(), "sales_brief": brief.model_dump()}, ensure_ascii=False)
    )
    session.add(l); session.commit(); session.refresh(l)
    return l

@router.get("/leads", dependencies=[Depends(check_api_key)])
def list_leads(
    brand_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=5, le=100),
    session: Session = Depends(get_session)
):
    rows = session.exec(select(Lead).where(Lead.brand_id == brand_id)).all()
    total = len(rows)
    start = (page-1)*page_size; end = start + page_size
    return {"items": rows[start:end], "page": page, "page_size": page_size, "total": total, "has_next": end < total}
