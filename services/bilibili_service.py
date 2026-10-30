import asyncio
import base64
import hashlib
import json
import os
import re
import time
from io import BytesIO
from urllib.parse import unquote

import aiofiles
import aiohttp
import httpx
import qrcode

from astrbot.api import logger

# 统一使用 astrbot logger，避免 print 和 log_callback
log_callback = logger.info

# 路径常量
from ..utils.paths import BILI_COOKIE_FILE as COOKIE_FILE
from ..utils import client_identity
from ..utils.secure_storage import read_secure_json, write_secure_json, CredentialStoreError
from .bili_account import REFRESH_KEY, cookie_header, ensure_buvid
from .bili_http import get_session
from .bili_read_circuit import is_open as _is_bili_read_circuit_open, today as _bili_circuit_today, trip as trip_bili_read_circuit
from ..utils.paths import DOWNLOAD_DIR_BILI as BILI_DOWNLOAD_DIR
from ..utils.paths import IMAGE_DIR

# Cookie有效性缓存
COOKIE_VALID = None
_COOKIE_VALID_CACHE: dict[str, tuple[bool, float]] = {}
_COOKIE_VALID_LOCK = asyncio.Lock()
_COOKIE_VALID_TTL = 6 * 3600
_COOKIE_INVALID_TTL = 60
_BILI_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(1)
RISK_NOTIFY_HOOK = None
RISK_CODES = {-352, -412}
_RISK_CIRCUIT_LOCAL_OPEN = False
_RISK_CIRCUIT_LOCAL_DATE = ""


def bili_read_circuit_open() -> bool:
    global _RISK_CIRCUIT_LOCAL_OPEN, _RISK_CIRCUIT_LOCAL_DATE
    today = _bili_circuit_today()
    if _RISK_CIRCUIT_LOCAL_DATE and _RISK_CIRCUIT_LOCAL_DATE != today:
        _RISK_CIRCUIT_LOCAL_OPEN = False
        _RISK_CIRCUIT_LOCAL_DATE = ""
    return _RISK_CIRCUIT_LOCAL_OPEN or _is_bili_read_circuit_open()

# 正则表达式
REG_B23 = re.compile(r"(b23\.tv|bili2233\.cn)\/[\w]+")
REG_BV = re.compile(r"BV1\w{9}")
REG_AV = re.compile(r"av\d+", re.I)
REG_FULL_URL = re.compile(r"https?://www\.bilibili\.com/video/(BV1\w{9}|av\d+)")

# AV转BV算法参数（2024 年起 B 站使用的新算法，兼容 aid >= 2^30）
AV2BV_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
AV2BV_XOR = 23442827791579
AV2BV_MAX_AID = 1 << 51

def format_number(num):
    """格式化数字显示"""
    num = int(num)
    if num < 1e4:
        return str(num)
    elif num < 1e8:
        return f"{num/1e4:.1f}万"
    else:
        return f"{num/1e8:.1f}亿"

def av2bv(av):
    """AV号转BV号"""
    av_num = re.search(r"\d+", str(av))
    if not av_num:
        return None
    tmp = (AV2BV_MAX_AID | int(av_num.group())) ^ AV2BV_XOR
    chars = list("BV1000000000")
    idx = len(chars) - 1
    while tmp > 0:
        chars[idx] = AV2BV_ALPHABET[tmp % 58]
        tmp //= 58
        idx -= 1
    chars[3], chars[9] = chars[9], chars[3]
    chars[4], chars[7] = chars[7], chars[4]
    return "".join(chars)

async def bili_request(url, return_json=True):
    """发送B站API请求；共享连接池，统一超时与响应错误处理。"""
    if not url or not isinstance(url, str):
        return {"code": -400, "message": "Invalid URL"}
    headers = client_identity.headers(Referer="https://www.bilibili.com/")
    try:
        session = get_session()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
            response.raise_for_status()
            if return_json:
                try:
                    data = await response.json(content_type=None)
                    return data if isinstance(data, dict) else {"code": -400, "message": "Invalid JSON format"}
                except Exception as exc:
                    return {"code": -400, "message": f"JSON parse error: {type(exc).__name__}"}
            return await response.read()
    except aiohttp.ClientResponseError as exc:
        return {"code": -400, "message": f"HTTP {exc.status}"}
    except aiohttp.ClientError as exc:
        return {"code": -400, "message": f"Network error: {type(exc).__name__}"}
    except asyncio.TimeoutError:
        return {"code": -400, "message": "Request timeout"}



