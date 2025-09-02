# backend/routers/channels.py
from fastapi import APIRouter, Depends, HTTPException
from db import Session, get_session, Brand
from security import check_api_key
from wa_evolution import (
    create_instance, connect_instance, get_qr as evo_get_qr,
    connection_state, EvolutionError
)
import logging

router = APIRouter(prefix="/api", tags=["channels"])
log = logging.getLogger("channels")

@router.post("/wa/start", dependencies=[Depends(check_api_key)])
def wa_start(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{brand_id}"
    # crear (si ya existe, ok)
    try:
        create_instance(instance)
    except EvolutionError as e:
        if "already" not in str(e).lower():
            raise HTTPException(502, f"Evolution create_instance: {e}")
        log.info("Instance %s ya existía; seguimos.", instance)
    # conectar siempre
    try:
        connect_instance(instance)
    except Exception as e:
        log.warning("connect_instance fallo: %s", e)
    return {"ok": True, "instance": instance}

@router.get("/wa/qr", dependencies=[Depends(check_api_key)])
def wa_qr(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{brand_id}"

    try:
        st = connection_state(instance)
    except Exception as e:
        st = {"error": str(e)}

    try:
        data = evo_get_qr(instance)  # <-- dict
    except Exception as e:
        raise HTTPException(502, f"Evolution get_qr: {e}")

    # normalizar "connected"
    state_str = ""
    if isinstance(st, dict):
        state_str = str(st.get("state", "")).lower()
    connected = bool(
        data.get("connected")
        or state_str in ("open", "connected")
    )

    # normalizar QR/base64
    def pick_qr(d: dict):
        for k in ("qr", "qrcode", "base64", "image"):
            v = d.get(k)
            if isinstance(v, str) and v:
                if v.startswith("data:image"):
                    return v
                return f"data:image/png;base64,{v}"
        return None

    qr_b64 = None if connected else pick_qr(data)
    pairing = None if connected else (data.get("pairingCode") or data.get("code"))

    return {
        "connected": connected,
        "qr": qr_b64,
        "pairingCode": pairing,
        "state": st,
        "raw": data,   # útil para debug en el front
    }

# (opcional para pruebas rápidas)
@router.get("/wa/test", dependencies=[Depends(check_api_key)])
def wa_test():
    return {"ok": True}
