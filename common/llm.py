# backend/common/llm.py
import os, json, re
from typing import Any, Dict, Optional
from openai import OpenAI
import httpx

# Evita que openai pase proxies a httpx (tu versión de httpx no los soporta)
for _v in ("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"):
    os.environ.pop(_v, None)

DEFAULT_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()

_client_singleton: Optional[OpenAI] = None

def _client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY no configurado")
        # Cliente httpx sin proxies ni extras (firma mínima compatible)
        httpx_client = httpx.Client(timeout=60.0)
        _client_singleton = OpenAI(api_key=OPENAI_API_KEY, http_client=httpx_client)
    return _client_singleton

def generate(system: str, user: str, *, temperature: float = 0.2,
             model: Optional[str] = None, max_tokens: Optional[int] = None) -> str:
    """
    Devuelve texto (Markdown permitido).
    """
    m = model or DEFAULT_MODEL
    resp = _client().chat.completions.create(
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
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start:end+1]
    try:
        json.loads(candidate)
        return candidate
    except Exception:
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
    Pide SOLO JSON. Parsea y devuelve dict.
    """
    hint = ""
    if schema_hint:
        hint = "\n\nEsquema de ejemplo:\n" + json.dumps(schema_hint, ensure_ascii=False)
    user_msg = (
        "Devuelve SOLO un JSON válido (sin texto adicional, sin markdown, sin comentarios)."
        " Si no hay datos para alguna clave, usa null o cadena vacía.\n\n"
        + (user or "")
        + hint
    )
    raw = generate(system, user_msg, temperature=temperature, model=model, max_tokens=max_tokens)
    try:
        return json.loads(raw)
    except Exception:
        pass
    block = _extract_json_block(raw)
    if block:
        try:
            return json.loads(block)
        except Exception:
            pass
    if strict:
        raise ValueError("No se pudo parsear JSON del LLM")
    return {}
