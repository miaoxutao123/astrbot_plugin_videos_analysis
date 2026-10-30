"""
B站登录态维护：refresh_token 续期、buvid 补全、Cookie 头构造

cookie 字典里以 "__" 开头的键是插件自己的元数据（如 __refresh_token），
不会出现在请求头里，但会随 cookie 一起保存、进账号池、被轮换。

续期流程（bilibili-API-collect: docs/login/cookie_refresh.md）：
1. GET  passport /cookie/info           → 是否需要刷新 + 服务器时间戳
2. RSA-OAEP 加密 "refresh_{ts}"         → correspondPath
3. GET  www /correspond/1/{path}        → 页面里的 refresh_csrf
4. POST passport /cookie/refresh        → 新 cookie（Set-Cookie）+ 新 refresh_token
5. POST passport /confirm/refresh       → 用新 bili_jct 确认，让旧 refresh_token 失效
"""

import random
import re
import time

import aiohttp

from astrbot.api import logger

from ..utils import client_identity
from .bili_http import get_session

META_PREFIX = "__"
REFRESH_KEY = "__refresh_token"
PASSPORT = "https://passport.bilibili.com/x/passport-login/web"

_PUBKEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----"""


def cookie_header(cookies: dict | None) -> str:
    """只拼真正的 cookie，跳过 __ 开头的插件元数据。"""
    return "; ".join(f"{k}={v}" for k, v in (cookies or {}).items() if not k.startswith(META_PREFIX))


def headers(cookies: dict | None = None, referer: str = "https://www.bilibili.com/") -> dict:
    """和抖音浏览器同一套 UA / Client Hints：一台机器上一个人用的是同一个浏览器。"""
    return client_identity.headers(Referer=referer, Origin="https://www.bilibili.com",
                                   Cookie=cookie_header(cookies) if cookies else "")


def _correspond_path(ts_ms: int) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_public_key(_PUBKEY)
    enc = key.encrypt(
        f"refresh_{ts_ms}".encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return enc.hex()


def _merge_set_cookies(cookies: dict, resp: aiohttp.ClientResponse) -> dict:
    out = dict(cookies)
    for name, morsel in resp.cookies.items():
        out[name] = morsel.value
    return out


def _gen_uuid() -> str:
    """网页端首次访问时 JS 生成的 _uuid：8-4-4-4-12 位大写十六进制 + 毫秒时间戳后 5 位 + infoc。"""
    hx = lambda n: "".join(random.choice("0123456789ABCDEF") for _ in range(n))
    return f"{hx(8)}-{hx(4)}-{hx(4)}-{hx(4)}-{hx(12)}{int(time.time() * 1000) % 100000:05d}infoc"


async def ensure_buvid(cookies: dict) -> dict:
    """
    补齐浏览器访问 B站时本来就有的设备 cookie。

    扫码登录拿到的 cookie 没有 buvid3/buvid4/_uuid/b_nut，真实浏览器里这些先于登录态存在；
    缺了它们评论、搜索等接口容易被风控（-352/-412）。生成一次后随账号保存，之后不再变。
    """
    out = dict(cookies)
    if not out.get("_uuid"):
        out["_uuid"] = _gen_uuid()
    if not out.get("b_nut"):
        out["b_nut"] = str(int(time.time()))
    if out.get("buvid3") and out.get("buvid4"):
        return out
    try:
        s = get_session()
        async with s.get("https://api.bilibili.com/x/frontend/finger/spi", headers=headers(),
                         timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = (await r.json(content_type=None)).get("data") or {}
        out.setdefault("buvid3", data.get("b_3", ""))
        out.setdefault("buvid4", data.get("b_4", ""))
        return {k: v for k, v in out.items() if v}
    except Exception as e:
        logger.warning("[bili] 获取 buvid 失败 (%s)", type(e).__name__)
        return out


async def needs_refresh(cookies: dict) -> tuple[bool | None, int]:
    """返回 (是否需要刷新, 服务器毫秒时间戳)。None 表示无法判断（未登录/网络错误）。"""
    url = f"{PASSPORT}/cookie/info?csrf={cookies.get('bili_jct', '')}"
    s = get_session()
    async with s.get(url, headers=headers(cookies), timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json(content_type=None)
    if data.get("code") != 0:
        return None, 0
    d = data.get("data") or {}
    return bool(d.get("refresh")), int(d.get("timestamp") or time.time() * 1000)


async def refresh_cookies(cookies: dict, force: bool = False) -> tuple[dict | None, str]:
    """
    按需续期。返回 (新 cookie 或 None, 说明)。
    None 表示没有换新（不需要 / 无 refresh_token / 失败），说明里写原因。
    """
    token = cookies.get(REFRESH_KEY)
    if not token:
        return None, "没有 refresh_token（旧版扫码登录），无法自动续期，请重新 /bili_login"
    try:
        need, ts = await needs_refresh(cookies)
    except Exception as e:
        return None, f"检查是否需要续期失败: {type(e).__name__}"
    if need is None:
        return None, "cookie 已失效，无法续期，请重新 /bili_login"
    if not need and not force:
        return None, "暂不需要续期"
    if not ts:
        ts = int(time.time() * 1000)

    timeout = aiohttp.ClientTimeout(total=15)
    session = get_session()
    try:
        async with session.get(
            f"https://www.bilibili.com/correspond/1/{_correspond_path(ts)}",
            headers=headers(cookies), timeout=timeout,
        ) as r:
            html = await r.text()
        m = re.search(r'<div id="1-name">\s*([0-9a-f]+)\s*</div>', html)
        if not m:
            return None, "未能从 correspond 页面取到 refresh_csrf（可能时间戳过期或接口改版）"
        form = {
            "csrf": cookies.get("bili_jct", ""), "refresh_csrf": m.group(1),
            "source": "main_web", "refresh_token": token,
        }
        async with session.post(
            f"{PASSPORT}/cookie/refresh", data=form, headers=headers(cookies), timeout=timeout,
        ) as r:
            data = await r.json(content_type=None)
            new = _merge_set_cookies(cookies, r)
        if data.get("code") != 0:
            return None, f"续期失败: {data.get('code')} {data.get('message')}"
        new_token = (data.get("data") or {}).get("refresh_token")
        if not new_token or new.get("SESSDATA") == cookies.get("SESSDATA"):
            return None, "续期接口没有返回新的 SESSDATA"
        new[REFRESH_KEY] = new_token
        # 用新 cookie 确认，旧 refresh_token 随之作废；失败不影响新 cookie 使用
        async with session.post(
            f"{PASSPORT}/confirm/refresh",
            data={"csrf": new.get("bili_jct", ""), "refresh_token": token},
            headers=headers(new), timeout=timeout,
        ) as r:
            confirm = await r.json(content_type=None)
        if confirm.get("code") != 0:
            logger.warning(f"[bili] 确认续期返回 {confirm.get('code')} {confirm.get('message')}（新 cookie 仍可用）")
        return new, "已续期"
    except Exception as e:
        return None, f"续期请求失败: {type(e).__name__}"


def encode_refresh_preview(token: str) -> str:
    """日志里只显示 refresh_token 末 4 位。"""
    return "***" + token[-4:] if token else "<none>"




# ── WBI 签名（推荐流、搜索等 /wbi/ 接口需要） ─────────────────────

_MIXIN = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
    12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]
_wbi_cache: dict = {"key": "", "at": 0.0}


async def _mixin_key(cookies: dict | None) -> str:
    if _wbi_cache["key"] and time.time() - _wbi_cache["at"] < 3600:
        return _wbi_cache["key"]
    s = get_session()
    async with s.get("https://api.bilibili.com/x/web-interface/nav", headers=headers(cookies),
                     timeout=aiohttp.ClientTimeout(total=10)) as r:
        img = ((await r.json(content_type=None)).get("data") or {}).get("wbi_img") or {}
    raw = "".join(u.rsplit("/", 1)[-1].split(".")[0] for u in (img.get("img_url", ""), img.get("sub_url", "")))
    if len(raw) < 64:
        raise RuntimeError("获取 WBI 密钥失败")
    _wbi_cache.update(key="".join(raw[i] for i in _MIXIN)[:32], at=time.time())
    return _wbi_cache["key"]


async def wbi_sign(params: dict, cookies: dict | None = None) -> dict:
    import hashlib
    from urllib.parse import quote, urlencode

    key = await _mixin_key(cookies)
    signed = {**params, "wts": int(time.time())}
    signed = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in sorted(signed.items())}
    # 必须按 %20 编码空格（与实际请求一致），用默认的 "+" 会导致签名不匹配、搜索结果为空
    signed["w_rid"] = hashlib.md5((urlencode(signed, quote_via=quote) + key).encode()).hexdigest()
    return signed


# ── bili_ticket（网页端风控凭证，带上可显著降低 -352/-412） ─────────

async def ensure_ticket(cookies: dict) -> tuple[dict, bool]:
    """缺少或快过期时生成 bili_ticket，返回 (cookie, 是否有更新)。失败时原样返回。"""
    import hashlib
    import hmac

    exp = int(cookies.get("bili_ticket_expires") or 0)
    if cookies.get("bili_ticket") and exp - time.time() > 6 * 3600:
        return cookies, False
    ts = int(time.time())
    sign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
    params = {"key_id": "ec02", "hexsign": sign, "context[ts]": ts, "csrf": cookies.get("bili_jct", "")}
    try:
        s = get_session()
        async with s.post("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                          params=params, headers=headers(cookies),
                          timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json(content_type=None)
        d = data.get("data") or {}
        if data.get("code") != 0 or not d.get("ticket"):
            logger.info(f"[bili] 生成 bili_ticket 失败: {data.get('code')} {data.get('message')}")
            return cookies, False
        out = dict(cookies)
        out["bili_ticket"] = d["ticket"]
        out["bili_ticket_expires"] = str(int(d.get("created_at") or ts) + int(d.get("ttl") or 259200))
        return out, True
    except Exception as e:
        logger.info("[bili] 生成 bili_ticket 异常 (%s)", type(e).__name__)
        return cookies, False
