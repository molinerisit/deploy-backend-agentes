# backend/common/llm.py
import os
from typing import Optional
from openai import OpenAI

_client = None
def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

def generate(system: str, prompt: str, *, temperature: float = 0.2, model: Optional[str] = None, max_tokens: int = 700) -> str:
    """
    Genera texto con OpenAI Chat Completions (GPT). Usa model override si se pasa.
    """
    client = _get_client()
    m = (model or DEFAULT_MODEL).strip()
    resp = client.chat.completions.create(
        model=m,
        temperature=float(temperature),
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()
