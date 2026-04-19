import os
import json
import threading
import time
import sys
from importlib import import_module
from datetime import datetime, timedelta
import requests
from flask import Flask, Response, request, abort
from tisService import TisService
from bbService import bbService
from ics import Calendar, Event
import pytz
import re


def _load_kv_client():
    # 1) Prefer official Upstash Redis client.
    # Supports both native Upstash env names and Vercel KV env names.
    try:
        Redis = import_module("upstash_redis").Redis
        url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get(
            "KV_REST_API_URL"
        )
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get(
            "KV_REST_API_TOKEN"
        )

        if url and token:
            print("[kv] using upstash_redis client (url/token)")
            return Redis(url=url, token=token)

        print("[kv] using upstash_redis client (from_env)")
        return Redis.from_env()
    except Exception as exc:
        print(f"[kv] upstash_redis init failed: {exc}")

    # 2) Backward compatibility for old vercel_kv usage.
    try:
        print("[kv] using vercel_kv client")
        return import_module("vercel_kv").kv
    except Exception as exc:
        print(f"[kv] vercel_kv import failed: {exc}")
        return None


kv = _load_kv_client()

SCHEDULER_MODULE_PATH = os.environ.get("SCHEDULER_MODULE_PATH", "scheduler")
CAS_QR_AUTH_MODULE_PATH = os.environ.get("CAS_QR_AUTH_MODULE_PATH", "cas_qr_auth")

try:
    scheduler_module = import_module(SCHEDULER_MODULE_PATH)
    Scheduler = scheduler_module.Scheduler
    EventSource = scheduler_module.EventSource
except Exception as exc:
    Scheduler = None
    EventSource = None
    _SCHEDULER_IMPORT_ERROR = exc
else:
    _SCHEDULER_IMPORT_ERROR = None

try:
    from holiday_provider import HolidayProvider
except Exception as exc:
    HolidayProvider = None
    _HOLIDAY_PROVIDER_IMPORT_ERROR = exc
else:
    _HOLIDAY_PROVIDER_IMPORT_ERROR = None

try:
    CasQRSessionManager = import_module(CAS_QR_AUTH_MODULE_PATH).CasQRSessionManager
except Exception as exc:
    CasQRSessionManager = None
    _QR_IMPORT_ERROR = exc
else:
    _QR_IMPORT_ERROR = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return token[:6] + "..." + token[-6:]


def _sanitize_storage_mode(mode: str) -> str:
    candidate = (mode or "dual").strip().lower()
    if candidate not in {"kv", "db", "dual"}:
        return "dual"
    return candidate


def _sanitize_location_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    cleaned = prefix.strip()
    # Vercel 环境变量有时会把引号作为值的一部分传入。
    cleaned = cleaned.strip('"').strip("'").strip()
    return cleaned


# --- 配置 ---
# 这些将从 Vercel 的环境变量中读取
SUSTECH_SID = os.environ.get("SUSTECH_SID")
SUSTECH_PASSWORD = os.environ.get("SUSTECH_PASSWORD")
ICAL_TOKEN = os.environ.get("ICAL_TOKEN")
CRON_TOKEN = os.environ.get("CRON_TOKEN")  # Cron Job 的安全令牌
CRON_SECRET = os.environ.get("CRON_SECRET")  # Vercel Cron Bearer 令牌
HOLIDAY_API_TEMPLATE = os.environ.get(
    "HOLIDAY_API_TEMPLATE", "https://date.nager.at/api/v3/PublicHolidays/{year}/CN"
)
HOLIDAY_API_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("HOLIDAY_API_TIMEOUT_SECONDS", "8"))
)

REQUESTED_STORAGE_MODE = _sanitize_storage_mode(
    os.environ.get("SCHEDULE_STORAGE_MODE", "dual")
)
SCHEDULE_DB_PATH = os.environ.get(
    "SCHEDULE_DB_PATH", os.path.join("data", "scheduler.db")
)

