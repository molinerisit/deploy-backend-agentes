# backend/agents/mc.py
import os, json, re
from typing import Tuple, Optional
from db import get_session, select, WAConfig, BrandDataSource, Brand
from common.llm import generate
from common.pwhash import verify_password  # 👈

SUPER_PASS = os.getenv("WA_SUPERADMIN_PASSWORD", "")
SUPER_NUMS = [x.strip() for x in os.getenv("WA_SUPERADMIN_NUMBERS","").split(",") if x.strip()]
SUPER_KEY  = os.getenv("WA_SUPERADMIN_KEYWORD", "#admin")

SYSTEM = (
    "Eres 'MC', el coordinador maestro del sistema. Das respuestas claras y concisas en español y en Markdown."
)

def _is_allowed_number(sender: str, allow_json: Optional[str]) -> bool:
    num = sender.split("@")[0]
    allow = []
    if allow_json:
        try: allow = json.loads(allow_json)
        except: allow = []
    white = set([n.strip() for n in allow if n] + SUPER_NUMS)
    return (not white) or (num in white)

def _parse_kv(segment: str):
    if "=" in segment:
        k, v = segment.split("=", 1)
    elif ":" in segment:
        k, v = segment.split(":", 1)
    else:
        return segment.strip(), ""
    return k.strip(), v.strip()

def try_admin_command(brand_id: int, sender: str, text: str) -> Tuple[bool, str]:
    if not text:
        return False, ""
    with get_session() as session:
        cfg = session.exec(select(WAConfig).where(WAConfig.brand_id == brand_id)).first()
        brand = session.get(Brand, brand_id)
        if not (cfg and cfg.super_enabled):
            return False, ""
        keyword = (cfg.super_keyword or SUPER_KEY or "#admin").strip()
        if keyword not in text:
            return False, ""

        m = re.search(re.escape(keyword) + r"\s+(\S+)\s*(.*)$", text, flags=re.IGNORECASE|re.DOTALL)
        if not m:
            return True, "🔒 Modo admin: formato inválido. Usá: `#admin <password> <comandos>`"
        pwd = m.group(1)
        rest = (m.group(2) or "").strip()

        # --- verificación de password ---
        ok = False
        if cfg.super_password_hash:
            ok = verify_password(pwd, cfg.super_password_hash)
        elif SUPER_PASS:
            ok = (pwd == SUPER_PASS)
        if not ok:
            return True, "❌ Password incorrecto o no configurado."

        if not _is_allowed_number(sender, cfg.super_allow_list_json):
            return True, "❌ Este número no está habilitado para admin."

        cmds = [c.strip() for c in re.split(r"[;\n]+", rest) if c.strip()]
        changed, out = [], []

        for cmd in cmds:
            low = cmd.lower()

            if low in ("help", "ayuda", "?"):
                out.append(
                    "**Comandos:**\n"
                    "- `set agent=ventas|reservas|auto`\n"
                    "- `set model=<openai-model>`\n"
                    "- `set temp=<0..1>`\n"
                    "- `rulemd=<markdown...>`\n"
                    "- `rulejson=<json...>`\n"
                    "- `cfg show`\n"
                    "- `ds add name=<n> kind=postgres|http url=<...>`\n"
                    "- `ds del id=<id>`"
                )
                continue

            if low.startswith("set "):
                _, restkv = cmd.split(" ", 1)
                k, v = _parse_kv(restkv)
                if k == "agent" and v in ("ventas","reservas","auto"):
                    cfg.agent_mode = v; changed.append("agent_mode")
                elif k == "model":
                    cfg.model_name = (v or None); changed.append("model_name")
                elif k == "temp":
                    try:
                        t = float(v)
                        if 0 <= t <= 1:
                            cfg.temperature = t; changed.append("temperature")
                    except: pass
                else:
                    out.append(f"⚠️ set {k} no reconocido.")
                continue

            if low.startswith("rulemd"):
                k, v = _parse_kv(cmd)
                cfg.rules_md = v or ""
                changed.append("rules_md")
                continue

            if low.startswith("rulejson"):
                k, v = _parse_kv(cmd)
                try:
                    json.loads(v)
                    cfg.rules_json = v
                except:
                    cfg.rules_json = v
                changed.append("rules_json")
                continue

            if low == "cfg show":
                out.append("**Config actual:**\n" + json.dumps({
                    "agent_mode": cfg.agent_mode,
                    "model_name": cfg.model_name,
                    "temperature": cfg.temperature,
                    "super_enabled": cfg.super_enabled,
                    "super_keyword": cfg.super_keyword,
                }, ensure_ascii=False, indent=2))
                continue

            if low.startswith("ds add"):
                parts = [p.strip() for p in cmd.split(" ") if p.strip()][2:]
                kvs = dict(_parse_kv(p) for p in parts)
                name = kvs.get("name","").strip()
                kind = kvs.get("kind","postgres").strip()
                url  = kvs.get("url","").strip()
                if not name or not url or kind not in ("postgres","http"):
                    out.append("⚠️ ds add: faltan campos (name, kind, url)")
                else:
                    ds = BrandDataSource(brand_id=brand_id, name=name, kind=kind, url=url, enabled=True, read_only=True)
                    session.add(ds); session.commit()
                    out.append(f"✅ DS agregado: {ds.id} {name} ({kind})")
                continue

            if low.startswith("ds del"):
                parts = [p.strip() for p in cmd.split(" ") if p.strip()][2:]
                kvs = dict(_parse_kv(p) for p in parts)
                try:
                    did = int(kvs.get("id","0"))
                except:
                    did = 0
                if not did:
                    out.append("⚠️ ds del: `id` inválido")
                else:
                    from db import BrandDataSource
                    ds = session.get(BrandDataSource, did)
                    if ds and ds.brand_id == brand_id:
                        session.delete(ds); session.commit()
                        out.append(f"🗑️ DS eliminado: {did}")
                    else:
                        out.append("⚠️ DS no encontrado o de otra marca")
                continue

            out.append(f"🤷 Comando no entendido: `{cmd}` (usá `help`)")

        if changed:
            session.add(cfg); session.commit()

        summary = ""
        if changed:
            summary += "✅ Cambios: " + ", ".join(changed) + "\n"
        if out:
            summary += "\n".join(out)
        if not summary:
            summary = "✅ Admin OK (sin cambios). Usá `help` para ver comandos."
        return True, summary

def run_mc(user_text: str, *, context: str = "", model_name: Optional[str] = None, temperature: float = 0.2) -> str:
    prompt = []
    if context:
        prompt.append(f"Contexto del negocio (visible para todos los agentes):\n{context}\n")
    prompt.append("Mensaje del equipo / requerimiento:\n" + user_text)
    prompt.append("Devuelve un breve plan de acción en 3-5 bullets, y si aplica, checklist con tareas claras.")
    return generate(SYSTEM, "\n\n".join(prompt), temperature=temperature, model=model_name)
