from __future__ import annotations
from common.llm import generate

def cm_plan(brief: str) -> str:
    system = "Eres un CM senior. Devuelve calendario 7 días (red, formato, idea) + métrica."
    return generate(system, f"Brief:\n{brief}")

def copy_variants(context: str) -> str:
    system = "Copywriter performance. Devuelve A/B/C con hook + CTA + 5 hashtags."
    return generate(system, context)

def designer_prompts(context: str) -> str:
    system = "Director creativo: 3 prompts text-to-image + 1 safe-for-ads."
    return generate(system, context)
