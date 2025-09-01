from __future__ import annotations
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, conint
from common.llm import generate_json

class LeadQualification(BaseModel):
    interested: bool = Field(default=False)
    intent_strength: conint(ge=0, le=100) = 0
    name: Optional[str] = None
    budget: Optional[str] = None
    service: Optional[str] = None
    deadline: Optional[str] = None
    notes: Optional[str] = None

def lead_qualify(text: str) -> LeadQualification:
    system = (
        "Clasifica el mensaje de un potencial cliente. "
        "Devuelve JSON: {interested: true|false, intent_strength: 0-100, name, budget, service, deadline, notes}. "
        "Si algún campo no existe, usa null."
    )
    data = generate_json(system, text)
    return LeadQualification.model_validate(data)

class NBAAction(BaseModel):
    channel: str
    message_template: str

class ObjectionAnswer(BaseModel):
    objection: str
    answer: str

class SalesBrief(BaseModel):
    profile: Dict[str, Any] = Field(default_factory=dict)
    next_best_actions: List[NBAAction] = Field(default_factory=list)
    objections_and_answers: List[ObjectionAnswer] = Field(default_factory=list)

def sales_brief(lead_summary: str, brand_context: Optional[str] = None) -> SalesBrief:
    system = (
        "Eres asesor de ventas. A partir de la info del lead y el contexto de marca, "
        "devuelve JSON: {profile:{pain_points:[],goals:[],budget_hint,style}, "
        "next_best_actions:[{channel, message_template}], objections_and_answers:[{objection, answer}]}. "
        "Usa null si falta algo. No agregues texto fuera de JSON."
    )
    prompt = f"LEAD: {lead_summary}\n\nBRAND_CTX: {brand_context or ''}"
    data = generate_json(system, prompt)
    return SalesBrief.model_validate(data)
