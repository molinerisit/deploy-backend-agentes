# backend/agents/reservas.py
import re
from typing import Optional
from datetime import datetime
from common.llm import generate

SYSTEM = (
    "Eres un agente de RESERVAS para un negocio local. "
    "Tu objetivo es: (1) entender la solicitud, (2) pedir datos faltantes, "
    "(3) proponer fecha/hora válidas y (4) confirmar próximos pasos. "
    "Responde SIEMPRE en español, breve, accionable y en Markdown."
)

def _try_extract_iso(text: str) -> Optional[str]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", text)
    if not m:
        return None
    raw = f"{m.group(1)} {m.group(2)}"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
        return dt.strftime("%Y-%m-%dT%H:%M:00Z")
    except Exception:
        return None

def run_reservas(user_text: str, *, context: str = "") -> str:
    prompt_parts = []
    if context:
        prompt_parts.append(f"Contexto del negocio:\n{context}\n")
    prompt_parts.append(
        "Solicitud del usuario (reservas):\n"
        f"{user_text}\n\n"
        "Devuelve un bloque Markdown con:\n"
        "1) Resumen breve de lo pedido.\n"
        "2) Datos faltantes (si aplica).\n"
        "3) Propuestas concretas de fecha/hora (2 opciones).\n"
        "4) Siguiente acción (qué debe responder el usuario).\n"
    )
    md = generate(SYSTEM, "\n".join(prompt_parts), temperature=0.2)
    iso = _try_extract_iso(user_text)
    if iso:
        md += f"\n\n> Nota: detecté fecha/hora en el mensaje → **{iso}** (UTC aprox)."
    return md
