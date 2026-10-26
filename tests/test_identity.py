"""带登录态的请求统一使用浏览器身份；B站设备 cookie 格式。"""

import json
import os
import re

from astrbot_plugin_videos_analysis.services import bili_account
from astrbot_plugin_videos_analysis.utils import client_identity


def test_headers_follow_saved_browser_identity():
    brands = [{"brand": "Not:A-Brand", "version": "8"}, {"brand": "Chromium", "version": "156"},
              {"brand": "Google Chrome", "version": "156"}]
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    client_identity.save(ua, brands, "Linux")
    assert json.load(open(client_identity.IDENTITY_FILE, encoding="utf-8"))["ua"] == ua
    h = client_identity.headers(Referer="https://www.bilibili.com/", Cookie="", Sec_Fetch_Mode="cors")
    assert h["User-Agent"] == ua
    assert h["sec-ch-ua"] == '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"'
    assert h["sec-ch-ua-platform"] == '"Linux"' and h["Sec-Fetch-Mode"] == "cors"
    assert "Cookie" not in h  # 空值不发


def test_identity_file_written_even_when_equal_to_default():
    os.remove(client_identity.IDENTITY_FILE)
    ident = client_identity.get()
    client_identity.save(ident["ua"], ident["brands"], ident["platform"])
    assert os.path.exists(client_identity.IDENTITY_FILE)


def test_bili_headers_use_identity():
    h = bili_account.headers({"SESSDATA": "x", "__refresh_token": "meta"})
    assert h["User-Agent"] == client_identity.ua()
    assert h["Cookie"] == "SESSDATA=x"  # __ 开头的插件元数据不发出去


def test_bili_uuid_format():
    assert re.fullmatch(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\d{5}infoc", bili_account._gen_uuid())
