# --- backend/wa_evolution.py ---
import os, logging, json as _json
import httpx
from typing import Any, Dict, Optional, Tuple, List

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
EVOLUTION_INTEGRATION = os.getenv("EVOLUTION_INTEGRATION", "WHATSAPP").strip()

DEFAULT_TIMEOUT = 25.0

def _hdr_sets() -> List[Dict[str, str]]:
    base = {"Content-Type": "application/json"}
    hs = []
    if EVOLUTION_API_KEY:
        hs.append({**base, "X-API-KEY": EVOLUTION_API_KEY})
        hs.append({**base, "Authorization": f"Bearer {EVOLUTION_API_KEY}"})
        hs.append({**base, "apikey": EVOLUTION_API_KEY})
    else:
        hs.append(base)
    return hs

def _url(path: str) -> str:
    return f"{EVOLUTION_BASE_URL}{path}"

def _ok(status: int) -> bool:
    return 200 <= status < 400

class EvolutionClient:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        if not EVOLUTION_BASE_URL:
            log.warning("EVOLUTION_BASE_URL no configurado")
        if not EVOLUTION_API_KEY:
            log.warning("EVOLUTION_API_KEY no configurado")

    def _request(self, method: str, path: str, *, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last = {"http_status": 599, "body": {"error": "request_failed"}}
        for headers in _hdr_sets():
            try:
                url = _url(path)
                log.debug("HTTP %s %s params=%s json=%s", method, url, params, (json if not json else {k: json[k] for k in list(json)[:10]}))
                with httpx.Client(timeout=self.timeout) as cli:
                    r = cli.request(method, url, headers=headers, json=json, params=params)
                    try:
                        body = r.json()
                    except Exception:
                        body = {"raw": (r.text[:2000] if isinstance(r.text, str) else str(r.text))}
                    out = {"http_status": r.status_code, "body": body}
                    sample = body if isinstance(body, dict) else {"_non_dict_": str(body)[:1000]}
                    log.debug("HTTP %s %s -> %s body=%s", method, url, r.status_code, _json.dumps(sample)[:1200])
                    if r.status_code not in (401, 403):
                        return out
                    last = out
            except Exception as e:
                last = {"http_status": 599, "body": {"error": str(e)}}
                log.warning("HTTP error %s %s: %s", method, path, e)
        return last

    def _post(self, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("POST", path, json=json, params=params)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", path, params=params)

    # ---------------- Instances ----------------
    def fetch_instances(self) -> Tuple[int, Dict[str, Any]]:
        resp = self._get("/instance/fetchInstances")
        return resp["http_status"], resp["body"]

    def create_instance(self, instance: str, webhook_url: Optional[str] = None, integration: Optional[str] = None) -> Dict[str, Any]:
        integ = (integration or EVOLUTION_INTEGRATION or "WHATSAPP").strip()
        attempts = [
            ("POST", "/instance/create", {"instanceName": instance, "webhook": webhook_url, "integration": integ}, None),
            ("POST", "/instance/create", {"name": instance, "webhookUrl": webhook_url, "integration": integ}, None),
            ("POST", f"/instance/create/{instance}", None, {"integration": integ}),
            ("POST", "/instance/add",  {"instanceName": instance, "webhook": webhook_url, "integration": integ}, None),
            ("POST", "/instance/init", {"instanceName": instance, "webhook": webhook_url, "integration": integ}, None),
        ]
        last = None
        for method, path, body, params in attempts:
            resp = self._request(method, path, json=body, params=params)
            if _ok(resp["http_status"]):
                return resp
            last = resp
            log.warning("create_instance intento %s %s -> %s %s", method, path, resp["http_status"], resp["body"])
        return last or {"http_status": 500, "body": {"error": "create_failed"}}

    def set_webhook(self, instance: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
        attempts = [
            ("/instance/webhook/set", {"instanceName": instance, "webhook": webhook_url}, None),
            (f"/instance/setWebhook/{instance}", {"webhook": webhook_url}, None),
            ("/instance/setWebhook", {"instanceName": instance, "webhook": webhook_url}, None),
        ]
        last = (599, {"error": "no_attempts"})
        for path, body, params in attempts:
            resp = self._post(path, json=body, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
            last = (resp["http_status"], resp["body"])
            log.warning("set_webhook intento %s -> %s %s", path, resp["http_status"], resp["body"])
        return last

    def connect_instance(self, instance: str) -> Dict[str, Any]:
        resp = self._get(f"/instance/connect/{instance}")
        if _ok(resp["http_status"]):
            return resp
        return self._post("/instance/connect", json={"instanceName": instance})

    def connection_state(self, instance: str) -> Dict[str, Any]:
        resp = self._get(f"/instance/connectionState/{instance}")
        if _ok(resp["http_status"]):
            return resp
        return self._get("/instance/connectionState", params={"instanceName": instance})

    # ---------------- QR / Pairing ----------------
    def qr_by_param(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        attempts = [
            ("GET", "/instance/qr", {"instanceName": instance}),
            ("GET", f"/instance/qr/{instance}", None),
            ("GET", "/instance/qrbase64", {"instanceName": instance}),
            ("GET", "/instance/pairingCode", {"instanceName": instance}),
            ("GET", f"/instance/pairingCode/{instance}", None),
        ]
        last = None
        for method, path, params in attempts:
            resp = self._request(method, path, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
            last = resp
        return last["http_status"], last["body"]

    # ---------------- Chats / Messages ----------------
    def send_text(self, instance: str, to_number: str, text: str) -> Dict[str, Any]:
        payload = {"number": str(to_number), "text": str(text)}
        return self._post(f"/message/sendText/{instance}", json=payload)

    def list_chats(self, instance: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
        for path, params in [
            ("/chat/findChats", {"instanceName": instance, "limit": limit}),
            (f"/chat/findChats/{instance}", {"limit": limit}),
        ]:
            resp = self._get(path, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
        return 500, {"status": 500, "error": "No endpoint matched"}

    def get_chat_messages(self, instance: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        for path, params in [
            ("/messages/list", {"instanceName": instance, "jid": jid, "limit": limit}),
            (f"/messages/{instance}/list", {"jid": jid, "limit": limit}),
        ]:
            resp = self._get(path, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
        return 500, {"status": 500, "error": "No endpoint matched"}

    # ---------------- Orquestador ----------------
    def ensure_started(self, instance: str, webhook_url: Optional[str], integration: Optional[str] = None) -> Dict[str, Any]:
        detail = {"step": "ensure_started", "create": None, "webhook": None, "connect": None}
        cr = self.create_instance(instance, webhook_url, integration=integration)
        detail["create"] = cr
        sc, wjs = self.set_webhook(instance, webhook_url)
        detail["webhook"] = {"http_status": sc, "body": wjs}
        conn = self.connect_instance(instance)
        detail["connect"] = conn
        if 200 <= (conn.get("http_status", 500)) < 400:
            return {"http_status": 200, "body": {"ok": True, "detail": detail}}
        return {"http_status": conn.get("http_status", 500), "body": {"error": "connect_failed", "detail": detail}}
