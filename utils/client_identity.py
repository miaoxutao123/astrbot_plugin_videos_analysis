"""
统一的客户端身份：所有带登录态的 HTTP 请求都用和抖音浏览器一致的 UA / Client Hints。

一台机器上的一个人只有一个浏览器。之前各处写死了 Windows Chrome 90/119/120/130/131、
Firefox 136/137、iPhone 等七八套 UA，同一个 cookie 一天内出现在好几种"设备"上，
而真正登录的浏览器是 Linux Chrome——这正是盗号、多开的典型特征。

浏览器启动后把 navigator 的真实值写进 identity.json；浏览器还没起过时，
按配置目录里记录的 Chrome 版本和当前系统生成同样格式的默认值。
"""

import json
import os
import sys

from .paths import ACCOUNTS_DIR

IDENTITY_FILE = os.path.join(ACCOUNTS_DIR, "identity.json")
_PROFILE_VERSION = os.path.join(ACCOUNTS_DIR, "dy_browser_profile", "Last Version")
_cache: dict | None = None


def _default() -> dict:
    try:
        with open(_PROFILE_VERSION, encoding="utf-8") as f:
            major = f.read().strip().split(".", 1)[0]
    except OSError:
        major = ""
    major = major if major.isdigit() else "156"
    if sys.platform.startswith("win"):
        osua, plat = "Windows NT 10.0; Win64; x64", "Windows"
    elif sys.platform == "darwin":
        osua, plat = "Macintosh; Intel Mac OS X 10_15_7", "macOS"
    else:
        osua, plat = "X11; Linux x86_64", "Linux"
    return {
        "ua": f"Mozilla/5.0 ({osua}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "brands": [{"brand": "Not:A-Brand", "version": "8"}, {"brand": "Chromium", "version": major}],
        "platform": plat,
    }


def get() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(IDENTITY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) and data.get("ua") else _default()
        except (OSError, ValueError):
            _cache = _default()
    return _cache


def save(ua: str, brands: list | None, platform: str) -> None:
    """浏览器启动后调用，记下真实的 UA 和 Client Hints。"""
    global _cache
    data = {"ua": ua, "brands": brands or [], "platform": platform or get()["platform"]}
    if data == get() and os.path.exists(IDENTITY_FILE):
        return
    _cache = data
    try:
        os.makedirs(ACCOUNTS_DIR, exist_ok=True)
        tmp = IDENTITY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, IDENTITY_FILE)
    except OSError:
        pass


def ua() -> str:
    return get()["ua"]


def headers(**extra) -> dict:
    """桌面 Chrome 会带的身份类请求头；extra 里的 Referer/Cookie 等原样合并。"""
    ident = get()
    h = {"User-Agent": ident["ua"], "Accept-Language": "zh-CN,zh;q=0.9"}
    if ident.get("brands"):
        h["sec-ch-ua"] = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in ident["brands"])
        h["sec-ch-ua-mobile"] = "?0"
        h["sec-ch-ua-platform"] = f'"{ident["platform"]}"'
    h.update({k.replace("_", "-"): v for k, v in extra.items() if v})
    return h
