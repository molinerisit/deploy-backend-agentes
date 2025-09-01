# backend/common/llm.py
import os, json
from typing import Optional, Dict, Any
from tenacity import retry, stop_after_attempt, wait_exponential
from openai import OpenAI
import httpx

# Config
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip() or None
_OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
_DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

# Cliente OpenAI (SDK >= 1.x) usando nuestro httpx.Client SIN 'proxies'
def _client() -> OpenAI:
    if not _OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY no configurado")

    http_client = httpx.Client(timeout=60.0)  # clave: no pasamos 'proxies'

    if _OPENAI_BASE_URL:
        return OpenAI(api_key=_OPENAI_API_KEY, base_url=_OPENAI_BASE_URL, http_client=http_client)
    return OpenAI(api_key=_OPENAI_API_KEY, http_client=http_client)

@retry(reraise=True, stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
def _chat(system: str, prompt: str, *, model: Optional[str] = None, temperature: float = 0.2, json_mode: bool = False) -> str:
    client = _client()
    mdl = model or _DEFAULT_MODEL
    kwargs: Dict[str, Any] = {
        "model": mdl,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""

def generate(system: str, prompt: str, *, temperature: float = 0.2, model: Optional[str] = None) -> str:
    return _chat(system, prompt, model=model, temperature=temperature, json_mode=False)

def generate_json(system: str, prompt: str, *, temperature: float = 0.0, model: Optional[str] = None) -> Dict[str, Any]:
    txt = _chat(system, prompt, model=model, temperature=temperature, json_mode=True)
    try:
        return json.loads(txt)
    except Exception:
        return {}
