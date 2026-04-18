from casService import CasService
import json
from typing import Optional, Any


class TisService(CasService):

    def LoginTIS(self):
        if self.TGC is None:
            print("Login TIS failed: missing CAS TGC token")
            return False
        response = self.session.get("https://cas.sustech.edu.cn/cas/login", headers=self.headers,
                                    cookies={"TGC": self.TGC}, allow_redirects=False,
                                    params={"service": "https://tis.sustech.edu.cn/cas"})
        if response.status_code != 302:
            print(f"Login TIS failed: CAS redirect status={response.status_code}")
            return False

        url_tis = response.headers.get("Location")
        if not url_tis:
            print("Login TIS failed: missing redirect location")
            return False

        confirm_response = self.session.get(
            url_tis, headers=self.headers, allow_redirects=True)
        if confirm_response.status_code == 200:
            print("Login TIS successfully!")
            return True

        print("Login TIS failed!")
        return False

    def queryGPA(self):
        data = {
            "xn": None,
            "xq": None,
            "kcmc": None,
            "cxbj": "-1",
            "pylx": "1",
            "current": 1,
            "pageSize": 50,
            "sffx": None
        }
        data = json.dumps(data)
        headers = self.headers
        headers["Content-Type"] = "application/json"
        response = self.session.post(
            "https://tis.sustech.edu.cn/cjgl/grcjcx/grcjcx", headers=headers, data=data)

        if response.status_code == 200:
            #   save response text to json
            with open("GPA.json", "w") as f:
                f.write(response.text)
            print("Query successfully!")
        else:
            print("Query failed!")

    def querySchedule(self, date):
        # date: YYYY-MM-DD

        headers = self.headers.copy()
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Accept"] = "*/*"
        headers["X-Requested-With"] = "XMLHttpRequest"
        headers["Origin"] = "https://tis.sustech.edu.cn"
        headers["Referer"] = "https://tis.sustech.edu.cn/authentication/main"

        data = {
            "rcrq": date,
        }

        response = self.session.post(
            "https://tis.sustech.edu.cn/component/queryrcxxlist", headers=headers, data=data)

        if response.status_code == 200:
            return response.json()

    def queryScheduleInterval(
        self,
        startDate,
        endDate,
        holiday_provider: Optional[Any] = None,
        skip_holidays: bool = False,
        max_workers: int = 10,
    ):
        # date: YYYY-MM-DD
        import datetime
        import concurrent.futures

        start_date = datetime.datetime.strptime(startDate, "%Y-%m-%d")
        end_date = datetime.datetime.strptime(endDate, "%Y-%m-%d")
        delta = datetime.timedelta(days=1)
        max_workers = max(1, int(max_workers))

        dates_to_fetch = [start_date + i * delta for i in range((end_date - start_date).days + 1)]

        result = {}
        holiday_skipped_dates = []
        failed_dates = []

        dates_for_request = []
        for d in dates_to_fetch:
            day = d.strftime("%Y-%m-%d")
            if skip_holidays and holiday_provider is not None and holiday_provider.is_holiday(day):
                result[day] = []
                holiday_skipped_dates.append(day)
                continue
            dates_for_request.append(d)

        # 使用线程池并行抓取，max_workers可以根据网络情况调整
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 创建一个将 future 映射到日期的字典
            future_to_date = {executor.submit(self.querySchedule, d.strftime("%Y-%m-%d")): d.strftime("%Y-%m-%d") for d in dates_for_request}

            for future in concurrent.futures.as_completed(future_to_date):
                date_str = future_to_date[future]
                try:
                    # 获取任务结果并存入字典
                    data = future.result()
                    result[date_str] = data or []
                except Exception as exc:
                    print(f'Fetching for {date_str} generated an exception: {exc}')
                    failed_dates.append(date_str)
                    result[date_str] = []  # 如果出错，返回一个空列表

        self.last_query_metadata = {
            "start_date": startDate,
            "end_date": endDate,
            "requested_dates": len(dates_for_request),
            "holiday_skipped_dates": holiday_skipped_dates,
            "failed_dates": failed_dates,
            "skip_holidays": bool(skip_holidays),
        }

        # 按日期排序，确保结果有序
        sorted_result = {d.strftime("%Y-%m-%d"): result.get(d.strftime("%Y-%m-%d"), []) for d in dates_to_fetch}
        return sorted_result
