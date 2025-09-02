# backend/wa_evolution.py
import os
import logging
from typing import Optional, Dict, Any
import httpx

log = logging.getLogger("wa_evolution")

EVO_BASE = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVO_KEY  = os.getenv("EVOLUTION_API_KEY", "")

class EvolutionError(Exception):
    pass

def _headers() -> Dict[str, str]:
    return {"apikey": EVO_KEY} if EVO_KEY else {}

def _must_cfg():
    if not EVO_BASE or not EVO_KEY:
        raise EvolutionError("EVOLUTION_BASE_URL/EVOLUTION_API_KEY no configurados")

# -------------------- Funciones de alto nivel --------------------

def create_instance(instance_name: str) -> Dict[str, Any]:
    """
    Crea la instancia si no existe. Si ya existe, lo toma como OK.
    POST /instance/create  { instanceName }
    """
    _must_cfg()
    url = f"{EVO_BASE}/instance/create"
    payload = {"instanceName": instance_name}
    with httpx.Client(timeout=20) as c:
        r = c.post(url, headers=_headers(), json=payload)

    # Cuando ya existe suele devolver 403 con mensaje "already in use"
    if r.status_code == 403 and "already" in r.text.lower():
        log.info("Instance %s ya existía; seguimos.", instance_name)
        return {"ok": True, "status": 403, "alreadyExists": True}

    if r.status_code >= 400:
        log.warning("create_instance %s -> %s %s", url, r.status_code, r.text)
        raise EvolutionError(f"create_instance error ({r.status_code}) {r.text}")

    try:
        return r.json()
    except Exception:
        return {"ok": True}

def connect_instance(instance_name: str) -> Dict[str, Any]:
    """
    GET /instance/connect/{instanceName}
    (usado para disparar pairing/qr y reconectar)
    """
    _must_cfg()
    url = f"{EVO_BASE}/instance/connect/{instance_name}"
    with httpx.Client(timeout=20) as c:
        r = c.get(url, headers=_headers())
    if r.status_code >= 400:
        log.warning("connect_instance %s -> %s %s", url, r.status_code, r.text)
        raise EvolutionError(f"connect_instance error ({r.status_code}) {r.text}")
    try:
        return r.json()
    except Exception:
        return {"ok": r.status_code < 400}

def connection_state(instance_name: str) -> Dict[str, Any]:
    """
    GET /instance/connectionState/{instanceName}
    """
    _must_cfg()
    url = f"{EVO_BASE}/instance/connectionState/{instance_name}"
    with httpx.Client(timeout=15) as c:
        r = c.get(url, headers=_headers())
    if r.status_code >= 400:
        return {"state": "unknown", "status": r.status_code}
    try:
        return r.json()
    except Exception:
        return {"state": "unknown"}

def get_qr(instance_name: str) -> Dict[str, Any]:
    """
    Intenta obtener QR/pairing. Hay instancias que exponen:
      - GET /instance/qr?instanceName=...
      - GET /instance/qr/{instanceName}  (fallback)
    Devolvemos un dict con lo que haya: code | pairingCode | qr | qrcode | connected | state
    """
    _must_cfg()
    out: Dict[str, Any] = {"connected": False}

    with httpx.Client(timeout=15) as c:
        # Estado
        try:
            rs = c.get(f"{EVO_BASE}/instance/connectionState/{instance_name}", headers=_headers())
            try:
                out["state"] = rs.json()
            except Exception:
                out["state"] = {"state": "unknown"}
        except Exception as e:
            out["state"] = {"state": f"error: {e}"}

        # QR por query param
        r = c.get(f"{EVO_BASE}/instance/qr", headers=_headers(), params={"instanceName": instance_name})
        if r.status_code == 404:
            # fallback por path param
            r = c.get(f"{EVO_BASE}/instance/qr/{instance_name}", headers=_headers())

        try:
            jq = r.json()
            if isinstance(jq, dict):
                out.update(jq)
        except Exception:
            # puede venir texto/PNG/ASCII; lo exponemos crudo
            out["qr"] = r.text

        # algunos servidores retornan pairing info en /connect
        try:
            rc = c.get(f"{EVO_BASE}/instance/connect/{instance_name}", headers=_headers())
            if rc.status_code < 400:
                j2 = rc.json()
                if isinstance(j2, dict):
                    for k in ("code", "pairingCode", "qrcode", "qr"):
                        if k in j2:
                            out[k] = j2[k]
        except Exception:
            pass

    return out

def send_text(instance_name: str, number: str, text: str) -> Dict[str, Any]:
    """
    POST /message/sendText/{instanceName}
    Body: { number, text }
    """
    _must_cfg()
    url = f"{EVO_BASE}/message/sendText/{instance_name}"
    with httpx.Client(timeout=15) as c:
        r = c.post(url, headers=_headers(), json={"number": number, "text": text})
    if r.status_code >= 400:
        log.warning("send_text %s -> %s %s", url, r.status_code, r.text)
        raise EvolutionError(f"send_text error ({r.status_code}) {r.text}")
    try:
        return r.json()
    except Exception:
        return {"ok": True}

# -------------------- Cliente OO (compatibilidad) --------------------

class EvolutionClient:
    def create_instance(self, name: str) -> Dict[str, Any]:
        return create_instance(name)

    def connect_instance(self, name: str) -> Dict[str, Any]:
        return connect_instance(name)

    def get_qr(self, name: str) -> Dict[str, Any]:
        return get_qr(name)

    def connection_state(self, name: str) -> Dict[str, Any]:
        return connection_state(name)

    def send_text(self, name: str, number: str, text: str) -> Dict[str, Any]:
        return send_text(name, number, text)
