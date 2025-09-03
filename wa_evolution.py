# backend/wa_evolution.py
import os
import logging
from typing import Dict, Any, Tuple, Optional
import httpx

log = logging.getLogger("wa_evolution")

EVO_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVO_KEY  = os.getenv("EVOLUTION_API_KEY", "")


def _headers() -> Dict[str, str]:
    return {"apikey": EVO_KEY} if EVO_KEY else {}


def _must_cfg():
    if not EVO_BASE or not EVO_KEY:
        raise RuntimeError("EVOLUTION_BASE_URL/EVOLUTION_API_KEY no configurados")


def _json(r: httpx.Response) -> Dict[str, Any]:
    try:
        j = r.json()
        return j if isinstance(j, dict) else {"raw": j}
    except Exception:
        txt = (r.text or "").strip()
        return {"message": txt} if txt else {}


# -------------------- Funciones base --------------------

def create_instance(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    url = f"{EVO_BASE}/instance/create"
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_headers(), json={"instanceName": instance_name})

    if r.status_code == 403 and "already" in r.text.lower():
        log.info("Instance %s ya existía; seguimos.", instance_name)
        return {"ok": True, "status": 403, "alreadyExists": True}

    if r.status_code >= 400:
        log.warning("create_instance %s -> %s %s", url, r.status_code, r.text)
    out = _json(r)
    out.setdefault("ok", r.status_code < 400)
    out.setdefault("status", r.status_code)
    return out


def connect_instance(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    url = f"{EVO_BASE}/instance/connect/{instance_name}"
    with httpx.Client(timeout=20) as c:
        r = c.get(url, headers=_headers())
    if r.status_code >= 400:
        log.warning("connect_instance %s -> %s %s", url, r.status_code, r.text)
    out = _json(r)
    out.setdefault("ok", r.status_code < 400)
    out.setdefault("status", r.status_code)
    return out


def connect(instance_name: str) -> Dict[str, Any]:
    return connect_instance(instance_name)


def connection_state(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    url = f"{EVO_BASE}/instance/connectionState/{instance_name}"
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers=_headers())
    if r.status_code >= 400:
        return {"instance": {"state": "unknown"}, "status": r.status_code}
    out = _json(r)
    out.setdefault("status", r.status_code)
    return out


def get_qr(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    out: Dict[str, Any] = {}
    with httpx.Client(timeout=15) as c:
        r = c.get(f"{EVO_BASE}/instance/qr", headers=_headers(), params={"instanceName": instance_name})
        if r.status_code == 404:
            r = c.get(f"{EVO_BASE}/instance/qr/{instance_name}", headers=_headers())
        try:
            jq = r.json()
            if isinstance(jq, dict):
                out.update(jq)
        except Exception:
            txt = (r.text or "").strip()
            if txt:
                out["qr"] = txt

        try:
            rc = c.get(f"{EVO_BASE}/instance/connect/{instance_name}", headers=_headers())
            if rc.status_code < 400:
                j2 = rc.json()
                if isinstance(j2, dict):
                    for k in ("code", "pairingCode", "qrcode", "qr", "base64", "dataUrl", "image"):
                        if k in j2:
                            out[k] = j2[k]
        except Exception:
            pass
    return out


def qr_by_param(instance_name: str) -> Tuple[int, Dict[str, Any]]:
    _must_cfg()
    url = f"{EVO_BASE}/instance/qr"
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers=_headers(), params={"instanceName": instance_name})
    try:
        j = r.json()
        j = j if isinstance(j, dict) else {"raw": j}
    except Exception:
        j = {"qr": r.text}
    return r.status_code, j


def send_text(instance_name: str, number: str, text: str) -> Dict[str, Any]:
    _must_cfg()
    url = f"{EVO_BASE}/message/sendText/{instance_name}"
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_headers(), json={"number": number, "text": text})
    if r.status_code >= 400:
        log.warning("send_text %s -> %s %s", url, r.status_code, r.text)
    out = _json(r)
    out.setdefault("ok", r.status_code < 400)
    out.setdefault("status", r.status_code)
    return out


