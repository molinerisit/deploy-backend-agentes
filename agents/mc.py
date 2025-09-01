from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

# imports locales SIN el prefijo "backend."
from agents.stubs import cm_plan, copy_variants, designer_prompts
from common.llm import generate_json  # usado en mc_extract_context


def _truncate(s: str, max_chars: int = 1200) -> str:
    s = (s or "").strip()
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


def run_mc(input_text: str, context: Optional[str] = None) -> str:
    """
    Orquesta CM, Copy y Diseño y devuelve un resumen markdown.
    Corre en paralelo para bajar latencia.
    """
    base = f"{(context or '').strip()}\n\nUsuario: {input_text}".strip()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_cm = ex.submit(cm_plan, base)
        f_cp = ex.submit(copy_variants, base)
        f_ds = ex.submit(designer_prompts, base)

        p = f_cm.result()
        c = f_cp.result()
        d = f_ds.result()

    summary = (
        "### Resumen MC\n"
        "1) Plan CM (7 días):\n" + _truncate(p) + "\n\n"
        "2) Variantes de copy A/B/C:\n" + _truncate(c) + "\n\n"
        "3) Prompts creativos:\n" + _truncate(d)
    )
    return summary


def mc_extract_context(user_text: str) -> str:
    """
    Extrae posibles datos de campaña/marca para actualizar contexto compartido.
    Devuelve JSON (como string) para que puedas persistir fácilmente.
    """
    system = (
        "Extrae contexto de marketing. Devuelve JSON con "
        "{brand_name, campaign_name, objective, key_points:[], platforms:[]}. "
        "Usa null si no hay info. No agregues texto fuera del JSON."
    )
    data = generate_json(system, user_text)
    return json_dumps_compact(data)


def json_dumps_compact(data) -> str:
    import json
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
