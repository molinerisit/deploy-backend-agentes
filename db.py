# backend/db.py
import os, logging
from typing import Optional
from contextlib import contextmanager
from sqlmodel import SQLModel, Field, Session, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy import inspect, text as sqltext

log = logging.getLogger("db")

# ---------------------------
# Modelos base
# ---------------------------
class Brand(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    tone: Optional[str] = None
    context: Optional[str] = None

class Campaign(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    name: str

class ContentItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    campaign_id: Optional[int] = Field(default=None, index=True, foreign_key="campaign.id")
    platform: str
    title: str
    copy_text: Optional[str] = None
    asset_url: Optional[str] = None
    status: str = "draft"
    scheduled_iso: Optional[str] = None
    notes: Optional[str] = None

class Task(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    title: str
    assignee: Optional[str] = None
    status: str = "open"

class Customer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: Optional[str] = None
    phone: Optional[str] = None

class Reservation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    customer_id: Optional[int] = Field(default=None, foreign_key="customer.id")
    service: Optional[str] = None
    scheduled_iso: Optional[str] = None
    status: str = "booked"
    notes: Optional[str] = None

class Availability(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    weekday: int
    start: str
    end: str

class ChannelAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    channel: str  # 'wa' | 'fb' | 'ig'
    external_id: Optional[str] = None
    meta: Optional[str] = None

class ConversationThread(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    topic: str

class ChatMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: int = Field(index=True, foreign_key="conversationthread.id")
    sender: str           # 'user' | 'agent' | 'bot'
    agent: Optional[str]  # 'mc','copy','designer','reservas','sales','cm'
    text: str
    created_at: Optional[str] = None

class Lead(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    name: Optional[str] = None
    channel: Optional[str] = None
    status: str = "new"
    score: Optional[int] = None
    notes: Optional[str] = None
    profile_json: Optional[str] = None

# ---- Config WA + datasources para RAG ----
class WAConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")

    agent_mode: str = "ventas"          # ventas | reservas | auto
    model_name: Optional[str] = None
    temperature: float = 0.2

    rules_md: Optional[str] = None      # markdown libre
    rules_json: Optional[str] = None    # JSON serializado (texto)

    super_enabled: bool = True
    super_keyword: Optional[str] = "#admin"
    super_allow_list_json: Optional[str] = None  # JSON array con números autorizados
    super_password_hash: Optional[str] = None    # hash del password de superadmin

class BrandDataSource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    name: str
    kind: str              # 'postgres' | 'http'
    url: str               # conn string o endpoint
    headers_json: Optional[str] = None  # para HTTP
    enabled: bool = True
    read_only: bool = True

# ---- NUEVO: metadatos por chat para el tablero (prioridad/columna/tags) ----
class WAChatMeta(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    jid: str = Field(index=True)        # 549xxx@s.whatsapp.net

    title: Optional[str] = None         # nombre manual
    color: Optional[str] = None         # color columna (hex o tailwind-key)
    column: str = "inbox"               # inbox / hot / seguimiento / etc.
    priority: int = 0                   # 0..3
    interest: int = 0                   # 0..3
    pinned: bool = False
    archived: bool = False
    tags_json: Optional[str] = None     # JSON list
    notes: Optional[str] = None
    updated_at: Optional[str] = None    # ISO str si querés manejar timestamps

# ---- NUEVO: almacenamiento opcional de mensajes WA (para auditoría/depuración) ----
class WAMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    brand_id: int = Field(index=True, foreign_key="brand.id")
    instance: Optional[str] = Field(default=None, index=True)   # p.ej. brand_1
    jid: str = Field(index=True)                                 # 549xxx@s.whatsapp.net
    from_me: bool = False
    text: Optional[str] = None
    ts: Optional[int] = Field(default=None, index=True)          # epoch/WA timestamp si lo tenés
    raw_json: Optional[str] = None                               # persistencia cruda (opcional)

# ---------------------------
# Engine & Session
# ---------------------------
_engine: Optional[Engine] = None

def _compute_sqlite_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    return "sqlite:///./pro.db"

def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    url = _compute_sqlite_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    log.info("Creando engine en %s", url)
    _engine = create_engine(url, connect_args=connect_args, echo=False, future=True)
    return _engine

def _apply_light_migrations(engine: Engine):
    """Pequeñas migraciones sin Alembic."""
    insp = inspect(engine)
    try:
        if "waconfig" in insp.get_table_names():
            cols = [c["name"] for c in insp.get_columns("waconfig")]
            if "super_password_hash" not in cols:
                with engine.connect() as conn:
                    conn.execute(sqltext("ALTER TABLE waconfig ADD COLUMN super_password_hash TEXT"))
                    conn.commit()
                    log.info("Migración: waconfig.super_password_hash agregado")
    except Exception as e:
        log.warning("Light migrations warning: %s", e)

def init_db():
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _apply_light_migrations(engine)

def get_session():
    engine = get_engine()
    with Session(engine) as session:
        yield session

@contextmanager
def session_cm():
    engine = get_engine()
    with Session(engine) as s:
        yield s

__all__ = [
    "SQLModel","Field","Session","select",
    "Brand","Campaign","ContentItem","Task","Customer",
    "Reservation","Availability","ChannelAccount",
    "ConversationThread","ChatMessage","Lead",
    "WAConfig","BrandDataSource","WAChatMeta","WAMessage",
    "init_db","get_session","session_cm"
]
