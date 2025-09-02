# backend/wa_evolution.py
import os, logging
from typing import Optional, Dict, Any
import httpx

log = logging.getLogger("wa_evolution")

BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
APIKEY = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

HEAD = {"apikey": APIKEY, "Content-Type": "application/json"}

def _looks_connected(state: Dict[str, Any]) -> bool:
    if not isinstance(state, dict): return False
    def ok(v: Optional[str]) -> bool:
        s = (v or "").strip().lower()
        return s in {"open", "connected", "connected_to_whatsapp", "connectedtowhatsapp"}
    inst = state.get("instance") or {}
    if inst.get("connected") is True: return True
    for k in ("state","status","connectionState"):
        if ok(state.get(k)) or ok(inst.get(k)): return True
    return False

class EvolutionClient:
    def __init__(self, base: Optional[str] = None, apikey: Optional[str] = None):
        self.base = (base or BASE).rstrip("/")
        self.head = {"apikey": apikey or APIKEY, "Content-Type": "application/json"}
        if not self.base or not (apikey or APIKEY):
            raise RuntimeError("EVOLUTION_BASE_URL/API_KEY no configurados")

    # --- Instance management
    def create_instance(self, instance: str, webhook_token: Optional[str] = None) -> Dict[str, Any]:
        webhook_url = f"{PUBLIC_BASE}/api/wa/webhook?token={webhook_token}&instance={instance}" if webhook_token and PUBLIC_BASE else None
        payload = {
            "instanceName": instance,
            "token": (APIKEY),
            "qrcode": True,
            "webhook": webhook_url,
            "webhookEnabled": bool(webhook_url),
        }
        r = httpx.post(f"{self.base}/instance/create", headers=self.head, json=payload, timeout=30)
        if r.status_code == 403 and "already in use" in r.text.lower():
            log.info("Instance %s ya existía", instance)
            return {"ok": True, "existed": True}
        r.raise_for_status()
        return r.json() if "application/json" in r.headers.get("content-type","") else {"ok": True}

    def connect(self, instance: str) -> None:
        try:
            httpx.get(f"{self.base}/instance/connect/{instance}", headers=self.head, timeout=10)
        except Exception:
            pass

    def connection_state(self, instance: str) -> Dict[str, Any]:
        try:
            r = httpx.get(f"{self.base}/instance/connectionState/{instance}", headers=self.head, timeout=20)
            return r.json() if r.status_code == 200 else {}
        except Exception:
            return {}

    def qr_or_code(self, instance: str) -> Dict[str, Any]:
        # intenta QR
        try:
            r = httpx.get(f"{self.base}/instance/qr/{instance}", headers=self.head, timeout=20)
            if r.status_code == 200:
                d = r.json() or {}
                if d.get("qr") or d.get("base64") or d.get("image"): return {"qr": d.get("qr") or d.get("base64") or d.get("image")}
        except Exception:
            pass
        # variante query param
        try:
            r = httpx.get(f"{self.base}/instance/qr", params={"instanceName": instance}, headers=self.head, timeout=20)
            if r.status_code == 200:
                d = r.json() or {}
                if d.get("qr") or d.get("base64") or d.get("image"): return {"qr": d.get("qr") or d.get("base64") or d.get("image")}
        except Exception:
            pass
        # pairing code
        try:
            r = httpx.get(f"{self.base}/instance/pairingCode/{instance}", headers=self.head, timeout=20)
            if r.status_code == 200:
                d = r.json() or {}
                if d.get("pairingCode") or d.get("code"): return {"pairingCode": d.get("pairingCode") or d.get("code")}
        except Exception:
            pass
        return {}

    # --- Messaging
    def send_text(self, instance: str, to_msisdn: str, text: str) -> Dict[str, Any]:
        payload = {"number": to_msisdn, "text": text}
        r = httpx.post(f"{self.base}/message/sendText/{instance}", headers=self.head, json=payload, timeout=30)
        if r.is_error:
            log.warning("send_text -> %s %s", r.status_code, r.text)
            r.raise_for_status()
        return r.json() if "application/json" in r.headers.get("content-type","") else {"ok": True}

    # convenience
    def ensure_ready(self, instance: str, webhook_token: Optional[str]) -> Dict[str, Any]:
        out = self.create_instance(instance, webhook_token=webhook_token)
        self.connect(instance)
        st = self.connection_state(instance)
        if _looks_connected(st): return {"connected": True, "state": st}
        qr = self.qr_or_code(instance)
        return {"connected": False, "state": st, **qr}