def set_webhook(instance_name: str, url_to_call: str) -> Tuple[int, Dict[str, Any]]:
    _must_cfg()
    attempts = [
        (f"{EVO_BASE}/webhook/set/{instance_name}", {"url": url_to_call}),
        (f"{EVO_BASE}/instance/webhook/set", {"instanceName": instance_name, "url": url_to_call}),
        (f"{EVO_BASE}/instance/setWebhook/{instance_name}", {"url": url_to_call}),
    ]
    last = (0, {"error": "no attempts"})
    with httpx.Client(timeout=15) as c:
        for url, payload in attempts:
            try:
                r = c.post(url, headers=_headers(), json=payload)
                try:
                    j = r.json()
                except Exception:
                    j = {"message": r.text}
                last = (r.status_code, j)
                if r.status_code < 400:
                    return last
            except Exception as e:
                last = (0, {"error": str(e)})
    return last


# ----------- NUEVO: listar chats y mensajes -----------

def list_chats(instance_name: str, limit: int = 1000) -> Tuple[int, Dict[str, Any]]:
    """
    Devuelve (status, json). Intenta varias rutas comunes:
      /chats/list/{instance}
      /chats/list?instanceName=...
      /chat/list/{instance}
      /chat/list?instanceName=...
    """
    _must_cfg()
    attempts = [
        (f"{EVO_BASE}/chats/list/{instance_name}", None),
        (f"{EVO_BASE}/chats/list", {"instanceName": instance_name, "limit": limit, "count": limit}),
        (f"{EVO_BASE}/chat/list/{instance_name}", None),
        (f"{EVO_BASE}/chat/list", {"instanceName": instance_name, "limit": limit, "count": limit}),
        (f"{EVO_BASE}/chats/{instance_name}", None),
    ]
    with httpx.Client(timeout=20) as c:
        for url, params in attempts:
            try:
                r = c.get(url, headers=_headers(), params=params)
                j = _json(r)
                if r.status_code < 400:
                    return r.status_code, j
                last = (r.status_code, j)
            except Exception as e:
                last = (0, {"error": str(e)})
        return last


def get_chat_messages(instance_name: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
    """
    Devuelve (status, json). Intenta:
      /chat/messages/{instance}/{jid}
      /chat/messages?instanceName=...&jid=...
      /messages/list/{instance}/{jid}
      /messages/list?instanceName=...&jid=...
    """
    _must_cfg()
    attempts = [
        (f"{EVO_BASE}/chat/messages/{instance_name}/{jid}", {"limit": limit}),
        (f"{EVO_BASE}/chat/messages", {"instanceName": instance_name, "jid": jid, "limit": limit}),
        (f"{EVO_BASE}/messages/list/{instance_name}/{jid}", {"limit": limit}),
        (f"{EVO_BASE}/messages/list", {"instanceName": instance_name, "jid": jid, "limit": limit}),
    ]
    with httpx.Client(timeout=20) as c:
        for url, params in attempts:
            try:
                r = c.get(url, headers=_headers(), params=params)
                j = _json(r)
                if r.status_code < 400:
                    return r.status_code, j
                last = (r.status_code, j)
            except Exception as e:
                last = (0, {"error": str(e)})
        return last


# -------------------- Wrapper OO --------------------

class EvolutionClient:
    def create_instance(self, name: str) -> Dict[str, Any]:
        return create_instance(name)

    def connect_instance(self, name: str) -> Dict[str, Any]:
        return connect_instance(name)

    def connection_state(self, name: str) -> Dict[str, Any]:
        return connection_state(name)

    def get_qr(self, name: str) -> Dict[str, Any]:
        return get_qr(name)

    def send_text(self, name: str, number: str, text: str) -> Dict[str, Any]:
        return send_text(name, number, text)

    def set_webhook(self, name: str, url_to_call: str) -> Tuple[int, Dict[str, Any]]:
        return set_webhook(name, url_to_call)

    def connect(self, name: str) -> Dict[str, Any]:
        return connect_instance(name)

    def qr_by_param(self, name: str) -> Tuple[int, Dict[str, Any]]:
        return qr_by_param(name)

    # NUEVOS
    def list_chats(self, name: str, limit: int = 1000) -> Tuple[int, Dict[str, Any]]:
        return list_chats(name, limit)

    def get_chat_messages(self, name: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        return get_chat_messages(name, jid, limit)
