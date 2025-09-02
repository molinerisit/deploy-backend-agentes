# backend/common/llm.py
import os, json, re
from typing import Any, Dict, Optional
from openai import OpenAI

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

_client: Optional[OpenAI] = None
def _client_singleton() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

def generate(system: str, user: str, *, temperature: float = 0.2, model: Optional[str] = None, max_tokens: Optional[int] = None) -> str:
    """
    Devuelve texto plano (Markdown).
    """
    client = _client_singleton()
    m = model or DEFAULT_MODEL
    resp = client.chat.completions.create(
        model=m,
        messages=[
            {"role": "system", "content": system or ""},
            {"role": "user", "content": user or ""},
        ],
        temperature=temperature,
        max_tokens=max_tokens
    )
    return (resp.choices[0].message.content or "").strip()

def _extract_json_block(text: str) -> Optional[str]:
    # intenta encontrar el primer bloque {...} balanceado
    # estrategia simple: buscar la primera "{" y la última "}"
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start:end+1]
    # intenta parsear directo
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        # fallback: eliminar code fences ```json ... ```
        candidate = re.sub(r"^```(json)?\s*|\s*```$", "", candidate.strip(), flags=re.IGNORECASE|re.MULTILINE)
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return None

def generate_json(
    system: str,
    user: str,
    *,
    schema_hint: Optional[Dict[str, Any]] = None,
    temperature: float = 0.2,
    model: Optional[str] = None,
    strict: bool = False,
    max_tokens: Optional[int] = None
) -> Dict[str, Any]:
    """
    Pide al modelo devolver SOLO JSON. Parsea y devuelve dict.
    - schema_hint: dict opcional con claves esperadas para guiar al modelo
    - strict=True: si no se puede parsear, levanta excepción
    """
    hint = ""
    if schema_hint:
        hint = (
            "\n\nEsquema de ejemplo (claves esperadas):\n"
            + json.dumps(schema_hint, ensure_ascii=False)
        )

    user_msg = (
        "Devuelve SOLO un JSON válido (sin texto adicional, sin markdown, sin comentarios)."
        " Si no hay datos para alguna clave, usa null o cadena vacía.\n\n"
        + (user or "")
        + hint
    )
    raw = generate(system, user_msg, temperature=temperature, model=model, max_tokens=max_tokens)
    # 1) intento directo
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 2) intento extraer bloque {...}
    block = _extract_json_block(raw)
    if block:
        try:
            return json.loads(block)
        except Exception:
            pass
    if strict:
        raise ValueError("No se pudo parsear JSON del LLM")
    return {}
