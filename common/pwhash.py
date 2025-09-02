# backend/common/pwhash.py
import os, base64, hashlib

ALG = "sha256"
ITER = 200_000

def _b64(x: bytes) -> str:
    return base64.b64encode(x).decode("utf-8")

def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("utf-8"))

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(ALG, password.encode("utf-8"), salt, ITER)
    return f"pbkdf2${ALG}${ITER}${_b64(salt)}${_b64(dk)}"

def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, alg, iters, salt_b64, hash_b64 = stored.split("$", 4)
        if scheme != "pbkdf2": return False
        iters = int(iters)
        salt = _b64d(salt_b64)
        expected = _b64d(hash_b64)
        dk = hashlib.pbkdf2_hmac(alg, password.encode("utf-8"), salt, iters)
        return hashlib.compare_digest(dk, expected)
    except Exception:
        return False
