# backend/wa_evolution.py
import os
import logging
from typing import Tuple, Optional
import httpx
from fastapi import HTTPException

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_KEY  = os.getenv("EVOLUTION_API_KEY", "")

# Opcional: podés definir un secreto para derivar tokens por instancia.
# Si no está, usamos el propio nombre de instancia como token (común en Evolution).
EVOLUTION_INSTANCE_TOKEN_SECRET = os.getenv("EVOLUTION_INSTANCE_TOKEN_SECRET", None)

def _make_instance_token(instance_name: str) -> str:
    if EVOLUTION_INSTANCE_TOKEN_SECRET:
        # token determinístico pero "bonito": primeros 24 chars hex
        import hmac, hashlib
        d = hmac.new(
            EVOLUTION_INSTANCE_TOKEN_SECRET.encode("utf-8"),
            instance_name.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return d[:24]
    # fallback simple y compatible con muchos deployments
    return instance_name

def _headers_apikey() -> dict:
    return {
        "apikey": EVOLUTION_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

def _headers_bearer() -> dict:
    return {
        "Authorization": f"Bearer {EVOLUTION_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

class EvolutionClient:
    def __init__(self):
        if not EVOLUTION_BASE:
            raise RuntimeError("Falta EVOLUTION_BASE_URL")
        if not EVOLUTION_KEY:
            raise RuntimeError("Falta EVOLUTION_API_KEY")
        self.base = EVOLUTION_BASE

    def _try_requests(self, attempts):
        last = None
        for headers, method, url, kwargs in attempts:
            try:
                with httpx.Client(timeout=30) as c:
                    r = c.request(method, url, headers=headers, **kwargs)
                if r.status_code < 400:
                    return r
                last = (r.status_code, r.text)
                log.warning("Evolution %s %s -> %s %s", method, url, r.status_code, (r.text or "")[:300])
            except Exception as e:
                last = (0, str(e))
                log.warning("Evolution %s %s -> ex: %s", method, url, e)
        return last  # tuple (code, text)

    def create_instance(self, instance_name: str) -> dict:
        """
        Tu Evolution (según logs) expone:
          - POST /instance/create    (NO /instances)
        Y devuelve 400 si falta 'token'. Por eso lo incluimos.
        """
        token = _make_instance_token(instance_name)

        attempts = []
        # 1) apikey (más común)
        attempts.append((
            _headers_apikey(), "POST", f"{self.base}/instance/create",
            {"json": {"instanceName": instance_name, "token": token}}
        ))
        # 2) bearer (algunos servers usan bearer)
        attempts.append((
            _headers_bearer(), "POST", f"{self.base}/instance/create",
            {"json": {"instanceName": instance_name, "token": token}}
        ))
        # 3) apikey sin token (por si tu server no lo pide – menos probable)
        attempts.append((
            _headers_apikey(), "POST", f"{self.base}/instance/create",
            {"json": {"instanceName": instance_name}}
        ))

        r = self._try_requests(attempts)
        if isinstance(r, tuple):
            code, txt = r
            raise HTTPException(status_code=502, detail=f"Evolution create_instance error ({code}) {txt}")

        # ok
        ct = r.headers.get("content-type", "")
        return r.json() if "application/json" in ct else {"ok": True}

    def get_qr(self, instance_name: str) -> Tuple[bool, Optional[str]]:
        """
        En tu server, el path con parámetro /instance/qr/brand_1 dio 404,
        así que usamos el formato de **query** que suele ser el correcto:
          GET /instance/qr?instanceName=brand_1
        Si tu server devolviera imagen binaria, lo convertimos a data URL.
        Si devuelve JSON {connected, qr}, lo normalizamos a data URL si hace falta.
        """
        attempts = []
        params = {"instanceName": instance_name}

        # 1) apikey con query
        attempts.append((_headers_apikey(), "GET", f"{self.base}/instance/qr", {"params": params}))
        # 2) bearer con query
        attempts.append((_headers_bearer(), "GET", f"{self.base}/instance/qr", {"params": params}))
        # 3) (fallback) path-style por si existiera /instance/qr/{name}
        attempts.append((_headers_apikey(), "GET", f"{self.base}/instance/qr/{instance_name}", {"params": None}))

        r = self._try_requests(attempts)
        if isinstance(r, tuple):
            code, txt = r
            if code == 404:
                raise HTTPException(status_code=404, detail="Instancia WA inexistente")
            raise HTTPException(status_code=502, detail=f"Evolution get_qr error ({code}) {txt}")

        ct = r.headers.get("content-type", "")
        if ct.startswith("image/"):
            import base64
            b64 = base64.b64encode(r.content).decode("utf-8")
            return False, f"data:{ct};base64,{b64}"

        data = r.json()
        connected = bool(data.get("connected"))
        qr = data.get("qr")
        # normalizar a data-url si viene base64 pelado
        if qr and not str(qr).startswith("data:") and not connected:
            qr = f"data:image/png;base64,{qr}"
        return connected, (None if connected else qr)
