import json
from typing import List
from sqlmodel import SQLModel
import httpx

def _safe_text_cut(s: str, max_chars: int = 1200) -> str:
    s = s or ""
    return s[:max_chars]

def build_context_from_datasources(dss: List[SQLModel], query: str, max_snippets: int = 12) -> str:
    snippets = []
    for ds in dss or []:
        try:
            if not ds.enabled:
                continue
            name = getattr(ds, "name", "ds")
            if ds.kind == "http":
                headers = {}
                if ds.headers_json:
                    try: headers = json.loads(ds.headers_json)
                    except: headers = {}
                with httpx.Client(timeout=12) as c:
                    r = c.get(ds.url, headers=headers)
                    txt = r.text
                snippets.append(f"[{name}] HTTP\n" + _safe_text_cut(txt))
            elif ds.kind == "postgres":
                sql = None
                if ds.headers_json:
                    try:
                        j = json.loads(ds.headers_json)
                        sql = (j.get("sql") or "").strip()
                    except:
                        pass
                if not sql or "select" not in sql.lower():
                    continue
                from sqlalchemy import create_engine, text as sqltext
                engine = create_engine(ds.url, future=True)
                with engine.connect() as conn:
                    res = conn.execute(sqltext(sql), {"q": query}).fetchmany(10)
                rows = [str(dict(r._mapping)) for r in res]
                if rows:
                    snippets.append(f"[{name}] SQL\n" + _safe_text_cut("\n".join(rows), 2000))
        except Exception as e:
            snippets.append(f"[{getattr(ds,'name','ds')}] (error: {e})")
        if len(snippets) >= max_snippets:
            break
    return "\n\n".join(snippets)
