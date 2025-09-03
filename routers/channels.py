import os
import logging
import io
import base64
from typing import Optional, Dict, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import qrcode

from wa_evolution import EvolutionClient  # ← usamos sólo lo que tu wrapper expone

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")


def _qr_data_url_from_code(code: str) -> str:
    """Genera una imagen PNG (dataURL) a partir del string de pairing/QR code."""
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("utf-8")


def _is_connected(state_json: Dict[str, Any]) -> bool:
    """Devuelve True si la instancia está conectada según el JSON de Evolution."""
    try:
        s = (state_json or {}).get("instance", {}).get("state", "")
        return s.lower() in ("open", "connected")
    except Exception:
        return False


def _digits(x: str) -> str:
    return "".join(ch for ch in (x or "") if ch.isdigit())


@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    """
    Crea (si hace falta) y conecta la instancia en Evolution.
    Devuelve la URL del webhook que debes configurar en Evolution.
    """
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    # Crear si no existe (tu wrapper ya maneja 'ya existía' como OK)
    try:
        evo.create_instance(instance)
    except Exception as e:
        # Si falla por algo no-reintentable, lo dejamos logueado y seguimos
        log.info("Evolution create_instance(%s): %s", instance, e)

    # Conectar (dispara pairing/QR en Evolution)
    try:
        evo.connect_instance(instance)
    except Exception as e:
        log.warning("Evolution connect_instance(%s) error: %s", instance, e)

    # URL de webhook que Evolution debe llamar para entrantes
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"

    # Nota: si tu build de Evolution no soporta setear webhook por API, hacelo manual
    # en el panel o con Postman usando la URL anterior.

    return {"ok": True, "webhook": webhook_url}


@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    """
    Devuelve estado de conexión y, si no está conectado, un QR (dataURL) o pairingCode cuando esté disponible.
    """
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    # Estado
    try:
        st = evo.connection_state(instance)  # dict
    except Exception as e:
        raise HTTPException(502, f"Evolution connection_state error: {e}")

    connected = _is_connected(st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        # Intentamos obtener cualquier variante de QR/pairing que exponga tu build
        try:
            out = evo.get_qr(instance)  # dict con posibles claves: code, pairingCode, qr, base64, qrcode, dataUrl, etc.
        except Exception as e:
            out = {"error": str(e)}

        raw_dump = out or {}

        pairing = out.get("pairingCode") or out.get("pairing_code")

        # 1) Si viene dataURL directo, lo usamos
        data_candidates = [
            out.get("base64"), out.get("image"), out.get("qrcode"),
            out.get("dataUrl"), out.get("qr")
        ]
        first_data = next((x for x in data_candidates if isinstance(x, str) and x.startswith("data:image")), None)
        if first_data:
            qr_data_url = first_data
        else:
            # 2) Si viene un 'code' de pairing/qr en texto, generamos nosotros el PNG
            code = out.get("code") or out.get("qrCode") or out.get("qrcode") or out.get("pairing")
            if isinstance(code, str) and code:
                try:
                    qr_data_url = _qr_data_url_from_code(code)
                except Exception as e:
                    log.warning("QR local error: %s", e)

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
    Envío de prueba saliente para verificar que el camino de salida funciona
    independientemente de que el webhook entrante esté configurado o no.
    """
    brand_id = int(body.get("brand_id") or 0)
    to = _digits(body.get("to") or "")
    text = body.get("text") or "Hola desde API"

    if not brand_id or not to:
        raise HTTPException(400, "brand_id y to requeridos")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    try:
        st = evo.connection_state(instance)
    except Exception as e:
        raise HTTPException(502, f"Evolution connection_state error: {e}")

    if not _is_connected(st):
        stname = (st.get("instance", {}) or {}).get("state", "unknown")
        raise HTTPException(409, f"No conectado (state: {stname})")

    try:
        evo.send_text(instance, to, text)
    except Exception as e:
        raise HTTPException(502, f"Evolution send_text error: {e}")

    return {"ok": True}
