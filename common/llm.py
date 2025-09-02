# backend/common/llm.py
import os, json, re, logging
from typing import Any, Dict, Optional
from openai import OpenAI

log = logging.getLogger("llm")

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

_client_singleton: Optional[OpenAI] = None
def _client() -> OpenAI:
    global _client_singleton
    if _client_singleton is None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY no configurada")
        _client_singleton = OpenAI(api_key=OPENAI_API_KEY)
    return _client_singleton

def generate(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None
) -> str:
    """Devuelve texto plano (Markdown)."""
    resp = _client().chat.completions.create(
        model=model or DEFAULT_MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system or ""},
            {"role": "user", "content": user or ""},
        ],
    )
    return (resp.choices[0].message.content or "").strip()

def _strip_code_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    if s.lower().startswith("json"):
        s = s[4:].lstrip(":").strip()
    return s

def _extract_json_candidate(s: str) -> str:
    s = _strip_code_fences(s)
    try:
        json.loads(s); return s
    except Exception:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1 and j > i:
        cand = s[i:j+1]
        return _strip_code_fences(cand)
    return s

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
    Pide al modelo devolver SOLO JSON y lo parsea.
    - schema_hint: dict opcional con claves esperadas (solo guía al modelo).
    - strict=True: si no se puede parsear, levanta excepción con el raw.
    """
    hint = ""
    if schema_hint:
        hint = "\n\nEsquema (claves sugeridas): " + json.dumps(schema_hint, ensure_ascii=False)

    base_user = (
        "Responde SOLO con un objeto JSON válido. Sin explicaciones ni markdown. "
        "Si falta algún valor usa null o \"\".\n\n"
        + (user or "")
        + hint
    )

    # Intento 1: JSON mode nativo (si la lib/modelo lo soporta)
    raw: str
    try:
        resp = _client().chat.completions.create(
            model=model or DEFAULT_MODEL,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system or ""},
                {"role": "user", "content": base_user},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        # Fallback a texto plano
        log.debug("JSON mode no disponible o falló (%s); usando fallback plano.", e)
        raw = generate(system, base_user, temperature=temperature, model=model, max_tokens=max_tokens)

    cand = _extract_json_candidate(raw)
    try:
        return json.loads(cand)
    except Exception as e:
        if strict:
            raise ValueError(f"No se pudo parsear JSON del LLM: {e}. Raw: {raw!r}") from e
        log.warning("generate_json: parse falló; devolviendo __raw__.")
        return {"__raw__": raw}

__all__ = ["generate", "generate_json"]
