# backend/routers/channels.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from db import Session, get_session, Brand
from security import check_api_key
from wa_evolution import create_instance, get_qr, send_text

router = APIRouter(prefix="/api", tags=["channels"])

@router.post("/wa/start", dependencies=[Depends(check_api_key)])
def wa_start(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{brand_id}"
    create_instance(instance)
    return {"ok": True, "instance": instance}

@router.get("/wa/qr", dependencies=[Depends(check_api_key)])
def wa_qr(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{brand_id}"
    connected, qr, pairing, state = get_qr(instance)
    # devolvemos todo para que el front pueda debuggear si quiere
    return {
        "connected": connected,
        "qr": (None if connected else qr),
        "pairingCode": (None if connected else pairing),
        "state": state
    }

# --------- Test de envío (para tu botón "Prueba rápida") ------------
class WATestIn(BaseModel):
    brand_id: int
    to: str
    text: str

@router.post("/wa/test", dependencies=[Depends(check_api_key)])
def wa_test(payload: WATestIn, session: Session = Depends(get_session)):
    if not session.get(Brand, payload.brand_id):
        raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{payload.brand_id}"
    res = send_text(instance, payload.to, payload.text)
    return {"ok": True, "result": res}