IS_DOCKER = os.path.exists("/.dockerenv")
CAS_QR_BOOTSTRAP_ENABLED = _env_bool("CAS_QR_BOOTSTRAP_ENABLED", IS_DOCKER)
CAS_QR_BOOTSTRAP_REFRESH_SECONDS = max(
    60, int(os.environ.get("CAS_QR_BOOTSTRAP_REFRESH_SECONDS", "300"))
)
CAS_QR_BOOTSTRAP_POLL_INTERVAL = max(
    0.5, float(os.environ.get("CAS_QR_BOOTSTRAP_POLL_INTERVAL", "3"))
)
CAS_QR_BOOTSTRAP_RESTART_DELAY = max(
    1.0, float(os.environ.get("CAS_QR_BOOTSTRAP_RESTART_DELAY", "2"))
)
CAS_QR_BOOTSTRAP_SHOW_URL = _env_bool("CAS_QR_BOOTSTRAP_SHOW_URL", True)
CAS_QR_ALLOW_PASSWORD_FALLBACK = _env_bool("CAS_QR_ALLOW_PASSWORD_FALLBACK", True)
ICS_ASYNC_REFRESH_ENABLED = _env_bool("ICS_ASYNC_REFRESH_ENABLED", True)
ICS_ASYNC_REFRESH_MIN_INTERVAL = max(
    5, int(os.environ.get("ICS_ASYNC_REFRESH_MIN_INTERVAL", "180"))
)
TIS_EXCLUDE_HOLIDAY_EVENTS = _env_bool("TIS_EXCLUDE_HOLIDAY_EVENTS", True)

# --- 通用配置 ---
SCHEDULE_FETCH_RANGE_DAYS = 120
SCHEDULE_PAST_DAYS = 30
SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")

# --- TIS 特定配置 ---
TIS_CACHE_KEY = "tis_schedule_ics"  # Vercel KV 中的键名
CLASS_TIME_MAP = {
    1: ("08:00", "09:50"),
    3: ("10:20", "12:10"),
    5: ("14:00", "15:50"),
    7: ("16:20", "18:10"),
    9: ("19:00", "20:50"),
    11: ("21:00", "21:50"),
}

# --- Blackboard 特定配置 ---
BB_CACHE_KEY = "bb_schedule_ics"  # Vercel KV 中的键名
BB_ICAL_FEED_URL = os.environ.get("BB_ICAL_FEED_URL")
APP_FEATURES_VERSION = "2026-04-19-bb-fallback-ics-async"
APP_BUILD_COMMIT = os.environ.get("VERCEL_GIT_COMMIT_SHA") or os.environ.get(
    "GIT_COMMIT_SHA"
)
APP_RUNTIME_ENV = os.environ.get("VERCEL_ENV") or os.environ.get("ENV") or "unknown"

app = Flask(__name__)

_RUNTIME_CAS_TOKEN_LOCK = threading.Lock()
_RUNTIME_CAS_TOKEN = None
_QR_THREAD_STARTED = False
_REFRESH_SOURCE_LOCKS = {
    "tis": threading.Lock(),
    "bb": threading.Lock(),
}
_ASYNC_REFRESH_STATE_LOCK = threading.Lock()
_LAST_ASYNC_REFRESH_AT = {
    "tis": 0.0,
    "bb": 0.0,
}


def _set_runtime_cas_token(token: str | None) -> None:
    if not token:
        return
    global _RUNTIME_CAS_TOKEN
    with _RUNTIME_CAS_TOKEN_LOCK:
        _RUNTIME_CAS_TOKEN = token


def _get_runtime_cas_token() -> str | None:
    with _RUNTIME_CAS_TOKEN_LOCK:
        return _RUNTIME_CAS_TOKEN


def _init_scheduler():
    if REQUESTED_STORAGE_MODE not in {"db", "dual"}:
        return None, REQUESTED_STORAGE_MODE

    if Scheduler is None:
        print(
            f"[storage] scheduler import failed ({SCHEDULER_MODULE_PATH}), "
            f"fallback to kv: {_SCHEDULER_IMPORT_ERROR}"
        )
        return None, "kv"

    try:
        scheduler = Scheduler(db_path=SCHEDULE_DB_PATH)
        return scheduler, REQUESTED_STORAGE_MODE
    except Exception as exc:
        print(f"[storage] scheduler init failed, fallback to kv: {exc}")
        return None, "kv"


