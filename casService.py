import requests
from lxml import etree
import os
import json
import time
import sys
from typing import Optional, Any
import dotenv
dotenv.load_dotenv(".env")

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_payload(data: Any) -> dict:
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        content = data.strip()
        if not content:
            return {}
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
            return {}
        except ValueError:
            return {}
    return {}


def get_execution_and_eventId(response):
    html = etree.HTML(response.text)
    execution_nodes = html.xpath('//input[@name="execution"]/@value')
    event_nodes = html.xpath('//input[@name="_eventId"]/@value')
    execution = execution_nodes[0] if execution_nodes else ""
    _eventId = event_nodes[0] if event_nodes else "submit"
    return execution, _eventId


class CasService:
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tgc_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.username = username or os.getenv("SUSTECH_SID")
        self.password = password or os.getenv("SUSTECH_PASSWORD")
        self.TGC = tgc_token or os.getenv("SUSTECH_CAS_TOKEN")
        self.url = 'cas.sustech.edu.cn/cas/login'
        self.session = session or requests.Session()
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                      "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br,zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        }
        self._wework_qr_active = False

    def _resolve_credentials(self, username: Optional[str], password: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        user = username or self.username or os.getenv("SUSTECH_SID")
        pwd = password or self.password or os.getenv("SUSTECH_PASSWORD")
        self.username = user
        self.password = pwd
        return user, pwd

    def _sync_tgc_from_cookies(self) -> None:
        token = self.session.cookies.get("TGC")
        if token:
            self.TGC = token

    def begin_wework_qr_login(self) -> dict:
        """Start CAS WeWork QR login and return QR payload."""
        base_url = f"https://{self.url}"
        try:
            # Prime CAS login session to ensure check endpoint works with same cookie jar.
            self.session.get(base_url, headers=self.headers, timeout=20)
            qr_response = self.session.get(
                "https://cas.sustech.edu.cn/cas/clientredirect",
                headers=self.headers,
                params={"client_name": "Wework"},
                timeout=20,
            )
            if qr_response.status_code != 200:
                return {
                    "ok": False,
                    "error": f"qr initialization failed with status {qr_response.status_code}",
                }
            payload = _coerce_payload(qr_response.text)
            qr_url = str(payload.get("url") or "")
            if not qr_url:
                return {"ok": False, "error": "qr url not found in CAS response"}
            self._wework_qr_active = True
            return {"ok": True, "url": qr_url}
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc)}

    def check_wework_qr_status(self) -> dict:
        """Check CAS WeWork QR status in current session."""
        if not self._wework_qr_active:
            return {"ok": False, "status": "expired", "error": "qr session not initialized"}
        try:
            response = self.session.get(
                f"https://{self.url}",
                headers=self.headers,
                params={"client_name": "Wework", "wechat": "check"},
                timeout=20,
            )
            if response.status_code != 200:
                return {
                    "ok": False,
                    "status": "error",
                    "error": f"check status failed with code {response.status_code}",
                }
            payload = _coerce_payload(response.text)
            status = str(payload.get("status") or "unknown").lower()
            if not status:
                status = "unknown"
            result = {"ok": True, "status": status}
            for key in ("client_name", "state", "code", "redirect"):
                if key in payload:
                    result[key] = payload.get(key)
            return result
        except requests.RequestException as exc:
            return {"ok": False, "status": "error", "error": str(exc)}

    def complete_wework_qr_login(self, status_payload: Optional[dict] = None) -> dict:
        """Complete CAS login when QR check returns confirm."""
        payload = status_payload or {}
        client_name = str(payload.get("client_name") or "Wework")
        state = str(payload.get("state") or "")
        code = str(payload.get("code") or "")

        try:
            self._sync_tgc_from_cookies()
            if self.TGC:
                self._wework_qr_active = False
                return {"ok": True, "cas_token": self.TGC}

            # Some CAS deployments complete QR flow via a confirm GET and cookie update.
            confirm_response = self.session.get(
                f"https://{self.url}",
                headers=self.headers,
                params={"client_name": client_name, "wechat": "confirm"},
                timeout=20,
                allow_redirects=True,
            )
            if confirm_response.status_code in (200, 302):
                self._sync_tgc_from_cookies()
                if self.TGC:
                    self._wework_qr_active = False
                    return {"ok": True, "cas_token": self.TGC}

            if not state or not code:
                return {"ok": False, "error": "missing state/code for qr completion"}

            response = self.session.post(
                f"https://{self.url}",
                headers=self.headers,
                data={
                    "client_name": client_name,
                    "state": state,
                    "code": code,
                },
                timeout=20,
                allow_redirects=True,
            )
            if response.status_code not in (200, 302):
                return {
                    "ok": False,
                    "error": f"qr completion failed with code {response.status_code}",
                }
            self._sync_tgc_from_cookies()
            self._wework_qr_active = False
            if not self.TGC:
                return {"ok": False, "error": "CAS did not issue TGC after qr completion"}
            return {"ok": True, "cas_token": self.TGC}
        except requests.RequestException as exc:
            return {"ok": False, "error": str(exc)}

    def _login_with_password(self, username: Optional[str], password: Optional[str]) -> bool:
        user, pwd = self._resolve_credentials(username, password)
        if not user or not pwd:
            print("Login failed: missing SUSTECH_SID or SUSTECH_PASSWORD")
            return False

        try:
            response = self.session.get(f"https://{self.url}", headers=self.headers, timeout=20)
            if response.status_code != 200:
                print(f"Login failed: CAS login page status={response.status_code}")
                return False
            execution, _eventId = get_execution_and_eventId(response)
            data = {
                "username": user,
                "password": pwd,
                "execution": execution,
                "_eventId": _eventId,
                "geolocation": ""
            }
            response = self.session.post(
                f"https://{self.url}", headers=self.headers, data=data, timeout=20, allow_redirects=True
            )
            if response.status_code in (200, 302):
                self._sync_tgc_from_cookies()
                if self.TGC:
                    print("Login successfully!")
                    return True
            print("Login failed!")
            return False
        except requests.RequestException as exc:
            print(f"Login failed: {exc}")
            return False

    def _login_with_qr(self, timeout_seconds: float = 300.0, poll_interval: float = 3.0, print_qr_url: bool = True) -> bool:
        try:
            from external.qr_utils import render_qr_ascii
        except ImportError:
            render_qr_ascii = None

        begin_payload = self.begin_wework_qr_login()
        if not begin_payload.get("ok"):
            print(f"QR login init failed: {begin_payload.get('error')}")
            return False

        qr_url = str(begin_payload.get("url") or "")
        if print_qr_url:
            print("=== CAS QR Login URL ===")
            print(qr_url)

        if render_qr_ascii is not None and qr_url:
            try:
                print("--- Scan This QR (ASCII) ---")
                print(render_qr_ascii(qr_url))
            except Exception as exc:
                print(f"QR ascii render failed: {exc}")
        sys.stdout.flush()

        started = time.time()
        last_status = None
        while time.time() - started <= timeout_seconds:
            status_payload = self.check_wework_qr_status()
            status = str(status_payload.get("status") or "unknown")

            if status != last_status:
                print(f"[CAS QR] status={status}")
                last_status = status

            if status == "confirm":
                done_payload = self.complete_wework_qr_login(status_payload)
                if done_payload.get("ok") and done_payload.get("cas_token"):
                    print("QR login successfully!")
                    return True
                print(f"QR login completion failed: {done_payload.get('error')}")
                return False

            if status in {"cancel", "warning", "expired", "invalid_signature", "error"}:
                print(f"QR login stopped with status={status}")
                return False

            time.sleep(max(0.5, poll_interval))

        print("QR login timeout")
        return False

    def Login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_qr: Optional[bool] = None,
        allow_password_fallback: Optional[bool] = None,
        qr_timeout_seconds: Optional[float] = None,
        qr_poll_interval: Optional[float] = None,
    ):
        self._sync_tgc_from_cookies()
        if self.TGC:
            return True

        use_qr_login = _env_bool("CAS_USE_QR_LOGIN", False) if use_qr is None else bool(use_qr)
        fallback_enabled = _env_bool("CAS_QR_ALLOW_PASSWORD_FALLBACK", True) if allow_password_fallback is None else bool(allow_password_fallback)
        timeout_seconds = qr_timeout_seconds or float(os.getenv("CAS_QR_LOGIN_TIMEOUT", "300"))
        poll_interval = qr_poll_interval or float(os.getenv("CAS_QR_POLL_INTERVAL", "3"))

        if use_qr_login:
            if self._login_with_qr(timeout_seconds=timeout_seconds, poll_interval=poll_interval):
                return True
            if not fallback_enabled:
                return False
            print("QR login failed, fallback to password login...")

        return self._login_with_password(username=username, password=password)

    def Logout(self):
        response = self.session.get(
            f"https://{self.url}", headers=self.headers, data={"TGC": self.TGC}, timeout=20)
        if response.status_code == 200:
            print("Logout successfully!")
            self.TGC = None
        else:
            print("Logout failed!")


if __name__ == '__main__':
    cas = CasService()
    cas.Login()
    cas.Logout()
