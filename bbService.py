import requests
from lxml import etree, html  # 导入 lxml 的 html 解析模块
import re
import pickle
import os
from casService import CasService
import json
import datetime


class bbService(CasService):

    def LoginBB(self):
        if self.TGC is None:
            print("TGC cookie not found. Please login CAS first.")
            return False

        # 1. 通过 CAS 认证并获取跳转到 BB 的票据 URL
        response = self.session.get("https://cas.sustech.edu.cn/cas/login", headers=self.headers,
                                    cookies={"TGC": self.TGC}, allow_redirects=False,
                                    params={"service": "https://bb.sustech.edu.cn/webapps/portal/execute/defaultTab"})

        if response.status_code != 302:
            print("Failed to get BB ticket from CAS. Status code:",
                  response.status_code)
            return False

        # 2. 访问票据 URL，让 session 登录 BB
        url_bb_ticket = response.headers.get("Location")
        confirm_response = self.session.get(
            url_bb_ticket, headers=self.headers, allow_redirects=True)

        if confirm_response.status_code != 200:
            print("Failed to confirm BB login. Status code:",
                  confirm_response.status_code)
            return False

        # 3. 访问主页以验证登录状态
        verification_url = "https://bb.sustech.edu.cn/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"
        verification_response = self.session.get(
            verification_url, headers=self.headers)

        if verification_response.status_code == 200:
            # 使用 lxml 解析 HTML 并精确定位到 <title> 标签
            try:
                doc = html.fromstring(verification_response.content)
                title_element = doc.find('.//title')

                if title_element is not None and title_element.text:
                    title_text = title_element.text
                    # 同时检查中文和英文的欢迎信息，使其更具通用性
                    is_welcome_present = "欢迎，" in title_text or "Welcome," in title_text
                    is_platform_present = "Blackboard Learn" in title_text

                    if is_welcome_present and is_platform_present:
                        print("Login BB successfully and verified by DOM!")
                        return True
                    else:
                        print(
                            f"Verification failed: Welcome message not found in <title> tag. Found title: {title_text}")
                        return False
                else:
                    print(
                        "Verification failed: Could not find <title> tag in BB homepage.")
                    return False
            except Exception as e:
                print(f"An error occurred during DOM parsing: {e}")
                return False
        else:
            print(
                f"Verification failed: Could not access BB homepage. Status code: {verification_response.status_code}")
            return False

    def queryCalendar(self, start_date: datetime, end_date: datetime):
        """
        查询 Blackboard 日历事件。

        :param start_date: 查询范围的开始时间 (datetime object)
        :param end_date: 查询范围的结束时间 (datetime object)
        :return: 包含日历事件的列表 (list of dicts)，如果失败则返回 None
        """
        # 将 datetime 对象转换为毫秒级 Unix 时间戳
        start_ts = int(start_date.timestamp() * 1000)
        end_ts = int(end_date.timestamp() * 1000)

        url = "https://bb.sustech.edu.cn/webapps/calendar/calendarData/selectedCalendarEvents"

        params = {
            "start": start_ts,
            "end": end_ts,
            "course_id": "",
            "mode": "personal"
        }

        headers = self.headers.copy()
        headers.update({
            "Accept": "*/*",
            "Referer": "https://bb.sustech.edu.cn/webapps/calendar/viewPersonal",
            "X-Requested-With": "XMLHttpRequest"
        })

        response = self.session.get(url, headers=headers, params=params)

        if response.status_code == 200:
            print("Query BB calendar successfully!")
            return response.json()
        else:
            print(
                f"Query BB calendar failed! Status code: {response.status_code}")
            # print(f"Response: {response.text}")
            with open("bb_calendar_error.html", "w", encoding="utf-8") as f:
                f.write(response.text)
            return None


if __name__ == '__main__':
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
            print("First event:", json.dumps(
                calendar_events[0], indent=2, ensure_ascii=False))

    bb.Logout()