# 添加检查Cookie是否有效的函数

async def check_cookie_valid(cookies: dict | None = None, force: bool = False):
    """通过 nav 接口验证登录态；按 Cookie 指纹缓存，网络失败不缓存。"""
    global COOKIE_VALID
    cookies = cookies if cookies is not None else await load_cookies()
    if not isinstance(cookies, dict) or not cookies:
        COOKIE_VALID = False
        return False
    fingerprint = hashlib.sha256(cookie_header(cookies).encode("utf-8")).hexdigest()
    if not cookies.get("SESSDATA") or not str(cookies.get("DedeUserID", "")).isdigit():
        _COOKIE_VALID_CACHE[fingerprint] = (False, time.monotonic())
        COOKIE_VALID = False
        return False
    now = time.monotonic()
    cached = _COOKIE_VALID_CACHE.get(fingerprint)
    if not force and cached:
        valid, checked_at = cached
        ttl = _COOKIE_VALID_TTL if valid else _COOKIE_INVALID_TTL
        if now - checked_at < ttl:
            COOKIE_VALID = valid
            return valid

    async with _COOKIE_VALID_LOCK:
        now = time.monotonic()
        cached = _COOKIE_VALID_CACHE.get(fingerprint)
        if not force and cached:
            valid, checked_at = cached
            ttl = _COOKIE_VALID_TTL if valid else _COOKIE_INVALID_TTL
            if now - checked_at < ttl:
                COOKIE_VALID = valid
                return valid
        info = await get_login_info(cookies)
        if info is None:
            # A network failure is not evidence that a credential has expired.
            COOKIE_VALID = None
            return False
        valid = bool(info.get("isLogin")) and str(info.get("mid", "")) == str(cookies.get("DedeUserID", ""))
        _COOKIE_VALID_CACHE[fingerprint] = (valid, time.monotonic())
        if len(_COOKIE_VALID_CACHE) > 16:
            oldest = sorted(_COOKIE_VALID_CACHE.items(), key=lambda item: item[1][1])[:-8]
            for key, _ in oldest:
                _COOKIE_VALID_CACHE.pop(key, None)
        COOKIE_VALID = valid
        if valid:
            log_callback("B站登录态验证通过")
        else:
            log_callback("B站登录态已失效或账号 UID 不匹配")
        return valid


def _cookie_validation_is_definitive(cookies: dict | None) -> bool:
    """True only when nav explicitly proved this exact cookie set invalid."""
    if not isinstance(cookies, dict) or not cookies:
        return False
    fingerprint = hashlib.sha256(cookie_header(cookies).encode("utf-8")).hexdigest()
    cached = _COOKIE_VALID_CACHE.get(fingerprint)
    return bool(cached and cached[0] is False)


async def get_login_info(cookies: dict | None = None) -> dict | None:
    """查询当前 cookie 对应的登录信息（x/web-interface/nav），失败返回 None。"""
    cookies = cookies if cookies is not None else await load_cookies()
    if not cookies:
        return None
    headers = {
        **client_identity.headers(),
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie_header(cookies),
    }
    try:
        session = get_session()
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=headers, timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
        if not isinstance(data, dict) or data.get("code") != 0:
            return None
        value = data.get("data")
        return value if isinstance(value, dict) else None
    except Exception as exc:
        logger.warning("[bili] 登录状态查询失败 (%s)", type(exc).__name__)
        return None


