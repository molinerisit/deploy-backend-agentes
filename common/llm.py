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
