import os
import json
from datetime import datetime, timedelta
from flask import Flask, Response, request, abort
from tisService import TisService
from bbService import bbService
from ics import Calendar, Event
import pytz
import re
from vercel_kv import kv  # 导入 Vercel KV 客户端

# --- 配置 ---
# 这些将从 Vercel 的环境变量中读取
SUSTECH_SID = os.environ.get('SUSTECH_SID')
SUSTECH_PASSWORD = os.environ.get('SUSTECH_PASSWORD')
ICAL_TOKEN = os.environ.get('ICAL_TOKEN')
CRON_TOKEN = os.environ.get('CRON_TOKEN')  # Cron Job 的安全令牌

# --- 通用配置 ---
SCHEDULE_FETCH_RANGE_DAYS = 120
SCHEDULE_PAST_DAYS = 30
SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")

# --- TIS 特定配置 ---
TIS_CACHE_KEY = "tis_schedule_ics"  # Vercel KV 中的键名
CLASS_TIME_MAP = {
    1: ("08:00", "09:50"), 3: ("10:20", "12:10"), 5: ("14:00", "15:50"),
    7: ("16:20", "18:10"), 9: ("19:00", "20:50"), 11: ("21:00", "21:50"),
}

# --- Blackboard 特定配置 ---
BB_CACHE_KEY = "bb_schedule_ics"  # Vercel KV 中的键名

app = Flask(__name__)

# =================================================================
# 数据转换函数 (与之前基本相同)
# =================================================================


def convert_tis_json_to_ical(schedule_json):
    """将从 TIS 获取的课表 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    data = json.loads(schedule_json) if isinstance(
        schedule_json, str) else schedule_json
    for date_str, events in data.items():
        for item in (events or []):
            if not item.get('KSJC') or not item.get('KCMC'):
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
            e.name = item.get('KCMC')
            filter = os.getenv('COURSE_NAME_FILTER')
            # filter is a array
            flag = True
            if filter:
                filter_list = json.loads(filter)
                flag = False
                for keyword in filter_list:
                    if keyword in e.name:
                        flag = True
                        break
            if not flag:
                continue
            e.begin = SHANGHAI_TZ.localize(naive_begin)
            e.end = SHANGHAI_TZ.localize(naive_end)
            location_str = item.get('NR')
            if location_str:
                prefix = os.getenv('LOCATION_PREFIX')
                replace_dict = {'一教': '第一教学楼', '二教': '第二教学楼',
                                '三教': '第三教学楼', '智华': '第三教学楼'}
                for short, full in replace_dict.items():
                    if short in location_str:
                        location_str = location_str.replace(short, full)
                        break
                e.location = (prefix or '') + location_str
            teacher_name = 'N/A'
            bt_string = item.get('BT', '')
            if ':' in bt_string:
                match = re.match(r'([^\d]*)', bt_string.split(':', 1)[1])
                if match:
                    teacher_name = match.group(1)
            e.description = f"教师: {teacher_name}\n课程标题: {bt_string}"
            cal.events.add(e)
    return str(cal)


def convert_bb_json_to_ical(events_json):
    """将从 Blackboard 获取的日历事件 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    for item in (events_json or []):
        e = Event()
        course_name = item.get('calendarName', 'Unknown Course')
        event_title = item.get('title', '未命名事件')
        event_type = item.get('eventType', 'Event')
        e.name = f"「{course_name}」 {event_title}" if event_type in [
            'Assignment', '作业'] else f"[{course_name}] {event_title}"
        try:
            start_str = item['startDate'][:-1] + \
                '+00:00' if item['startDate'].endswith(
                    'Z') else item['startDate']
            end_str = item['endDate'][:-1] + \
                '+00:00' if item['endDate'].endswith('Z') else item['endDate']
            e.begin = datetime.fromisoformat(start_str)
            e.end = datetime.fromisoformat(end_str)
        except (KeyError, ValueError):
            continue
        e.description = f"Course: {course_name}\nDescription: Deadline from Blackboard"
        cal.events.add(e)
    return str(cal)

# =================================================================
# 数据抓取与缓存函数 (修改为写入 Vercel KV)
# =================================================================


def fetch_and_cache_tis_schedule():
    """抓取 TIS 课表并更新到 Vercel KV"""
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
    kv.set(TIS_CACHE_KEY, ical_data)
    print(f"TIS cache updated successfully in Vercel KV.")
    return ical_data


def fetch_and_cache_bb_schedule():
    """抓取 Blackboard 日历事件并更新到 Vercel KV"""
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
    kv.set(BB_CACHE_KEY, ical_data)
    print(f"Blackboard cache updated successfully in Vercel KV.")
    return ical_data

# =================================================================
# Flask 路由
# =================================================================


@app.route('/api/cron/fetch')
def cron_fetch_handler():
    """由 Vercel Cron Job 调用的受保护的 API 端点"""
    # 安全检查
    cron_token = request.args.get('cron_token')
    if not CRON_TOKEN or cron_token != CRON_TOKEN:
        abort(401, "Unauthorized: Invalid cron token.")

    source = request.args.get('source', 'all')
    results = {}
    try:
        if source in ['tis', 'all']:
            fetch_and_cache_tis_schedule()
            results['tis'] = 'success'
    except Exception as e:
        results['tis'] = f'failed: {str(e)}'
        print(f"Error fetching TIS data: {e}")

    try:
        if source in ['bb', 'all']:
            fetch_and_cache_bb_schedule()
            results['bb'] = 'success'
    except Exception as e:
        results['bb'] = f'failed: {str(e)}'
        print(f"Error fetching BB data: {e}")

    return results


@app.route('/tis/schedule.ics')
def get_tis_schedule():
    """提供 TIS 课表日历 (从 KV 缓存读取)"""
    provided_token = request.args.get('token')
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, "Unauthorized: Invalid or missing token.")

    ical_data = kv.get(TIS_CACHE_KEY)
    if not ical_data:
        return "Calendar data is not yet available. Please wait for the next scheduled update (up to 12 hours) or trigger it manually if you are the admin.", 404

    return Response(ical_data, mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=tis_schedule.ics"})


@app.route('/blackboard/schedule.ics')
def get_bb_schedule():
    """提供 Blackboard DDL 日历 (从 KV 缓存读取)"""
    provided_token = request.args.get('token')
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, "Unauthorized: Invalid or missing token.")

    ical_data = kv.get(BB_CACHE_KEY)
    if not ical_data:
        return "Calendar data is not yet available. Please wait for the next scheduled update (up to 12 hours) or trigger it manually if you are the admin.", 404

    return Response(ical_data, mimetype="text/calendar", headers={"Content-Disposition": "attachment; filename=bb_schedule.ics"})
