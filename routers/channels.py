# backend/routers/channels.py
import os, logging, io, base64
from typing import Optional, Tuple, Dict, Any
import httpx
from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session  # solo si necesitás DB
from db import get_session     # idem
import qrcode

log = logging.getLogger("channels")
router = APIRouter(prefix="/api/wa", tags=["wa"])

EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "").rstrip("/")
EVOLUTION_API_KEY  = os.getenv("EVOLUTION_API_KEY", "")

HEADERS = {"apikey": EVOLUTION_API_KEY} if EVOLUTION_API_KEY else {}

def _qr_data_url_from_code(code: str) -> str:
    # Genera PNG en memoria desde el string del QR (Baileys: ref,pubKey,clientId,secret)
    img = qrcode.make(code)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"

def _is_connected(state_json: Dict[str, Any]) -> bool:
    try:
        s = (state_json or {}).get("instance", {}).get("state", "")
        return s.lower() in ("open", "connected")
    except Exception:
        return False

def _get_json(client: httpx.Client, path: str, *, params: Dict[str, Any] | None=None) -> Tuple[int, Dict[str, Any]]:
    url = f"{EVOLUTION_BASE_URL}{path}"
    r = client.get(url, headers=HEADERS, params=params, timeout=15)
    try:
        data = r.json()
    except Exception:
        data = {}
    return r.status_code, data

@router.post("/start")
def wa_start(brand_id: int = Query(...)):
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    instance = f"brand_{brand_id}"
    with httpx.Client() as c:
        # create (si ya existe, Evolution devuelve 403 y seguimos)
        url_create = f"{EVOLUTION_BASE_URL}/instance/create"
        payload = {"instanceName": instance}
        r = c.post(url_create, headers=HEADERS, json=payload, timeout=20)
        if r.status_code == 401:
            raise HTTPException(502, f"Evolution auth 401 (verifica API key)")
        if r.status_code not in (200, 201, 202, 204, 400, 403):
            log.warning("create_instance %s -> %s %s", url_create, r.status_code, r.text)
        # conectar siempre (dispara/refresh del QR interno)
        url_conn = f"{EVOLUTION_BASE_URL}/instance/connect/{instance}"
        rc = c.get(url_conn, headers=HEADERS, timeout=20)
        log.info("Evolution connect %s -> %s", url_conn, rc.status_code)
    return {"ok": True}

@router.get("/qr")
def wa_qr(brand_id: int = Query(...)):
    """
    Devuelve:
    {
      connected: bool,
      qr: data-url PNG | null,
      pairingCode: string | null (solo si Evolution lo provee explícitamente),
      state: {...},
      raw: {...} // útil para debug
    }
    """
    if not EVOLUTION_BASE_URL:
        raise HTTPException(500, "EVOLUTION_BASE_URL no configurado")
    instance = f"brand_{brand_id}"

    qr_data_url: Optional[str] = None
    pairing: Optional[str] = None
    state_json: Dict[str, Any] = {}
    raw_dump: Dict[str, Any] = {}

    with httpx.Client() as c:
        # 1) Estado de conexión
        st_code, st = _get_json(c, f"/instance/connectionState/{instance}")
        state_json = st
        connected = _is_connected(st)

        if not connected:
            # 2) Intento 1: endpoint de QR (algunas versiones no lo tienen)
            q_code, qj = _get_json(c, "/instance/qr", params={"instanceName": instance})
            if q_code == 200 and isinstance(qj, dict):
                # Algunas variantes: {"base64":"data:image/png;base64,..."} o {"qr": "data:..."} etc.
                for k in ("base64", "qr", "image", "qrcode", "dataUrl"):
                    val = qj.get(k)
                    if isinstance(val, str) and val.startswith("data:image"):
                        qr_data_url = val
                        break

            # 3) Intento 2: disparar connect y usar "code" para construir QR local
            if not qr_data_url:
                c_code, cj = _get_json(c, f"/instance/connect/{instance}")
                raw_dump = cj or {}
                # Evolution/baileys suele poner "pairingCode" (=> mostrar como texto) o "code" (=> string QR)
                pairing = cj.get("pairingCode") or cj.get("pairing_code")
                code = cj.get("code") or cj.get("qrcode") or cj.get("qrCode")
                if not pairing and code:
                    try:
                        qr_data_url = _qr_data_url_from_code(code)
                    except Exception as e:
                        log.warning("No se pudo generar QR local: %s", e)

        return JSONResponse({
            "connected": connected,
            "qr": qr_data_url,
            "pairingCode": pairing,
            "state": state_json,
            "raw": raw_dump or {"status": st_code},  # útil para debug
        })