async def parse_b23(short_url):
    """解析b23短链接"""
    try:
        session = get_session()
        async with session.head(f"https://{short_url}", allow_redirects=True,
                                timeout=aiohttp.ClientTimeout(total=15)) as response:
            real_url = str(response.url)
        if REG_BV.search(real_url):
            return await parse_video(REG_BV.search(real_url).group())
        if REG_AV.search(real_url):
            return await parse_video(aid=REG_AV.search(real_url).group()[2:])
        return None
    except aiohttp.ClientError:
        return None

async def parse_video(bvid=None, aid=None):
    """解析视频信息（BV 号或 aid 二选一）"""
    query = f"bvid={bvid}" if bvid else f"aid={int(aid)}"
    api_url = f"https://api.bilibili.com/x/web-interface/view?{query}"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        log_callback(f"B站视频信息获取失败 ({query}): {data.get('code')} {data.get('message')}")
        return None

    info = data["data"]
    return {
        "aid": info["aid"],
        "cid": info["cid"],
        "bvid": info.get("bvid") or bvid,
        "title": info["title"],
        "cover": info["pic"],
        "duration": info["duration"],
        "stats": {
            "view": format_number(info["stat"]["view"]),
            "like": format_number(info["stat"]["like"]),
            "danmaku": format_number(info["stat"]["danmaku"]),
            "coin": format_number(info["stat"]["coin"]),
            "favorite": format_number(info["stat"]["favorite"])
        }
    }

async def download_video(aid, cid, bvid, quality=16, video_url=None):
    """下载匿名 MP4；可复用上游已请求的 playurl，避免重复打接口。"""
    if not video_url:
        api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
        data = await bili_request(api_url)
        if data.get("code") != 0:
            return None
        try:
            video_url = data["data"]["durl"][0]["url"]
        except (KeyError, IndexError, TypeError):
            return None
    filename = f"{BILI_DOWNLOAD_DIR}/{bvid}.mp4"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    headers = {"Referer": "https://www.bilibili.com/", **client_identity.headers()}
    try:
        await download_file(video_url, filename, headers)
    except Exception:
        if os.path.exists(filename):
            os.remove(filename)
        return None
    return filename if os.path.getsize(filename) > 0 else None

async def get_video_download_url_by_bvid(bvid, quality=16, video_info=None):
    """获取视频下载链接（无需Cookie的备用方法）。可复用已解析的视频信息。"""
    video_info = video_info or await parse_video(bvid)
    if not video_info:
        log_callback("解析视频信息失败")
        return None
    aid = video_info["aid"]
    cid = video_info["cid"]
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
    data = await bili_request(api_url)
    if data.get("code") != 0:
        log_callback(f"获取匿名视频地址失败: {data.get('message')}")
        return None
    try:
        return data["data"]["durl"][0]["url"]
    except (KeyError, IndexError, TypeError):
        log_callback("匿名视频地址响应缺少 durl")
        return None


# 添加缺失的Cookie相关函数
async def save_cookies_dict(cookies):
    """Persist credentials with authenticated encryption and atomic replacement."""
    global COOKIE_VALID
    try:
        await asyncio.to_thread(write_secure_json, COOKIE_FILE, cookies)
        _COOKIE_VALID_CACHE.clear()
        COOKIE_VALID = None
        log_callback("B站 Cookie 已加密保存")
        return True
    except CredentialStoreError:
        logger.error("B站 Cookie 加密保存失败；保留原文件")
        return False


async def load_cookies():
    """Read credentials; migrate legacy plaintext JSON once (fail closed)."""
    try:
        cookies = await asyncio.to_thread(read_secure_json, COOKIE_FILE)
        return cookies if isinstance(cookies, dict) and cookies else None
    except CredentialStoreError:
        logger.error("B站 Cookie 解密失败，请检查 VA_CREDENTIAL_KEY / .credentials.key；不会自动覆盖数据")
        return None


async def generate_qrcode():
    """生成B站登录二维码（新版API，使用httpx）"""
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers={
                **client_identity.headers(),
            })
            data = resp.json()
    except Exception as e:
        log_callback(f"获取二维码网络请求失败: {type(e).__name__}")
        return None

    if data.get("code") != 0:
        log_callback(f"获取二维码失败: {data.get('message')}")
        return None

    qr_data = data["data"]
    qr_url = qr_data["url"]
    qrcode_key = qr_data["qrcode_key"]

    # 生成二维码图片
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # 转换为base64
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()

    return {
        "qrcode_key": qrcode_key,
        "image_base64": img_str,
        "url": qr_url
    }

