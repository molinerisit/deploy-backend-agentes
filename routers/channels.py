from fastapi import APIRouter, Depends, HTTPException
from db import Session, get_session, Brand
from security import check_api_key
from wa_evolution import EvolutionClient
import logging

router = APIRouter(prefix="/api", tags=["channels"])
log = logging.getLogger("channels")
client = EvolutionClient()

@router.post("/wa/start", dependencies=[Depends(check_api_key)])
def wa_start(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(status_code=404, detail="Brand no encontrada")
    instance = f"brand_{brand_id}"
    try:
        data = client.create_instance(instance)
        return {"ok": True, "instance": instance, "data": data}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("wa_start error brand_id=%s: %s", brand_id, e)
        raise HTTPException(status_code=500, detail="Error interno al iniciar WA")

@router.get("/wa/qr", dependencies=[Depends(check_api_key)])
def wa_qr(brand_id: int, session: Session = Depends(get_session)):
    if not session.get(Brand, brand_id):
        raise HTTPException(status_code=404, detail="Brand no encontrada")
    instance = f"brand_{brand_id}"
    try:
        connected, qr = client.get_qr(instance)
        return {"connected": connected, "qr": (None if connected else qr)}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("wa_qr error brand_id=%s: %s", brand_id, e)
        raise HTTPException(status_code=500, detail="Error interno al obtener QR")
