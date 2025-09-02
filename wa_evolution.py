# backend/wa_evolution.py
import os, logging, httpx, base64
from io import BytesIO
from typing import Tuple, Optional
from fastapi import HTTPException

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

def _h():
    if not EVOLUTION_KEY: raise RuntimeError("Falta EVOLUTION_API_KEY")
    return {"apikey": EVOLUTION_KEY, "Accept": "application/json", "Content-Type": "application/json"}

def _make_token(instance: str) -> str:
    # simple y suficiente; si querés HMAC, lo cambiamos
    return instance

def _qr_png_from_code(link_code: str) -> Optional[str]:
    try:
        import qrcode
        img = qrcode.make(link_code)
        buf = BytesIO(); img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None  # fallback: devolvemos code/pariringCode en JSON

class EvolutionClient:
    def __init__(self):
        if not EVOLUTION_BASE: raise RuntimeError("Falta EVOLUTION_BASE_URL")
        self.base = EVOLUTION_BASE

    def create_instance(self, instance: str) -> dict:
        url = f"{self.base}/instance/create"
        payload = {
            "instanceName": instance,
            "token": _make_token(instance),
            # clave: en v2 hay que indicar la integración
            "integration": "WHATSAPP-BAILEYS",
            # que te devuelva QR/code de una:
            "qrcode": True,
            # opcional: configurar webhook (si querés recibir eventos)
            # "webhook": { "url": f"{PUBLIC_BASE}/api/wa/webhook", "byEvents": True, "base64": True }
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers=_h(), json=payload)
        if r.status_code >= 400:
            log.warning("create_instance %s -> %s %s", url, r.status_code, r.text[:300])
            raise HTTPException(status_code=502, detail=f"Evolution create_instance error ({r.status_code}) {r.text}")
        return r.json() if "application/json" in r.headers.get("content-type","") else {"ok": True}

    def get_connect(self, instance: str) -> dict:
        """Devuelve dict con connected/pairingCode/code/qr (data URL si podemos)"""
        url = f"{self.base}/instance/connect/{instance}"
        with httpx.Client(timeout=30) as c:
            r = c.get(url, headers=_h())
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Instancia WA inexistente")
        if r.status_code >= 400:
            log.warning("connect %s -> %s %s", url, r.status_code, r.text[:300])
            raise HTTPException(status_code=502, detail=f"Evolution connect error ({r.status_code}) {r.text}")

        data = r.json()
        # docs v2 muestran { pairingCode, code, count }, no 'connected'
        pairing = data.get("pairingCode")
        code    = data.get("code")
        # si vino code, intento convertirlo a PNG base64 para tu <img>
        qr_data_url = _qr_png_from_code(code) if code else None
        return {
            "connected": False,      # hasta que conectes, asumimos false
            "pairingCode": pairing,  # ej: 'WZYEH1YY' (para vinculación por código)
            "code": code,            # string largo que también sirve para QR
            "qr": qr_data_url,       # data:image/png;base64,... si pudimos generarlo
        }
