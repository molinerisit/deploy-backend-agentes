#backend/routers/meta.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from security import check_api_key
from social.publish import fb_post, ig_image  # 👈 fix

router = APIRouter(prefix="/api/meta", tags=["meta"])

class FBPost(BaseModel):
    message: str; image_url: Optional[str] = None; link: Optional[str] = None

@router.post("/post/facebook", dependencies=[Depends(check_api_key)])
def post_fb(payload: FBPost):
    pid = fb_post(payload.message, payload.image_url, payload.link)
    return {"post_id": pid}

class IGPost(BaseModel):
    caption: str; image_url: str

@router.post("/post/instagram", dependencies=[Depends(check_api_key)])
def post_ig(payload: IGPost):
    mid = ig_image(payload.caption, payload.image_url)
    return {"media_id": mid}