def _init_holiday_provider():
    if HolidayProvider is None:
        print(f"[holiday] provider import failed: {_HOLIDAY_PROVIDER_IMPORT_ERROR}")
        return None
    try:
        return HolidayProvider(
            api_template=HOLIDAY_API_TEMPLATE,
            timeout_seconds=HOLIDAY_API_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        print(f"[holiday] provider init failed: {exc}")
        return None


SCHEDULER, STORAGE_MODE = _init_scheduler()
HOLIDAY_PROVIDER = (
    getattr(SCHEDULER, "holiday_provider", None)
    if SCHEDULER is not None
    else _init_holiday_provider()
)
if REQUESTED_STORAGE_MODE != STORAGE_MODE:
    print(f"[storage] requested={REQUESTED_STORAGE_MODE}, effective={STORAGE_MODE}")

print(
    "[boot] "
    f"app_features_version={APP_FEATURES_VERSION} "
    f"storage_mode={STORAGE_MODE} "
    f"scheduler_module_path={SCHEDULER_MODULE_PATH} "
    f"cas_qr_auth_module_path={CAS_QR_AUTH_MODULE_PATH} "
    f"kv_client_available={kv is not None} "
    f"bb_fallback_configured={bool(BB_ICAL_FEED_URL)} "
    f"holiday_provider_available={HOLIDAY_PROVIDER is not None} "
    f"tis_exclude_holiday_events={TIS_EXCLUDE_HOLIDAY_EVENTS}"
)


def _kv_set(key: str, value: str) -> bool:
    if kv is None:
        print(f"[kv] client unavailable, skip set for {key}")
        return False
    try:
        kv.set(key, value)
        return True
    except Exception as exc:
        print(f"[kv] set failed for {key}: {exc}")
        return False


def _kv_get(key: str):
    if kv is None:
        print(f"[kv] client unavailable, skip get for {key}")
        return None
    try:
        return kv.get(key)
    except Exception as exc:
        print(f"[kv] get failed for {key}: {exc}")
        return None


def _serialize_calendar(cal: Calendar) -> str:
    """Use explicit serialize API to avoid upstream deprecation warnings."""
    serialize = getattr(cal, "serialize", None)
    if callable(serialize):
        return serialize()
    return str(cal)


# =================================================================
# 数据转换函数 (与之前基本相同)
# =================================================================


def convert_tis_json_to_ical(schedule_json, holiday_provider=None):
    """将从 TIS 获取的课表 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    data = (
        json.loads(schedule_json) if isinstance(schedule_json, str) else schedule_json
    )
    location_prefix = _sanitize_location_prefix(os.getenv("LOCATION_PREFIX"))

    # COURSE_NAME_FILTER is an exclusion list in JSON array format.
    excluded_course_keywords = []
    raw_course_filter = os.getenv("COURSE_NAME_FILTER")
    if raw_course_filter:
        try:
            parsed = json.loads(raw_course_filter)
            if isinstance(parsed, list):
                excluded_course_keywords = [
                    str(keyword).strip() for keyword in parsed if str(keyword).strip()
                ]
        except ValueError:
            pass

    for date_str, events in data.items():
        for item in events or []:
            class_index = item.get("KSJC") or item.get("ksjc")
            course_name = item.get("KCMC") or item.get("kcmc")
            if not class_index or not course_name:
                continue

            try:
                class_index = int(class_index)
            except (TypeError, ValueError):
                continue

            start_time_str, end_time_str = CLASS_TIME_MAP.get(class_index, (None, None))
            if not start_time_str:
                continue
            event_day = item.get("SJ") or item.get("sj") or date_str

            holiday_name = None
            if holiday_provider is not None and holiday_provider.is_holiday(event_day):
                holiday_name = holiday_provider.holiday_name(event_day) or "Holiday"
                if TIS_EXCLUDE_HOLIDAY_EVENTS:
                    continue

            naive_begin = datetime.strptime(
                f"{event_day} {start_time_str}:00", "%Y-%m-%d %H:%M:%S"
            )
            naive_end = datetime.strptime(
                f"{event_day} {end_time_str}:00", "%Y-%m-%d %H:%M:%S"
            )
            e = Event()
            e.name = course_name

            # Exclude events whose course names match any configured keyword.
            if excluded_course_keywords and any(
                keyword in e.name for keyword in excluded_course_keywords
            ):
                continue

            e.begin = SHANGHAI_TZ.localize(naive_begin)
            e.end = SHANGHAI_TZ.localize(naive_end)
            location_str = item.get("NR") or item.get("nr")
            if location_str:
                replace_dict = {
                    "一教": "第一教学楼",
                    "二教": "第二教学楼",
                    "三教": "第三教学楼",
                    "智华": "智华教学楼",
                }
                for short, full in replace_dict.items():
                    if short in location_str:
                        location_str = location_str.replace(short, full)
                        break
                e.location = (
                    f"{location_prefix}{location_str}"
                    if location_prefix
                    else location_str
                )
            teacher_name = "N/A"
            bt_string = item.get("BT", "") or item.get("bt", "")
            if ":" in bt_string:
                match = re.match(r"([^\d]*)", bt_string.split(":", 1)[1])
                if match:
                    teacher_name = match.group(1)
            description = f"教师: {teacher_name}\n课程标题: {bt_string}"
            if holiday_name:
                description = description + f"\nHoliday anomaly: {holiday_name}"
                e.status = "CANCELLED"
            e.description = description
            cal.events.add(e)
    return _serialize_calendar(cal)


def convert_bb_json_to_ical(events_json):
    """将从 Blackboard 获取的日历事件 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    for item in events_json or []:
        e = Event()
        course_name = item.get("calendarName", "Unknown Course")
        event_title = item.get("title", "未命名事件")
        event_type = item.get("eventType", "Event")
        e.name = (
            f"「{course_name}」 {event_title}"
            if event_type in ["Assignment", "作业"]
            else f"[{course_name}] {event_title}"
        )
        try:
            start_str = (
                item["startDate"][:-1] + "+00:00"
                if item["startDate"].endswith("Z")
                else item["startDate"]
            )
            end_str = (
                item["endDate"][:-1] + "+00:00"
                if item["endDate"].endswith("Z")
                else item["endDate"]
            )
            e.begin = datetime.fromisoformat(start_str)
            e.end = datetime.fromisoformat(end_str)
        except (KeyError, ValueError):
            continue
        e.description = f"Course: {course_name}\nDescription: Deadline from Blackboard"
        cal.events.add(e)
    return _serialize_calendar(cal)


def _persist_tis_to_db(schedule_data: dict) -> None:
    if SCHEDULER is None or STORAGE_MODE not in {"db", "dual"}:
        return
    count = SCHEDULER.replace_tis_raw_schedule(schedule_data, clear_old=True)
    print(f"TIS db sync completed, events={count}")


def _persist_bb_to_db(events_json: list) -> None:
    if SCHEDULER is None or STORAGE_MODE not in {"db", "dual"}:
        return
    count = SCHEDULER.replace_bb_raw_events(events_json, clear_old=True)
    print(f"BB db sync completed, events={count}")


def _read_calendar_payload(source: str):
    source = source.lower()
    cache_key = TIS_CACHE_KEY if source == "tis" else BB_CACHE_KEY

    if SCHEDULER is not None and STORAGE_MODE in {"db", "dual"}:
        source_name = "tis" if source == "tis" else "bb"
        if EventSource is not None:
            source_name = (
                EventSource.TIS.value if source == "tis" else EventSource.BB.value
            )
        if len(SCHEDULER.query_events(source=source_name, limit=1)) > 0:
            return SCHEDULER.export_ics(source=source_name)

    if STORAGE_MODE in {"kv", "dual"}:
        return _kv_get(cache_key)

    return None


# =================================================================
# 数据抓取与缓存函数 (修改为写入 Vercel KV)
# =================================================================


def fetch_and_cache_tis_schedule():
    """抓取 TIS 课表并写入启用的存储后端。"""
    print("Fetching new schedule from TIS...")
    service = TisService(tgc_token=_get_runtime_cas_token())
    if not service.Login(
        use_qr=False, allow_password_fallback=CAS_QR_ALLOW_PASSWORD_FALLBACK
    ):
        raise ConnectionError("TIS CAS Login Failed.")
    if not service.LoginTIS():
        # 运行时 token 可能过期，重试一次完整登录流程。
        service.TGC = None
        if not service.Login(
            use_qr=_env_bool("CAS_USE_QR_LOGIN", False),
            allow_password_fallback=CAS_QR_ALLOW_PASSWORD_FALLBACK,
        ):
            raise ConnectionError("TIS CAS Login Failed (retry).")
        if not service.LoginTIS():
            raise ConnectionError("TIS Login Failed.")

    _set_runtime_cas_token(service.TGC)

    today = datetime.now(SHANGHAI_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = today - timedelta(days=SCHEDULE_PAST_DAYS)
    end_date = today + timedelta(days=SCHEDULE_FETCH_RANGE_DAYS)
    schedule_data = service.queryScheduleInterval(
        start_date.strftime("%Y-%m-%d"),
        end_date.strftime("%Y-%m-%d"),
        holiday_provider=HOLIDAY_PROVIDER,
        skip_holidays=TIS_EXCLUDE_HOLIDAY_EVENTS,
    )

    ical_data = convert_tis_json_to_ical(
        schedule_data,
        holiday_provider=HOLIDAY_PROVIDER,
    )

    if STORAGE_MODE in {"kv", "dual"}:
        kv_updated = _kv_set(TIS_CACHE_KEY, ical_data)
        if kv_updated:
            print("TIS cache updated successfully in Vercel KV.")
        else:
            print("TIS cache update skipped/failed in Vercel KV.")
            if STORAGE_MODE == "kv":
                raise ConnectionError("TIS cache update failed in kv mode.")

    if STORAGE_MODE in {"db", "dual"}:
        _persist_tis_to_db(schedule_data)

    return ical_data


def fetch_and_cache_bb_schedule():
    """抓取 Blackboard 日历事件并写入启用的存储后端。"""
    print("Fetching new schedule from Blackboard...")
    service = bbService(tgc_token=_get_runtime_cas_token())
    if not service.Login(
        use_qr=False, allow_password_fallback=CAS_QR_ALLOW_PASSWORD_FALLBACK
    ):
        raise ConnectionError("BB CAS Login Failed.")
    if not service.LoginBB():
        service.TGC = None
        if not service.Login(
            use_qr=_env_bool("CAS_USE_QR_LOGIN", False),
            allow_password_fallback=CAS_QR_ALLOW_PASSWORD_FALLBACK,
        ):
            raise ConnectionError("BB CAS Login Failed (retry).")
        if not service.LoginBB():
            raise ConnectionError("BB Login Failed.")

    _set_runtime_cas_token(service.TGC)

    today = datetime.now()
    start_date = today - timedelta(days=SCHEDULE_PAST_DAYS)
    end_date = today + timedelta(days=SCHEDULE_FETCH_RANGE_DAYS)
    schedule_data = None
    ical_data = None
    primary_failure = None

    try:
        schedule_data = service.queryCalendar(start_date, end_date)
    except Exception as exc:
        primary_failure = f"queryCalendar exception: {exc}"

    if schedule_data:
        print(f"[info] BB primary fetch succeeded, events={len(schedule_data)}")
        ical_data = convert_bb_json_to_ical(schedule_data)
    else:
        if primary_failure is None:
            primary_failure = "empty payload from queryCalendar"

        if BB_ICAL_FEED_URL:
            print(
                f"[info] BB primary fetch failed ({primary_failure}), fallback to BB_ICAL_FEED_URL"
            )
            try:
                feed_response = requests.get(BB_ICAL_FEED_URL, timeout=20)
                feed_response.raise_for_status()
                fallback_ical = (feed_response.text or "").strip()
                if not fallback_ical:
                    raise ValueError("empty response body")
                ical_data = fallback_ical
                print(
                    f"[info] BB fallback feed fetch succeeded, bytes={len(fallback_ical)}"
                )
            except Exception as exc:
                raise ConnectionError(f"BB fallback feed fetch failed: {exc}") from exc
        else:
            raise ConnectionError(
                "BB calendar query failed and no fallback feed configured: "
                f"{primary_failure}"
            )

    if STORAGE_MODE in {"kv", "dual"}:
        kv_updated = _kv_set(BB_CACHE_KEY, ical_data)
        if kv_updated:
            print("Blackboard cache updated successfully in Vercel KV.")
        else:
            print("Blackboard cache update skipped/failed in Vercel KV.")
            if STORAGE_MODE == "kv":
                raise ConnectionError("BB cache update failed in kv mode.")

    if STORAGE_MODE in {"db", "dual"}:
        _persist_bb_to_db(schedule_data or [])

    return ical_data


def _refresh_source_with_lock(
    source: str, trigger: str, blocking: bool = False
) -> dict:
    lock = _REFRESH_SOURCE_LOCKS.get(source)
    if lock is None:
        return {
            "ok": False,
            "source": source,
            "status": "error",
            "message": "unknown source",
            "trigger": trigger,
            "duration_ms": 0,
        }

    started = time.time()
    acquired = lock.acquire(blocking=blocking)
    if not acquired:
        return {
            "ok": False,
            "source": source,
            "status": "skipped",
            "message": "refresh already running",
            "trigger": trigger,
            "duration_ms": int((time.time() - started) * 1000),
        }

    result = {
        "ok": True,
        "source": source,
        "status": "success",
        "message": "",
        "trigger": trigger,
    }
    try:
        payload = (
            fetch_and_cache_tis_schedule()
            if source == "tis"
            else fetch_and_cache_bb_schedule()
        )
        if isinstance(payload, str):
            result["payload_size"] = len(payload)
        elif isinstance(payload, (list, dict)):
            result["payload_size"] = len(payload)
        else:
            result["payload_size"] = None
    except Exception as exc:
        result["ok"] = False
        result["status"] = "failed"
        result["message"] = str(exc)
    finally:
        result["duration_ms"] = int((time.time() - started) * 1000)
        lock.release()

    return result


def _trigger_async_refresh(source: str, reason: str) -> dict:
    if not ICS_ASYNC_REFRESH_ENABLED:
        return {"scheduled": False, "reason": "disabled"}

    if source not in _REFRESH_SOURCE_LOCKS:
        return {"scheduled": False, "reason": "unknown-source"}

    now = time.time()
    with _ASYNC_REFRESH_STATE_LOCK:
        elapsed = now - _LAST_ASYNC_REFRESH_AT[source]
        if elapsed < ICS_ASYNC_REFRESH_MIN_INTERVAL:
            return {
                "scheduled": False,
                "reason": "cooldown",
                "retry_after_seconds": int(ICS_ASYNC_REFRESH_MIN_INTERVAL - elapsed),
            }

        if _REFRESH_SOURCE_LOCKS[source].locked():
            return {"scheduled": False, "reason": "running"}

        _LAST_ASYNC_REFRESH_AT[source] = now

    def _worker() -> None:
        print(f"[ics-refresh] start source={source} reason={reason}")
        refresh_result = _refresh_source_with_lock(
            source, trigger=f"ics:{reason}", blocking=False
        )
        print(
            "[ics-refresh] done "
            f"source={source} status={refresh_result.get('status')} "
            f"duration_ms={refresh_result.get('duration_ms')} "
            f"message={refresh_result.get('message', '')}"
        )

    thread = threading.Thread(
        target=_worker,
        name=f"ics-refresh-{source}",
        daemon=True,
    )
    thread.start()
    return {"scheduled": True, "reason": "started"}


def _run_qr_bootstrap_loop() -> None:
    if CasQRSessionManager is None:
        print(f"[qr] cas qr manager unavailable: {_QR_IMPORT_ERROR}")
        return

    print("[qr] bootstrap thread started")
    sys.stdout.flush()
    manager = CasQRSessionManager(ttl_seconds=CAS_QR_BOOTSTRAP_REFRESH_SECONDS)

    while True:
        try:
            payload = manager.create_session()
            session_id = str(payload.get("session_id") or "")
            signature = str(payload.get("signature") or "")
            qr_url = str(payload.get("qr_url") or "")
            expires_in = int(
                payload.get("expires_in") or CAS_QR_BOOTSTRAP_REFRESH_SECONDS
            )
            qr_ascii = payload.get("qr_ascii")

            print("\n=== CAS QR Bootstrap ===")
            if CAS_QR_BOOTSTRAP_SHOW_URL and qr_url:
                print(f"[qr] url={qr_url}")
            if isinstance(qr_ascii, str) and qr_ascii.strip():
                print("--- Scan This QR (ASCII) ---")
                print(qr_ascii)
            print(f"[qr] expires_in={expires_in}s")
            sys.stdout.flush()

            start = time.time()
            last_status = None
            authorized = False
            while time.time() - start <= expires_in + 5:
                status_payload = manager.poll_status(session_id, signature)
                status = str(status_payload.get("status") or "unknown")

                if status != last_status:
                    print(f"[qr] status={status}")
                    sys.stdout.flush()
                    last_status = status

                if status == "authorized":
                    exchange_code = str(status_payload.get("exchange_code") or "")
                    token_payload = manager.exchange_token(
                        session_id, signature, exchange_code
                    )
                    if token_payload.get("ok"):
                        cas_token = str(token_payload.get("cas_token") or "")
                        _set_runtime_cas_token(cas_token)
                        print(f"[qr] CAS token acquired: {_mask_token(cas_token)}")
                    else:
                        print(f"[qr] token exchange failed: {token_payload}")
                    sys.stdout.flush()
                    authorized = True
                    break

                if status in {"cancel", "warning", "expired", "invalid_signature"}:
                    break

                time.sleep(CAS_QR_BOOTSTRAP_POLL_INTERVAL)

            if authorized:
                # 成功后等待当前二维码周期结束再重打，避免日志刷屏。
                time.sleep(max(30.0, float(expires_in)))
            else:
                time.sleep(CAS_QR_BOOTSTRAP_RESTART_DELAY)
        except Exception as exc:
            print(f"[qr] bootstrap loop error: {exc}")
            sys.stdout.flush()
            time.sleep(CAS_QR_BOOTSTRAP_RESTART_DELAY)


def _start_background_workers() -> None:
    global _QR_THREAD_STARTED
    if _QR_THREAD_STARTED:
        return
    if not CAS_QR_BOOTSTRAP_ENABLED:
        return
    if CasQRSessionManager is None:
        print(f"[qr] disabled because manager import failed: {_QR_IMPORT_ERROR}")
        return

    thread = threading.Thread(
        target=_run_qr_bootstrap_loop,
        name="cas-qr-bootstrap",
        daemon=True,
    )
    thread.start()
    _QR_THREAD_STARTED = True


_start_background_workers()

# =================================================================
# Flask 路由
# =================================================================


def _is_cron_request_authorized() -> bool:
    auth_header = request.headers.get("Authorization", "")
    if CRON_SECRET and auth_header == f"Bearer {CRON_SECRET}":
        return True

    # 兼容历史 query 参数鉴权方式。
    cron_token = request.args.get("cron_token")
    if CRON_TOKEN and cron_token == CRON_TOKEN:
        return True

    return False


@app.route("/api/cron/fetch")
def cron_fetch_handler():
    """由 Vercel Cron Job 调用的受保护的 API 端点"""
    request_started_at = time.time()
    request_id = (
        request.headers.get("x-vercel-id")
        or request.headers.get("x-request-id")
        or "n/a"
    )
    source = request.args.get("source", "all").strip().lower()
    user_agent = request.headers.get("user-agent", "unknown")

    print(
        f"[cron] incoming request_id={request_id} source={source} user_agent={user_agent}"
    )

    # 安全检查
    if not _is_cron_request_authorized():
        print(f"[cron] unauthorized request_id={request_id}")
        abort(401, "Unauthorized: Invalid cron credential.")

    if source not in {"tis", "bb", "all"}:
        print(f"[cron] invalid source request_id={request_id} source={source}")
        return {
            "ok": False,
            "request_id": request_id,
            "error": f"invalid source: {source}",
        }, 400

    auth_mode = "query"
    auth_header = request.headers.get("Authorization", "")
    if CRON_SECRET and auth_header == f"Bearer {CRON_SECRET}":
        auth_mode = "bearer"
    print(f"[cron] authorized request_id={request_id} auth_mode={auth_mode}")

    steps = {}
    ordered_sources = ["tis", "bb"] if source == "all" else [source]
    for source_name in ordered_sources:
        print(f"[cron] refresh start request_id={request_id} source={source_name}")
        result = _refresh_source_with_lock(
            source_name,
            trigger=f"cron:{request_id}",
            blocking=False,
        )
        steps[source_name] = result
        print(
            "[cron] refresh done "
            f"request_id={request_id} source={source_name} "
            f"status={result.get('status')} duration_ms={result.get('duration_ms')} "
            f"message={result.get('message', '')}"
        )

    ok = all(step.get("status") in {"success", "skipped"} for step in steps.values())
    response = {
        "ok": ok,
        "request_id": request_id,
        "source": source,
        "auth_mode": auth_mode,
        "duration_ms": int((time.time() - request_started_at) * 1000),
        "steps": steps,
    }
    print(
        f"[cron] completed request_id={request_id} ok={ok} duration_ms={response['duration_ms']}"
    )
    return response


@app.route("/")
def health():
    return {
        "status": "ok",
        "app_features_version": APP_FEATURES_VERSION,
        "storage_mode": STORAGE_MODE,
        "storage_mode_requested": REQUESTED_STORAGE_MODE,
        "kv_client_available": kv is not None,
        "bb_fallback_configured": bool(BB_ICAL_FEED_URL),
        "holiday_provider_available": HOLIDAY_PROVIDER is not None,
        "tis_exclude_holiday_events": TIS_EXCLUDE_HOLIDAY_EVENTS,
        "ics_async_refresh_enabled": ICS_ASYNC_REFRESH_ENABLED,
        "qr_bootstrap_enabled": CAS_QR_BOOTSTRAP_ENABLED,
    }


@app.route("/app_features_version")
def app_features_version():
    """Deployment fingerprint endpoint for quick version verification."""
    return {
        "app_features_version": APP_FEATURES_VERSION,
        "build_commit": APP_BUILD_COMMIT,
        "runtime_env": APP_RUNTIME_ENV,
        "storage_mode": STORAGE_MODE,
        "bb_fallback_configured": bool(BB_ICAL_FEED_URL),
        "kv_client_available": kv is not None,
    }


@app.route("/tis/schedule.ics")
def get_tis_schedule():
    """提供 TIS 课表日历 (按存储模式读取)"""
    provided_token = request.args.get("token")
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, "Unauthorized: Invalid or missing token.")

    ical_data = _read_calendar_payload("tis")
    refresh_meta = _trigger_async_refresh("tis", reason="tis-ics-hit")
    print(f"[ics] source=tis cache_hit={bool(ical_data)} refresh={refresh_meta}")

    if not ical_data:
        return (
            "Calendar data is not yet available. Please wait for the next scheduled update (up to 12 hours) or trigger it manually if you are the admin.",
            404,
        )

    return Response(
        ical_data,
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=tis_schedule.ics"},
    )


@app.route("/blackboard/schedule.ics")
def get_bb_schedule():
    """提供 Blackboard DDL 日历 (按存储模式读取)"""
    provided_token = request.args.get("token")
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, "Unauthorized: Invalid or missing token.")

    ical_data = _read_calendar_payload("bb")
    refresh_meta = _trigger_async_refresh("bb", reason="bb-ics-hit")
    print(f"[ics] source=bb cache_hit={bool(ical_data)} refresh={refresh_meta}")

    if not ical_data:
        return (
            "Calendar data is not yet available. Please wait for the next scheduled update (up to 12 hours) or trigger it manually if you are the admin.",
            404,
        )

    return Response(
        ical_data,
        mimetype="text/calendar",
        headers={"Content-Disposition": "attachment; filename=bb_schedule.ics"},
    )


@app.route("/bb/schedule.ics")
def get_bb_schedule_alias():
    """兼容旧路径：转发到 blackboard 日历端点。"""
    return get_bb_schedule()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    print(f"Starting service on 0.0.0.0:{port}, storage={STORAGE_MODE}")
    sys.stdout.flush()
    app.run(host="0.0.0.0", port=port)
