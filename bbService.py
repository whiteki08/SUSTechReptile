import requests
from casService import CasService
import json
import datetime
import dotenv
import time
from typing import Optional

dotenv.load_dotenv(".env")  # 加载 .env 文件中的环境变量


class bbService(CasService):
    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tgc_token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        super().__init__(
            username=username,
            password=password,
            tgc_token=tgc_token,
            session=session,
        )

    def _is_probable_cas_login_page(self, response: requests.Response) -> bool:
        final_url = (response.url or "").lower()
        if "cas.sustech.edu.cn/cas/login" in final_url:
            return True

        page_text = (response.text or "")[:8000].lower()
        markers = (
            "cas.sustech.edu.cn/cas/login",
            'name="execution"',
            'name="_eventid"',
            'name="username"',
        )
        return any(marker in page_text for marker in markers)

    def _calendar_request_headers(self) -> dict:
        headers = self.headers.copy()
        headers.update(
            {
                "Accept": "*/*",
                "Referer": "https://bb.sustech.edu.cn/webapps/calendar/viewMyBb?globalNavigation=false",
                "Origin": "https://bb.sustech.edu.cn",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        return headers

    def _ensure_calendar_timezone_cookie(self) -> None:
        """某些 BB 节点依赖该 cookie 决定日历窗口解析。"""
        if self.session.cookies.get("BbClientCalenderTimeZone"):
            return
        self.session.cookies.set(
            "BbClientCalenderTimeZone",
            "Asia/Shanghai",
            domain="bb.sustech.edu.cn",
            path="/",
        )

    def _warmup_calendar_context(self, headers: dict) -> None:
        """预热日历上下文，降低 selectedCalendarEvents 直接 500 的概率。"""
        self._ensure_calendar_timezone_cookie()
        warmup_urls = [
            "https://bb.sustech.edu.cn/webapps/calendar/viewMyBb?globalNavigation=false",
            "https://bb.sustech.edu.cn/webapps/calendar/viewPersonal",
            "https://bb.sustech.edu.cn/webapps/calendar/calendarData/calendars",
        ]
        for url in warmup_urls:
            try:
                self.session.get(url, headers=headers, timeout=20, allow_redirects=True)
            except requests.RequestException as exc:
                print(f"BB calendar warmup request failed for {url}: {exc}")

    def _safe_response_headers(self, response: requests.Response) -> dict:
        """只记录少量对排障有帮助的响应头，避免噪音过大。"""
        keys = (
            "Content-Type",
            "Server",
            "Date",
            "Via",
            "X-Request-Id",
            "X-Correlation-Id",
            "CF-Ray",
            "Set-Cookie",
            "Cache-Control",
            "Retry-After",
        )
        result = {}
        for key in keys:
            value = response.headers.get(key)
            if value is None:
                continue
            if key.lower() == "set-cookie":
                # 避免日志泄露具体 cookie 值。
                value = f"<present:{len(value)} chars>"
            result[key] = value
        return result

    def _window_context(self, start_ts: int, end_ts: int) -> dict:
        start_dt = datetime.datetime.fromtimestamp(start_ts / 1000, tz=datetime.timezone.utc)
        end_dt = datetime.datetime.fromtimestamp(end_ts / 1000, tz=datetime.timezone.utc)
        span_days = round((end_ts - start_ts) / (1000 * 60 * 60 * 24), 3)
        return {
            "window_start_utc": start_dt.isoformat(),
            "window_end_utc": end_dt.isoformat(),
            "window_span_days": span_days,
        }

    def _log_calendar_failure_context(
        self,
        response: requests.Response,
        *,
        phase: str,
        attempt: int,
        max_attempts: int,
        start_ts: int,
        end_ts: int,
        chunk_label: Optional[str] = None,
    ) -> None:
        preview = (response.text or "")[:500].replace("\n", " ").replace("\r", " ").strip()
        history = [
            {
                "status": item.status_code,
                "url": item.url,
            }
            for item in response.history[-5:]
        ]
        elapsed_ms = None
        if getattr(response, "elapsed", None) is not None:
            elapsed_ms = int(response.elapsed.total_seconds() * 1000)

        debug_payload = {
            "phase": phase,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "chunk": chunk_label,
            "status_code": response.status_code,
            "request_method": getattr(response.request, "method", None),
            "request_url": getattr(response.request, "url", None),
            "final_url": response.url,
            "elapsed_ms": elapsed_ms,
            "redirect_history": history,
            "headers": self._safe_response_headers(response),
            "looks_like_cas_login_page": self._is_probable_cas_login_page(response),
            "response_preview": preview,
        }
        debug_payload.update(self._window_context(start_ts, end_ts))
        print("[bb-debug] calendar request failure context=" + json.dumps(debug_payload, ensure_ascii=True))

    def _request_calendar_window(
        self,
        headers: dict,
        start_ts: int,
        end_ts: int,
        *,
        phase: str,
        attempt: int,
        max_attempts: int,
        chunk_label: Optional[str] = None,
    ):
        """请求一个时间窗口内的 Blackboard 日历事件。成功返回 list，失败返回 None。"""
        url = "https://bb.sustech.edu.cn/webapps/calendar/calendarData/selectedCalendarEvents"
        params = {
            "start": start_ts,
            "end": end_ts,
            "course_id": "",
            "mode": "personal",
        }

        try:
            self._ensure_calendar_timezone_cookie()
            response = self.session.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            error_payload = {
                "phase": phase,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "chunk": chunk_label,
                "exception": str(exc),
            }
            error_payload.update(self._window_context(start_ts, end_ts))
            print("[bb-debug] calendar request exception=" + json.dumps(error_payload, ensure_ascii=True))
            return None

        if response.status_code != 200:
            print(f"Query BB calendar failed! Status code: {response.status_code}")
            self._log_calendar_failure_context(
                response,
                phase=phase,
                attempt=attempt,
                max_attempts=max_attempts,
                start_ts=start_ts,
                end_ts=end_ts,
                chunk_label=chunk_label,
            )
            if "Could not initialize class org.springframework.aop.config.AopConfigUtils" in (response.text or ""):
                print(
                    "BB server-side Spring initialization error detected; retry later or re-login to hit another backend node."
                )
            try:
                with open("bb_calendar_error.html", "w", encoding="utf-8") as f:
                    f.write(response.text)
            except OSError:
                pass
            return None

        content_type = (response.headers.get("Content-Type") or "").lower()
        try:
            payload = response.json()
        except ValueError:
            preview = (response.text or "")[:300].replace("\n", " ")
            print(
                "Query BB calendar got non-JSON 200 response. "
                f"content-type={content_type}, preview={preview}"
            )
            return None

        if not isinstance(payload, list):
            print(f"Query BB calendar got unexpected JSON shape: {type(payload).__name__}")
            return None

        return payload

    def _verify_bb_session(self) -> bool:
        """多端点验证登录状态，避免单个 BB 页面故障导致误判。"""
        checkpoints = [
            (
                "calendar_home",
                "https://bb.sustech.edu.cn/webapps/calendar/viewPersonal",
            ),
            (
                "portal_default",
                "https://bb.sustech.edu.cn/webapps/portal/execute/defaultTab",
            ),
            (
                "legacy_tab_action",
                "https://bb.sustech.edu.cn/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1",
            ),
        ]

        for name, url in checkpoints:
            try:
                response = self.session.get(
                    url,
                    headers=self.headers,
                    timeout=20,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                print(f"BB verification checkpoint {name} request failed: {exc}")
                continue

            if response.status_code >= 500:
                if "Could not initialize class org.springframework.aop.config.AopConfigUtils" in (response.text or ""):
                    print(
                        f"BB verification checkpoint {name} hit server-side Spring init error; trying next checkpoint."
                    )
                else:
                    print(
                        f"BB verification checkpoint {name} returned server error: {response.status_code}"
                    )
                continue

            if response.status_code != 200:
                print(
                    f"BB verification checkpoint {name} returned non-200: {response.status_code}"
                )
                continue

            if self._is_probable_cas_login_page(response):
                print(
                    f"BB verification checkpoint {name} redirected to CAS login page."
                )
                continue

            # 若能访问任一业务页面且未跳回 CAS，即认为会话有效。
            print(f"Login BB successfully and verified by checkpoint: {name}")
            return True

        return False

    def LoginBB(self):
        if self.TGC is None:
            print("TGC cookie not found. Please login CAS first.")
            return False

        # 1. 通过 CAS 认证并获取跳转到 BB 的票据 URL
        response = self.session.get(
            "https://cas.sustech.edu.cn/cas/login",
            headers=self.headers,
            cookies={"TGC": self.TGC},
            allow_redirects=False,
            timeout=20,
            params={
                "service": "https://bb.sustech.edu.cn/webapps/portal/execute/defaultTab"
            },
        )

        if response.status_code != 302:
            print(
                "Failed to get BB ticket from CAS. Status code:", response.status_code
            )
            return False

        # 2. 访问票据 URL，让 session 登录 BB
        url_bb_ticket = response.headers.get("Location")
        if not url_bb_ticket:
            print("Failed to get BB ticket location from CAS response headers.")
            return False

        confirm_response = self.session.get(
            url_bb_ticket, headers=self.headers, allow_redirects=True, timeout=20
        )

        if confirm_response.status_code != 200:
            print(
                "Failed to confirm BB login. Status code:", confirm_response.status_code
            )
            return False

        # 3. 多端点验证，规避 BB 某些页面偶发 500。
        if self._verify_bb_session():
            return True

        # 4. 兜底：即使验证页面异常，只要没跳回 CAS，也允许后续 API 再次验证。
        if not self._is_probable_cas_login_page(confirm_response):
            print(
                "BB verification checkpoints unavailable, but CAS ticket exchange succeeded; continue with caution."
            )
            return True

        print("BB login verification failed: all checkpoints unavailable or redirected to CAS.")
        return False

    def queryCalendar(self, start_date: datetime, end_date: datetime):
        """
        查询 Blackboard 日历事件。

        :param start_date: 查询范围的开始时间 (datetime object)
        :param end_date: 查询范围的结束时间 (datetime object)
        :return: 包含日历事件的列表 (list of dicts)，如果失败则返回 None
        """
        cst = datetime.timezone(datetime.timedelta(hours=8))

        def _normalize_window_start(dt: datetime.datetime) -> datetime.datetime:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=cst)
            dt = dt.astimezone(cst)
            return dt.replace(hour=0, minute=0, second=0, microsecond=0)

        start_local = _normalize_window_start(start_date)
        end_local = _normalize_window_start(end_date)
        if end_local <= start_local:
            end_local = start_local + datetime.timedelta(days=1)

        # 将 datetime 对象转换为毫秒级 Unix 时间戳（归一化到上海时区日界线）
        start_ts = int(start_local.timestamp() * 1000)
        end_ts = int(end_local.timestamp() * 1000)
        print(
            "[bb-debug] normalized query window "
            f"start_cst={start_local.isoformat()} end_cst={end_local.isoformat()}"
        )
        def _query_once() -> Optional[list]:
            headers = self._calendar_request_headers()
            self._warmup_calendar_context(headers)

            # 第一阶段：整段重试，优先拿到完整区间。
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                data = self._request_calendar_window(
                    headers,
                    start_ts,
                    end_ts,
                    phase="primary",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if data is not None:
                    print(
                        f"Query BB calendar successfully! attempt={attempt}, events={len(data)}"
                    )
                    return data

                if attempt < max_attempts:
                    backoff = float(attempt)
                    print(
                        "Retrying BB calendar query in "
                        f"{backoff:.1f}s (attempt {attempt + 1}/{max_attempts})..."
                    )
                    time.sleep(backoff)
                    self._warmup_calendar_context(headers)

            # 第二阶段：分片查询，降低单次请求压力并尽量规避节点抖动。
            # 经验上 42 天窗口与浏览器手工请求更接近，稳定性更高。
            chunk_days = 42
            merged_events = []
            seen_ids = set()
            failed_chunks = 0
            chunk_count = 0
            failed_chunk_labels = []

            cursor = start_local
            while cursor < end_local:
                chunk_count += 1
                next_cursor = min(cursor + datetime.timedelta(days=chunk_days), end_local)
                chunk_start_ts = int(cursor.timestamp() * 1000)
                chunk_end_ts = int(next_cursor.timestamp() * 1000)
                chunk_label = (
                    f"{cursor.strftime('%Y-%m-%d')} -> {next_cursor.strftime('%Y-%m-%d')}"
                )

                chunk_data = None
                for chunk_attempt in range(1, 3):
                    chunk_data = self._request_calendar_window(
                        headers,
                        chunk_start_ts,
                        chunk_end_ts,
                        phase="chunked",
                        attempt=chunk_attempt,
                        max_attempts=2,
                        chunk_label=chunk_label,
                    )
                    if chunk_data is not None:
                        break
                    if chunk_attempt < 2:
                        time.sleep(0.8)
                        self._warmup_calendar_context(headers)

                if chunk_data is None:
                    failed_chunks += 1
                    failed_chunk_labels.append(chunk_label)
                    print(
                        "Chunk query failed: "
                        f"{chunk_label}"
                    )
                else:
                    for item in chunk_data:
                        event_key = item.get("id") or item.get("itemSourceId")
                        if not event_key:
                            event_key = json.dumps(item, sort_keys=True, ensure_ascii=True)
                        if event_key in seen_ids:
                            continue
                        seen_ids.add(event_key)
                        merged_events.append(item)

                cursor = next_cursor

            if failed_chunks == 0:
                print(
                    "Query BB calendar succeeded via chunked fallback. "
                    f"chunks={chunk_count}, events={len(merged_events)}"
                )
                return merged_events

            if merged_events:
                print(
                    "Query BB calendar partially succeeded via chunked fallback. "
                    f"failed_chunks={failed_chunks}/{chunk_count}, events={len(merged_events)}"
                )
                if failed_chunk_labels:
                    print(
                        "[bb-debug] failed chunk list: "
                        + "; ".join(failed_chunk_labels)
                    )
                return merged_events

            if failed_chunk_labels:
                print(
                    "[bb-debug] failed chunk list: "
                    + "; ".join(failed_chunk_labels)
                )
            return None

        data = _query_once()
        if data is not None:
            return data

        # 第三阶段：重建会话并重新交换 BB ticket，再完整重试一轮。
        # 目的：绕过偶发异常节点或失效上下文，避免稳定命中 500。
        print("[bb-debug] restarting BB session after failed primary/chunked query...")
        try:
            self.session = requests.Session()
            if not self.LoginBB():
                print("[bb-debug] BB session restart failed during LoginBB.")
            else:
                data = _query_once()
                if data is not None:
                    print("[bb-debug] BB query recovered after session restart.")
                    return data
        except Exception as exc:
            print(f"[bb-debug] session restart retry exception: {exc}")

        print("Query BB calendar failed after retries, chunked fallback, and session restart.")
        return None


if __name__ == "__main__":
    bb = bbService()
    bb.Login()
    bb.LoginBB()

    # 示例：查询从今天起未来 90 天的日历事件
    today = datetime.datetime.now()
    future_date = today + datetime.timedelta(days=90)
    calendar_events = bb.queryCalendar(today, future_date)

    if calendar_events:
        # 打印查询到的事件数量和第一个事件作为示例
        print(f"Found {len(calendar_events)} events.")
        if len(calendar_events) > 0:
            print(
                "First event:",
                json.dumps(calendar_events[0], indent=2, ensure_ascii=False),
            )

    bb.Logout()
