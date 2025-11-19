import os
import json
from datetime import datetime, timedelta
from flask import Flask, Response, request, abort
from tisService import TisService
from ics import Calendar, Event
import pytz  # 导入 pytz
import re  # 导入 re 模块

# --- 配置 ---
# 建议将这些敏感信息全部设置为环境变量
# 在终端中运行:
# export SUSTECH_SID="你的学号"
# export SUSTECH_PASSWORD="你的密码"
# export ICAL_TOKEN="一个长且随机的安全字符串"
SUSTECH_SID = os.environ.get('SUSTECH_SID')
SUSTECH_PASSWORD = os.environ.get('SUSTECH_PASSWORD')
ICAL_TOKEN = os.environ.get('ICAL_TOKEN')

# 缓存文件名和缓存有效期（天）
# 修改：将缓存文件路径指向容器内的 /app/cache 目录
CACHE_DIR = "cache"
CACHE_FILE = os.path.join(CACHE_DIR, "cached_schedule.ics")
CACHE_EXPIRY_DAYS = 7
# 抓取日程的时间范围（天）
SCHEDULE_FETCH_RANGE_DAYS = 120
# 抓取过去日程的时间范围（天），用于保留历史记录
SCHEDULE_PAST_DAYS = 30

# 南科大课程节次与时间的对应关系 (请根据你的实际情况调整)
CLASS_TIME_MAP = {
    1: ("08:00", "09:50"),
    3: ("10:20", "12:10"),
    5: ("14:00", "15:50"),
    7: ("16:20", "18:10"),
    9: ("19:00", "20:50"),
    11: ("21:00", "21:50"),
}

app = Flask(__name__)


def convert_json_to_ical(schedule_json):
    """将从 tisService 获取的日程 JSON 转换为 iCalendar 格式字符串"""
    cal = Calendar()
    shanghai_tz = pytz.timezone("Asia/Shanghai")  # 定义上海时区

    # 确保传入的是Python字典，如果不是则先解析
    if isinstance(schedule_json, str):
        data = json.loads(schedule_json)
    else:
        data = schedule_json

    for date_str, events in data.items():
        if not events:
            continue
        for item in events:
            if 'KSJC' not in item or not item['KSJC']:
                continue

            start_time_str, end_time_str = CLASS_TIME_MAP.get(
                item['KSJC'], (None, None))

            if not start_time_str:
                continue

            begin_datetime_str = f"{item['SJ']} {start_time_str}:00"
            end_datetime_str = f"{item['SJ']} {end_time_str}:00"

            # 创建不带时区的 "naive" datetime 对象
            naive_begin = datetime.strptime(
                begin_datetime_str, '%Y-%m-%d %H:%M:%S')
            naive_end = datetime.strptime(
                end_datetime_str, '%Y-%m-%d %H:%M:%S')

            e = Event()
            e.name = item.get('KCMC', '未命名事件')
            filter = os.getenv('FILTER')
            flag = True
            for keyword in (filter or []):
                if keyword in e.name:
                    # 跳过包含过滤关键词的课程
                    flag = False
                    break
            if not flag:
                continue

            # 使用 shanghai_tz.localize 将 naive datetime 转换为带时区的 "aware" datetime
            e.begin = shanghai_tz.localize(naive_begin)
            e.end = shanghai_tz.localize(naive_end)
            location_str = item.get('NR')
            if location_str:  # 检查 location_str 是否为 None 或空字符串
                prefix = os.getenv('LOCATION_PREFIX')
                replace_dict = {
                    '一教': '第一教学楼',
                    '二教': '第二教学楼',
                    '三教': '第三教学楼',
                    '智华': '第三教学楼'}  # 在地图更新前，先用第三教学楼代替智华楼
                # 替换地点中的简写
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

            # -- 修复BUG --
            # 使用更可靠的正则表达式来提取教师姓名
            teacher_name = 'N/A'
            bt_string = item.get('BT', '')
            if ':' in bt_string:
                part_after_colon = bt_string.split(':', 1)[1]
                # 匹配从字符串开始到第一个数字之间的所有字符
                match = re.match(r'([^\d]*)', part_after_colon)
                if match:
                    teacher_name = match.group(1)

            e.description = f"教师: {teacher_name}\n课程标题: {bt_string}"

            cal.events.add(e)

    return str(cal)


def fetch_and_cache_schedule():
    """抓取新日程并更新缓存文件"""
    # 确保缓存目录存在
    os.makedirs(CACHE_DIR, exist_ok=True)
    print("Fetching new schedule from TIS...")
    if not SUSTECH_SID or not SUSTECH_PASSWORD:
        raise ValueError(
            "SUSTECH_SID or SUSTECH_PASSWORD environment variable not set.")

    service = TisService()
    if not service.Login():
        raise ConnectionError("CAS Login Failed.")
    service.LoginTIS()

    # 定义一个包含过去和未来的完整时间范围
    today = datetime.now()
    start_date = today - timedelta(days=SCHEDULE_PAST_DAYS)
    end_date = today + timedelta(days=SCHEDULE_FETCH_RANGE_DAYS)

    schedule_data = service.queryScheduleInterval(
        start_date.strftime('%Y-%m-%d'),
        end_date.strftime('%Y-%m-%d')
    )

    ical_data = convert_json_to_ical(schedule_data)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        f.write(ical_data)
    print(f"Cache updated successfully in '{CACHE_FILE}'.")
    return ical_data


@app.route('/schedule.ics')
def get_schedule_ics():
    # 1. 鉴权
    provided_token = request.args.get('token')
    if not ICAL_TOKEN or provided_token != ICAL_TOKEN:
        abort(401, description="Unauthorized: Invalid or missing token.")

    # 2. 检查缓存是否存在且未过期
    try:
        cache_exists = os.path.exists(CACHE_FILE)
        is_cache_expired = False
        if cache_exists:
            cache_mod_time = os.path.getmtime(CACHE_FILE)
            if (datetime.now() - datetime.fromtimestamp(cache_mod_time)).days >= CACHE_EXPIRY_DAYS:
                is_cache_expired = True
                print("Cache is expired.")

        # 如果缓存不存在或已过期，则重新抓取
        if not cache_exists or is_cache_expired:
            ical_data = fetch_and_cache_schedule()
        else:
            # 否则，直接读取缓存
            print("Serving from cache.")
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                ical_data = f.read()

    except Exception as e:
        print(f"An error occurred: {e}")
        return f"An error occurred: {str(e)}", 500

    # 3. 返回 ICS 文件
    return Response(
        ical_data,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": "attachment; filename=schedule.ics"
        }
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
