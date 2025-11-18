import requests
from lxml import etree
import re
import pickle
import os


def get_execution_and_eventId(response):
    html = etree.HTML(response.text)
    execution = html.xpath('//input[@name="execution"]/@value')[0]
    _eventId = html.xpath('//input[@name="_eventId"]/@value')[0]
    return execution, _eventId


class CasService:
    def __init__(self):
        self.TGC = None
        self.url = 'cas.sustech.edu.cn/cas/login'
        self.session = requests.Session()
        self.headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                      "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br,zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        }

    def Login(self):
        response = self.session.get(
            f"https://{self.url}", headers=self.headers)
        if response.status_code == 200:
            execution, _eventId = get_execution_and_eventId(response)
            data = {
                "username": os.getenv("SUSTECH_SID"),
                "password": os.getenv("SUSTECH_PASSWORD"),
                "execution": execution,
                "_eventId": _eventId,
                "geolocation": ""
            }
            response = self.session.post(
                f"https://{self.url}", headers=self.headers, data=data)
            if response.status_code == 200:
                print("Login successfully!")
                self.TGC = response.cookies.get("TGC")
                # print(self.TGC)
                return True
            else:
                print("Login failed!")
        return False

    def Logout(self):
        response = self.session.get(
            f"https://{self.url}", headers=self.headers, data={"TGC": self.TGC})
        if response.status_code == 200:
            print("Logout successfully!")
        else:
            print("Logout failed!")


if __name__ == '__main__':
    cas = CasService()
    cas.Login()
    cas.Logout()
