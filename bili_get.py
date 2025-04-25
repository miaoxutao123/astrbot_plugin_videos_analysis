import asyncio
import aiohttp
import re
import os
import aiofiles
import json
import time
import qrcode
from PIL import Image
import base64
from io import BytesIO
from urllib.parse import unquote  # 添加这一行导入unquote函数
from astrbot.api import logger
import requests

# 添加Cookie相关配置
COOKIE_FILE = "data/plugins/astrbot_plugin_videos_analysis/bili_cookies.json"
os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)


log_callback = logger.info  # 默认使用 logger.info 作为回调
# 在文件顶部添加全局变量
COOKIE_VALID = None

def set_log_callback(callback):
    """设置日志回调函数"""
    global log_callback
    log_callback = callback

# 配置参数
CONFIG = {
    "VIDEO": {
        "enable": True,
        "send_link": False,
        "send_video": True
    }
}

# 正则表达式
REG_B23 = re.compile(r'(b23\.tv|bili2233\.cn)\/[\w]+')
REG_BV = re.compile(r'BV1\w{9}')
REG_AV = re.compile(r'av\d+', re.I)

# AV转BV算法参数·
AV2BV_TABLE = 'fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF'
AV2BV_TR = {c: i for i, c in enumerate(AV2BV_TABLE)}
AV2BV_S = [11, 10, 3, 8, 4, 6]
AV2BV_XOR = 177451812
AV2BV_ADD = 8728348608

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
    av_num = re.search(r'\d+', av)
    if not av_num:
        return None

    try:
        x = (int(av_num.group()) ^ AV2BV_XOR) + AV2BV_ADD
    except:
        return None

    r = list('BV1 0 4 1 7  ')
    for i in range(6):
        idx = (x // (58**i)) % 58
        r[AV2BV_S[i]] = AV2BV_TABLE[idx]

    return ''.join(r).replace(' ', '0')

async def bili_request(url, return_json=True):
    """发送B站API请求"""
    headers = {
        "referer": "https://www.bilibili.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                if return_json:
                    return await response.json()
                else:
                    return await response.read()
    except aiohttp.ClientError as e:
        return {"code": -400, "message": str(e)}
    
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
        "SESSDATA": lambda v: len(v) > 30 and ',' in v,
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://space.bilibili.com/",
        "Origin": "https://space.bilibili.com",
        "Cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()])
    }

    try:
        async with aiohttp.ClientSession() as session:
            # 添加超时和重试逻辑
            timeout = aiohttp.ClientTimeout(total=10)
            
            async with session.get(url, headers=headers, timeout=timeout) as response:
                # 详细响应分析
                print(f"[DEBUG] 验证响应状态: {response.status}")
                print(f"[DEBUG] 响应头: {dict(response.headers)}")
                
                data = await response.json()
                print(f"[DEBUG] 验证API响应: {data}")
                
                # 修复这里：确保类型一致再比较
                if data.get("code") == 0:
                    api_mid = str(data.get("data", {}).get("mid", ""))
                    cookie_mid = str(cookies["DedeUserID"])
                    
                    if api_mid == cookie_mid:
                        print(f"√ Cookie验证通过，用户名: {data['data']['uname']}")
                        COOKIE_VALID = True
                        return True
                    else:
                        print(f"× Cookie验证失败: 用户ID不匹配 (API: {api_mid}, Cookie: {cookie_mid})")
                else:
                    print(f"× Cookie验证失败: API返回错误 ({data.get('message')})")
                
                return False
                
    except Exception as e:
        print(f"Cookie验证异常: {str(e)}")
        return False


async def parse_b23(short_url):
    """解析b23短链接"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(f"https://{short_url}", allow_redirects=True) as response:
                real_url = str(response.url)

                if REG_BV.search(real_url):
                    return await parse_video(REG_BV.search(real_url).group())
                elif REG_AV.search(real_url):
                    return await parse_video(av2bv(REG_AV.search(real_url).group()))
                return None
    except aiohttp.ClientError:
        return None

async def parse_video(bvid):
    """解析视频信息"""
    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        return None

    info = data["data"]
    return {
        "aid": info["aid"],
        "cid": info["cid"],
        "bvid": bvid,
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
    video_data = await bili_request(video_url, return_json=False)

    if isinstance(video_data, dict):
        return None

    filename = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}.mp4"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    async with aiofiles.open(filename, "wb") as f:
        await f.write(video_data)

    return filename

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
    """保存Cookie到文件"""
    try:
        async with aiofiles.open(COOKIE_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(cookies, ensure_ascii=False, indent=2))
        log_callback(f"Cookie已保存到: {COOKIE_FILE}")
        return True
    except Exception as e:
        log_callback(f"保存Cookie失败: {str(e)}")
        return False

async def load_cookies():
    """从文件加载Cookie"""
    if not os.path.exists(COOKIE_FILE):
        log_callback(f"Cookie文件不存在: {COOKIE_FILE}")
        return None
    
    try:
        async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
            if not content.strip():
                log_callback("Cookie文件为空")
                return None
            cookies = json.loads(content)
            return cookies
    except json.JSONDecodeError:
        log_callback("Cookie文件格式错误")
        return None
    except Exception as e:
        log_callback(f"加载Cookie失败: {str(e)}")
        return None

async def generate_qrcode():
    """生成B站登录二维码（新版API）"""
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    data = await bili_request(url)
    
    if data.get("code") != 0:
        print(f"获取二维码失败: {data.get('message')}")
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
    
    # 修复这里：使用qr对象的make_image方法，而不是直接调用qrcode.make_image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # 转换为base64以便显示
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    
    return {
        "qrcode_key": qrcode_key,
        "image_base64": img_str,
        "url": qr_url
    }

async def check_login_status(qrcode_key):
    """检查登录状态（新版API）"""
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                result = await response.json()
                return result
    except aiohttp.ClientError as e:
        print(f"检查登录状态失败: {str(e)}")
        return {"code": -1, "message": str(e)}

import logging
logger = logging.getLogger(__name__)

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
    qr_text = "\n"
    for row in matrix:
        line = ""
        for cell in row:
            if cell:
                line += "██"  # 黑色方块
            else:
                line += "  "  # 空白
        qr_text += line + "\n"
    
    # 使用logger.info输出二维码
    from astrbot.api import logger
    logger.info(qr_text)
    

    # 保存二维码图片到指定路径
    image_dir = "data/plugins/astrbot_plugin_videos_analysis/image"
    os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, "bili_login_qrcode.png")
    with open(image_path, "wb") as f:
        f.write(base64.b64decode(qr_data["image_base64"]))
    # logger.info(f"二维码图片已保存到: {image_path}")

    # 同时也保留base64编码的输出，以防ASCII显示不正常
    logger.info(f"\n如果上方二维码显示异常，请到一下路径查看二维码:/n{image_path}")
    logger.info("\n如果无法找到，请自行解析一下base64编码的二维码:")
    logger.info(f"data:image/png;base64,{qr_data['image_base64']}")
    
    # 创建一个异步任务来检查登录状态
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key))
    
    # 返回登录任务，调用方可以使用await等待任务完成
    return login_task


async def check_login_status_loop(qrcode_key):
    """循环检查登录状态，直到登录成功或超时"""
    logger.info("等待登录...（最多40秒）")
    for _ in range(40):  # 最多等待90秒
        await asyncio.sleep(1)
        status = await check_login_status(qrcode_key)
        
        if status.get("code") == 0:
            data = status.get("data", {})
            # 0: 扫码登录成功, -1: 未扫码, -2: 二维码已过期, -4: 未确认, -5: 已扫码未确认
            if data.get("code") == 0:
                log_callback("\n登录成功!")
                
                try:
                    # 优先从URL参数获取Cookie
                    url = data.get("url", "")
                    if "?" in url:
                        url_params = url.split("?")[1]
                        cookies = {}
                        for param in url_params.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                useful_keys = [
                                    "_uuid", "DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct",
                                    "bili_ticket", "bili_ticket_expires", "CURRENT_FNVAL", "CURRENT_QUALITY",
                                    "enable_feed_channel", "enable_web_push", "header_theme_version",
                                    "home_feed_column", "LIVE_BUVID", "PVID", "browser_resolution",
                                    "buvid_fp", "buvid3", "fingerprint"
                                ]
                                if key in useful_keys:
                                    cookies[key] = unquote(value)
                        
                        # 验证提取的Cookie是否完整
                        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                            raise ValueError("获取的Cookie格式异常")
                        
                        log_callback(f"获取到的Cookie: {cookies}")
                        
                        await save_cookies_dict(cookies)
                        return cookies
                    else:
                        raise ValueError("URL格式异常，无法提取参数")
                    
                except Exception as e:
                    log_callback(f"登录异常: {str(e)}")
                    log_callback(f"原始响应数据: {data}")
                    return None
            
            elif data.get("code") == -2:
                log_callback("\n二维码已过期，请重新获取")
                return None
            
            elif data.get("code") == -4 or data.get("code") == -5:
                log_callback("请在手机上确认登录")
            
        # log_callback(".")  # 移除 end 和 flush 参数
    
    log_callback("\n登录超时，请重试")
    return None

async def get_video_download_url_with_cookie(bvid, quality=80, event=None):
    """使用Cookie获取高清视频下载链接"""
    # 检查Cookie是否有效
    is_valid = await check_cookie_valid()
    
    if not is_valid:
        log_callback("Cookie无效或不存在，尝试登录...")
        login_result = await bili_login(event)
        cookies = await login_result
        
        if not cookies:
            log_callback("登录失败，将使用默认画质")
            return await get_video_download_url_by_bvid(bvid, 16)
        
        is_valid = await check_cookie_valid()
        if not is_valid:
            log_callback("登录后Cookie仍然无效，将使用默认画质")
            return await get_video_download_url_by_bvid(bvid, 16)
    else:
        cookies = await load_cookies()
        log_callback("使用已有的有效Cookie")
    
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Referer": "https://www.bilibili.com/",
        "Cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()])
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers) as response:
                response.raise_for_status()
                data = await response.json()
                
                if data.get("code") != 0:
                    log_callback(f"获取视频地址失败: {data.get('message')}")
                    return await get_video_download_url_by_bvid(bvid, 16)
                
                # 获取视频和音频的URL
                video_url = data["data"]["dash"]["video"][0]["baseUrl"]
                audio_url = data["data"]["dash"]["audio"][0]["baseUrl"]
                return video_url, audio_url
    except Exception as e:
        log_callback(f"获取视频下载链接失败: {str(e)}")
        return await get_video_download_url_by_bvid(bvid, 16)

async def download_file(url, file_path, headers):
    """异步下载文件"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(8192):
                        await f.write(chunk)
    except Exception as e:
        log_callback(f"下载文件失败: {str(e)}")
        raise

async def download_video_with_cookie(aid, cid, bvid, quality=80, event=None):
    """使用Cookie下载高清视频并合成音视频"""
    # 检查是否已存在合成后的文件
    output_file = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}_output.mp4"
    if os.path.exists(output_file):
        log_callback(f"视频已存在，跳过下载和合成：{output_file}")
        return output_file

    # 获取视频和音频的URL
    result = await get_video_download_url_with_cookie(bvid, quality, event)
    if not result:
        log_callback("获取视频和音频URL失败")
        return None

    video_url, audio_url = result
    headers = {
        "Referer": "https://www.bilibili.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0"
    }

    # 下载视频和音频
    video_file = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}_video.mp4"
    audio_file = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}_audio.mp3"

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
    await merge_audio_and_video(audio_file, video_file, output_file)

    log_callback(f"视频合成完成，保存为 {output_file}")
    return output_file

