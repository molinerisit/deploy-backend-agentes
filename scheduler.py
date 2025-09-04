import os
import threading
import time
import logging
from typing import List

import httpx
from db import session_cm, select, Brand

log = logging.getLogger("scheduler")

_INTERVAL_SEC = int(os.getenv("WA_SYNC_PULL_INTERVAL_SEC", "20"))  # cada 20s por default

def _get_public_base() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        # Railway/uvicorn interno; intentamos puerto 8080
        base = "http://127.0.0.1:8080"
    return base

def _brand_ids() -> List[int]:
    try:
        with session_cm() as s:
            rows = s.exec(select(Brand)).all()
            return [b.id for b in rows if getattr(b, "id", None)]
    except Exception as e:
        log.warning("scheduler: no pude listar brands: %s", e)
        return []

def _tick_once():
    base = _get_public_base()
    ids = _brand_ids()
    if not ids:
        log.debug("scheduler: sin brands todavía")
        return
    for bid in ids:
        url = f"{base}/api/wa/sync_pull?brand_id={bid}"
        try:
            with httpx.Client(timeout=20.0) as cli:
                r = cli.post(url)
                ok = r.status_code < 400
                log.info("scheduler: sync_pull brand=%s -> %s %s", bid, r.status_code, r.text[:200])
                # No hacemos nada más; el endpoint guarda en DB
        except Exception as e:
            log.warning("scheduler: pull fallo brand=%s: %s", bid, e)

def _loop():
    while True:
        try:
            _tick_once()
        except Exception as e:
            log.warning("scheduler: tick error: %s", e)
        time.sleep(_INTERVAL_SEC)

def start_scheduler():
    t = threading.Thread(target=_loop, name="wa-pull-scheduler", daemon=True)
    t.start()
    log.info("Scheduler WA sync_pull iniciado cada %ss", _INTERVAL_SEC)
