import os
from fastapi import Header, HTTPException, status

API_KEY_HEADER = os.getenv("API_KEY_HEADER", "").strip()

def check_api_key(x_api_key: str | None = Header(default=None)):
    if not API_KEY_HEADER:
        return  # sin verificación
    if not x_api_key or x_api_key.strip() != API_KEY_HEADER:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="x-api-key inválida")
