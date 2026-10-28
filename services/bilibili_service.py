import asyncio
import base64
import json
import os
import re
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
from ..utils.paths import DOWNLOAD_DIR_BILI as BILI_DOWNLOAD_DIR
from ..utils.paths import IMAGE_DIR

# Cookie有效性缓存
COOKIE_VALID = None

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
    """发送B站API请求"""
    if not url or not isinstance(url, str):
        return {"code": -400, "message": "Invalid URL"}

    headers = client_identity.headers(Referer="https://www.bilibili.com/")

    try:
        timeout = aiohttp.ClientTimeout(total=30)  # 30秒超时
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()

                if return_json:
                    try:
                        data = await response.json()
                        if not isinstance(data, dict):
                            return {"code": -400, "message": "Invalid JSON format"}
                        return data
                    except aiohttp.ContentTypeError:
                        return {"code": -400, "message": "Invalid content type"}
                    except Exception as e:
                        return {"code": -400, "message": f"JSON parse error: {str(e)}"}
                else:
                    return await response.read()
    except aiohttp.ClientError as e:
        return {"code": -400, "message": f"Network error: {str(e)}"}
    except asyncio.TimeoutError:
        return {"code": -400, "message": "Request timeout"}

# 添加检查Cookie是否有效的函数

async def check_cookie_valid():
    """检查Cookie是否有效"""
    global COOKIE_VALID

    # 强制重新检查Cookie有效性
    COOKIE_VALID = None

    # 增加调试输出
    # log_callback("[DEBUG] 开始执行Cookie有效性检查")

    cookies = await load_cookies()
    # log_callback(f"[DEBUG] 加载的Cookie: {cookies}")
    if not cookies:
        log_callback("[DEBUG] 未找到Cookie文件或Cookie文件为空，需要登录")
        return False

    # 严格检查Cookie格式
    required_fields = {
        "SESSDATA": lambda v: len(v) > 30 and "," in v,
        "bili_jct": lambda v: len(v) == 32,
        "DedeUserID": lambda v: v.isdigit()
    }

    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            log_callback(f"[DEBUG] Cookie字段验证失败: {field} = {cookies.get(field)}")
            return False

    # 使用新的验证API
    url = "https://api.bilibili.com/x/member/web/account"

    # 增强请求头
    headers = {
        **client_identity.headers(),
        "Referer": "https://space.bilibili.com/",
        "Origin": "https://space.bilibili.com",
        "Cookie": cookie_header(cookies)
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 添加超时和重试逻辑
            timeout = aiohttp.ClientTimeout(total=10)

            async with session.get(url, headers=headers, timeout=timeout) as response:
                # 详细响应分析
                logger.debug(f"验证响应状态: {response.status}")

                data = await response.json()
                logger.debug(f"验证API响应: {data}")

                # 修复这里：确保类型一致再比较
                if data.get("code") == 0:
                    api_mid = str(data.get("data", {}).get("mid", ""))
                    cookie_mid = str(cookies["DedeUserID"])

                    if api_mid == cookie_mid:
                        log_callback(f"Cookie验证通过，用户名: {data['data']['uname']}")
                        COOKIE_VALID = True
                        return True
                    else:
                        log_callback(f"Cookie验证失败: 用户ID不匹配 (API: {api_mid}, Cookie: {cookie_mid})")
                else:
                    log_callback(f"Cookie验证失败: API返回错误 ({data.get('message')})")

                return False

    except Exception as e:
        log_callback(f"Cookie验证异常: {str(e)}")
        return False


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
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("https://api.bilibili.com/x/web-interface/nav", headers=headers) as resp:
                data = await resp.json(content_type=None)
                return data.get("data") if isinstance(data, dict) else None
    except Exception as e:
        log_callback(f"查询B站登录信息失败: {e}")
        return None


async def parse_b23(short_url):
    """解析b23短链接"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(f"https://{short_url}", allow_redirects=True) as response:
                real_url = str(response.url)

                if REG_BV.search(real_url):
                    return await parse_video(REG_BV.search(real_url).group())
                elif REG_AV.search(real_url):
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

async def download_video(aid, cid, bvid, quality=16):
    """下载视频"""

    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        return None

    video_url = data["data"]["durl"][0]["url"]
    filename = f"{BILI_DOWNLOAD_DIR}/{bvid}.mp4"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    headers = {
        "Referer": "https://www.bilibili.com/",
        **client_identity.headers(),
    }
    # 流式写盘，长视频不会因为 30 秒总超时或整段读进内存而失败
    try:
        await download_file(video_url, filename, headers)
    except Exception:
        if os.path.exists(filename):
            os.remove(filename)
        return None
    return filename if os.path.getsize(filename) > 0 else None

async def get_video_download_url_by_bvid(bvid, quality=16):
    """获取视频下载链接（无需Cookie的备用方法）"""
    # 获取视频信息
    video_info = await parse_video(bvid)
    if not video_info:
        log_callback("解析视频信息失败")
        return None

    aid = video_info["aid"]
    cid = video_info["cid"]

    # 使用无Cookie的API获取视频链接
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        log_callback(f"获取视频地址失败: {data.get('message')}")
        return None

    # 获取视频URL
    try:
        video_url = data["data"]["durl"][0]["url"]
        return video_url
    except (KeyError, IndexError) as e:
        log_callback(f"解析视频URL失败: {str(e)}")
        return None



# 添加缺失的Cookie相关函数
async def save_cookies_dict(cookies):
    """Persist credentials with authenticated encryption and atomic replacement."""
    try:
        await asyncio.to_thread(write_secure_json, COOKIE_FILE, cookies)
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
        log_callback(f"获取二维码网络请求失败: {e}")
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

    # 使用print直接输出到控制台，确保二维码能正常显示
    logger.info(qr_text)

    # 同时使用logger记录
    logger.info("B站登录二维码已显示在控制台")


    # 保存二维码图片到指定路径
    image_dir = IMAGE_DIR
    os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, "bili_login_qrcode.png")
    with open(image_path, "wb") as f:
        f.write(base64.b64decode(qr_data["image_base64"]))
    # logger.info(f"二维码图片已保存到: {image_path}")

    log_callback(f"\n如果上方二维码显示异常，请查看二维码文件: {image_path}")
    logger.info(f"二维码图片已保存到: {image_path}")
    logger.info("如果无法扫描，可复制下方base64码用在线工具解析:")
    logger.info(f"data:image/png;base64,{qr_data['image_base64'][:50]}...")

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
                    log_callback(f"登录异常: {e}（响应字段: {sorted(data)}，Set-Cookie: {sorted(status.get('_set_cookies') or {})}）")
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
RISK_CODES = {-101, -352, -412}


async def _report_failure(reason: str):
    if FAILURE_HOOK:
        try:
            await FAILURE_HOOK(reason)
        except Exception as e:
            log_callback(f"上报B站账号失败时出错: {e}")


async def get_video_download_url_with_cookie(bvid, quality=80, event=None):
    """使用Cookie获取高清视频下载链接"""
    # 检查Cookie是否有效
    if not await check_cookie_valid():
        if os.path.exists(COOKIE_FILE):
            await _report_failure("cookie 校验失败")
        log_callback("Cookie无效或不存在，将使用默认画质")
        return await get_video_download_url_by_bvid(bvid, 16)
    cookies = await load_cookies()

    # 获取视频信息
    video_info = await parse_video(bvid)
    if not video_info:
        log_callback("解析视频信息失败")
        return None

    aid = video_info["aid"]
    cid = video_info["cid"]

    # 使用Cookie请求高清视频
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&fnval=16&fourk=1"
    headers = {
        **client_identity.headers(),
        "Referer": "https://www.bilibili.com/",
        "Cookie": cookie_header(cookies)
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()

                if data.get("code") != 0:
                    log_callback(f"获取视频地址失败: {data.get('message')}")
                    if data.get("code") in RISK_CODES:
                        await _report_failure(f"playurl {data.get('code')} {data.get('message')}")
                    return await get_video_download_url_by_bvid(bvid, 16)

                # 获取视频和音频的URL
                video_url = data["data"]["dash"]["video"][0]["baseUrl"]
                audio_url = data["data"]["dash"]["audio"][0]["baseUrl"]
                return video_url, audio_url
    except Exception as e:
        log_callback(f"获取视频下载链接失败: {str(e)}")
        return await get_video_download_url_by_bvid(bvid, 16)

async def download_file(url, file_path, headers):
    """异步下载文件并显示进度"""
    try:
        timeout = aiohttp.ClientTimeout(total=3600)  # 设置1小时的超时
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                total_size = int(response.headers.get("Content-Length", 0))
                downloaded_size = 0
                last_reported_progress = -1

                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            progress = int((downloaded_size / total_size) * 100)
                            if progress > last_reported_progress:
                                log_callback(f"下载 {os.path.basename(file_path)}: {progress}%")
                                last_reported_progress = progress
                log_callback(f"文件 {os.path.basename(file_path)} 下载完成。")
    except Exception as e:
        log_callback(f"下载文件失败: {str(e)}")
        raise

async def download_video_with_cookie(aid, cid, bvid, quality=80, event=None):
    """使用Cookie下载高清视频并合成音视频"""
    # 检查是否已存在合成后的文件
    output_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_output.mp4"
    if os.path.exists(output_file):
        log_callback(f"视频已存在，跳过下载和合成：{output_file}")
        return output_file

    # 获取视频和音频的URL
    result = await get_video_download_url_with_cookie(bvid, quality, event)
    if not result:
        log_callback("获取视频和音频URL失败")
        return None

    headers = {
        "Referer": "https://www.bilibili.com/",
        **client_identity.headers(),
    }

    if isinstance(result, str):
        # Cookie 请求失败时回退为单文件 mp4 直链
        os.makedirs(BILI_DOWNLOAD_DIR, exist_ok=True)
        await download_file(result, output_file, headers)
        return output_file
    video_url, audio_url = result

    # 下载视频和音频
    video_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_video.mp4"
    audio_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_audio.mp3"

    os.makedirs(os.path.dirname(video_file), exist_ok=True)

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

    # 合成音视频
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
    return output_file

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
    """主处理函数

    参数:
        url: B站视频链接
        download_flag: 是否下载视频
        quality: 视频质量
        use_login: 是否使用登录状态下载，设为False则强制使用无Cookie方式
        event: 消息事件对象，用于发送提醒消息
    """
    if not url or not isinstance(url, str):
        log_callback("无效的URL参数")
        return None

    video_info = None

    # 判断链接类型并解析
    try:
        if REG_B23.search(url):
            video_info = await parse_b23(REG_B23.search(url).group())
        elif REG_FULL_URL.search(url):
            # 处理完整的B站URL
            match = REG_FULL_URL.search(url)
            video_id = match.group(1) if match else ""
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
    except Exception as e:
        log_callback(f"解析链接时发生错误: {str(e)}")
        return None

    if not video_info:
        log_callback("解析视频信息失败")
        return None

    # 安全获取统计信息
    stats = video_info.get("stats", {})
    bvid = video_info.get("bvid")

    if not bvid:
        log_callback("无法获取视频BV号")
        return None

    # 检查本地是否已存在相同 bvid 的视频文件
    video_file = f"{BILI_DOWNLOAD_DIR}/{bvid}_output.mp4"
    if os.path.exists(video_file):
        log_callback(f"本地已存在视频文件：{video_file}，跳过下载")
        return {
            "direct_url": None,
            "title": video_info.get("title", "未知标题"),
            "cover": video_info.get("cover", ""),
            "duration": video_info.get("duration", 0),
            "stats": stats,
            "video_path": video_file,
            "view_count": stats.get("view", 0),
            "like_count": stats.get("like", 0),
            "danmaku_count": stats.get("danmaku", 0),
            "coin_count": stats.get("coin", 0),
            "favorite_count": stats.get("favorite", 0),
            "bvid": bvid,
        }

    # 根据use_login参数决定使用哪种方式获取视频链接
    if use_login:
        # 先检查Cookie是否有效
        is_valid = await check_cookie_valid()

        if not is_valid:
            log_callback("B站 Cookie 无效或不存在，本次使用无登录方式。管理员可发送 /bili_login 扫码登录。")
            use_login = False
            if os.path.exists(COOKIE_FILE):
                await _report_failure("cookie 校验失败")

        # 根据Cookie有效性决定使用哪种方式获取视频链接
        if is_valid:
            log_callback("使用登录状态获取视频链接")
            direct_url = await get_video_download_url_with_cookie(video_info["bvid"], quality, event)
        else:
            log_callback("Cookie无效，使用无登录方式获取视频")
            direct_url = await get_video_download_url_by_bvid(video_info["bvid"], min(quality, 64))
    else:
        log_callback("根据设置，强制使用无登录方式获取视频")
        direct_url = await get_video_download_url_by_bvid(video_info["bvid"], min(quality, 64))  # 无登录模式下最高支持720P

    # 下载视频
    if download_flag:
        log_callback("开始下载视频...")

        # 根据use_login参数决定使用哪种方式下载视频
        if use_login:
            filename = await download_video_with_cookie(
                video_info["aid"],
                video_info["cid"],
                video_info["bvid"],
                quality,
                event
            )
        else:
            log_callback("根据设置，强制使用无登录方式下载视频")
            filename = await download_video(
                video_info["aid"],
                video_info["cid"],
                video_info["bvid"],
                min(quality, 64)  # 无登录模式下最高支持720P
            )

        if filename:
            log_callback(f"视频已保存为：{filename}")
        else:
            log_callback("下载视频失败")
        return {
            "direct_url": direct_url,
            "title": video_info["title"],
            "cover": video_info["cover"],
            "duration": video_info["duration"],
            "stats": video_info["stats"],
            "video_path": filename,
            "view_count": stats["view"],
            "like_count": stats["like"],
            "danmaku_count": stats["danmaku"],
            "coin_count": stats["coin"],
            "favorite_count": stats["favorite"],
            "bvid": video_info["bvid"],
        }
    else:
        return {
            "direct_url": direct_url,
            "title": video_info["title"],
            "cover": video_info["cover"],
            "duration": video_info["duration"],
            "stats": video_info["stats"],
            "video_path": None,
            "view_count": stats["view"],
            "like_count": stats["like"],
            "danmaku_count": stats["danmaku"],
            "coin_count": stats["coin"],
            "favorite_count": stats["favorite"],
            "bvid": video_info["bvid"],
        }

