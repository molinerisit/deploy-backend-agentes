# backend/scheduler.py
import logging, datetime as dt
from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import select
from db import session_cm, ContentItem
from social.publish import try_publish

log = logging.getLogger("scheduler")
_scheduler = None

def _publisher_tick():
    now_utc = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    try:
        with session_cm() as s:
            rows = s.exec(select(ContentItem).where(ContentItem.status == "scheduled")).all()
            for item in rows:
                # Publish si no tiene fecha o ya venció
                when = None
                if item.scheduled_iso:
                    try:
                        when = dt.datetime.fromisoformat(item.scheduled_iso.replace("Z","+00:00"))
                    except Exception:
                        when = None
                if when is None or when <= now_utc:
                    ok, message = try_publish(item)
                    item.status = "published" if ok else "failed"
                    note = f"[{now_utc.isoformat()}] {message}"
                    item.notes = f"{(item.notes or '')}\n{note}".strip()
                    s.add(item)
            s.commit()
    except Exception:
        log.exception("publisher_tick error")

def _reminders():
    # tu lógica de recordatorios acá (opcional)
    pass

def start_scheduler():
    global _scheduler
    if _scheduler:
        return _scheduler
    sched = BackgroundScheduler()
    sched.add_job(_publisher_tick, "interval", seconds=30, id="_publisher_tick")
    sched.add_job(_reminders, "interval", minutes=5, id="_reminders")
    sched.start()
    log.info("Scheduler iniciado.")
    _scheduler = sched
    return sched
