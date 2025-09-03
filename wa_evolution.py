import os, logging, httpx
from typing import Optional, Dict, Any, Tuple

log = logging.getLogger("wa_evolution")

BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
APIKEY = os.getenv("EVOLUTION_API_KEY", "")

def _headers():
    return {"apikey": APIKEY} if APIKEY else {}

class EvolutionClient:
    def __init__(self, base: Optional[str] = None, apikey: Optional[str] = None, timeout: int = 20):
        self.base = (base or BASE).rstrip("/")
        self.apikey = apikey or APIKEY
        self.timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout, headers=_headers())

    # ---------- Instance ----------
    def create_instance(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            r = c.post(f"{self.base}/instance/create", json={"instanceName": instance})
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}

    def connect(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            r = c.get(f"{self.base}/instance/connect/{instance}")
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}

    def connection_state(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            r = c.get(f"{self.base}/instance/connectionState/{instance}")
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}

    def logout(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            r = c.get(f"{self.base}/instance/logout/{instance}")
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}

    def qr_by_param(self, instance: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            r = c.get(f"{self.base}/instance/qr", params={"instanceName": instance})
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}

    # ---------- Webhook por instancia ----------
    def set_webhook(self, instance: str, url: str) -> Tuple[int, Dict[str, Any]]:
        """
        Evolution tiene variantes de endpoint según versión.
        Probamos varias rutas hasta que una funcione.
        """
        payloads = [
            {"url": url},                        # forma más común
            {"webhookUrl": url},
            {"webhook": url},
            {"instanceName": instance, "url": url},
        ]
        paths = [
            f"{self.base}/webhook/set/{instance}",
            f"{self.base}/instance/webhook/set/{instance}",
            f"{self.base}/instance/webhook/{instance}/set",
            f"{self.base}/webhook/set",
        ]
        with self._client() as c:
            last = (599, {})
            for p in paths:
                for body in payloads:
                    try:
                        r = c.post(p, json=body)
                        if r.status_code < 400:
                            try: return r.status_code, r.json()
                            except Exception: return r.status_code, {"ok": True}
                        last = (r.status_code, (r.json() if r.headers.get("content-type","").startswith("application/json") else {"text": r.text}))
                    except Exception as e:
                        last = (599, {"error": str(e)})
            return last

    # ---------- Send message (varias variantes) ----------
    def send_text(self, instance: str, to: str, text: str) -> Tuple[int, Dict[str, Any]]:
        with self._client() as c:
            # A) /message/sendText/{instance} con "text"
            url = f"{self.base}/message/sendText/{instance}"
            r = c.post(url, json={"number": to, "text": text})
            if r.status_code < 400:
                try: return r.status_code, r.json()
                except Exception: return r.status_code, {}

            # B) misma ruta con "textMessage"
            r = c.post(url, json={"number": to, "textMessage": {"text": text}})
            if r.status_code < 400:
                try: return r.status_code, r.json()
                except Exception: return r.status_code, {}

            # C) /message/send/{instance} con textMessage
            url2 = f"{self.base}/message/send/{instance}"
            r = c.post(url2, json={"number": to, "textMessage": {"text": text}})
            try: return r.status_code, r.json()
            except Exception: return r.status_code, {}
