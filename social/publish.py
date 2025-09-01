# backend/social/publish.py
from __future__ import annotations

import os
import logging
from typing import Optional, Dict, Any, Tuple, Literal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("social.publish")

# --- Config de entorno ---
GRAPH_VERSION = os.getenv("META_GRAPH_VERSION", "v21.0")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN")
FB_PAGE_ID = os.getenv("FB_PAGE_ID")               # para Facebook
IG_BUSINESS_ID = os.getenv("IG_BUSINESS_ID")       # para Instagram

# --- HTTP session con reintentos ---
def _session() -> requests.Session:
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"User-Agent": "orbytal-social-publisher/1.0"})
    return s

def _base_url() -> str:
    return f"https://graph.facebook.com/{GRAPH_VERSION}"

def _need_env(platform: Literal["facebook", "instagram"]) -> Optional[str]:
    if not ACCESS_TOKEN:
        return "META_ACCESS_TOKEN faltante"
    if platform == "facebook" and not FB_PAGE_ID:
        return "FB_PAGE_ID faltante"
    if platform == "instagram" and not IG_BUSINESS_ID:
        return "IG_BUSINESS_ID faltante"
    return None

# --- Facebook ---
def fb_post(message: str, image_url: Optional[str] = None, link: Optional[str] = None) -> str:
    err = _need_env("facebook")
    if err:
        raise RuntimeError(err)

    base = _base_url()
    data: Dict[str, Any] = {"access_token": ACCESS_TOKEN}
    sess = _session()

    if image_url:
        # Publica una foto con caption
        url = f"{base}/{FB_PAGE_ID}/photos"
        data.update({"caption": message or "", "url": image_url})
    else:
        # Publica un post de texto (y opcionalmente link)
        url = f"{base}/{FB_PAGE_ID}/feed"
        data.update({"message": message or ""})
        if link:
            data["link"] = link

    r = sess.post(url, data=data, timeout=30)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Facebook error {r.status_code}: {r.text}") from e

    j = r.json()
    return j.get("post_id") or j.get("id") or "OK"

# --- Instagram ---
def ig_image(caption: str, image_url: str) -> str:
    err = _need_env("instagram")
    if err:
        raise RuntimeError(err)

    base = _base_url()
    sess = _session()

    # 1) Crear contenedor
    r1 = sess.post(
        f"{base}/{IG_BUSINESS_ID}/media",
        data={"image_url": image_url, "caption": caption or "", "access_token": ACCESS_TOKEN},
        timeout=30,
    )
    try:
        r1.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Instagram(media) {r1.status_code}: {r1.text}") from e

    creation_id = r1.json().get("id")
    if not creation_id:
        raise RuntimeError("Instagram: no se obtuvo creation_id")

    # 2) Publicar
    r2 = sess.post(
        f"{base}/{IG_BUSINESS_ID}/media_publish",
        data={"creation_id": creation_id, "access_token": ACCESS_TOKEN},
        timeout=30,
    )
    try:
        r2.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"Instagram(publish) {r2.status_code}: {r2.text}") from e

    return r2.json().get("id") or "OK"

# --- Tipado fallback si no existe db.ContentItem ---
try:
    from db import ContentItem  # type: ignore
except Exception:
    from pydantic import BaseModel
    class ContentItem(BaseModel):  # fallback mínimo
        platform: Literal["facebook", "instagram"]
        copy_text: Optional[str] = None
        asset_url: Optional[str] = None

# --- Punto de entrada usado por el scheduler ---
def try_publish(c: ContentItem) -> Tuple[bool, str]:
    """
    Devuelve:
      - ok: bool
      - message: str (ID de post o descripción de error)
    Compatible con el scheduler: ok, message = try_publish(item)
    """
    try:
        platform = getattr(c, "platform", None)
        text = getattr(c, "copy_text", "") or ""
        asset = getattr(c, "asset_url", None)

        if platform == "facebook":
            post_id = fb_post(message=text, image_url=asset)
            return True, f"Facebook OK (id={post_id})"

        if platform == "instagram":
            if not asset:
                return False, "Instagram requiere 'asset_url' (imagen)"
            media_id = ig_image(caption=text, image_url=asset)
            return True, f"Instagram OK (id={media_id})"

        return False, f"Plataforma no soportada: {platform}"
    except Exception as e:
        logger.exception("Error publicando: %s", e)
        return False, f"Error publicando: {e}"
