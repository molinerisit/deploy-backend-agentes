import os, requests
from typing import Optional, Tuple

BASE = os.getenv("EVOLUTION_BASE_URL")
KEY  = os.getenv("EVOLUTION_API_KEY")

def _headers():
    return {"apikey": KEY} if KEY else {}

def create_instance(instance_name: str) -> dict:
    r = requests.post(f"{BASE}/instance/create", json={"instanceName": instance_name}, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()

def get_qr(instance_name: str) -> Tuple[bool, Optional[str]]:
    r = requests.get(f"{BASE}/instance/qr/{instance_name}", headers=_headers(), timeout=30)
    if r.status_code == 204:  # conectado
        return (True, None)
    r.raise_for_status()
    j = r.json(); b64 = j.get("qrcode") or j.get("base64")
    if b64 and not b64.startswith("data:image"):
        b64 = "data:image/png;base64," + b64
    return (False, b64)

def send_text(instance_name: str, to_phone: str, message: str) -> dict:
    r = requests.post(f"{BASE}/message/sendText/{instance_name}", json={"number": to_phone, "text": message},
                      headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json()