async def check_login_status(qrcode_key):
    """检查登录状态（新版API，使用httpx）"""
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {
        **client_identity.headers(),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
            result = response.json()
            # 新版接口扫码成功后通过 Set-Cookie 下发登录 cookie（URL 里只剩 crossDomain ticket）
            result["_set_cookies"] = {k: v for k, v in response.cookies.items()}
            return result
    except Exception as e:
        log_callback(f"检查登录状态失败: {str(e)}")
        return {"code": -1, "message": str(e)}

async def bili_login(event=None):
    """B站扫码登录流程（新版API）

    参数:
        event: 消息事件对象，用于发送提醒消息
    """
    log_callback("正在生成B站登录二维码...")
    qr_data = await generate_qrcode()

    if not qr_data:
        return None

    log_callback("\n请使用B站APP扫描以下二维码登录:")

    # 获取qrcode_key - 修复变量名
    qrcode_key = qr_data["qrcode_key"]

    # 重新创建二维码用于终端显示
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1,
    )
    # 使用原始二维码数据中的URL
    qr.add_data(qr_data["url"])
    qr.make(fit=True)

    # 获取二维码矩阵并在终端中打印
    matrix = qr.get_matrix()
    qr_text = "\n======= B站登录二维码 =======\n"
    for row in matrix:
        line = ""
        for cell in row:
            if cell:
                line += "██"  # 黑色方块
            else:
                line += "  "  # 空白
        qr_text += line + "\n"
    qr_text += "==========================\n"

    # 登录二维码包含一次性凭据，不输出到日志或控制台。


    # 保存二维码图片到指定路径
    image_dir = IMAGE_DIR
    os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, f"bili_login_qrcode_{os.urandom(5).hex()}.png")
    fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64decode(qr_data["image_base64"]))
    if os.name == "posix":
        os.chmod(image_path, 0o600)
    # logger.info(f"二维码图片已保存到: {image_path}")

    # 仅记录文件已生成，不暴露二维码 URL、base64 或可扫码内容。
    # 二维码内容包含一次性登录凭据；绝不写入日志或控制台。

    # 创建一个异步任务来检查登录状态
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key))

    # 返回登录任务，调用方可以使用await等待任务完成
    return login_task


_LOGIN_COOKIE_KEYS = {
    "_uuid", "DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "sid",
    "bili_ticket", "bili_ticket_expires", "buvid_fp", "buvid3", "buvid4",
}


def _login_cookies(status: dict, data: dict) -> dict:
    """扫码成功后的登录 cookie：新版在 Set-Cookie 里，旧版拼在跳转 URL 的参数里。"""
    cookies = {k: unquote(v) for k, v in (status.get("_set_cookies") or {}).items() if v and k in _LOGIN_COOKIE_KEYS}
    if cookies.get("SESSDATA"):
        return cookies
    url = data.get("url", "")
    for param in (url.split("?", 1)[1] if "?" in url else "").split("&"):
        if "=" in param:
            key, value = param.split("=", 1)
            if key in _LOGIN_COOKIE_KEYS:
                cookies[key] = unquote(value)
    return cookies


