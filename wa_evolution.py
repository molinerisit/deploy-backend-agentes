import os, logging
from typing import Optional, Dict, Any, Tuple, List
import httpx

log = logging.getLogger("wa_evolution")

EVO_BASE = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
EVO_KEY  = (os.getenv("EVOLUTION_API_KEY") or "").strip()

class EvolutionError(Exception):
    pass

def _headers() -> Dict[str, str]:
    return {"apikey": EVO_KEY} if EVO_KEY else {}

def _must_cfg():
    if not EVO_BASE or not EVO_KEY:
        raise EvolutionError("EVOLUTION_BASE_URL/EVOLUTION_API_KEY no configurados")

# ---------------- HTTP helpers ----------------
def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
    url = f"{EVO_BASE}{path}"
    with httpx.Client(timeout=20) as c:
        r = c.get(url, headers=_headers(), params=params)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"status": r.status_code, "error": "text", "text": r.text}

def _post(path: str, json_body: Dict[str, Any]) -> Tuple[int, Any]:
    url = f"{EVO_BASE}{path}"
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_headers(), json=json_body)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"status": r.status_code, "error": "text", "text": r.text}

# ---------------- Instance mgmt ----------------
def create_instance(instance_name: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
    """
    Intenta crear/upsert la instancia. Algunos servers exigen 'webhook' en el create.
    Probamos varias variantes.
    """
    _must_cfg()
    variants: List[Dict[str, Any]] = [
        {"instanceName": instance_name, "webhook": webhook_url},
        {"instanceName": instance_name, "webhookUrl": webhook_url},
        {"instanceName": instance_name, "webhook": {"url": webhook_url} if webhook_url else None},
        {"instanceName": instance_name},
    ]
    for body in variants:
        body = {k: v for k, v in body.items() if v is not None}
        sc, js = _post("/instance/create", body)
        if sc == 403 and "already" in str(js).lower():
            log.info("Instance %s ya existía; seguimos.", instance_name)
            return {"ok": True, "http_status": 403, "alreadyExists": True, "body": js}
        if sc < 400:
            return {"ok": True, "http_status": sc, "body": js}
        # hay builds que validan: 'instance requires property "webhook"'
        if "requires property \"webhook\"" in str(js).lower() and webhook_url:
            # seguimos probando otras variantes que incluyan webhook
            continue
        log.warning("create_instance fallo (%s): %s", sc, js)
    raise EvolutionError("No se pudo crear instancia (todas las variantes fallaron)")

def set_webhook(instance_name: str, webhook_url: str) -> Tuple[int, Any]:
    """
    Varias rutas posibles según versión de Evolution.
    """
    _must_cfg()
    tries = [
        (f"/webhook/set/{instance_name}", {"url": webhook_url}),
        ("/instance/webhook/set", {"instanceName": instance_name, "url": webhook_url}),
        (f"/instance/setWebhook/{instance_name}", {"url": webhook_url}),
        ("/instance/setWebhook", {"instanceName": instance_name, "webhook": webhook_url}),
        ("/instance/update", {"instanceName": instance_name, "webhook": webhook_url}),
        (f"/instance/update/{instance_name}", {"webhook": webhook_url}),
        (f"/instance/edit/{instance_name}", {"webhook": webhook_url}),
        ("/instance/edit", {"instanceName": instance_name, "webhook": webhook_url}),
    ]
    last = (500, {"error": "no endpoint matched"})
    for path, body in tries:
        sc, js = _post(path, body)
        if sc < 400:
            return sc, js
        last = (sc, js)
    return last

def delete_instance(instance_name: str) -> Tuple[int, Any]:
    """
    Intentos de borrado o logout para poder recrear con webhook.
    """
    _must_cfg()
    tries = [
        (f"/instance/delete/{instance_name}", None),
        ("/instance/delete", {"instanceName": instance_name}),
        (f"/instance/remove/{instance_name}", None),
        (f"/instance/logout/{instance_name}", None),
        (f"/logout/{instance_name}", None),
    ]
    last = (500, {"error": "no endpoint matched"})
    for path, body in tries:
        if body is None:
            sc, js = _get(path)
        else:
            sc, js = _post(path, body)
        if sc < 400:
            return sc, js
        last = (sc, js)
    return last

def ensure_webhook(instance_name: str, webhook_url: str) -> Dict[str, Any]:
    """
    Garantiza que la instancia exista y tenga webhook. Si no se puede setear,
    intenta borrar y recrear con webhook.
    """
    ci = create_instance(instance_name, webhook_url=webhook_url)
    sc, js = set_webhook(instance_name, webhook_url)
    if sc and sc < 400:
        return {"ok": True, "action": "set_webhook", "status": sc, "body": js}

    # Si no hay endpoint de set_webhook o falla, forzar recreación con webhook
    dsc, djs = delete_instance(instance_name)
    if dsc and dsc < 400:
        ci2 = create_instance(instance_name, webhook_url=webhook_url)
        return {"ok": True, "action": "recreate_with_webhook", "create": ci2, "delete_status": dsc}
    return {"ok": False, "error": "no webhook set", "set_webhook_status": sc, "set_webhook_body": js, "delete_status": dsc, "delete_body": djs}

def connect_instance(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    sc, js = _get(f"/instance/connect/{instance_name}")
    if sc >= 400:
        log.warning("connect_instance %s -> %s %s", instance_name, sc, js)
        raise EvolutionError(f"connect_instance error ({sc}) {js}")
    return js if isinstance(js, dict) else {"ok": True}

def connection_state(instance_name: str) -> Dict[str, Any]:
    _must_cfg()
    sc, js = _get(f"/instance/connectionState/{instance_name}")
    if sc >= 400:
        return {"instance": {"instanceName": instance_name, "state": "unknown"}, "http_status": sc}
    return js if isinstance(js, dict) else {"instance": {"state": "unknown"}}

def qr_by_param(instance_name: str) -> Tuple[int, Any]:
    _must_cfg()
    sc, js = _get("/instance/qr", params={"instanceName": instance_name})
    if sc == 404:
        sc, js = _get(f"/instance/qr/{instance_name}")
    return sc, js

def send_text(instance_name: str, number: str, text: str) -> Dict[str, Any]:
    _must_cfg()
    sc, js = _post(f"/message/sendText/{instance_name}", {"number": number, "text": text})
    if sc >= 400:
        log.warning("send_text %s -> %s %s", instance_name, sc, js)
        raise EvolutionError(f"send_text error ({sc}) {js}")
    return {"http_status": sc, "body": js}

# ------ listar chats / mensajes (si el server no los tiene, devolverá 404) ------
def list_chats(instance_name: str, limit: int = 200) -> Tuple[int, Any]:
    _must_cfg()
    attempts = [
        ("/chat/list", {"instanceName": instance_name, "limit": limit, "count": limit}),
        (f"/chat/list/{instance_name}", None),
        ("/chats/list", {"instanceName": instance_name, "limit": limit, "count": limit}),
        (f"/chats/{instance_name}", None),
        ("/chats", {"instanceName": instance_name}),
        ("/chat/all", {"instanceName": instance_name}),
    ]
    last = (500, {"status": 500, "error": "No endpoint matched"})
    for path, params in attempts:
        sc, js = _get(path, params=params)
        if sc == 404:
            continue
        if sc < 400:
            return sc, js
        last = (sc, js)
    return last

def get_chat_messages(instance_name: str, jid: str, limit: int = 100) -> Tuple[int, Any]:
    _must_cfg()
    attempts = [
        ("/messages/list", {"instanceName": instance_name, "jid": jid, "limit": limit}),
        (f"/messages/{instance_name}", {"jid": jid, "limit": limit}),
        (f"/chat/messages/{instance_name}", {"jid": jid, "limit": limit}),
    ]
    last = (500, {"status": 500, "error": "No endpoint matched"})
    for path, params in attempts:
        sc, js = _get(path, params=params)
        if sc == 404:
            continue
        if sc < 400:
            return sc, js
        last = (sc, js)
    return last

# ------------- Cliente OO -------------
class EvolutionClient:
    def create_instance(self, name: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
        return create_instance(name, webhook_url)

    def set_webhook(self, name: str, webhook_url: str) -> Tuple[int, Any]:
        return set_webhook(name, webhook_url)

    def delete_instance(self, name: str) -> Tuple[int, Any]:
        return delete_instance(name)

    def ensure_webhook(self, name: str, webhook_url: str) -> Dict[str, Any]:
        return ensure_webhook(name, webhook_url)

    def connect_instance(self, name: str) -> Dict[str, Any]:
        return connect_instance(name)

    def connection_state(self, name: str) -> Dict[str, Any]:
        return connection_state(name)

    def qr_by_param(self, name: str) -> Tuple[int, Any]:
        return qr_by_param(name)

    def list_chats(self, name: str, limit: int = 200) -> Tuple[int, Any]:
        return list_chats(name, limit=limit)

    def get_chat_messages(self, name: str, jid: str, limit: int = 100) -> Tuple[int, Any]:
        return get_chat_messages(name, jid=jid, limit=limit)

    def send_text(self, name: str, number: str, text: str) -> Dict[str, Any]:
        return send_text(name, number, text)
