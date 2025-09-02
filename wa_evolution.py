# backend/wa_evolution.py
import os, logging
from typing import Tuple, Optional, Dict, Any
import httpx

log = logging.getLogger("wa_evolution")

BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
APIKEY = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")

HEAD = {
    "apikey": APIKEY,
    "Content-Type": "application/json",
}

def _is_connected_state(state: Dict[str, Any]) -> bool:
    """
    Interpreta la respuesta de Evolution para saber si la sesión está conectada.
    Acepta múltiples variantes: state/status/connectionState en el root o dentro de 'instance'.
    """
    if not isinstance(state, dict):
        return False

    def ok(v: Optional[str]) -> bool:
        s = (v or "").strip().lower()
        return s in {"open", "connected", "connected_to_whatsapp", "connectedtowhatsapp"}

    # chequear root
    for key in ("state", "status", "connectionState"):
        if ok(state.get(key)):
            return True

    inst = state.get("instance") or {}
    # booleano explícito
    if inst.get("connected") is True:
        return True

    for key in ("state", "status", "connectionState"):
        if ok(inst.get(key)):
            return True

    return False

def create_instance(instance: str) -> None:
    """
    Crea la instancia si no existe y setea el webhook. Si ya existe, no rompe.
    """
    if not BASE or not APIKEY:
        raise RuntimeError("EVOLUTION_BASE_URL/API_KEY no configurados")

    # A veces Evolution requiere tener el webhook listo desde la creación
    webhook_url = f"{PUBLIC_BASE}/api/wa/webhook?token={WEBHOOK_TOKEN}" if PUBLIC_BASE else None
    payload = {
        "instanceName": instance,
        # muchas instalaciones aceptan 'token' y/o 'qrcode' y 'webhook'
        "token": APIKEY,
        "qrcode": True,
        "webhook": webhook_url,
        "webhookEnabled": bool(webhook_url),
    }

    r = httpx.post(f"{BASE}/instance/create", headers=HEAD, json=payload, timeout=30)
    if r.status_code == 403 and "already in use" in r.text.lower():
        log.info("Instance %s ya existía; seguimos.", instance)
    elif r.is_error:
        log.warning("create_instance %s -> %s %s", r.request.url, r.status_code, r.text)
        r.raise_for_status()

    # "despertar"/asegurar connect
    try:
        httpx.get(f"{BASE}/instance/connect/{instance}", headers=HEAD, timeout=15)
    except Exception:
        pass

def _connection_state(instance: str) -> Dict[str, Any]:
    try:
        r = httpx.get(f"{BASE}/instance/connectionState/{instance}", headers=HEAD, timeout=20)
        if r.is_error:
            return {}
        return r.json() or {}
    except Exception:
        return {}

def _try_qr(instance: str) -> Optional[str]:
    """
    Devuelve dataURL del QR si existe (cuando la sesión requiere escaneo).
    Evolution puede exponerlo con dos rutas distintas según versión:
    - /instance/qr/{instance}
    - /instance/qr?instanceName={instance}
    """
    # 1) /instance/qr/{instance}
    try:
        r1 = httpx.get(f"{BASE}/instance/qr/{instance}", headers=HEAD, timeout=20)
        if r1.status_code == 200:
            data = r1.json() or {}
            # algunos devuelven {"qr":"data:image/png;base64,...."} otros {"base64": "..."}
            return data.get("qr") or data.get("base64") or data.get("image") or data.get("imageUrl")
    except Exception:
        pass

    # 2) /instance/qr?instanceName={instance}
    try:
        r2 = httpx.get(f"{BASE}/instance/qr", params={"instanceName": instance}, headers=HEAD, timeout=20)
        if r2.status_code == 200:
            data = r2.json() or {}
            return data.get("qr") or data.get("base64") or data.get("image") or data.get("imageUrl")
    except Exception:
        pass

    return None

def _try_pairing_code(instance: str) -> Optional[str]:
    """
    Algunas instalaciones permiten pairing code en vez de QR.
    """
    try:
        r = httpx.get(f"{BASE}/instance/pairingCode/{instance}", headers=HEAD, timeout=20)
        if r.status_code == 200:
            data = r.json() or {}
            # suele venir como {"pairingCode": "123-456"}
            return data.get("pairingCode") or data.get("code")
    except Exception:
        pass
    return None

def get_qr(instance: str) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
    """
    Devuelve: (connected, qr_dataurl, pairing_code, raw_state)
    - Si está conectado: (True, None, None, state)
    - Si NO está conectado: intenta QR o pairing code.
    """
    # "connect ping" (no rompe si falla)
    try:
        httpx.get(f"{BASE}/instance/connect/{instance}", headers=HEAD, timeout=10)
    except Exception:
        pass

    state = _connection_state(instance)
    if _is_connected_state(state):
        return True, None, None, state

    qr = _try_qr(instance)
    if qr:
        return False, qr, None, state

    code = _try_pairing_code(instance)
    return False, None, code, state

def send_text(instance: str, to_msisdn: str, text: str) -> Dict[str, Any]:
    """
    Envía texto. Path típico Evolution:
    POST /message/sendText/{instance}  body: {"number":"549351...", "text":"..."}
    """
    payload = {"number": to_msisdn, "text": text}
    r = httpx.post(f"{BASE}/message/sendText/{instance}", headers=HEAD, json=payload, timeout=30)
    if r.is_error:
        log.warning("send_text %s -> %s %s", r.request.url, r.status_code, r.text)
        r.raise_for_status()
    return r.json() if r.headers.get("content-type","").startswith("application/json") else {"ok": True}
