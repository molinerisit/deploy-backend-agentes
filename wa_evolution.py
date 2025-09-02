# backend/wa_evolution.py
import os, logging, httpx
from typing import Tuple
from fastapi import HTTPException

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_KEY  = os.getenv("EVOLUTION_API_KEY", "")

class EvolutionClient:
    def __init__(self):
        if not EVOLUTION_BASE:
            raise RuntimeError("Falta EVOLUTION_BASE_URL")
        if not EVOLUTION_KEY:
            raise RuntimeError("Falta EVOLUTION_API_KEY")
        self.base = EVOLUTION_BASE
        self.key  = EVOLUTION_KEY

    def _header_sets(self):
        return [
            {"Authorization": f"Bearer {self.key}", "Accept": "application/json", "Content-Type": "application/json"},
            {"apikey": self.key, "Accept": "application/json", "Content-Type": "application/json"},
        ]

    def create_instance(self, instance_name: str) -> dict:
        # Probar rutas conocidas
        candidates = [
            ("POST", f"{self.base}/instance/create", {"instanceName": instance_name}),
            ("POST", f"{self.base}/instances",      {"instance": instance_name}),
        ]
        last_err = None
        for headers in self._header_sets():
            for method, url, payload in candidates:
                try:
                    with httpx.Client(timeout=30) as c:
                        r = c.request(method, url, headers=headers, json=payload)
                    if r.status_code < 400:
                        return r.json() if r.headers.get("content-type","").startswith("application/json") else {"ok": True}
                    last_err = (r.status_code, r.text)
                    log.warning("create_instance fallo %s %s -> %s %s", method, url, r.status_code, r.text[:200])
                except Exception as e:
                    last_err = (0, str(e))
                    log.warning("create_instance error %s %s -> %s", method, url, e)
        code, txt = last_err or (502, "Evolution desconocido")
        raise HTTPException(status_code=502, detail=f"Evolution create_instance error ({code}) {txt}")

    def get_qr(self, instance_name: str) -> Tuple[bool, str | None]:
        candidates = [
            ("GET", f"{self.base}/instance/qr", {"instanceName": instance_name}, None),                    # JSON con {connected, qr}
            ("GET", f"{self.base}/instances/{instance_name}/qr", None, "image"),                           # binario o data-url
        ]
        last_err = None
        for headers in self._header_sets():
            for method, url, params, mode in candidates:
                try:
                    with httpx.Client(timeout=30) as c:
                        r = c.request(method, url, headers=headers, params=params)
                    if r.status_code == 404:
                        raise HTTPException(status_code=404, detail="Instancia WA inexistente")
                    if r.status_code >= 400:
                        last_err = (r.status_code, r.text)
                        log.warning("get_qr fallo %s %s -> %s %s", method, url, r.status_code, r.text[:200])
                        continue

                    ct = r.headers.get("content-type","")
                    if mode == "image" or ct.startswith("image/"):
                        # binario -> devolvemos data URL base64 (para que el front lo ponga directo en <img src>)
                        import base64
                        b64 = base64.b64encode(r.content).decode("utf-8")
                        return (False, f"data:{ct or 'image/png'};base64,{b64}")
                    # JSON esperado: {"connected": bool, "qr": "<b64 o data-url>"}
                    data = r.json()
                    connected = bool(data.get("connected"))
                    qr = data.get("qr")
                    # si viene solo base64, lo convertimos en data-url
                    if qr and not str(qr).startswith("data:") and not connected:
                        qr = f"data:image/png;base64,{qr}"
                    return (connected, qr if not connected else None)
                except HTTPException:
                    raise
                except Exception as e:
                    last_err = (0, str(e))
                    log.warning("get_qr error %s %s -> %s", method, url, e)
        code, txt = last_err or (502, "Evolution desconocido")
        raise HTTPException(status_code=502, detail=f"Evolution get_qr error ({code}) {txt}")
