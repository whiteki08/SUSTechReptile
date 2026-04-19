"""
Scheduler Module - 统一日程管理 (ICS 格式 + SQLModel ORM)

数据模型完全对齐 RFC 5545 (iCalendar) VEVENT 规范，
附加 EventSource / EventType 扩展字段。
使用 SQLModel 作为 ORM + DTO 层，避免手动编写 SQL。
所有时间以 **UTC** 存储，对外接口支持 Asia/Shanghai (UTC+8) 展示。
"""

import json
import uuid
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, List, Dict, Any

from sqlmodel import SQLModel, Field, Session, create_engine, select, col
from pydantic import field_validator

# ---------------------------------------------------------------------------
# 路径设置：兼容直接运行和包导入
# ---------------------------------------------------------------------------
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_DIR not in sys.path:
    sys.path.insert(0, _PROJECT_DIR)

try:
    from bbService import bbService
    from tisService import TisService
except ImportError:
    # Backward compatibility for old package layout.
    from sustech.bb import bbService
    from sustech.tis import TisService
try:
    from .holiday_provider import HolidayProvider
except ImportError:
    from holiday_provider import HolidayProvider

logger = logging.getLogger(__name__)

# ========================== 时区常量 ==========================
UTC = timezone.utc
CST = timezone(timedelta(hours=8))  # China Standard Time, UTC+8


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _ensure_utc(dt: datetime) -> datetime:
    """确保 datetime 带有 UTC 时区信息；无时区默认视为 CST"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(UTC)


def _to_utc_iso(dt: datetime) -> str:
    return _ensure_utc(dt).isoformat()


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _cst_str(iso_utc: Optional[str]) -> str:
    dt = _parse_iso(iso_utc)
    if dt is None:
        return iso_utc or ""
    return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S CST")


# ========================== 枚举 ==========================

class EventSource(str, Enum):
    """日程来源"""
    BB = "bb"              # Blackboard
    TIS = "tis"            # 教务系统 (Teaching Information System)
    PERSONAL = "personal"  # 个人创建


class EventType(str, Enum):
    """日程类型（扩展字段，非 ICS 标准）"""
    ASSIGNMENT = "assignment"
    EXAM = "exam"
    CLASS = "class"
    DEADLINE = "deadline"
    PERSONAL = "personal"
    MEETING = "meeting"
    HOLIDAY_ANOMALY = "holiday_anomaly"
    OTHER = "other"


class ICSStatus(str, Enum):
    """RFC 5545 STATUS"""
    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


class ICSTransp(str, Enum):
    """RFC 5545 TRANSP (Time Transparency)"""
    OPAQUE = "OPAQUE"          # 占用时间
    TRANSPARENT = "TRANSPARENT" # 不占用时间


class ICSClass(str, Enum):
    """RFC 5545 CLASS"""
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    CONFIDENTIAL = "CONFIDENTIAL"


# ========================== ICS VEVENT 数据模型 (SQLModel) ==========================

class VEvent(SQLModel, table=True):
    """
    ICS VEVENT - RFC 5545 日历事件数据模型

    字段命名对齐 RFC 5545 属性名（小写 + 下划线风格）。
    附加 x_source / x_event_type 等扩展字段（ICS 中以 X- 前缀表示扩展属性）。
    """
    __tablename__ = "vevent"

    # ── RFC 5545 核心属性 ──
    uid: str = Field(primary_key=True, max_length=255,
                     description="RFC5545 UID - 事件全局唯一标识")
    dtstamp: datetime = Field(description="RFC5545 DTSTAMP - 创建时的时间戳 (UTC)")
    dtstart: datetime = Field(index=True,
                              description="RFC5545 DTSTART - 事件开始时间 (UTC)")
    dtend: Optional[datetime] = Field(default=None, index=True,
                                      description="RFC5545 DTEND - 事件结束时间 (UTC)")
    duration: Optional[str] = Field(default=None, max_length=32,
                                    description="RFC5545 DURATION - ISO 8601 时段, 如 PT1H30M")

    summary: str = Field(default="", max_length=512,
                         description="RFC5545 SUMMARY - 事件摘要/标题")
    description: Optional[str] = Field(default=None,
                                       description="RFC5545 DESCRIPTION - 详细描述")
    location: Optional[str] = Field(default=None, max_length=512,
                                    description="RFC5545 LOCATION - 地点")
    url: Optional[str] = Field(default=None, max_length=1024,
                               description="RFC5545 URL - 关联链接")
    geo: Optional[str] = Field(default=None, max_length=64,
                               description="RFC5545 GEO - 地理坐标 'lat;lon'")

    # ── 分类与状态 ──
    status: str = Field(default=ICSStatus.CONFIRMED.value, max_length=16,
                        description="RFC5545 STATUS - TENTATIVE/CONFIRMED/CANCELLED")
    classification: str = Field(default=ICSClass.PUBLIC.value, max_length=16,
                                description="RFC5545 CLASS - PUBLIC/PRIVATE/CONFIDENTIAL")
    transp: str = Field(default=ICSTransp.OPAQUE.value, max_length=16,
                        description="RFC5545 TRANSP - OPAQUE/TRANSPARENT")
    categories: Optional[str] = Field(default=None, max_length=512,
                                      description="RFC5545 CATEGORIES - 逗号分隔的分类标签")
    priority: int = Field(default=0, ge=0, le=9,
                          description="RFC5545 PRIORITY - 0=未定义, 1=最高, 9=最低")

    # ── 组织者 / 参与者 ──
    organizer: Optional[str] = Field(default=None, max_length=512,
                                     description="RFC5545 ORGANIZER - 组织者")

    # ── 重复规则 ──
    rrule: Optional[str] = Field(default=None, max_length=512,
                                 description="RFC5545 RRULE - 重复规则, 如 FREQ=WEEKLY;BYDAY=MO,WE")
    exdate: Optional[str] = Field(default=None,
                                  description="RFC5545 EXDATE - 排除日期, 逗号分隔 ISO 字符串")
    recurrence_id: Optional[str] = Field(default=None, max_length=255,
                                         description="RFC5545 RECURRENCE-ID - 重复事件的特定实例 ID")

    # ── 版本控制 ──
    sequence: int = Field(default=0, ge=0,
                          description="RFC5545 SEQUENCE - 修订序号")
    created: datetime = Field(description="RFC5545 CREATED - 事件首次创建时间 (UTC)")
    last_modified: datetime = Field(description="RFC5545 LAST-MODIFIED - 最后修改时间 (UTC)")

    # ── 闹钟 / 提醒 ──
    valarm: Optional[str] = Field(default=None,
                                  description="RFC5545 VALARM - JSON: [{action, trigger, description}]")

    # ── 全天事件 ──
    all_day: bool = Field(default=False,
                          description="是否全天事件（ICS 中以 DATE 类型 DTSTART 表示）")

    # ── 扩展属性 (X-*) ──
    x_source: str = Field(index=True, max_length=32,
                          description="X-SOURCE - 数据来源: bb/tis/personal")
    x_source_id: str = Field(default="", max_length=255, index=True,
                             description="X-SOURCE-ID - 来源系统中的原始 ID")
    x_event_type: str = Field(default=EventType.OTHER.value, max_length=32,
                              description="X-EVENT-TYPE - 事件类型: assignment/exam/class/...")
    x_course_name: Optional[str] = Field(default=None, max_length=256,
                                         description="X-COURSE-NAME - 关联课程名称")
    x_course_id: Optional[str] = Field(default=None, max_length=128,
                                       description="X-COURSE-ID - 关联课程编号")
    x_raw_data: Optional[str] = Field(default=None,
                                      description="X-RAW-DATA - 原始 JSON 数据")

    # ── 软删除 ──
    is_deleted: bool = Field(default=False, index=True,
                             description="软删除标记")

    # ── Pydantic 校验 ──
    @field_validator("dtstart", "dtend", "dtstamp", "created", "last_modified", mode="before")
    @classmethod
    def _ensure_tz(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = datetime.fromisoformat(v)
        if isinstance(v, datetime) and v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    # ── 便捷方法 ──

    def to_cst_dict(self) -> dict:
        """返回所有 datetime 字段转为 CST 展示的 dict"""
        d = self.model_dump()
        for key in ("dtstart", "dtend", "dtstamp", "created", "last_modified"):
            val = d.get(key)
            if isinstance(val, datetime):
                d[key] = val.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S CST")
            elif isinstance(val, str) and val:
                d[key] = _cst_str(val)
        return d

    def to_ics_string(self) -> str:
        """
        导出为 ICS VEVENT 文本块

        Returns:
            RFC 5545 VEVENT 格式字符串
        """
        def _dt_fmt(dt: Optional[datetime]) -> str:
            if dt is None:
                return ""
            return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

        def _escape_ics_text(value: str) -> str:
            """Escape TEXT values per RFC 5545 §3.3.11."""
            value = value.replace("\r\n", "\n").replace("\r", "")
            value = value.replace("\\", "\\\\")
            value = value.replace(";", r"\;")
            value = value.replace(",", r"\,")
            value = value.replace("\n", r"\n")
            return value

        def _fold_ics_line(line: str) -> List[str]:
            """Fold a logical line at 75 octets (UTF-8) per RFC 5545 §3.1."""
            encoded = line.encode("utf-8")
            max_octets = 75
            if len(encoded) <= max_octets:
                return [line]
            parts: List[str] = []
            start = 0
            first = True
            while start < len(encoded):
                limit = max_octets if first else max_octets - 1
                end = min(start + limit, len(encoded))
                # Step back while pointing at a UTF-8 continuation byte (10xxxxxx)
                while end > start and (encoded[end - 1] & 0xC0) == 0x80:
                    end -= 1
                chunk = encoded[start:end].decode("utf-8", errors="strict")
                parts.append(chunk if first else " " + chunk)
                first = False
                start = end
            return parts

        out_lines: List[str] = []

        def _add_line(line: str) -> None:
            out_lines.extend(_fold_ics_line(line))

        _add_line("BEGIN:VEVENT")
        _add_line(f"UID:{self.uid}")
        _add_line(f"DTSTAMP:{_dt_fmt(self.dtstamp)}")
        _add_line(f"DTSTART:{_dt_fmt(self.dtstart)}")
        if self.dtend:
            _add_line(f"DTEND:{_dt_fmt(self.dtend)}")
        if self.duration:
            _add_line(f"DURATION:{self.duration}")
        if self.summary:
            _add_line(f"SUMMARY:{_escape_ics_text(self.summary)}")
        if self.description:
            _add_line(f"DESCRIPTION:{_escape_ics_text(self.description)}")
        if self.location:
            _add_line(f"LOCATION:{_escape_ics_text(self.location)}")
        if self.url:
            _add_line(f"URL:{self.url}")
        if self.status:
            _add_line(f"STATUS:{self.status}")
        if self.categories:
            _add_line(f"CATEGORIES:{_escape_ics_text(self.categories)}")
        if self.rrule:
            _add_line(f"RRULE:{self.rrule}")
        _add_line(f"SEQUENCE:{self.sequence}")
        _add_line(f"CREATED:{_dt_fmt(self.created)}")
        _add_line(f"LAST-MODIFIED:{_dt_fmt(self.last_modified)}")
        _add_line(f"CLASS:{self.classification}")
        _add_line(f"TRANSP:{self.transp}")
        if self.priority:
            _add_line(f"PRIORITY:{self.priority}")
        if self.organizer:
            _add_line(f"ORGANIZER:{self.organizer}")
        # 扩展属性
        _add_line(f"X-SOURCE:{_escape_ics_text(self.x_source)}")
        if self.x_source_id:
            _add_line(f"X-SOURCE-ID:{_escape_ics_text(self.x_source_id)}")
        if self.x_event_type:
            _add_line(f"X-EVENT-TYPE:{_escape_ics_text(self.x_event_type)}")
        if self.x_course_name:
            _add_line(f"X-COURSE-NAME:{_escape_ics_text(self.x_course_name)}")
        _add_line("END:VEVENT")
        return "\r\n".join(out_lines)


# ── 用于创建 / 更新的 DTO ──

class VEventCreate(SQLModel):
    """创建事件用的 DTO（不含自动生成的字段）"""
    summary: str
    dtstart: datetime
    dtend: Optional[datetime] = None
    duration: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    geo: Optional[str] = None
    status: str = ICSStatus.CONFIRMED.value
    classification: str = ICSClass.PUBLIC.value
    transp: str = ICSTransp.OPAQUE.value
    categories: Optional[str] = None
    priority: int = 0
    organizer: Optional[str] = None
    rrule: Optional[str] = None
    exdate: Optional[str] = None
    valarm: Optional[str] = None
    all_day: bool = False
    x_source: str = EventSource.PERSONAL.value
    x_source_id: str = ""
    x_event_type: str = EventType.PERSONAL.value
    x_course_name: Optional[str] = None
    x_course_id: Optional[str] = None
    x_raw_data: Optional[str] = None


class VEventUpdate(SQLModel):
    """更新事件用的 DTO（所有字段可选）"""
    summary: Optional[str] = None
    dtstart: Optional[datetime] = None
    dtend: Optional[datetime] = None
    duration: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    geo: Optional[str] = None
    status: Optional[str] = None
    classification: Optional[str] = None
    transp: Optional[str] = None
    categories: Optional[str] = None
    priority: Optional[int] = None
    organizer: Optional[str] = None
    rrule: Optional[str] = None
    exdate: Optional[str] = None
    valarm: Optional[str] = None
    all_day: Optional[bool] = None
    x_event_type: Optional[str] = None
    x_course_name: Optional[str] = None
    x_course_id: Optional[str] = None


class VEventRead(SQLModel):
    """读取事件用的 DTO（用于 API 响应序列化）"""
    uid: str
    summary: str
    dtstart: datetime
    dtend: Optional[datetime] = None
    duration: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    status: str = ICSStatus.CONFIRMED.value
    classification: str = ICSClass.PUBLIC.value
    transp: str = ICSTransp.OPAQUE.value
    categories: Optional[str] = None
    priority: int = 0
    organizer: Optional[str] = None
    rrule: Optional[str] = None
    all_day: bool = False
    sequence: int = 0
    created: datetime
    last_modified: datetime
    x_source: str
    x_source_id: str = ""
    x_event_type: str = EventType.OTHER.value
    x_course_name: Optional[str] = None
    x_course_id: Optional[str] = None


class SyncJobStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class SyncJob(SQLModel, table=True):
    """持久化同步任务执行结果，便于可观测与审计。"""

    __tablename__ = "sync_job"

    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True, max_length=32)
    trigger: str = Field(index=True, max_length=32, description="api/mcp/cron")
    status: str = Field(default=SyncJobStatus.RUNNING.value, index=True, max_length=16)

    attempts: int = Field(default=1, ge=1)
    retry_count: int = Field(default=0, ge=0)

    started_at: datetime = Field(default_factory=_now_utc)
    finished_at: Optional[datetime] = Field(default=None)
    duration_ms: Optional[int] = Field(default=None, ge=0)

    synced_events: int = Field(default=0, ge=0)
    holiday_filtered_days: int = Field(default=0, ge=0)
    holiday_cancelled_events: int = Field(default=0, ge=0)
    parse_failed: int = Field(default=0, ge=0)
    failed_dates_json: Optional[str] = Field(default=None)

    error_message: Optional[str] = Field(default=None)


# ========================== 数据库引擎 ==========================

def get_engine(db_path: Optional[str] = None):
    """
    创建 SQLAlchemy 引擎

    Args:
        db_path: SQLite 文件路径，None 则使用默认路径，":memory:" 使用内存数据库
    """
    if db_path is None:
        db_path = os.path.join(_PROJECT_DIR, "data", "scheduler.db")

    if db_path == ":memory:":
        url = "sqlite://"
    else:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        url = f"sqlite:///{db_path}"

    engine = create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    SQLModel.metadata.create_all(engine)
    return engine


# ========================== 数据解析 ==========================

class EventParser:
    """将 BB / TIS 原始数据解析为 VEvent 对象"""

    TIS_CLASS_TIME_MAP = {
        1: ("08:00", "09:50"),
        3: ("10:20", "12:10"),
        5: ("14:00", "15:50"),
        7: ("16:20", "18:10"),
        9: ("19:00", "20:50"),
        11: ("21:00", "21:50"),
    }

    # ---- Blackboard ----

    @staticmethod
    def parse_bb_event(raw: dict) -> VEvent:
        """
        解析 BB 日历事件为 ICS VEvent

        BB API 响应字段映射:
            startDate/endDate → dtstart/dtend (UTC, 带 .000Z)
            start/end         → dtstart/dtend (CST 本地时间, 备用)
            title             → summary
            calendarName      → x_course_name
            calendarId        → x_course_id / categories
            eventType         → x_event_type
            location          → location
            itemSourceId      → x_source_id
        """
        now = _now_utc()

        # --- 时间处理 ---
        start_utc_raw = raw.get("startDate") or ""
        end_utc_raw = raw.get("endDate") or ""
        start_local = raw.get("start") or ""
        end_local = raw.get("end") or ""

        def _parse_utc(t: str) -> Optional[datetime]:
            if not t:
                return None
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
            except (ValueError, AttributeError):
                return None

        def _parse_local(t: str) -> Optional[datetime]:
            if not t:
                return None
            try:
                dt = datetime.fromisoformat(t)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CST)
                return dt.astimezone(UTC)
            except (ValueError, AttributeError):
                return None

        dtstart = _parse_utc(start_utc_raw) or _parse_local(start_local) or now
        dtend = _parse_utc(end_utc_raw) or _parse_local(end_local)

        # --- 事件类型 ---
        bb_type = (raw.get("eventType") or "").lower()
        title = raw.get("title") or raw.get("calendarName") or ""

        if "assignment" in bb_type or "due" in title.lower():
            evt_type = EventType.ASSIGNMENT.value
        elif "exam" in bb_type or "exam" in title.lower() or "考试" in title:
            evt_type = EventType.EXAM.value
        elif "deadline" in bb_type:
            evt_type = EventType.DEADLINE.value
        else:
            evt_type = EventType.OTHER.value

        # --- 课程信息 ---
        calendar_id = raw.get("calendarId") or ""
        calendar_name = raw.get("calendarName") or ""
        course_code = calendar_id.split("-")[0] if calendar_id else ""
        course_name = f"{course_code} {calendar_name}".strip() if course_code else calendar_name

        source_id = raw.get("itemSourceId") or raw.get("id") or ""

        # --- categories ---
        cat_parts = [c for c in [evt_type, course_code] if c]

        return VEvent(
            uid=str(uuid.uuid4()),
            dtstamp=now,
            dtstart=dtstart,
            dtend=dtend,
            summary=title,
            description=raw.get("description") or None,
            location=raw.get("location") or None,
            status=ICSStatus.CONFIRMED.value,
            transp=ICSTransp.OPAQUE.value if evt_type != EventType.OTHER.value else ICSTransp.TRANSPARENT.value,
            categories=",".join(cat_parts) if cat_parts else None,
            all_day=bool(raw.get("allDay", False)),
            created=now,
            last_modified=now,
            x_source=EventSource.BB.value,
            x_source_id=str(source_id),
            x_event_type=evt_type,
            x_course_name=course_name or None,
            x_course_id=calendar_id or None,
            x_raw_data=json.dumps(raw, ensure_ascii=False, default=str),
        )

    @classmethod
    def parse_bb_events(cls, raw_list: list) -> List[VEvent]:
        events = []
        for item in raw_list:
            try:
                events.append(cls.parse_bb_event(item))
            except Exception as exc:
                logger.error("scheduler parse bb event failed", extra={"error": str(exc), "item": str(item)})
        return events

    # ---- TIS 教务系统 ----

    @staticmethod
    def parse_tis_event(raw: dict, date_str: str, holiday_name: Optional[str] = None) -> VEvent:
        """
        解析 TIS 日程事件为 ICS VEvent

        TIS 字段映射:
            kcmc(课程名) → summary / x_course_name
            jsxm(教师)   → organizer
            cdmc(地点)   → location
            kssj(HH:MM)  → dtstart (拼接 date_str, 视为 CST)
            jssj(HH:MM)  → dtend
            skjcmc(节次)  → description
            rcflmc(日程分类) → x_event_type
        """
        now = _now_utc()

        def _pick(*keys: str, default=None):
            for key in keys:
                if key in raw and raw.get(key) not in (None, ""):
                    return raw.get(key)
            return default

        # --- 时间 ---
        kssj = str(_pick("kssj", "qssj", "KSSJ", "QSSJ", default="") or "")
        jssj = str(_pick("jssj", "JSSJ", default="") or "")

        class_index = _pick("ksjc", "KSJC")
        try:
            class_index = int(class_index) if class_index not in (None, "") else None
        except (TypeError, ValueError):
            class_index = None

        if (not kssj or not jssj) and class_index in EventParser.TIS_CLASS_TIME_MAP:
            period_start, period_end = EventParser.TIS_CLASS_TIME_MAP[class_index]
            if not kssj:
                kssj = period_start
            if not jssj:
                jssj = period_end

        def _build_dt(date_s: str, time_s: str) -> Optional[datetime]:
            if not time_s:
                return None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(f"{date_s} {time_s}", fmt)
                    return dt.replace(tzinfo=CST).astimezone(UTC)
                except ValueError:
                    continue
            return None

        dtstart = _build_dt(date_str, kssj) or datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=CST).astimezone(UTC)
        dtend = _build_dt(date_str, jssj)

        title = _pick("kcmc", "KCMC", "rcnr", "RCNR", default="TIS 日程")
        location = _pick("cdmc", "CDMC", "cdxxmc", "CDXXMC", "dd", "DD")

        teacher = _pick("jsxm", "JSXM", "skjs", "SKJS")
        bt_string = str(_pick("bt", "BT", default="") or "")
        if not teacher and ":" in bt_string:
            # BT 格式通常为“xxx:教师名...”，截取开头非数字部分作为教师。
            tail = bt_string.split(":", 1)[1].strip()
            teacher_chars = []
            for ch in tail:
                if ch.isdigit():
                    break
                teacher_chars.append(ch)
            teacher = "".join(teacher_chars).strip() or None

        # description
        desc_parts = []
        if holiday_name:
            desc_parts.append(f"Holiday anomaly: {holiday_name}")
        skjcmc = _pick("skjcmc", "SKJCMC")
        if skjcmc:
            desc_parts.append(f"节次: {skjcmc}")
        if class_index is not None:
            desc_parts.append(f"课程序号: {class_index}")
        if teacher:
            desc_parts.append(f"教师: {teacher}")
        if bt_string:
            desc_parts.append(f"课程标题: {bt_string}")

        # 类型推断
        rcflmc = str(_pick("rcflmc", "RCFLMC", default="") or "").lower()
        if "考试" in title or "exam" in title.lower() or "考试" in rcflmc:
            evt_type = EventType.EXAM.value
        elif "课" in rcflmc or _pick("kcmc", "KCMC"):
            evt_type = EventType.CLASS.value
        else:
            evt_type = EventType.OTHER.value

        source_id = _pick("id", "ID", "rcid", "RCID") or f"tis_{date_str}_{kssj}_{title}"
        course_name = _pick("kcmc", "KCMC") or None
        course_id = _pick("kcid", "KCID", "kcdm", "KCDM") or None

        cat_parts = [c for c in [evt_type, course_id] if c]
        if holiday_name:
            cat_parts.append(EventType.HOLIDAY_ANOMALY.value)

        final_status = ICSStatus.CANCELLED.value if holiday_name else ICSStatus.CONFIRMED.value
        final_transp = ICSTransp.TRANSPARENT.value if holiday_name else ICSTransp.OPAQUE.value
        final_event_type = EventType.HOLIDAY_ANOMALY.value if holiday_name else evt_type

        return VEvent(
            uid=str(uuid.uuid4()),
            dtstamp=now,
            dtstart=dtstart,
            dtend=dtend,
            summary=title,
            description="; ".join(desc_parts) if desc_parts else None,
            location=location,
            organizer=teacher,
            status=final_status,
            transp=final_transp,
            categories=",".join(cat_parts) if cat_parts else None,
            all_day=False,
            created=now,
            last_modified=now,
            x_source=EventSource.TIS.value,
            x_source_id=str(source_id),
            x_event_type=final_event_type,
            x_course_name=course_name,
            x_course_id=course_id,
            x_raw_data=json.dumps(raw, ensure_ascii=False, default=str),
        )

    @classmethod
    def parse_tis_schedule(
        cls,
        schedule_data: dict,
        holiday_provider: Optional[HolidayProvider] = None,
    ) -> tuple[List[VEvent], Dict[str, int]]:
        events = []
        stats = {
            "parse_failed": 0,
            "holiday_cancelled": 0,
        }
        for date_str, items in schedule_data.items():
            if not isinstance(items, list):
                continue

            holiday_name = None
            if holiday_provider is not None and holiday_provider.is_holiday(date_str):
                holiday_name = holiday_provider.holiday_name(date_str) or "Holiday"

            for item in items:
                try:
                    event = cls.parse_tis_event(item, date_str, holiday_name=holiday_name)
                    if event.status == ICSStatus.CANCELLED.value and event.x_event_type == EventType.HOLIDAY_ANOMALY.value:
                        stats["holiday_cancelled"] += 1
                    events.append(event)
                except Exception as exc:
                    stats["parse_failed"] += 1
                    logger.error(
                        "scheduler parse tis event failed",
                        extra={"date": date_str, "error": str(exc), "item": str(item)},
                    )
        return events, stats


# ========================== 核心调度器 ==========================

class Scheduler:
    """
    统一日程调度器

    集成 Blackboard / TIS 数据源 + 个人事件，
    基于 ICS VEVENT 模型提供 CRUD 和同步接口。
    """

    def __init__(self, db_path: Optional[str] = None, holiday_provider: Optional[HolidayProvider] = None):
        self.engine = get_engine(db_path)
        self._bb: Optional[bbService] = None
        self._tis: Optional[TisService] = None
        self.holiday_provider = holiday_provider or HolidayProvider()
        self.last_sync_report: Dict[str, Any] = {}

    def _session(self) -> Session:
        return Session(self.engine)

    # ================================================================
    #  服务登录
    # ================================================================

    def login_bb(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        cas_token: Optional[str] = None,
    ) -> bool:
        self._bb = bbService(username=username, password=password, tgc_token=cas_token)
        if not cas_token and not self._bb.Login(username=username, password=password):
            logger.error("scheduler bb login failed: cas login failed")
            return False
        if not self._bb.LoginBB():
            logger.error("scheduler bb login failed: bb service rejected")
            return False
        logger.info("scheduler bb login succeeded")
        return True

    def login_tis(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        cas_token: Optional[str] = None,
    ) -> bool:
        self._tis = TisService(username=username, password=password, tgc_token=cas_token)
        if not cas_token and not self._tis.Login(username=username, password=password):
            logger.error("scheduler tis login failed: cas login failed")
            return False
        if not self._tis.LoginTIS():
            logger.error("scheduler tis login failed: tis service rejected")
            return False
        logger.info("scheduler tis login succeeded")
        return True

    def login_all(self) -> Dict[str, bool]:
        return {"bb": self.login_bb(), "tis": self.login_tis()}

    # ================================================================
    #  数据同步
    # ================================================================

    def _delete_by_source(self, session: Session, source: str, hard: bool = True) -> int:
        """删除指定来源的所有事件"""
        stmt = select(VEvent).where(VEvent.x_source == source)
        if not hard:
            stmt = stmt.where(VEvent.is_deleted == False)  # noqa: E712
        events = session.exec(stmt).all()
        count = len(events)
        for e in events:
            if hard:
                session.delete(e)
            else:
                e.is_deleted = True
                e.last_modified = _now_utc()
                session.add(e)
        return count

    def _replace_source_events(self, source: str, events: List[VEvent], clear_old: bool = True) -> int:
        """替换指定来源的事件集合（兼容 app.py 的旧调用路径）。"""
        with self._session() as session:
            if clear_old:
                deleted = self._delete_by_source(session, source, hard=True)
                logger.info(
                    "scheduler replace source old records removed",
                    extra={"source": source, "count": deleted},
                )
            for event in events:
                session.add(event)
            session.commit()
        return len(events)

    def replace_bb_raw_events(self, raw_events: list, clear_old: bool = True) -> int:
        """将 Blackboard 原始事件列表解析后写入数据库。"""
        events = EventParser.parse_bb_events(raw_events or [])
        count = self._replace_source_events(EventSource.BB.value, events, clear_old=clear_old)
        self.last_sync_report = {
            "source": EventSource.BB.value,
            "synced_events": count,
        }
        return count

    def replace_tis_raw_schedule(self, schedule_data: dict, clear_old: bool = True) -> int:
        """将 TIS 课表字典解析后写入数据库。"""
        events, parse_stats = EventParser.parse_tis_schedule(
            schedule_data or {},
            holiday_provider=self.holiday_provider,
        )
        count = self._replace_source_events(EventSource.TIS.value, events, clear_old=clear_old)
        self.last_sync_report = {
            "source": EventSource.TIS.value,
            "synced_events": count,
            "holiday_cancelled_events": parse_stats.get("holiday_cancelled", 0),
            "parse_failed": parse_stats.get("parse_failed", 0),
        }
        return count

    def sync_bb(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        clear_old: bool = True,
    ) -> int:
        if self._bb is None:
            raise RuntimeError("请先调用 login_bb() 登录 Blackboard")

        if start is None:
            start = datetime.now(tz=CST).replace(hour=0, minute=0, second=0, microsecond=0)
        if end is None:
            end = start + timedelta(days=90)
        if start.tzinfo is None:
            start = start.replace(tzinfo=CST)
        if end.tzinfo is None:
            end = end.replace(tzinfo=CST)

        logger.info(
            "scheduler sync bb started",
            extra={"start": start.strftime('%Y-%m-%d'), "end": end.strftime('%Y-%m-%d')},
        )

        raw_events = self._bb.queryCalendar(start, end)
        if raw_events is None:
            logger.error("scheduler sync bb failed: bb query returned none")
            return 0

        events = EventParser.parse_bb_events(raw_events)

        with self._session() as session:
            if clear_old:
                deleted = self._delete_by_source(session, EventSource.BB.value, hard=True)
                logger.info("scheduler sync bb old records removed", extra={"count": deleted})
            for e in events:
                session.add(e)
            session.commit()

        self.last_sync_report = {
            "source": "bb",
            "synced_events": len(events),
        }
        logger.info("scheduler sync bb completed", extra={"count": len(events)})
        return len(events)

    def sync_tis(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        clear_old: bool = True,
    ) -> int:
        if self._tis is None:
            raise RuntimeError("请先调用 login_tis() 登录教务系统")

        today = datetime.now(tz=CST)
        if start_date is None:
            start_date = today.strftime("%Y-%m-%d")
        if end_date is None:
            end_date = (today + timedelta(days=14)).strftime("%Y-%m-%d")

        logger.info(
            "scheduler sync tis started",
            extra={"start_date": start_date, "end_date": end_date},
        )

        raw_schedule = self._tis.queryScheduleInterval(
            start_date,
            end_date,
            holiday_provider=self.holiday_provider,
            skip_holidays=True,
        )
        if not raw_schedule:
            logger.warning("scheduler sync tis got empty schedule")
            return 0

        events, parse_stats = EventParser.parse_tis_schedule(
            raw_schedule,
            holiday_provider=self.holiday_provider,
        )
        tis_meta = getattr(self._tis, "last_query_metadata", {})

        with self._session() as session:
            if clear_old:
                deleted = self._delete_by_source(session, EventSource.TIS.value, hard=True)
                logger.info("scheduler sync tis old records removed", extra={"count": deleted})
            for e in events:
                session.add(e)
            session.commit()

        self.last_sync_report = {
            "source": "tis",
            "synced_events": len(events),
            "holiday_filtered_days": len(tis_meta.get("holiday_skipped_dates", [])),
            "holiday_cancelled_events": parse_stats.get("holiday_cancelled", 0),
            "parse_failed": parse_stats.get("parse_failed", 0),
            "failed_dates": tis_meta.get("failed_dates", []),
        }
        logger.info("scheduler sync tis completed", extra=self.last_sync_report)
        return len(events)

    def sync_all(self, **kwargs) -> Dict[str, int]:
        result = {}
        if self._bb:
            result["bb"] = self.sync_bb(**{k: v for k, v in kwargs.items()
                                           if k in ("start", "end", "clear_old")})
        if self._tis:
            result["tis"] = self.sync_tis(**{k: v for k, v in kwargs.items()
                                             if k in ("start_date", "end_date", "clear_old")})
        return result

    def get_last_sync_report(self) -> Dict[str, Any]:
        return dict(self.last_sync_report)

    def begin_sync_job(self, source: str, trigger: str = "manual") -> int:
        """Create a running sync job record and return job id."""
        with self._session() as session:
            job = SyncJob(source=source, trigger=trigger, status=SyncJobStatus.RUNNING.value)
            session.add(job)
            session.commit()
            session.refresh(job)
            return int(job.id)

    def finish_sync_job(
        self,
        job_id: int,
        *,
        status: str,
        attempts: int = 1,
        synced_events: int = 0,
        error_message: Optional[str] = None,
        report: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Finalize a sync job and persist aggregated metrics."""
        report = report or {}
        with self._session() as session:
            job = session.get(SyncJob, job_id)
            if job is None:
                return False

            now = _now_utc()
            started_at = job.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            job.status = status
            job.attempts = max(1, int(attempts))
            job.retry_count = max(0, int(attempts) - 1)
            job.synced_events = max(0, int(synced_events))
            job.finished_at = now
            job.duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
            job.error_message = error_message

            job.holiday_filtered_days = max(0, int(report.get("holiday_filtered_days") or 0))
            job.holiday_cancelled_events = max(0, int(report.get("holiday_cancelled_events") or 0))
            job.parse_failed = max(0, int(report.get("parse_failed") or 0))

            failed_dates = report.get("failed_dates")
            if isinstance(failed_dates, list):
                job.failed_dates_json = json.dumps(failed_dates, ensure_ascii=False)
            elif failed_dates is None:
                job.failed_dates_json = None
            else:
                job.failed_dates_json = json.dumps([str(failed_dates)], ensure_ascii=False)

            session.add(job)
            session.commit()
            return True

    def list_sync_jobs(
        self,
        *,
        limit: int = 50,
        source: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._session() as session:
            stmt = select(SyncJob)
            if source:
                stmt = stmt.where(SyncJob.source == source)
            if status:
                stmt = stmt.where(SyncJob.status == status)

            stmt = stmt.order_by(col(SyncJob.started_at).desc()).limit(max(1, min(limit, 200)))
            jobs = list(session.exec(stmt).all())

        result: List[Dict[str, Any]] = []
        for job in jobs:
            item = job.model_dump(mode="json")
            if item.get("failed_dates_json"):
                try:
                    item["failed_dates"] = json.loads(item["failed_dates_json"])
                except ValueError:
                    item["failed_dates"] = []
            else:
                item["failed_dates"] = []
            result.append(item)
        return result

    # ================================================================
    #  CRUD: Create
    # ================================================================

    def create_event(self, data: VEventCreate) -> VEvent:
        """
        创建事件

        Args:
            data: VEventCreate DTO

        Returns:
            持久化后的 VEvent
        """
        now = _now_utc()
        payload = data.model_dump()
        if payload.get("dtstart") is not None:
            payload["dtstart"] = _ensure_utc(payload["dtstart"])
        if payload.get("dtend") is not None:
            payload["dtend"] = _ensure_utc(payload["dtend"])
        event = VEvent(
            uid=str(uuid.uuid4()),
            dtstamp=now,
            created=now,
            last_modified=now,
            **payload,
        )
        with self._session() as session:
            session.add(event)
            session.commit()
            session.refresh(event)
        return event

    def create_personal_event(
        self,
        summary: str,
        dtstart: datetime,
        dtend: Optional[datetime] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        all_day: bool = False,
        event_type: str = EventType.PERSONAL.value,
        **extra,
    ) -> VEvent:
        """快捷方式：创建个人事件"""
        return self.create_event(VEventCreate(
            summary=summary,
            dtstart=_ensure_utc(dtstart),
            dtend=_ensure_utc(dtend) if dtend else None,
            description=description,
            location=location,
            all_day=all_day,
            x_source=EventSource.PERSONAL.value,
            x_event_type=event_type,
            **extra,
        ))

    # ================================================================
    #  CRUD: Read
    # ================================================================

    def get_event(self, uid: str) -> Optional[VEvent]:
        with self._session() as session:
            stmt = select(VEvent).where(
                VEvent.uid == uid, VEvent.is_deleted == False  # noqa: E712
            )
            return session.exec(stmt).first()

    def get_event_by_source(self, source: str, source_id: str) -> Optional[VEvent]:
        with self._session() as session:
            stmt = select(VEvent).where(
                VEvent.x_source == source,
                VEvent.x_source_id == source_id,
                VEvent.is_deleted == False,  # noqa: E712
            )
            return session.exec(stmt).first()

    def query_events(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        source: Optional[str] = None,
        event_type: Optional[str] = None,
        keyword: Optional[str] = None,
        status: Optional[str] = None,
        include_deleted: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> List[VEvent]:
        """
        综合查询接口

        Args:
            start/end: 时间范围（无时区默认 CST）
            source: 按来源过滤
            event_type: 按类型过滤
            keyword: 按 summary/description 模糊搜索
            status: 按 ICS STATUS 过滤
            include_deleted: 是否包含已删除
            limit/offset: 分页
        """
        with self._session() as session:
            stmt = select(VEvent)

            if not include_deleted:
                stmt = stmt.where(VEvent.is_deleted == False)  # noqa: E712

            if start is not None:
                s = _ensure_utc(start)
                stmt = stmt.where(
                    (col(VEvent.dtend) >= s) | ((col(VEvent.dtend).is_(None)) & (col(VEvent.dtstart) >= s))
                )

            if end is not None:
                e = _ensure_utc(end)
                stmt = stmt.where(col(VEvent.dtstart) <= e)

            if source is not None:
                stmt = stmt.where(VEvent.x_source == source)

            if event_type is not None:
                stmt = stmt.where(VEvent.x_event_type == event_type)

            if status is not None:
                stmt = stmt.where(VEvent.status == status)

            if keyword:
                kw = f"%{keyword}%"
                stmt = stmt.where(
                    col(VEvent.summary).ilike(kw) | col(VEvent.description).ilike(kw)
                )

            stmt = stmt.order_by(col(VEvent.dtstart).asc()).offset(offset).limit(limit)
            return list(session.exec(stmt).all())

    def get_today_events(self) -> List[VEvent]:
        today = datetime.now(tz=CST).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.query_events(start=today, end=today + timedelta(days=1))

    def get_week_events(self) -> List[VEvent]:
        now = datetime.now(tz=CST)
        monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.query_events(start=monday, end=monday + timedelta(days=7))

    def search_events(self, keyword: str, limit: int = 50) -> List[VEvent]:
        return self.query_events(keyword=keyword, limit=limit)

    def has_events(
        self,
        source: Optional[str] = None,
        include_deleted: bool = False,
    ) -> bool:
        """Compatibility helper for callers that need a quick existence check."""
        events = self.query_events(source=source, include_deleted=include_deleted, limit=1)
        return bool(events)

    # ================================================================
    #  CRUD: Update
    # ================================================================

    def update_event(self, uid: str, data: VEventUpdate) -> Optional[VEvent]:
        """
        更新事件

        Args:
            uid: 事件 UID
            data: VEventUpdate DTO（仅更新非 None 字段）

        Returns:
            更新后的 VEvent，未找到事件返回 None
        """
        with self._session() as session:
            event = session.exec(
                select(VEvent).where(VEvent.uid == uid, VEvent.is_deleted == False)  # noqa: E712
            ).first()
            if event is None:
                return None

            update_fields = data.model_dump(exclude_unset=True)
            for key, value in update_fields.items():
                if value is not None:
                    if isinstance(value, datetime):
                        value = _ensure_utc(value)
                    setattr(event, key, value)

            event.last_modified = _now_utc()
            event.sequence += 1
            session.add(event)
            session.commit()
            session.refresh(event)
            return event

    def update_event_fields(self, uid: str, **fields) -> Optional[VEvent]:
        """使用 kwargs 快捷更新"""
        return self.update_event(uid, VEventUpdate(**fields))

    # ================================================================
    #  CRUD: Delete
    # ================================================================

    def delete_event(self, uid: str, hard: bool = False) -> bool:
        with self._session() as session:
            event = session.exec(select(VEvent).where(VEvent.uid == uid)).first()
            if event is None:
                return False
            if hard:
                session.delete(event)
            else:
                event.is_deleted = True
                event.status = ICSStatus.CANCELLED.value
                event.last_modified = _now_utc()
                event.sequence += 1
                session.add(event)
            session.commit()
        return True

    def cancel_event(self, uid: str) -> Optional[VEvent]:
        """取消事件（ICS 语义：设为 CANCELLED 状态）"""
        return self.update_event_fields(uid, status=ICSStatus.CANCELLED.value)

    # ================================================================
    #  高级功能
    # ================================================================

    def detect_conflicts(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> List[tuple]:
        """检测时间冲突，返回 [(event_a, event_b), ...]"""
        events = self.query_events(start=start, end=end)
        timed = [e for e in events if e.dtend is not None and not e.all_day
                 and e.status != ICSStatus.CANCELLED.value]
        timed.sort(key=lambda e: e.dtstart)

        conflicts = []
        for i in range(len(timed)):
            for j in range(i + 1, len(timed)):
                a, b = timed[i], timed[j]
                if b.dtstart >= a.dtend:
                    break
                conflicts.append((a, b))
        return conflicts

    def get_summary(self) -> dict:
        """统计摘要"""
        with self._session() as session:
            total = session.exec(
                select(VEvent).where(VEvent.is_deleted == False)  # noqa: E712
            ).all()
        counts = {"total": len(total), "bb": 0, "tis": 0, "personal": 0}
        for e in total:
            src = e.x_source
            if src in counts:
                counts[src] += 1
        return counts

    def export_ics(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        source: Optional[str] = None,
        prodid: str = "-//SUSTech Student Agent//Scheduler//CN",
    ) -> str:
        """
        导出为完整的 .ics 文件内容

        Returns:
            RFC 5545 VCALENDAR 字符串
        """
        events = self.query_events(start=start, end=end, source=source)
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:{prodid}",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-TIMEZONE:Asia/Shanghai",
        ]
        for e in events:
            lines.append(e.to_ics_string())
        lines.append("END:VCALENDAR")
        return "\r\n".join(lines)

    def export_events(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        as_cst: bool = True,
    ) -> List[dict]:
        events = self.query_events(start=start, end=end)
        if as_cst:
            return [e.to_cst_dict() for e in events]
        return [e.model_dump() for e in events]


# ========================== CLI 演示 ==========================

def _demo():
    print("=" * 60)
    print("Scheduler 冒烟测试（ICS + SQLModel, 内存数据库）")
    print("=" * 60)

    scheduler = Scheduler(db_path=":memory:")
    now = datetime.now(tz=CST)

    # 创建个人事件
    e1 = scheduler.create_personal_event(
        summary="完成数据结构作业",
        dtstart=now + timedelta(hours=1),
        dtend=now + timedelta(hours=3),
        event_type=EventType.ASSIGNMENT.value,
        x_course_name="CS203 数据结构",
    )
    print(f"[创建] {e1.summary}  |  {e1.dtstart.astimezone(CST):%H:%M} ~ {e1.dtend.astimezone(CST):%H:%M}")

    e2 = scheduler.create_personal_event(
        summary="团队项目开会",
        dtstart=now + timedelta(hours=2),
        dtend=now + timedelta(hours=4),
        event_type=EventType.MEETING.value,
        location="理学院 308",
    )
    print(f"[创建] {e2.summary}  |  {e2.dtstart.astimezone(CST):%H:%M} ~ {e2.dtend.astimezone(CST):%H:%M}")

    e3 = scheduler.create_personal_event(
        summary="跑步",
        dtstart=now + timedelta(hours=18),
        dtend=now + timedelta(hours=19),
        event_type=EventType.PERSONAL.value,
        location="操场",
    )
    print(f"[创建] {e3.summary}  |  {e3.dtstart.astimezone(CST):%H:%M} ~ {e3.dtend.astimezone(CST):%H:%M}")

    # 模拟 BB 事件解析
    bb_raw = {
        "itemSourceId": "_413545_1",
        "calendarId": "CS401-30000313-2026SP",
        "calendarName": "Intelligent Robotics Spring 2026",
        "allDay": False,
        "start": "2026-03-11T23:55:00",
        "end": "2026-03-11T23:55:00",
        "id": "_blackboard.platform.gradebook2.GradableItem-_413545_1",
        "location": None,
        "title": "Lab Assignment2",
        "endDate": "2026-03-11T15:55:00.000Z",
        "startDate": "2026-03-11T15:55:00.000Z",
        "eventType": "Assignment",
    }
    bb_event = EventParser.parse_bb_event(bb_raw)
    # 先记录解析结果（未进入 session 之前可直接访问）
    bb_summary = bb_event.summary
    bb_dtstart = bb_event.dtstart
    with scheduler._session() as session:
        session.add(bb_event)
        session.commit()
    print(f"[BB解析] {bb_summary}  |  {bb_dtstart.astimezone(CST):%Y-%m-%d %H:%M}")

    # 查询今日事件
    print(f"\n--- 今日事件 ({now:%Y-%m-%d}) ---")
    for e in scheduler.get_today_events():
        loc = e.location or "N/A"
        print(f"  [{e.x_event_type:10}] {e.summary}  @{loc}  {e.dtstart.astimezone(CST):%H:%M}")

    # 搜索
    print("\n--- 搜索 '作业' ---")
    for e in scheduler.search_events("作业"):
        print(f"  {e.summary} (source={e.x_source})")

    # 冲突检测
    print("\n--- 冲突检测 ---")
    for a, b in scheduler.detect_conflicts():
        print(f"  冲突: [{a.summary}] vs [{b.summary}]")

    # 更新 (通过 DTO)
    updated = scheduler.update_event(e1.uid, VEventUpdate(summary="完成数据结构作业（加急）", priority=1))
    print(f"\n[更新] {e1.summary} -> {updated.summary} (priority={updated.priority}, seq={updated.sequence})")

    # 取消 (ICS 语义)
    cancelled = scheduler.cancel_event(e3.uid)
    print(f"[取消] {e3.summary} -> status={cancelled.status}")

    # 删除
    scheduler.delete_event(e3.uid)
    print(f"[删除] {e3.summary}")

    # 统计
    summary = scheduler.get_summary()
    print(f"\n--- 统计 ---")
    print(f"  总计: {summary['total']} | BB: {summary['bb']} | TIS: {summary['tis']} | 个人: {summary['personal']}")

    # ICS 导出
    print(f"\n--- ICS 导出（前 500 字符）---")
    ics = scheduler.export_ics()
    print(ics[:500] + "\n...")

    print("\n冒烟测试完成 ✓")


if __name__ == "__main__":
    _demo()
