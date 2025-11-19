import os
import json
from datetime import datetime, timedelta
from flask import Flask, Response, request, abort
from tisService import TisService
from bbService import bbService  # 导入 bbService
from ics import Calendar, Event
import pytz
import re

# --- 配置 ---
SUSTECH_SID = os.environ.get('SUSTECH_SID')
SUSTECH_PASSWORD = os.environ.get('SUSTECH_PASSWORD')
ICAL_TOKEN = os.environ.get('ICAL_TOKEN')

# --- 通用配置 ---
CACHE_DIR = "cache"
SCHEDULE_FETCH_RANGE_DAYS = 120
SCHEDULE_PAST_DAYS = 30
SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")

# --- TIS 特定配置 ---
TIS_CACHE_FILE = os.path.join(CACHE_DIR, "tis_schedule.ics")
TIS_CACHE_EXPIRY_DAYS = 7  # 课表每周更新一次
CLASS_TIME_MAP = {
    1: ("08:00", "09:50"),
    3: ("10:20", "12:10"),
    5: ("14:00", "15:50"),
    7: ("16:20", "18:10"),
    9: ("19:00", "20:50"),
    11: ("21:00", "21:50"),
}

# --- Blackboard 特定配置 ---
BB_CACHE_FILE = os.path.join(CACHE_DIR, "bb_schedule.ics")
BB_CACHE_EXPIRY_DAYS = 1  # 作业DDL每天更新一次可能更合适

app = Flask(__name__)


# =================================================================
# TIS (课表) 相关函数
# =================================================================

def convert_tis_json_to_ical(schedule_json):
    """将从 TIS 获取的课表 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    data = json.loads(schedule_json) if isinstance(
        schedule_json, str) else schedule_json

    for date_str, events in data.items():
        for item in (events or []):
            if not item.get('KSJC'):
                continue

            start_time_str, end_time_str = CLASS_TIME_MAP.get(
                item['KSJC'], (None, None))
            if not start_time_str:
                continue

            naive_begin = datetime.strptime(
                f"{item['SJ']} {start_time_str}:00", '%Y-%m-%d %H:%M:%S')
            naive_end = datetime.strptime(
                f"{item['SJ']} {end_time_str}:00", '%Y-%m-%d %H:%M:%S')

            e = Event()
            e.name = item.get('KCMC', '未命名事件')
            e.begin = SHANGHAI_TZ.localize(naive_begin)
            e.end = SHANGHAI_TZ.localize(naive_end)

            # 保留先前对 TIS 地点的处理逻辑
            location_str = item.get('NR')
            if location_str:
                prefix = os.getenv('LOCATION_PREFIX')
                replace_dict = {
                    '一教': '第一教学楼',
                    '二教': '第二教学楼',
                    '三教': '第三教学楼',
                    '智华': '第三教学楼'}
                for short, full in replace_dict.items():
                    if short in location_str:
                        location_str = location_str.replace(short, full)
                        print(
                            f"Replaced '{short}' with '{full}'. New location: {location_str}")
                        break
                if prefix:
                    e.location = prefix + location_str
                else:
                    e.location = location_str

            teacher_name = 'N/A'
            bt_string = item.get('BT', '')
            if ':' in bt_string:
                match = re.match(r'([^\d]*)', bt_string.split(':', 1)[1])
                if match:
                    teacher_name = match.group(1)
            e.description = f"教师: {teacher_name}\n课程标题: {bt_string}"

            cal.events.add(e)
    return str(cal)


def fetch_and_cache_tis_schedule():
    """抓取 TIS 课表并更新缓存"""
    print("Fetching new schedule from TIS...")
    service = TisService()
    if not service.Login():
        raise ConnectionError("TIS CAS Login Failed.")
    service.LoginTIS()

    today = datetime.now()
    start_date = today - timedelta(days=SCHEDULE_PAST_DAYS)
    end_date = today + timedelta(days=SCHEDULE_FETCH_RANGE_DAYS)
    schedule_data = service.queryScheduleInterval(
        start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))

    ical_data = convert_tis_json_to_ical(schedule_data)
    with open(TIS_CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(ical_data)
    print(f"TIS cache updated successfully in '{TIS_CACHE_FILE}'.")
    return ical_data


# =================================================================
# Blackboard (DDL) 相关函数
# =================================================================

def convert_bb_json_to_ical(events_json):
    """将从 Blackboard 获取的日历事件 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    for item in (events_json or []):
        e = Event()
        if not item.get('title') or not item.get('startDate') or not item.get('endDate') or not item.get('calendarName'):
            continue
        course_name = item.get('calendarName')
        event_title = item.get('title')
        event_type = item.get('eventType')

        # 为截止日期类型的事件添加明确的前缀
        if event_type == 'Assignment' or event_type == '作业':

            e.name = f"「{course_name}」 {event_title}"
        else:
            e.name = f"[{course_name}] {event_title}"

        try:
            # 使用 startDate 和 endDate，它们是带有时区信息的 UTC 时间
            start_str = item['startDate']
            end_str = item['endDate']

            # Python 3.11+ 的 fromisoformat 可以直接处理 'Z'
            # 为了更好的兼容性，我们手动替换 'Z'
            if start_str.endswith('Z'):
                start_str = start_str[:-1] + '+00:00'
            if end_str.endswith('Z'):
                end_str = end_str[:-1] + '+00:00'

            # 解析为带时区的 datetime 对象
            e.begin = datetime.fromisoformat(start_str)
            e.end = datetime.fromisoformat(end_str)

            # 保持截止日期为零时长事件，这是最准确的表示方法
            # 不再人为增加时长

        except (KeyError, ValueError) as err:
            print(
                f"Skipping event due to invalid time format: {event_title}, Error: {err}")
            continue

        # 将课程名和事件类型放入描述中
        e.description = f"Course: {course_name}\nDescription: Deadline from Blackboard"
        # BB 日历不需要地点信息
        cal.events.add(e)
    return str(cal)


