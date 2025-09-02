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
    return instance  # simple; si querés HMAC, lo cambiamos

def _qr_png_from_code(link_code: str) -> Optional[str]:
    try:
        import qrcode
        img = qrcode.make(link_code)
        buf = BytesIO(); img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None

class EvolutionClient:
    def __init__(self):
        if not EVOLUTION_BASE: raise RuntimeError("Falta EVOLUTION_BASE_URL")
        self.base = EVOLUTION_BASE

    def create_instance(self, instance: str) -> dict:
        url = f"{self.base}/instance/create"
        payload = {
            "instanceName": instance,
            "token": _make_token(instance),
            "integration": "WHATSAPP-BAILEYS",
            "qrcode": True,
        }
        with httpx.Client(timeout=30) as c:
            r = c.post(url, headers=_h(), json=payload)
        # ✅ si ya existe, lo tratamos como OK
        if r.status_code == 403 and 'already in use' in (r.text or '').lower():
            log.info("Instance %s ya existía; seguimos.", instance)
            return {"ok": True, "detail": "already exists"}
        if r.status_code >= 400:
            log.warning("create_instance %s -> %s %s", url, r.status_code, r.text[:300])
            raise HTTPException(status_code=502, detail=f"Evolution create_instance error ({r.status_code}) {r.text}")
        return r.json() if "application/json" in r.headers.get("content-type","") else {"ok": True}

    def _try_qr_endpoint(self, instance: str) -> Optional[str]:
        """
        Intenta obtener un QR (imagen) desde /instance/qr?instanceName=...
        Devuelve data URL o None si no hay QR todavía.
        """
        url = f"{self.base}/instance/qr"
        params = {"instanceName": instance}
        with httpx.Client(timeout=30) as c:
            r = c.get(url, headers=_h(), params=params)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            log.warning("instance/qr %s -> %s %s", url, r.status_code, r.text[:200])
            return None

        ct = r.headers.get("content-type","")
        if ct.startswith("image/"):
            b64 = base64.b64encode(r.content).decode("utf-8")
            return f"data:{ct};base64,{b64}"
        # algunos devuelven JSON { qr: "<b64>" } o similar
        try:
            data = r.json()
            b64 = data.get("qr") or data.get("image") or data.get("qrcode")
            if isinstance(b64, str):
                if b64.startswith("data:"):
                    return b64
                return f"data:image/png;base64,{b64}"
        except Exception:
            pass
        return None

    def get_connect(self, instance: str) -> dict:
        """
        1) GET /instance/connect/{instance} → intenta traer {pairingCode, code}
        2) Si no trae nada usable, usamos /instance/qr?instanceName=... para tener una imagen.
        """
        url = f"{self.base}/instance/connect/{instance}"
        with httpx.Client(timeout=30) as c:
            r = c.get(url, headers=_h())
        if r.status_code == 404:
            raise HTTPException(status_code=404, detail="Instancia WA inexistente")
        if r.status_code >= 400:
            log.warning("connect %s -> %s %s", url, r.status_code, r.text[:300])
            raise HTTPException(status_code=502, detail=f"Evolution connect error ({r.status_code}) {r.text}")

        data = r.json()
        pairing = data.get("pairingCode")
        code    = data.get("code")

        # Intentamos generar PNG local si vino "code"
        qr_data_url = None
        if code:
            qr_data_url = _qr_png_from_code(code)

        # Si no hay code/pairing o no pudimos generar PNG, probamos el endpoint de QR
        if not qr_data_url and not pairing and not code:
            qr_data_url = self._try_qr_endpoint(instance)

        # Si igual no hay nada, devolvemos lo que haya (el front mostrará "esperando...")
        return {
            "connected": False,
            "pairingCode": pairing,
            "code": code,
            "qr": qr_data_url,  # data URL si pudimos obtener/crear
        }