async def merge_audio_and_video(audio_file, video_file, output_file):
    """异步合成音视频"""
    cmd = f'ffmpeg -i "{audio_file}" -i "{video_file}" -acodec copy -vcodec copy "{output_file}"'
    process = await asyncio.create_subprocess_shell(cmd)
    await process.communicate()

async def process_bili_video(url, download_flag=True, quality=80, use_login=True, event=None):
    """主处理函数
    
    参数:
        url: B站视频链接
        download_flag: 是否下载视频
        quality: 视频质量
        use_login: 是否使用登录状态下载，设为False则强制使用无Cookie方式
        event: 消息事件对象，用于发送提醒消息
    """
    # 判断链接类型
    if REG_B23.search(url):
        video_info = await parse_b23(REG_B23.search(url).group())
    elif REG_BV.search(url):
        video_info = await parse_video(REG_BV.search(url).group())
    elif REG_AV.search(url):
        bvid = av2bv(REG_AV.search(url).group())
        video_info = await parse_video(bvid) if bvid else None
    else:
        print("不支持的链接格式")
        return

    if not video_info:
        print("解析视频信息失败")
        return

    stats = video_info["stats"]
    bvid = video_info["bvid"]

    # 检查本地是否已存在相同 bvid 的视频文件
    video_file = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}_output.mp4"
    if os.path.exists(video_file):
        log_callback(f"本地已存在视频文件：{video_file}，跳过下载")
        return {
            "direct_url": None,
            "title": video_info["title"],
            "cover": video_info["cover"],
            "duration": video_info["duration"],
            "stats": video_info["stats"],
            "video_path": video_file,
            "view_count": stats["view"],
            "like_count": stats["like"],
            "danmaku_count": stats["danmaku"],
            "coin_count": stats["coin"],
            "favorite_count": stats["favorite"],
            "bvid": bvid,
        }

    # 根据use_login参数决定使用哪种方式获取视频链接
    if use_login:
        # 先检查Cookie是否有效
        is_valid = await check_cookie_valid()
        
        # 如果Cookie无效，尝试登录
        if not is_valid:
            log_callback("Cookie无效或不存在，尝试登录...")
            # 直接调用bili_login并等待结果完成
            login_result = await bili_login(event)
            # 因为bili_login返回的是一个任务，所以需要再次await
            cookies = await login_result
            
            if cookies:
                log_callback("登录成功，重新检查Cookie有效性")
                is_valid = await check_cookie_valid()
            else:
                log_callback("登录失败或超时")
                is_valid = False
        
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
    if CONFIG["VIDEO"]["send_video"]:
        if download_flag:
            print("\n开始下载视频...")
            
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
                print("根据设置，强制使用无登录方式下载视频")
                filename = await download_video(
                    video_info["aid"],
                    video_info["cid"],
                    video_info["bvid"],
                    min(quality, 64)  # 无登录模式下最高支持720P
                )

            if filename:
                print(f"视频已保存为：{filename}")
            else:
                print("下载视频失败")
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

