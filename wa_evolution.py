# backend/wa_evolution.py
import os, logging, json
from typing import Optional, Dict, Any, List, Tuple
import httpx

log = logging.getLogger("wa_evolution")

EVO_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVO_KEY  = os.getenv("EVOLUTION_API_KEY", "")

class EvolutionError(Exception):
    pass

def _headers() -> Dict[str, str]:
    return {"apikey": EVO_KEY} if EVO_KEY else {}

def _must_cfg():
    if not EVO_BASE or not EVO_KEY:
        raise EvolutionError("EVOLUTION_BASE_URL/EVOLUTION_API_KEY no configurados")

def _json_or_text(resp: httpx.Response) -> Dict[str, Any]:
    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "response": resp.text}

def _try_get(paths: List[str], params: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    _must_cfg()
    with httpx.Client(timeout=20) as c:
        for p in paths:
            url = f"{EVO_BASE}{p}"
            r = c.get(url, headers=_headers(), params=params or {})
            if r.status_code < 400:
                return r.status_code, _json_or_text(r)
            log.info("GET %s -> %s", url, r.status_code)
    # última respuesta
    return r.status_code, _json_or_text(r)

def _try_post(paths: List[str], body_variants: List[Dict[str, Any]], params: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any]]:
    _must_cfg()
    with httpx.Client(timeout=20) as c:
        last = None
        for p in paths:
            url = f"{EVO_BASE}{p}"
            for b in body_variants:
                r = c.post(url, headers=_headers(), params=params or {}, json=b)
                last = r
                if r.status_code < 400:
                    return r.status_code, _json_or_text(r)
                log.info("POST %s body=%s -> %s", url, b, r.status_code)
        if last is None:
            raise EvolutionError("POST falló sin intentos?")
        return last.status_code, _json_or_text(last)

# -------------------- API de alto nivel --------------------

def create_instance(instance_name: str) -> Dict[str, Any]:
    """
    POST /instance/create { instanceName }
    Si ya existe, algunos servers devuelven 403 con texto 'already'.
    """
    _must_cfg()
    paths = ["/instance/create"]
    bodies = [{"instanceName": instance_name}]
    status, js = _try_post(paths, bodies)
    if status == 403 and "already" in json.dumps(js).lower():
        log.info("Instance %s ya existía; seguimos.", instance_name)
        return {"ok": True, "status": 403, "alreadyExists": True}
    if status >= 400:
        log.warning("create_instance error: %s %s", status, js)
    return {"status": status, **(js if isinstance(js, dict) else {})}

def connect_instance(instance_name: str) -> Dict[str, Any]:
    """
    Dispara reconexión/QR.
    GET /instance/connect/{name}
    fallback: GET /instance/connect?instanceName=...
    """
    paths = [f"/instance/connect/{instance_name}", "/instance/connect"]
    params = {"instanceName": instance_name}
    status, js = _try_get(paths, params)
    return {"status": status, **js}

def connection_state(instance_name: str) -> Dict[str, Any]:
    """
    GET /instance/connectionState/{name}
    fallback: GET /instance/connectionState?instanceName=...
    """
    paths = [f"/instance/connectionState/{instance_name}", "/instance/connectionState"]
    params = {"instanceName": instance_name}
    status, js = _try_get(paths, params)
    if status >= 400:
        return {"status": status, "instance": {"state": "unknown"}}
    return js if isinstance(js, dict) else {"instance": {"state": "unknown"}}

def get_qr(instance_name: str) -> Dict[str, Any]:
    """
    Intenta obtener QR (parámetro o path), y también recoge pairingCode desde /instance/connect.
    """
    out: Dict[str, Any] = {"connected": False}
    # estado
    try:
        st = connection_state(instance_name)
        out["state"] = st
        s = (st or {}).get("instance", {}).get("state", "")
        out["connected"] = str(s).lower() in ("open", "connected")
    except Exception as e:
        out["state"] = {"error": str(e)}

    # QR
    if not out["connected"]:
        # por query
        status, jq = _try_get(["/instance/qr"], {"instanceName": instance_name})
        if status == 404:
            # por path
            status2, jq2 = _try_get([f"/instance/qr/{instance_name}"])
            jq = jq2 if status2 < 400 else jq
        if isinstance(jq, dict):
            out.update(jq)
        else:
            out["qr"] = jq

        # pairing extra via connect
        cj = connect_instance(instance_name)
        if isinstance(cj, dict):
            for k in ("code", "pairingCode", "qrcode", "qr"):
                if k in cj:
                    out[k] = cj[k]
    return out

