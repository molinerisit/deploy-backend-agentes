from fastapi import APIRouter, Depends, HTTPException
from db import Session, get_session, Brand
from security import check_api_key
from wa_evolution import create_instance, get_qr

router = APIRouter(prefix="/api", tags=["channels"])

@router.post("/wa/start", dependencies=[Depends(check_api_key)])
def wa_start(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id): raise HTTPException(404, "Brand no encontrada")
    instance = f"brand_{brand_id}"
    create_instance(instance)
    return {"ok": True, "instance": instance}

@router.get("/wa/qr", dependencies=[Depends(check_api_key)])
def wa_qr(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id): raise HTTPException(404, "Brand no encontrada")
    connected, b64 = get_qr(f"brand_{brand_id}")
    return {"connected": connected, "qr": (None if connected else b64)}
