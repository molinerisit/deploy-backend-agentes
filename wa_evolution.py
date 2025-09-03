# backend/wa_evolution.py
import os
import logging
from typing import Any, Dict, Optional, Tuple

import httpx

log = logging.getLogger("wa_evolution")

EVOLUTION_BASE_URL = (os.getenv("EVOLUTION_BASE_URL") or "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")


class EvolutionError(Exception):
    """Errores específicos del cliente Evolution."""


def _headers() -> Dict[str, str]:
    """
    Algunos servidores Evolution validan 'apikey' y otros aceptan 'Authorization: Bearer'.
    Enviamos ambos por compatibilidad.
    """
    h = {"Content-Type": "application/json"}
    if EVOLUTION_API_KEY:
        h["apikey"] = EVOLUTION_API_KEY
        h["Authorization"] = f"Bearer {EVOLUTION_API_KEY}"
    return h


def _ensure_base(url: str) -> str:
    if not url:
        raise EvolutionError("EVOLUTION_BASE_URL no configurado")
    return url


def _jid_from_number(n: str) -> str:
    n = (n or "").strip()
    if not n:
        return ""
    return n if "@s.whatsapp.net" in n else f"{''.join(ch for ch in n if ch.isdigit())}@s.whatsapp.net"


class EvolutionClient:
    """
    Cliente mínimo para Evolution API v2.3.0 (tu despliegue).
    Notas importantes para esta versión:
      - El endpoint correcto para crear instancia es: POST /instance/create
      - En tu build, el create exige que el 'webhook' se pase ahí mismo (validation 'instance requires property "webhook"').
      - No existen endpoints estables de 'setWebhook' -> lo omitimos (no-op).
      - Listado de chats/mensajes no está publicado -> devolvemos 500 controlado para activar fallback a DB en tu backend.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = _ensure_base((base_url or EVOLUTION_BASE_URL).rstrip("/"))
        self.api_key = api_key or EVOLUTION_API_KEY

    # -------------------- Instances --------------------

    def create_instance(self, instance_name: str, webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """
        Crea una instancia. En tu v2.3.0 el 'webhook' debe viajar en el create.
        Payload típico:
          {
            "instanceName": "brand_1",
            "webhook": "https://tu-backend/api/wa/webhook?token=...&instance=brand_1"
          }
        """
        url = f"{self.base_url}/instance/create"
        payload: Dict[str, Any] = {"instanceName": instance_name}
        if webhook_url:
            # Clave para tu build: mandar 'webhook' directamente acá
            payload["webhook"] = webhook_url

        with httpx.Client(timeout=60.0, headers=_headers()) as c:
            r = c.post(url, json=payload)

        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}

        if r.status_code >= 400:
            # Algunos servers devuelven 4xx si ya existe; dejamos seguir si detectamos 'already'
            text = str(body)
            if "already" not in text.lower():
                raise EvolutionError(f"create_instance {r.status_code}: {body}")
            log.info("Instance %s ya existía: %s", instance_name, body)

        return body

    def set_webhook(self, instance_name: str, webhook_url: str) -> Tuple[int, Dict[str, Any]]:
        """
        No soportado en tu versión.
        Dejamos no-op para compatibilidad con el resto del código.
        """
        log.warning("set_webhook omitido: no soportado en Evolution v2.3.0")
        return (404, {"error": "set_webhook not supported in this Evolution version"})

    def delete_instance(self, instance_name: str) -> Tuple[int, Dict[str, Any]]:
        """
        En algunos builds existen variantes; intentamos rutas conocidas.
        Si no existe ninguna, devolvemos el último error.
        """
        candidates_get = [
            f"{self.base_url}/instance/delete/{instance_name}",
            f"{self.base_url}/instance/remove/{instance_name}",
            f"{self.base_url}/instance/logout/{instance_name}",
            f"{self.base_url}/logout/{instance_name}",
        ]
        candidates_post = [
            (f"{self.base_url}/instance/delete", {"instanceName": instance_name}),
            (f"{self.base_url}/instance/remove", {"instanceName": instance_name}),
            (f"{self.base_url}/instance/logout", {"instanceName": instance_name}),
        ]

        last = (500, {"error": "no endpoint matched"})
        with httpx.Client(timeout=30.0, headers=_headers()) as c:
            for u in candidates_get:
                try:
                    r = c.get(u)
                    if r.status_code < 400:
                        return (r.status_code, r.json())
                    last = (r.status_code, r.json() if r.headers.get("content-type", "").lower().startswith("application/json") else {"raw": r.text})
                except Exception as e:
                    last = (500, {"error": str(e)})

            for u, body in candidates_post:
                try:
                    r = c.post(u, json=body)
                    if r.status_code < 400:
                        return (r.status_code, r.json())
                    last = (r.status_code, r.json() if r.headers.get("content-type", "").lower().startswith("application/json") else {"raw": r.text})
                except Exception as e:
                    last = (500, {"error": str(e)})

        return last

    def connect_instance(self, instance_name: str) -> Dict[str, Any]:
        url = f"{self.base_url}/instance/connect/{instance_name}"
        with httpx.Client(timeout=60.0, headers=_headers()) as c:
            r = c.get(url)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "raw": r.text}

    def connection_state(self, instance_name: str) -> Dict[str, Any]:
        url = f"{self.base_url}/instance/connectionState/{instance_name}"
        with httpx.Client(timeout=30.0, headers=_headers()) as c:
            r = c.get(url)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "raw": r.text}

    # -------------------- QR / Pairing --------------------

    def qr_by_param(self, instance_name: str) -> Tuple[int, Dict[str, Any]]:
        """
        Algunos despliegues exponen /instance/qr/:instance o /instance/qr?instanceName=...
        Si no existe, devolvemos 404 controlado.
        """
        candidates = [
            f"{self.base_url}/instance/qr/{instance_name}",
            f"{self.base_url}/instance/qr?instanceName={instance_name}",
        ]
        with httpx.Client(timeout=30.0, headers=_headers()) as c:
            for u in candidates:
                try:
                    r = c.get(u)
                    if r.status_code < 400:
                        try:
                            return (r.status_code, r.json())
                        except Exception:
                            return (r.status_code, {"raw": r.text})
                except Exception:
                    pass
        return (404, {"error": "QR endpoint not found"})

    # -------------------- Chats & Mensajes (no soportado en tu build) --------------------

    def list_chats(self, instance_name: str, limit: int = 200) -> Tuple[int, Dict[str, Any]]:
        """
        Tu Evolution v2.3.0 no publica endpoints de listado de chats.
        Devolvemos 500 controlado para que el backend haga fallback a DB.
        """
        return (500, {"error": "No endpoint matched"})

    def get_chat_messages(self, instance_name: str, jid: str, limit: int = 50) -> Tuple[int, Dict[str, Any]]:
        return (500, {"error": "No endpoint matched"})

    # -------------------- Envío de mensajes --------------------

    def send_text(self, instance_name: str, number: str, text: str) -> Dict[str, Any]:
        """
        number: puede ser MSISDN (549... sin +) o JID completo.
        """
        jid = _jid_from_number(number)
        if not jid:
            raise EvolutionError("Número/JID destino inválido")

        url = f"{self.base_url}/message/sendText/{instance_name}"
        payload = {"number": jid, "text": text}

        with httpx.Client(timeout=30.0, headers=_headers()) as c:
            r = c.post(url, json=payload)

        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text}

        if r.status_code >= 400:
            log.warning("send_text %s -> %s %s", instance_name, r.status_code, body)

        return {"http_status": r.status_code, "body": body}