def set_webhook(instance_name: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
    """
    Prueba múltiples variantes conocidas:
      - POST /webhook/set/{instance}        body: { url } | { webhookUrl }
      - POST /webhook/set?instanceName=...  body: { url } | { webhookUrl }
      - POST /instance/webhook/set          body: { instanceName, url } | { instanceName, webhookUrl }
      - POST /instance/setWebhook/{instance} body: { url } | { webhookUrl }
    """
    paths = [
        f"/webhook/set/{instance_name}",
        "/webhook/set",
        "/instance/webhook/set",
        f"/instance/setWebhook/{instance_name}",
    ]
    bodies = [
        {"url": webhook_url},
        {"webhookUrl": webhook_url},
        {"instanceName": instance_name, "url": webhook_url},
        {"instanceName": instance_name, "webhookUrl": webhook_url},
    ]
    params = {"instanceName": instance_name}
    status, js = _try_post(paths, bodies, params=params)
    return status, js

def list_chats(instance_name: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
    """
    Intenta rutas para listar chats:
      - GET /chats/list/{instance}?limit=...
      - GET /chats/list?instanceName=...&limit=...
      - GET /chat/list/{instance}
      - GET /chat/list?instanceName=...
      - GET /chats/{instance}
      - GET /chats?instanceName=...
    """
    params = {"instanceName": instance_name, "limit": limit, "count": limit}
    paths = [
        f"/chats/list/{instance_name}",
        "/chats/list",
        f"/chat/list/{instance_name}",
        "/chat/list",
        f"/chats/{instance_name}",
        "/chats",
    ]
    status, js = _try_get(paths, params)
    return status, js

def get_chat_messages(instance_name: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
    """
    Variantes para mensajes de un chat:
      - GET /messages/list/{instance}?jid=...&limit=...
      - GET /messages/{instance}?jid=...
      - GET /chat/messages/{instance}?jid=...
      - GET /chat/messages?instanceName=...&jid=...
    """
    params = {"instanceName": instance_name, "jid": jid, "limit": limit, "count": limit}
    paths = [
        f"/messages/list/{instance_name}",
        f"/messages/{instance_name}",
        f"/chat/messages/{instance_name}",
        "/chat/messages",
    ]
    status, js = _try_get(paths, params)
    return status, js

def send_text(instance_name: str, number: str, text: str) -> Dict[str, Any]:
    """
    POST /message/sendText/{instanceName}
    Body: { number, text }
    Devuelve siempre dict con 'status'.
    """
    _must_cfg()
    url = f"{EVO_BASE}/message/sendText/{instance_name}"
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_headers(), json={"number": number, "text": text})
    try:
        js = r.json()
    except Exception:
        js = {"response": r.text}
    return {"status": r.status_code, **(js if isinstance(js, dict) else {})}

# -------------------- Cliente OO --------------------

class EvolutionClient:
    def create_instance(self, name: str) -> Dict[str, Any]:
        return create_instance(name)

    def connect_instance(self, name: str) -> Dict[str, Any]:
        return connect_instance(name)

    def connection_state(self, name: str) -> Dict[str, Any]:
        return connection_state(name)

    def get_qr(self, name: str) -> Dict[str, Any]:
        return get_qr(name)

    def set_webhook(self, name: str, url: str) -> Tuple[int, Dict[str, Any]]:
        return set_webhook(name, url)

    def list_chats(self, name: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
        return list_chats(name, limit=limit)

    def get_chat_messages(self, name: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        return get_chat_messages(name, jid=jid, limit=limit)

    def send_text(self, name: str, number: str, text: str) -> Dict[str, Any]:
        return send_text(name, number, text)
