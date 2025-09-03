import os
import logging
import httpx
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")

DEFAULT_TIMEOUT = 25.0


def _hdrs() -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if EVOLUTION_API_KEY:
        h["X-API-KEY"] = EVOLUTION_API_KEY
    return h


def _url(path: str) -> str:
    return f"{EVOLUTION_BASE_URL}{path}"


def _ok(status: int) -> bool:
    return 200 <= status < 400


def _pick_str(d: Dict[str, Any], keys) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


class EvolutionClient:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout
        if not EVOLUTION_BASE_URL:
            log.warning("EVOLUTION_BASE_URL no configurado")
        if not EVOLUTION_API_KEY:
            log.warning("EVOLUTION_API_KEY no configurado")

    # ---------------- HTTP helpers ----------------
    def _post(self, path: str, json: Optional[Dict[str, Any]] = None, params: Optional[Dict[str,str]] = None) -> Dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as cli:
                r = cli.post(_url(path), headers=_hdrs(), json=json or {}, params=params or {})
                out = {"http_status": r.status_code, "body": {}}
                try:
                    out["body"] = r.json()
                except Exception:
                    out["body"] = {"raw": r.text}
                return out
        except Exception as e:
            return {"http_status": 599, "body": {"error": str(e)}}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            with httpx.Client(timeout=self.timeout) as cli:
                r = cli.get(_url(path), headers=_hdrs(), params=params or {})
                out = {"http_status": r.status_code, "body": {}}
                try:
                    out["body"] = r.json()
                except Exception:
                    out["body"] = {"raw": r.text}
                return out
        except Exception as e:
            return {"http_status": 599, "body": {"error": str(e)}}

    # ---------------- Instances ----------------
    def fetch_instances(self) -> Tuple[int, Dict[str, Any]]:
        resp = self._get("/instance/fetchInstances")
        return resp["http_status"], resp["body"]

    def instance_exists(self, instance: str) -> bool:
        sc, js = self.fetch_instances()
        if not _ok(sc) or not isinstance(js, dict):
            return False
        items = js.get("instances") or js.get("data") or js.get("response") or []
        if isinstance(items, dict):
            items = items.get("instances") or []
        if not isinstance(items, list):
            return False
        for it in items:
            name = it.get("instanceName") or it.get("name") or it.get("instance") or it.get("id")
            if name == instance:
                return True
        return False

    def create_instance(self, instance: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
        tries = [
            ("/instance/create", {"instanceName": instance, "webhook": webhook_url}),
            ("/instance/create", {"name": instance, "webhookUrl": webhook_url}),
            ("/instance/create", {"instanceName": instance}),
        ]
        last = None
        for path, body in tries:
            resp = self._post(path, json=body)
            if _ok(resp["http_status"]):
                return resp
            last = resp
            log.warning("create_instance intento %s -> %s %s", path, resp["http_status"], resp["body"])
        return last or {"http_status": 500, "body": {"error": "create_failed"}}

    def set_webhook(self, instance: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
        tries = [
            ("/instance/webhook/set", {"instanceName": instance, "webhook": webhook_url}, None),
            (f"/instance/setWebhook/{instance}", {"webhook": webhook_url}, None),
            ("/instance/setWebhook", {"instanceName": instance, "webhook": webhook_url}, None),
        ]
        last = (599, {"error": "no_attempts"})
        for path, body, params in tries:
            resp = self._post(path, json=body, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
            last = (resp["http_status"], resp["body"])
            log.warning("set_webhook intento %s -> %s %s", path, resp["http_status"], resp["body"])
        return last

    def connect_instance(self, instance: str) -> Dict[str, Any]:
        return self._get(f"/instance/connect/{instance}")

    def connection_state(self, instance: str) -> Dict[str, Any]:
        return self._get(f"/instance/connectionState/{instance}")

    def delete_instance(self, instance: str) -> Dict[str, Any]:
        resp = self._post("/instance/delete", json={"instanceName": instance})
        if _ok(resp["http_status"]):
            return resp
        return self._get(f"/instance/delete/{instance}")

    # ---------------- QR / Pairing ----------------
    def qr_by_param(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        """
        Devuelve lo que tenga Evolution. Claves posibles (según build):
        - 'qrcode' | 'qrCode' | 'QRCode' | 'qr'  (puede ser data:image... o solo texto)
        - 'base64' (data:image/...)
        - 'image'  (data:image/...)
        - 'code'   (string corto/largo)
        - 'pairingCode' | 'pairing_code'
        """
        # A) /instance/qr?instanceName=brand_1
        r1 = self._get("/instance/qr", params={"instanceName": instance})
        if _ok(r1["http_status"]):
            return r1["http_status"], r1["body"]
        # B) /instance/qr/brand_1
        r2 = self._get(f"/instance/qr/{instance}")
        if _ok(r2["http_status"]):
            return r2["http_status"], r2["body"]
        # C) algunas builds exponen /instance/qrbase64
        r3 = self._get("/instance/qrbase64", params={"instanceName": instance})
        if _ok(r3["http_status"]):
            return r3["http_status"], r3["body"]
        # D) último intento
        return r2["http_status"], r2["body"]

    # ---------------- Chats / Messages ----------------
    def list_chats(self, instance: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
        for path, params in [
            ("/messages/chats", {"instanceName": instance, "limit": limit}),
            (f"/messages/{instance}/chats", {"limit": limit}),
            ("/chat/list", {"instanceName": instance, "limit": limit, "count": limit}),
            (f"/chat/list/{instance}", None),
            ("/chats/list", {"instanceName": instance, "limit": limit, "count": limit}),
            (f"/chats/{instance}", None),
            ("/chats", {"instanceName": instance}),
            ("/chat/all", {"instanceName": instance}),
        ]:
            resp = self._get(path, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
        return 500, {"status": 500, "error": "No endpoint matched"}

    def get_chat_messages(self, instance: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        for path, params in [
            ("/messages/list", {"instanceName": instance, "jid": jid, "limit": limit}),
            (f"/messages/{instance}/list", {"jid": jid, "limit": limit}),
            (f"/messages/{instance}/chats/{jid}", {"limit": limit}),
        ]:
            resp = self._get(path, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
        return 500, {"status": 500, "error": "No endpoint matched"}

    def send_text(self, instance: str, to_number: str, text: str) -> Dict[str, Any]:
        payload = {"number": str(to_number), "text": str(text)}
        return self._post(f"/message/sendText/{instance}", json=payload)

    # ---------------- Orquestador ----------------
    def ensure_started(self, instance: str, webhook_url: Optional[str]) -> Dict[str, Any]:
        exists = self.instance_exists(instance)
        if not exists:
            cr = self.create_instance(instance, webhook_url)
            if not _ok(cr["http_status"]):
                return {"http_status": cr["http_status"], "body": {"error": "create_failed", "detail": cr["body"]}}
        if webhook_url:
            sc, wjs = self.set_webhook(instance, webhook_url)
            if sc >= 400:
                log.warning("ensure_started: set_webhook fallo (%s): %s", sc, wjs)
        return self.connect_instance(instance)

    # ---------------- Normalizadores útiles ----------------
    @staticmethod
    def extract_qr_fields(js: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """
        Devuelve dict normalizado:
          {'qr_data_url': str|None, 'link_code': str|None, 'pairing_code': str|None}
        """
        if not isinstance(js, dict):
            return {"qr_data_url": None, "link_code": None, "pairing_code": None}

        # Posibles ubicaciones
        cand = js
        # a veces viene dentro de {"response": {...}} o {"data": {...}}
        for k in ("response", "data"):
            if isinstance(js.get(k), dict):
                cand = js[k]
                break

        qr_data_url = _pick_str(cand, ("base64", "image", "qrcode", "qrCode", "QRCode", "qr", "dataUrl", "dataURL"))
        link_code = _pick_str(cand, ("code", "linkCode", "link", "loginCode"))
        pairing_code = _pick_str(cand, ("pairingCode", "pairing_code", "pin", "code_short"))

        # A veces el "qrcode" es el CODE en texto, no data-url
        if qr_data_url and not qr_data_url.startswith("data:image"):
            # tratamos esto como link_code si no teníamos
            if not link_code:
                link_code = qr_data_url
            qr_data_url = None

        return {
            "qr_data_url": qr_data_url,
            "link_code": link_code,
            "pairing_code": pairing_code,
        }
