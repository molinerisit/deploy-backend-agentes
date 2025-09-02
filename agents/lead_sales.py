# backend/agents/lead_sales.py
from typing import List, Optional
from pydantic import BaseModel, Field
from common.llm import generate_json

class LeadQualification(BaseModel):
    interested: Optional[bool] = None
    intent_strength: Optional[int] = Field(default=None, ge=0, le=100)
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None

class SalesBrief(BaseModel):
    summary: str = ""
    key_needs: List[str] = []
    objections: List[str] = []
    next_step: str = ""

SYSTEM_QLF = (
    "Eres un analista de leads. Devuelve sólo JSON con los campos: "
    "{interested: bool, intent_strength: 0..100, name, email, phone, notes}."
)
SYSTEM_BRIEF = (
    "Eres un SDR senior. Devuelve sólo JSON con: "
    "{summary: str, key_needs: [str], objections: [str], next_step: str}."
)

def lead_qualify(raw_text: str) -> LeadQualification:
    data = generate_json(
        SYSTEM_QLF,
        f"Califica este lead:\n{raw_text}",
        schema_hint={"interested": True, "intent_strength": 50, "name": "", "email": "", "phone": "", "notes": ""},
        strict=False,
        temperature=0.1,
    )
    try:
        return LeadQualification(**data)
    except Exception:
        return LeadQualification(notes=str(data))

def sales_brief(raw_text: str, brand_context: str = "") -> SalesBrief:
    user = (
        (f"Contexto del negocio:\n{brand_context}\n\n" if brand_context else "") +
        f"Mensaje del lead:\n{raw_text}"
    )
    data = generate_json(
        SYSTEM_BRIEF,
        user,
        schema_hint={"summary": "", "key_needs": [""], "objections": [""], "next_step": ""},
        strict=False,
        temperature=0.2,
    )
    try:
        return SalesBrief(**data)
    except Exception:
        return SalesBrief(summary=str(data))
