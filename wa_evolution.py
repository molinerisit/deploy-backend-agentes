# backend/wa_evolution.py
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
        # Evolution 2.x usa X-API-KEY
        h["X-API-KEY"] = EVOLUTION_API_KEY
    return h

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

    # ---------------- Core helpers ----------------
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
        # v2.3 suele exponer /instance/fetchInstances si AUTHENTICATION_EXPOSE_IN_FETCH_INSTANCES=true
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
        inst_names = set()
        for it in items:
            name = it.get("instanceName") or it.get("name") or it.get("instance") or it.get("id")
            if isinstance(name, str):
                inst_names.add(name)
        return instance in inst_names

    def create_instance(self, instance: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Intenta varias rutas/payloads de Evolution 2.3
        """
        payloads = []
        # variante A (más común)
        payloads.append(("/instance/create", {"instanceName": instance, "webhook": webhook_url}))
        # variante B (algunas builds)
        payloads.append(("/instance/create", {"name": instance, "webhookUrl": webhook_url}))
        # variante C (sin webhook en create, se setea después)
        payloads.append(("/instance/create", {"instanceName": instance}))

        for path, body in payloads:
            resp = self._post(path, json=body)
            if _ok(resp["http_status"]):
                return resp
            log.warning("create_instance intento %s -> %s %s", path, resp["http_status"], resp["body"])
        return resp  # último intento fallido

    def set_webhook(self, instance: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
        """
        Variantes conocidas en 2.3
        """
        tries = [
            ("/instance/webhook/set", {"instanceName": instance, "webhook": webhook_url}, None),
            (f"/instance/setWebhook/{instance}", {"webhook": webhook_url}, None),
            ("/instance/setWebhook", {"instanceName": instance, "webhook": webhook_url}, None),
        ]
        for path, body, params in tries:
            resp = self._post(path, json=body, params=params)
            if _ok(resp["http_status"]):
                return resp["http_status"], resp["body"]
            log.warning("set_webhook intento %s -> %s %s", path, resp["http_status"], resp["body"])
        return resp["http_status"], resp["body"]

    def connect_instance(self, instance: str) -> Dict[str, Any]:
        # v2.3 estable
        return self._get(f"/instance/connect/{instance}")

    def connection_state(self, instance: str) -> Dict[str, Any]:
        return self._get(f"/instance/connectionState/{instance}")

    def delete_instance(self, instance: str) -> Dict[str, Any]:
        # no todas las builds tienen DELETE; algunas usan POST
        resp = self._post("/instance/delete", json={"instanceName": instance})
        if _ok(resp["http_status"]):
            return resp
        # fallback GET
        return self._get(f"/instance/delete/{instance}")

    # ---------------- QR / Pairing ----------------
    def qr_by_param(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        # intentamos dos variantes
        r1 = self._get("/instance/qr", params={"instanceName": instance})
        if _ok(r1["http_status"]):
            return r1["http_status"], r1["body"]
        r2 = self._get(f"/instance/qr/{instance}")
        return r2["http_status"], r2["body"]

    # ---------------- Chats / Messages ----------------
    def list_chats(self, instance: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
        """
        Muchas rutas de 'chat list' ya no existen en 2.3 (404).
        Devolvemos 500 'No endpoint matched' para que el caller sepa que debe degradar a DB local.
        """
        # Intentos conservadores (por si tu build los expone)
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
        """
        Similar: varias builds no exponen lectura de historial.
        """
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
        """
        Este endpoint sí está estable en 2.3 (y ya te devolvió 201).
        """
        payload = {"number": str(to_number), "text": str(text)}
        return self._post(f"/message/sendText/{instance}", json=payload)

    # ---------------- Orquestador para /start ----------------
    def ensure_started(self, instance: str, webhook_url: Optional[str]) -> Dict[str, Any]:
        """
        - Si existe, intenta setear webhook (best-effort) y conectar.
        - Si no existe, crea, setea webhook y conecta.
        """
        exists = self.instance_exists(instance)
        if not exists:
            cr = self.create_instance(instance, webhook_url)
            if not _ok(cr["http_status"]):
                return {"http_status": cr["http_status"], "body": {"error": "create_failed", "detail": cr["body"]}}
        if webhook_url:
            sc, wjs = self.set_webhook(instance, webhook_url)
            # no abortamos por webhook 4xx; solo log
            if sc >= 400:
                log.warning("ensure_started: set_webhook fallo (%s): %s", sc, wjs)
        conn = self.connect_instance(instance)
        return conn