async def check_login_status_loop(qrcode_key, timeout_seconds: int = 150):
    """循环检查登录状态，直到登录成功或超时（B站二维码本身 180 秒过期）"""
    logger.info(f"等待登录...（最多{timeout_seconds}秒）")
    for _ in range(timeout_seconds):
        await asyncio.sleep(1)
        status = await check_login_status(qrcode_key)

        if status.get("code") == 0:
            data = status.get("data", {})
            # 0: 扫码登录成功, -1: 未扫码, -2: 二维码已过期, -4: 未确认, -5: 已扫码未确认
            if data.get("code") == 0:
                log_callback("\n登录成功!")

                try:
                    cookies = _login_cookies(status, data)
                    if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                        raise ValueError("扫码成功但没有拿到 SESSDATA/DedeUserID（接口可能又改版了）")
                    if data.get("refresh_token"):
                        cookies[REFRESH_KEY] = data["refresh_token"]
                    else:
                        log_callback("扫码结果里没有 refresh_token，cookie 到期后需要重新扫码")
                    cookies = await ensure_buvid(cookies)
                    log_callback(f"B站登录成功: DedeUserID={cookies.get('DedeUserID')}")
                    await save_cookies_dict(cookies)
                    return cookies
                except Exception as e:
                    log_callback(f"登录异常: {type(e).__name__}（响应字段: {sorted(data)}，Set-Cookie 字段数: {len(status.get('_set_cookies') or {})}）")
                    return None

            elif data.get("code") == -2:
                log_callback("\n二维码已过期，请重新获取")
                return None

            elif data.get("code") == -4 or data.get("code") == -5:
                log_callback("请在手机上确认登录")

        # log_callback(".")  # 移除 end 和 flush 参数

    log_callback("\n登录超时，请重试")
    return None

# 账号池轮换钩子：AccountManager 注册 async (reason) -> None，遇到失效/风控时调用
FAILURE_HOOK = None


async def _report_failure(reason: str):
    if FAILURE_HOOK:
        try:
            await FAILURE_HOOK(reason)
        except Exception as e:
            log_callback(f"上报B站账号失败时出错: {type(e).__name__}")


async def _open_bili_read_circuit(code, message: str) -> None:
    """Open the daily read circuit once and notify configured admins once."""
    global _RISK_CIRCUIT_LOCAL_OPEN, _RISK_CIRCUIT_LOCAL_DATE
    try:
        first = await asyncio.to_thread(trip_bili_read_circuit, code, message)
    except Exception as exc:
        logger.error("[bili] 无法持久化当日风控熔断状态 (%s)，本进程内仍将停止登录态请求", type(exc).__name__)
        first = not _RISK_CIRCUIT_LOCAL_OPEN
        _RISK_CIRCUIT_LOCAL_OPEN = True
        _RISK_CIRCUIT_LOCAL_DATE = _bili_circuit_today()
    if not first:
        return
    logger.warning("[bili] 收到风控响应 %s；当日停止登录态 playurl 请求，后续降级匿名解析", code)
    if RISK_NOTIFY_HOOK:
        try:
            await RISK_NOTIFY_HOOK(f"[B站] 收到平台风控响应 {code}，已开启当日登录态下载熔断；本日后续仅尝试匿名下载。")
        except Exception as exc:
            logger.warning("[bili] 风控告警推送失败 (%s)", type(exc).__name__)


async def get_video_download_url_with_cookie(bvid, quality=80, event=None, video_info=None, cookies=None):
    """使用登录态获取 DASH 音视频地址；明确风控后当日不再重试登录态接口。"""
    if bili_read_circuit_open():
        log_callback("B站登录态下载已触发当日熔断，改用匿名通道")
        return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)
    cookies = cookies if cookies is not None else await load_cookies()
    if not cookies or not await check_cookie_valid(cookies):
        if _cookie_validation_is_definitive(cookies) and os.path.exists(COOKIE_FILE):
            await _report_failure("cookie 校验失败")
        log_callback("Cookie无效或不存在，将使用匿名默认画质")
        return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)

    video_info = video_info or await parse_video(bvid)
    if not video_info:
        log_callback("解析视频信息失败")
        return None
    aid, cid = video_info["aid"], video_info["cid"]
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&fnval=16&fourk=1"
    headers = {
        **client_identity.headers(),
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie_header(cookies),
    }
    try:
        session = get_session()
        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if data.get("code") != 0:
            code = data.get("code")
            message = str(data.get("message") or "unknown")[:120]
            log_callback(f"获取B站视频地址失败: code={code}")
            if code in RISK_CODES:
                await _open_bili_read_circuit(code, message)
            elif code == -101:
                await _report_failure("登录态失效 (-101)")
            return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)
        dash = data.get("data", {}).get("dash", {})
        video = (dash.get("video") or [{}])[0]
        audio = (dash.get("audio") or [{}])[0]
        video_url = video.get("baseUrl") or video.get("base_url")
        audio_url = audio.get("baseUrl") or audio.get("base_url")
        if not video_url or not audio_url:
            log_callback("B站 DASH 响应缺少音视频地址，改用匿名通道")
            return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)
        return video_url, audio_url
    except aiohttp.ClientResponseError as exc:
        if exc.status in (412, 429):
            await _open_bili_read_circuit(exc.status, f"HTTP {exc.status}")
        else:
            logger.warning("[bili] playurl HTTP 请求失败 (HTTP %s)", exc.status)
        return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)
    except Exception as exc:
        logger.warning("[bili] playurl 请求失败 (%s)，改用匿名通道", type(exc).__name__)
        return await get_video_download_url_by_bvid(bvid, 16, video_info=video_info)

