# backend/routers/channels.py
import os, logging, io, base64
from typing import Optional, Dict, Any, Tuple
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
import qrcode

from wa_evolution import EvolutionClient, EvolutionError

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")

# ---------------- Helpers ----------------

def _headers() -> Dict[str, str]:
    return {"apikey": EVOLUTION_API_KEY} if EVOLUTION_API_KEY else {}

def _qr_data_url_from_code(code: str) -> str:
    """Genera un PNG embebido (data URL) a partir de una cadena (QR)."""
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")

def _is_connected(state_json: Dict[str, Any]) -> bool:
    try:
        s = (state_json or {}).get("instance", {}).get("state", "")
        return s.lower() in ("open", "connected")
    except Exception:
        return False

def _set_webhook(instance: str, url: str) -> Tuple[int, Dict[str, Any]]:
    """
    Intenta setear el webhook en Evolution.
    Algunos builds exponen: POST /webhook/set/{instanceName}  body: { url }
    Si no existe, no fallamos la operación principal.
    """
    if not EVOLUTION_BASE_URL or not EVOLUTION_API_KEY:
        return 500, {"error": "evo env not configured"}

    endpoint = f"{EVOLUTION_BASE_URL}/webhook/set/{instance}"
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(endpoint, headers=_headers(), json={"url": url})
            try:
                data = r.json()
            except Exception:
                data = {"message": r.text}
            return r.status_code, data
    except Exception as e:
        return 599, {"error": str(e)}

def _logout_instance(instance: str) -> Tuple[int, Dict[str, Any]]:
    """
    Cierra la sesión del dispositivo (si el servidor lo soporta).
    Variantes comunes:
      - POST /instance/logout/{instanceName}
    """
    if not EVOLUTION_BASE_URL or not EVOLUTION_API_KEY:
        return 500, {"error": "evo env not configured"}
    url = f"{EVOLUTION_BASE_URL}/instance/logout/{instance}"
    try:
        with httpx.Client(timeout=20) as c:
            r = c.post(url, headers=_headers())
            try:
                data = r.json()
            except Exception:
                data = {"message": r.text}
            return r.status_code, data
    except Exception as e:
        return 599, {"error": str(e)}

# ---------------- Endpoints ----------------

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    """
    Crea (si no existe) y conecta la instancia brand_{id}. También configura el webhook.
    """
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    # Crear (idempotente: nuestro wrapper tolera 'ya existe')
    try:
        evo.create_instance(instance)
    except EvolutionError as e:
        # Si tu servidor devuelve 403 por 'already exists', el wrapper ya lo maneja
        raise HTTPException(502, f"Evolution create_instance: {e}")

    # Conectar (dispara pairing/qr)
    try:
        evo.connect_instance(instance)
    except EvolutionError as e:
        log.warning("connect_instance fallo: %s", e)

    # Setear webhook (no bloqueante)
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    wsc, wjs = _set_webhook(instance, webhook_url)
    if wsc >= 400:
        log.warning("No pude setear webhook %s -> %s %s", webhook_url, wsc, wjs)

    return {"ok": True, "webhook": webhook_url}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    """
    Devuelve estado, un dataURL de QR si está disponible, y/o pairingCode si corresponde.
    Soporta tanto /instance/qr?instanceName=... como /instance/qr/{instanceName} (via wa_evolution.get_qr).
    """
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    # Estado
    try:
        st = evo.connection_state(instance)
    except EvolutionError as e:
        st = {"error": str(e)}
    connected = _is_connected(st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        try:
            out = evo.get_qr(instance)  # dict con posibles keys: qr/base64/code/pairingCode/state...
            raw_dump = out or {}
            # priorizamos dataURL listo
            candidates = [
                out.get("dataUrl"),
                out.get("image"),
                out.get("qr"),
                out.get("qrcode"),
                out.get("base64"),
            ] if isinstance(out, dict) else []
            for val in candidates:
                if isinstance(val, str) and val:
                    if val.startswith("data:image"):
                        qr_data_url = val
                    else:
                        # si nos dieron base64 crudo, intentamos armar data URL
                        try:
                            # pequeña heurística: si es base64 válido sin encabezado
                            base64.b64decode(val, validate=True)
                            qr_data_url = "data:image/png;base64," + val
                        except Exception:
                            # si es una cadena estándar (e.g., "XXXX,YYYY,ZZZ"), igual generamos QR
                            qr_data_url = _qr_data_url_from_code(val)
                    if qr_data_url:
                        break
            # pairing codes o "code" plano -> lo devolvemos (y además generamos QR para mostrar por cámara)
            pairing = out.get("pairingCode") or out.get("pairing_code")
            code = out.get("code") or out.get("qrCode") or out.get("qrcode")
            if not qr_data_url and isinstance(code, str) and code:
                try:
                    qr_data_url = _qr_data_url_from_code(code)
                except Exception as e:
                    log.warning("QR local error: %s", e)
        except EvolutionError as e:
            raw_dump = {"error": str(e)}

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump,
    })

@router.post("/test")
def wa_test(body: Dict[str, Any]):
    """
    Envía un texto al número indicado (msisdn sin '+', con código de país).
    """
    brand_id = int(body.get("brand_id") or 0)
    to = (body.get("to") or "").strip()
    text = body.get("text") or "Hola desde API"

    if not brand_id or not to:
        raise HTTPException(400, "brand_id y to requeridos")

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    # Verificar conexión
    try:
        st = evo.connection_state(instance)
    except EvolutionError as e:
        raise HTTPException(502, f"Evolution connection_state: {e}")

    if not _is_connected(st):
        stname = (st.get("instance", {}) or {}).get("state", "unknown")
        raise HTTPException(409, f"No conectado (state: {stname})")

    # Enviar
    try:
        js = evo.send_text(instance, to, text)  # dict
    except EvolutionError as e:
        raise HTTPException(502, f"Evolution send_text: {e}")

    return {"ok": True, "result": js}

@router.post("/reset")
def wa_reset(brand_id: int = Query(...)):
    """
    Intenta hacer logout de la sesión y reconectar para forzar nuevo QR.
    """
    instance = f"brand_{brand_id}"

    sc, js = _logout_instance(instance)
    if sc >= 400 and sc != 404:
        # si 404, probablemente el endpoint no esté soportado: seguimos igual
        log.warning("logout %s -> %s %s", instance, sc, js)

    evo = EvolutionClient()
    try:
        evo.connect_instance(instance)
    except EvolutionError as e:
        log.warning("connect_instance tras reset fallo: %s", e)

    return {"ok": True, "reset": True, "logout_status": sc, "logout_result": js}
