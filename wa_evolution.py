import os, logging, httpx
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")

def _auth_headers() -> Dict[str, str]:
    # v2.3 usa Bearer en la mayoría de despliegues
    h = {"Accept": "application/json"}
    if EVOLUTION_API_KEY:
        h["Authorization"] = f"Bearer {EVOLUTION_API_KEY}"
        # algunos builds también aceptan X-API-Key; no molesta tener ambos
        h["X-API-Key"] = EVOLUTION_API_KEY
    return h

class EvolutionClient:
    def __init__(self, timeout: float = 20.0):
        if not EVOLUTION_BASE_URL:
            raise RuntimeError("EVOLUTION_BASE_URL no configurado")
        self.base = EVOLUTION_BASE_URL
        self.timeout = timeout

    # -------------------- Instances --------------------

    def fetch_instances(self) -> Dict[str, Any]:
        url = f"{self.base}/instance/fetchInstances"
        r = httpx.get(url, headers=_auth_headers(), timeout=self.timeout)
        return {"http_status": r.status_code, "body": r.json() if r.text else {}}

    def create_instance(self, instance: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """
        v2.3.0: POST /instance/create  body: { instanceName, integration, webhook? }
        integration debe ser "WHATSAPP"
        """
        url = f"{self.base}/instance/create"
        body = {"instanceName": instance, "integration": "WHATSAPP"}
        if webhook_url:
            body["webhook"] = {"url": webhook_url, "events": ["ALL"]}
        r = httpx.post(url, headers=_auth_headers(), json=body, timeout=self.timeout)
        if r.status_code >= 400:
            log.warning("create_instance intento POST /instance/create -> %s %s", r.status_code, r.json() if r.text else r.text)
        return {"http_status": r.status_code, "body": r.json() if r.text else {}}

    def set_webhook(self, instance: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
        """
        v2.3.0: POST /webhook/set  body: { instanceName, url, events }
        (algunas builds legacy usan /instance/setWebhook -> acá ya probamos el nuevo primero)
        """
        tried = []

        def _post(url: str, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
            rr = httpx.post(url, headers=_auth_headers(), json=body, timeout=self.timeout)
            return rr.status_code, rr.json() if rr.text else {}

        # nuevo
        url_new = f"{self.base}/webhook/set"
        code, js = _post(url_new, {"instanceName": instance, "url": webhook_url, "events": ["ALL"]})
        tried.append((url_new, code))
        if code < 400:
            return code, js

        # compat 1
        url_compat = f"{self.base}/instance/setWebhook"
        code2, js2 = _post(url_compat, {"instanceName": instance, "webhook": webhook_url})
        tried.append((url_compat, code2))
        if code2 < 400:
            return code2, js2

        log.warning("set_webhook fallo: %s", tried)
        return code2, js2

    def connect_instance(self, instance: str) -> Dict[str, Any]:
        """
        v2.3.0: GET /instance/connect/{instanceName}
        """
        url = f"{self.base}/instance/connect/{instance}"
        r = httpx.get(url, headers=_auth_headers(), timeout=self.timeout)
        return {"http_status": r.status_code, "body": r.json() if r.text else {}}

    def connection_state(self, instance: str) -> Dict[str, Any]:
        url = f"{self.base}/instance/connectionState/{instance}"
        r = httpx.get(url, headers=_auth_headers(), timeout=self.timeout)
        return {"http_status": r.status_code, "body": r.json() if r.text else {}}

    def qr_by_param(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        """
        Algunas builds exponen:
          - GET /instance/qr?instanceName=brand_1
          - GET /instance/qr/{instanceName}
          - GET /instance/qrbase64?instanceName=brand_1
        Probamos varias rutas y devolvemos la primera 2xx.
        """
        cand = [
            f"{self.base}/instance/qr?instanceName={instance}",
            f"{self.base}/instance/qr/{instance}",
            f"{self.base}/instance/qrbase64?instanceName={instance}",
        ]
        for u in cand:
            r = httpx.get(u, headers=_auth_headers(), timeout=self.timeout)
            if r.status_code < 400:
                try:
                    return r.status_code, (r.json() if r.text else {})
                except Exception:
                    return r.status_code, {}
        return 404, {"error": "QR not found"}

    def delete_instance(self, instance: str) -> None:
        for path in (
            f"/instance/delete/{instance}",
            f"/instance/delete?instanceName={instance}",
            f"/instance/remove/{instance}",
        ):
            try:
                httpx.delete(self.base + path, headers=_auth_headers(), timeout=self.timeout)
            except Exception:
                pass

    # -------------------- Messages --------------------

    def send_text(self, instance: str, number_digits: str, text: str) -> Dict[str, Any]:
        """
        v2.3.0: POST /message/sendText/{instanceName}
          body: { number, text }
        """
        url = f"{self.base}/message/sendText/{instance}"
        body = {"number": number_digits, "text": text}
        r = httpx.post(url, headers=_auth_headers(), json=body, timeout=self.timeout)
        return {"http_status": r.status_code, "body": r.json() if r.text else {}}

    # (No hay list_chats estable en 2.3 — no implementar para evitar 404)
