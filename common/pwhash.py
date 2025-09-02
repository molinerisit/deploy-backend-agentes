# backend/common/pwhash.py
import os, secrets, hashlib

def hash_password(password: str, iterations: int = 200_000) -> str:
    if not isinstance(password, str) or not password:
        raise ValueError("password inválido")
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return f"pbkdf2$sha256${iterations}${salt}${dk.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, algo, iters, salt, hexdigest = encoded.split("$")
        if scheme != "pbkdf2" or algo != "sha256":
            return False
        iters = int(iters)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iters)
        return dk.hex() == hexdigest
    except Exception:
        return False
