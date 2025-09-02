# backend/agents/reservas.py
import re
from typing import Optional
from datetime import datetime
from common.llm import generate

SYSTEM = (
    "Eres un agente de RESERVAS para un negocio local. "
    "Objetivo: (1) entender solicitud, (2) pedir datos faltantes, "
    "(3) proponer fecha/hora válidas, (4) confirmar próximos pasos. "
    "Responde en español, breve y en Markdown."
)

def _try_extract_iso(text: str) -> Optional[str]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", text)
    if not m: return None
    raw = f"{m.group(1)} {m.group(2)}"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%dT%H:%M:00Z")
    except Exception:
        return None

def run_reservas(
    user_text: str,
    *,
    context: str = "",
    rag_context: str = "",
    model_name: Optional[str] = None,
    temperature: float = 0.2,
) -> str:
    ctx_parts = []
    if context: ctx_parts.append(context)
    if rag_context: ctx_parts.append("Disponibilidad/Reglas:\n" + rag_context)
    joined_ctx = "\n\n".join(ctx_parts).strip()

    prompt = []
    if joined_ctx:
        prompt.append(f"{joined_ctx}\n")
    prompt.append(
        "Solicitud del usuario (reservas):\n"
        f"{user_text}\n\n"
        "Devuelve Markdown con:\n"
        "1) Resumen breve.\n"
        "2) Datos faltantes (si aplica).\n"
        "3) 2 opciones de fecha/hora concretas.\n"
        "4) Siguiente acción.\n"
    )
    md = generate(SYSTEM, "\n".join(prompt), temperature=temperature, model=model_name)
    iso = _try_extract_iso(user_text)
    if iso:
        md += f"\n\n> Nota: detecté fecha/hora → **{iso}** (UTC aprox)."
    return md
