from __future__ import annotations
import os, logging, requests
from typing import Optional, Tuple, Literal
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("social.publish")

GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
IG_BUSINESS_ID = os.getenv("IG_BUSINESS_ID")

def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=(429,500,502,503,504), allowed_methods=("POST","GET"))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": "orbytal-social-publisher/1.0"})
    return s

def _base_url() -> str:
    return f"https://graph.facebook.com/{GRAPH_VERSION}"

def _check_env() -> Optional[str]:
    if not ACCESS_TOKEN: return "META_ACCESS_TOKEN faltante"
    if not (FB_PAGE_ID or IG_BUSINESS_ID): return "FB_PAGE_ID o IG_BUSINESS_ID faltantes"
    return None

def fb_post(message: str, image_url: Optional[str] = None, link: Optional[str] = None) -> str:
    err = _check_env(); if_err = err is not None
    if if_err: raise RuntimeError(err)
    base = _base_url()
    data = {"access_token": ACCESS_TOKEN}
    if image_url:
        url = f"{base}/{FB_PAGE_ID}/photos"; data.update({"caption": message or "", "url": image_url})
    else:
        url = f"{base}/{FB_PAGE_ID}/feed"; data.update({"message": message or ""})
        if link: data["link"] = link
    r = _session().post(url, data=data, timeout=30); r.raise_for_status()
    j = r.json()
    return j.get("post_id") or j.get("id") or "OK"

def ig_image(caption: str, image_url: str) -> str:
    err = _check_env(); 
    if err: raise RuntimeError(err)
    base = _base_url(); s = _session()
    r1 = s.post(f"{base}/{IG_BUSINESS_ID}/media",
                data={"image_url": image_url, "caption": caption or "", "access_token": ACCESS_TOKEN}, timeout=30)
    r1.raise_for_status()
    creation_id = r1.json().get("id") or ""
    r2 = s.post(f"{base}/{IG_BUSINESS_ID}/media_publish",
                data={"creation_id": creation_id, "access_token": ACCESS_TOKEN}, timeout=30)
    r2.raise_for_status()
    return r2.json().get("id") or "OK"

class ContentItemIn:  # tipado mínimo compatible
    platform: Literal["facebook","instagram"]
    copy_text: Optional[str]; asset_url: Optional[str]
    def __init__(self, platform, copy_text=None, asset_url=None):
        self.platform = platform; self.copy_text = copy_text; self.asset_url = asset_url

def try_publish(c) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        if c.platform == "facebook":
            pid = fb_post(message=getattr(c,"copy_text","") or "", image_url=getattr(c,"asset_url",None))
            return True, pid, None
        if c.platform == "instagram":
            if not getattr(c,"asset_url",None):
                return False, None, "Instagram requiere 'asset_url' (imagen)"
            mid = ig_image(caption=getattr(c,"copy_text","") or "", image_url=c.asset_url)
            return True, mid, None
        return False, None, "Plataforma no soportada"
    except Exception as e:
        log.exception("Error publicando: %s", e)
        return False, None, str(e)
