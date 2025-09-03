import os, logging, io, base64
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
import qrcode

from wa_evolution import EvolutionClient

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")
PUBLIC_BASE_URL    = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
EVOLUTION_WEBHOOK_TOKEN = os.getenv("EVOLUTION_WEBHOOK_TOKEN", "evolution")

def _qr_data_url_from_code(code: str) -> str:
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

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    if not PUBLIC_BASE_URL:
        raise HTTPException(500, "PUBLIC_BASE_URL no configurado")

    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    sc, _ = evo.create_instance(instance)
    if sc == 401:
        raise HTTPException(502, "Evolution 401 (verifica EVOLUTION_API_KEY)")

    evo.connect(instance)  # dispara QR/pairing

    # Configurar webhook para que Evolution nos avise los mensajes entrantes
    webhook_url = f"{PUBLIC_BASE_URL}/api/wa/webhook?token={EVOLUTION_WEBHOOK_TOKEN}&instance={instance}"
    wsc, wjs = evo.set_webhook(instance, webhook_url)
    if wsc >= 400:
        log.warning("No pude setear webhook %s -> %s %s", webhook_url, wsc, wjs)

    return {"ok": True, "webhook": webhook_url}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    instance = f"brand_{brand_id}"
    evo = EvolutionClient()

    st_code, st = evo.connection_state(instance)
    connected = _is_connected(st)

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    raw_dump: Dict[str, Any] = {}

    if not connected:
        q_code, qj = evo.qr_by_param(instance)
        if q_code == 200 and isinstance(qj, dict):
            for k in ("base64","qr","image","qrcode","dataUrl"):
                val = qj.get(k)
                if isinstance(val, str) and val.startswith("data:image"):
                    qr_data_url = val
                    break

        if not qr_data_url:
            c_code, cj = evo.connect(instance)
            raw_dump = cj or {}
            pairing = cj.get("pairingCode") or cj.get("pairing_code")
            code = cj.get("code") or cj.get("qrcode") or cj.get("qrCode")
            if code:
                try: qr_data_url = _qr_data_url_from_code(code)
                except Exception as e: log.warning("QR local error: %s", e)

    return JSONResponse({
        "connected": connected,
        "qr": qr_data_url,
        "pairingCode": pairing,
        "state": st,
        "raw": raw_dump or {"status": st_code},
    })

@router.post("/test")
def wa_test(body: Dict[str, Any]):
    brand_id = int(body.get("brand_id") or 0)
    to = (body.get("to") or "").strip()
    text = body.get("text") or "Hola desde API"

    if not brand_id or not to:
        raise HTTPException(400, "brand_id y to requeridos")

    evo = EvolutionClient()
    instance = f"brand_{brand_id}"

    stc, st = evo.connection_state(instance)
    if not _is_connected(st):
        stname = (st.get("instance", {}) or {}).get("state", "unknown")
        raise HTTPException(409, f"No conectado (state: {stname})")

    sc, js = evo.send_text(instance, to, text)
    if sc >= 400:
        raise HTTPException(sc, js.get("error") or js.get("message") or f"HTTP {sc}")
    return {"ok": True, "result": js}

@router.post("/reset")
def wa_reset(brand_id: int = Query(...)):
    evo = EvolutionClient()
    instance = f"brand_{brand_id}"
    evo.logout(instance)
    evo.connect(instance)
    return {"ok": True, "reset": True}
