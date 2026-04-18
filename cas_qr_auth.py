from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Callable

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
_SUSTECH_DIR = os.path.join(_BACKEND_DIR, "sustech")
if _SUSTECH_DIR not in sys.path:
    sys.path.insert(0, _SUSTECH_DIR)

try:
    from sustech.cas import CasService
except ImportError:
    from cas import CasService

try:
    from .qr_utils import build_qr_png_base64, render_qr_ascii
except ImportError:
    from qr_utils import build_qr_png_base64, render_qr_ascii

logger = logging.getLogger(__name__)


@dataclass
class _QRSession:
    cas_service: CasService
    created_at: float
    issued_at: int
    nonce: str
    signature: str


@dataclass
class _AuthorizedGrant:
    cas_token: str
    created_at: float
    signature: str
    exchange_code: str
    consumed: bool = False


class CasQRSessionManager:
    """In-memory QR login session manager for CAS WeWork login."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        auth_factory: Callable[[], CasService] | None = None,
        signing_secret: str | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._auth_factory = auth_factory or CasService
        self._signing_secret = (signing_secret or os.getenv("CAS_QR_SESSION_SECRET") or uuid.uuid4().hex).encode("utf-8")
        self._lock = Lock()
        self._sessions: dict[str, _QRSession] = {}
        self._authorized: dict[str, _AuthorizedGrant] = {}
        self._consumed_exchange_codes: dict[str, float] = {}

    def create_session(self) -> dict:
        cas_service = self._auth_factory()
        qr_payload = cas_service.begin_wework_qr_login()
        if not qr_payload.get("ok"):
            raise RuntimeError(qr_payload.get("error") or "failed to initialize CAS QR login")

        session_id = uuid.uuid4().hex
        created_at = time.time()
        issued_at = int(created_at)
        nonce = uuid.uuid4().hex
        signature = self._build_signature(session_id, nonce, issued_at)
        with self._lock:
            self._cleanup_locked()
            self._sessions[session_id] = _QRSession(
                cas_service=cas_service,
                created_at=created_at,
                issued_at=issued_at,
                nonce=nonce,
                signature=signature,
            )

        result = {
            "session_id": session_id,
            "signature": signature,
            "qr_url": qr_payload.get("url"),
            "expires_in": self.ttl_seconds,
        }
        qr_url = result.get("qr_url")
        if isinstance(qr_url, str) and qr_url:
            try:
                result["qr_base64"] = build_qr_png_base64(qr_url)
                result["qr_ascii"] = render_qr_ascii(qr_url)
            except Exception as exc:
                logger.warning("qr rendering unavailable", extra={"error": str(exc)})
        return result

    def poll_status(self, session_id: str, signature: str) -> dict:
        with self._lock:
            self._cleanup_locked()
            record = self._sessions.get(session_id)
            if record and not self._is_valid_signature(session_id, record, signature):
                return {"ok": False, "status": "invalid_signature", "error": "invalid session signature"}

        if record is None:
            return {"ok": False, "status": "expired", "error": "session not found or expired"}

        status_payload = record.cas_service.check_wework_qr_status()
        if not status_payload.get("ok"):
            return status_payload

        status = str(status_payload.get("status") or "unknown")
        if status == "confirm":
            complete_payload = record.cas_service.complete_wework_qr_login(status_payload)
            if not complete_payload.get("ok"):
                return complete_payload
            exchange_code = uuid.uuid4().hex
            status = "authorized"
            status_payload = {
                "ok": True,
                "status": "authorized",
                "exchange_code": exchange_code,
            }
            with self._lock:
                self._sessions.pop(session_id, None)
                self._authorized[session_id] = _AuthorizedGrant(
                    cas_token=str(complete_payload.get("cas_token") or ""),
                    created_at=time.time(),
                    signature=record.signature,
                    exchange_code=exchange_code,
                )

        return {"ok": True, "status": status} if status != "authorized" else status_payload

    def exchange_token(self, session_id: str, signature: str, exchange_code: str) -> dict:
        with self._lock:
            self._cleanup_locked()

            if exchange_code in self._consumed_exchange_codes:
                return {"ok": False, "error": "exchange code already consumed"}

            grant = self._authorized.get(session_id)
            if grant is None:
                return {"ok": False, "error": "authorized session not found or expired"}

            if not hmac.compare_digest(signature, grant.signature):
                return {"ok": False, "error": "invalid session signature"}

            if grant.consumed:
                return {"ok": False, "error": "session token already consumed"}

            if not hmac.compare_digest(exchange_code, grant.exchange_code):
                return {"ok": False, "error": "invalid exchange code"}

            grant.consumed = True
            self._authorized.pop(session_id, None)
            self._consumed_exchange_codes[exchange_code] = time.time()

            return {"ok": True, "cas_token": grant.cas_token}

    def close_session(self, session_id: str, signature: str) -> bool:
        with self._lock:
            self._cleanup_locked()
            record = self._sessions.get(session_id)
            if record is None:
                return False
            if not self._is_valid_signature(session_id, record, signature):
                return False
            self._sessions.pop(session_id, None)
            return True

    def _is_valid_signature(self, session_id: str, record: _QRSession, signature: str) -> bool:
        expected = self._build_signature(session_id, record.nonce, record.issued_at)
        return hmac.compare_digest(signature, expected) and hmac.compare_digest(record.signature, expected)

    def _build_signature(
        self,
        session_id: str,
        nonce: str,
        issued_at: int,
    ) -> str:
        payload = f"{session_id}:{nonce}:{issued_at}".encode("utf-8")
        digest = hmac.new(self._signing_secret, payload, hashlib.sha256).hexdigest()
        return digest

    def _cleanup_locked(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, record in self._sessions.items()
            if now - record.created_at > self.ttl_seconds
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            self._authorized.pop(sid, None)

        expired_authorized = [
            sid
            for sid, grant in self._authorized.items()
            if now - grant.created_at > self.ttl_seconds
        ]
        for sid in expired_authorized:
            self._authorized.pop(sid, None)

        consumed_expired = [
            code
            for code, consumed_at in self._consumed_exchange_codes.items()
            if now - consumed_at > self.ttl_seconds
        ]
        for code in consumed_expired:
            self._consumed_exchange_codes.pop(code, None)

        if expired:
            logger.info("cleaned expired cas qr sessions", extra={"count": len(expired)})
