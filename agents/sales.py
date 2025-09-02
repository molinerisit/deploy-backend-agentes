# backend/agents/sales.py
import re
from typing import Dict, Optional
from common.llm import generate

SYSTEM = (
    "Eres un SDR/Agente de VENTAS para un negocio local. "
    "Tu tarea: (1) calificar el lead, (2) identificar necesidad/dolor, "
    "(3) proponer siguiente paso (demo, llamada, cotización) y (4) responder claro. "
    "Siempre en español, conciso y en Markdown."
)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+?\d{1,3}\s*)?(?:\(?\d{2,4}\)?[\s\-\.]?)?\d{3,4}[\s\-\.]?\d{3,4}")

def extract_contact(text: str) -> Dict[str, Optional[str]]:
    email = EMAIL_RE.search(text)
    phone = PHONE_RE.search(text)
    return {"email": email.group(0) if email else None, "phone": phone.group(0) if phone else None}

def run_sales(
    user_text: str,
    *,
    context: str = "",
    rag_context: str = "",
    model_name: Optional[str] = None,
    temperature: float = 0.3,
) -> str:
    contact = extract_contact(user_text)
    ctx_parts = []
    if context: ctx_parts.append(context)
    if rag_context: ctx_parts.append("Conocimiento:\n" + rag_context)
    joined_ctx = "\n\n".join(ctx_parts).strip()

    prompt = []
    if joined_ctx:
        prompt.append(f"{joined_ctx}\n")
    prompt.append(
        "Mensaje del lead (ventas):\n"
        f"{user_text}\n\n"
        "Devuelve un bloque Markdown con:\n"
        "1) Resumen del interés.\n"
        "2) Calificación (frío/templado/caliente) con motivo.\n"
        "3) Objeciones posibles.\n"
        "4) Próximo paso (concreta día/hora o acción).\n"
        "5) Respuesta sugerida (3-5 líneas).\n"
    )
    md = generate(SYSTEM, "\n".join(prompt), temperature=temperature, model=model_name)
    if any(contact.values()):
        md += "\n\n> Datos detectados: " + ", ".join(f"{k}: {v}" for k, v in contact.items() if v)
    return md