async def download_file(url, file_path, headers):
    """异步流式下载；复用连接池，并对长媒体请求单独设置总超时。"""
    try:
        session = get_session()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3600)) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded_size = 0
            last_reported_progress = -1
            async with aiofiles.open(file_path, "wb") as file:
                async for chunk in response.content.iter_chunked(8192):
                    await file.write(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0:
                        progress = int((downloaded_size / total_size) * 100)
                        if progress > last_reported_progress:
                            log_callback(f"下载 {os.path.basename(file_path)}: {progress}%")
                            last_reported_progress = progress
            log_callback(f"文件 {os.path.basename(file_path)} 下载完成。")
    except Exception as exc:
        logger.warning("[bili] 文件下载失败 (%s)", type(exc).__name__)
        raise

async def download_video_with_cookie(aid, cid, bvid, quality=80, event=None, result=None):
    """使用Cookie下载高清视频并合成；result 可复用上游已请求的 playurl。"""
    output_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_output.mp4"
    if os.path.exists(output_file):
        log_callback(f"视频已存在，跳过下载和合成：{output_file}")
        return output_file
    if result is None:
        result = await get_video_download_url_with_cookie(bvid, quality, event)
    if not result:
        log_callback("获取视频和音频URL失败")
        return None
    headers = {"Referer": "https://www.bilibili.com/", **client_identity.headers()}
    os.makedirs(BILI_DOWNLOAD_DIR, exist_ok=True)
    if isinstance(result, str):
        await download_file(result, output_file, headers)
        return output_file if os.path.exists(output_file) and os.path.getsize(output_file) > 0 else None
    video_url, audio_url = result
    video_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_video.mp4"
    audio_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_audio.mp3"
    if not os.path.exists(video_file):
        log_callback("正在下载视频...")
        await download_file(video_url, video_file, headers)
    else:
        log_callback(f"视频文件已存在，跳过下载：{video_file}")
    if not os.path.exists(audio_file):
        log_callback("正在下载音频...")
        await download_file(audio_url, audio_file, headers)
    else:
        log_callback(f"音频文件已存在，跳过下载：{audio_file}")
    log_callback("正在合成音视频...")
    if not await merge_audio_and_video(audio_file, video_file, output_file):
        log_callback("ffmpeg 合成音视频失败")
        return None
    for tmp in (video_file, audio_file):
        try:
            os.remove(tmp)
        except OSError:
            pass
    log_callback(f"视频合成完成，保存为 {output_file}")
    return output_file if os.path.exists(output_file) and os.path.getsize(output_file) > 0 else None

async def merge_audio_and_video(audio_file, video_file, output_file):
    """异步合成音视频"""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", audio_file, "-i", video_file,
        "-acodec", "copy", "-vcodec", "copy", output_file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logger.error(f"ffmpeg 合成失败: {stderr.decode(errors='ignore')[-300:]}")
        return False
    return os.path.exists(output_file)

async def process_bili_video(url, download_flag=True, quality=80, use_login=True, event=None):
    """解析 B站视频并按需下载；登录态失效或风控时安全降级到匿名通道。"""
    if not url or not isinstance(url, str):
        log_callback("无效的URL参数")
        return None
    try:
        if REG_B23.search(url):
            video_info = await parse_b23(REG_B23.search(url).group())
        elif REG_FULL_URL.search(url):
            video_id = REG_FULL_URL.search(url).group(1)
            if REG_BV.search(video_id):
                video_info = await parse_video(video_id)
            elif REG_AV.search(video_id):
                video_info = await parse_video(aid=video_id[2:])
            else:
                video_info = None
        elif REG_BV.search(url):
            video_info = await parse_video(REG_BV.search(url).group())
        elif REG_AV.search(url):
            video_info = await parse_video(aid=REG_AV.search(url).group()[2:])
        else:
            log_callback("不支持的链接格式")
            return None
    except Exception as exc:
        logger.warning("[bili] 解析链接失败 (%s)", type(exc).__name__)
        return None
    if not video_info:
        log_callback("解析视频信息失败")
        return None

    stats = video_info.get("stats", {})
    bvid = video_info.get("bvid")
    if not bvid:
        log_callback("无法获取视频BV号")
        return None

    def result_payload(direct_url=None, video_path=None):
        return {
            "direct_url": direct_url,
            "title": video_info.get("title", "未知标题"),
            "cover": video_info.get("cover", ""),
            "duration": video_info.get("duration", 0),
            "stats": stats,
            "video_path": video_path,
            "view_count": stats.get("view", 0),
            "like_count": stats.get("like", 0),
            "danmaku_count": stats.get("danmaku", 0),
            "coin_count": stats.get("coin", 0),
            "favorite_count": stats.get("favorite", 0),
            "bvid": bvid,
        }

    def existing_download():
        for candidate in (
            os.path.join(BILI_DOWNLOAD_DIR, f"{bvid}_output.mp4"),
            os.path.join(BILI_DOWNLOAD_DIR, f"{bvid}.mp4"),
        ):
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
        return None

    async def resolve_and_download():
        nonlocal use_login
        if use_login and bili_read_circuit_open():
            log_callback("B站登录态下载已触发当日熔断，改用匿名通道")
            use_login = False
        cookies = None
        if use_login:
            cookies = await load_cookies()
            is_valid = await check_cookie_valid(cookies)
            if not is_valid:
                log_callback("B站 Cookie 无效或当前无法验证，本次使用匿名方式。管理员可私聊发送 /bili_login 登录。")
                use_login = False
                if _cookie_validation_is_definitive(cookies) and os.path.exists(COOKIE_FILE):
                    await _report_failure("cookie 校验失败")
        if use_login:
            log_callback("使用登录状态获取视频链接")
            direct_url = await get_video_download_url_with_cookie(
                bvid, quality, event, video_info=video_info, cookies=cookies
            )
        else:
            log_callback("使用匿名方式获取视频链接")
            direct_url = await get_video_download_url_by_bvid(
                bvid, min(quality, 64), video_info=video_info
            )
        filename = None
        if download_flag and direct_url:
            log_callback("开始下载视频...")
            if use_login:
                filename = await download_video_with_cookie(
                    video_info["aid"], video_info["cid"], bvid, quality, event, result=direct_url
                )
            else:
                filename = await download_video(
                    video_info["aid"], video_info["cid"], bvid, min(quality, 64), video_url=direct_url
                )
            if filename:
                log_callback(f"视频已保存为：{filename}")
            else:
                log_callback("下载视频失败；仍返回可用的视频信息")
        return direct_url, filename

    if download_flag:
        # Serialize media downloads. Re-check the cache after acquiring the lock
        # so concurrent messages for the same BV do not repeat playurl/download.
        async with _BILI_DOWNLOAD_SEMAPHORE:
            cached_path = existing_download()
            if cached_path:
                log_callback(f"本地已存在视频文件：{cached_path}，跳过下载")
                return result_payload(video_path=cached_path)
            direct_url, filename = await resolve_and_download()
    else:
        direct_url, filename = await resolve_and_download()
    return result_payload(direct_url=direct_url, video_path=filename)
