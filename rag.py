# backend/rag.py
import json, re, httpx
from typing import List, Tuple
from sqlalchemy import create_engine, text, inspect

# ---- Helpers ----
def _sanitize_terms(q: str) -> List[str]:
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]{3,}", q)
    return list({w.lower() for w in words})[:6]

def _pg_text_columns(url: str) -> List[Tuple[str, List[str]]]:
    engine = create_engine(url, future=True)
    insp = inspect(engine)
    out = []
    with engine.connect() as conn:
        for table in insp.get_table_names(schema=None):
            cols = [c["name"] for c in insp.get_columns(table) if str(c["type"]).startswith(("VARCHAR", "TEXT", "String"))]
            if cols:
                out.append((table, cols))
    return out

def _pg_keyword_search(url: str, terms: List[str], per_table: int = 3, tables_limit: int = 5) -> List[str]:
    """
    Busca términos en columnas de texto usando to_tsvector sin índices (ok para pocas filas).
    """
    if not terms: return []
    engine = create_engine(url, future=True)
    snippets = []
    with engine.connect() as conn:
        meta = _pg_text_columns(url)[:tables_limit]
        q = " & ".join(terms[:4])  # tsquery sencillo
        for table, cols in meta:
            concat = " || ' ' || ".join([f"COALESCE({c}::text,'')" for c in cols])
            sql = text(f"""
                SELECT {', '.join(cols[:3])}
                FROM {table}
                WHERE to_tsvector('simple', {concat}) @@ plainto_tsquery('simple', :q)
                LIMIT :lim
            """)
            try:
                rows = conn.execute(sql, {"q": q, "lim": per_table}).mappings().all()
                for r in rows:
                    obj = {k: (str(v)[:280] if v is not None else "") for k, v in dict(r).items()}
                    snippets.append(f"[{table}] " + json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass
            if len(snippets) >= per_table * tables_limit:
                break
    return snippets[: per_table * tables_limit]

def _http_fetch(url: str, headers_json: str | None) -> str:
    headers = {}
    if headers_json:
        try: headers = json.loads(headers_json)
        except: pass
    with httpx.Client(timeout=10) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        # cortamos por tamaño
        txt = r.text
        return txt[:2000]

def build_context_from_datasources(datasources: list, user_query: str, max_snippets: int = 12) -> str:
    """
    Recorre datasources habilitados y arma contexto para el prompt (RAG liviano).
    """
    if not datasources:
        return ""
    terms = _sanitize_terms(user_query)
    pieces = []
    for ds in datasources:
        if not ds.enabled:
            continue
        if ds.kind == "postgres":
            try:
                snips = _pg_keyword_search(ds.url, terms, per_table=3, tables_limit=4)
                if snips:
                    pieces.append(f"Fuente: {ds.name} (Postgres)\n" + "\n".join("- " + s for s in snips[:max_snippets]))
            except Exception as e:
                pieces.append(f"Fuente: {ds.name} (Postgres) -> error de lectura: {e}")
        elif ds.kind == "http":
            try:
                body = _http_fetch(ds.url, ds.headers_json)
                pieces.append(f"Fuente: {ds.name} (HTTP)\n" + body)
            except Exception as e:
                pieces.append(f"Fuente: {ds.name} (HTTP) -> error: {e}")
        if len(pieces) >= 4:
            break
    return "\n\n".join(pieces)