def fetch_and_cache_bb_schedule():
    """抓取 Blackboard 日历事件并更新缓存"""
    print("Fetching new schedule from Blackboard...")
    service = bbService()
    if not service.Login():
        raise ConnectionError("BB CAS Login Failed.")
    if not service.LoginBB():
        raise ConnectionError("BB Login Failed.")

    today = datetime.now()
    start_date = today - timedelta(days=SCHEDULE_PAST_DAYS)
    end_date = today + timedelta(days=SCHEDULE_FETCH_RANGE_DAYS)
    schedule_data = service.queryCalendar(start_date, end_date)

    ical_data = convert_bb_json_to_ical(schedule_data)
    with open(BB_CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(ical_data)
    print(f"Blackboard cache updated successfully in '{BB_CACHE_FILE}'.")
    return ical_data


# =================================================================
# Flask 路由和主逻辑
# =================================================================

def handle_ical_request(cache_file, fetch_function, expiry_days):
    """通用 iCal 请求处理逻辑"""
    provided_token = request.args.get('token')
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, description="Unauthorized: Invalid or missing token.")

    try:
        cache_exists = os.path.exists(cache_file)
        is_cache_expired = False
        if cache_exists:
            cache_mod_time = os.path.getmtime(cache_file)
            if (datetime.now() - datetime.fromtimestamp(cache_mod_time)).days >= expiry_days:
                is_cache_expired = True
                print(f"Cache '{cache_file}' is expired.")

        if not cache_exists or is_cache_expired:
            ical_data = fetch_function()
        else:
            print(f"Serving '{cache_file}' from cache.")
            with open(cache_file, "r", encoding="utf-8") as f:
                ical_data = f.read()
    except Exception as e:
        print(f"An error occurred: {e}")
        return f"An error occurred: {str(e)}", 500

    return Response(
        ical_data,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f"attachment; filename={os.path.basename(cache_file)}"}
    )


@app.route('/tis/schedule.ics')
def get_tis_schedule():
    """提供 TIS 课表日历"""
    return handle_ical_request(TIS_CACHE_FILE, fetch_and_cache_tis_schedule, TIS_CACHE_EXPIRY_DAYS)


@app.route('/blackboard/schedule.ics')
def get_bb_schedule():
    """提供 Blackboard DDL 日历"""
    return handle_ical_request(BB_CACHE_FILE, fetch_and_cache_bb_schedule, BB_CACHE_EXPIRY_DAYS)


if __name__ == '__main__':
    os.makedirs(CACHE_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=5001, debug=True)
