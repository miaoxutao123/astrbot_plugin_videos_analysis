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

# 将这个函数移到文件前面，确保在被调用前已定义
async def get_video_download_url_by_bvid(bvid, quality=16):
    """获取视频下载链接（无需Cookie的备用方法）"""
    # 获取视频信息
    video_info = await parse_video(bvid)
    if not video_info:
        print("解析视频信息失败")
        return None
    
    aid = video_info["aid"]
    cid = video_info["cid"]
    
    # 使用无Cookie的API获取视频链接
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
    data = await bili_request(api_url)
    
    if data.get("code") != 0:
        print(f"获取视频地址失败: {data.get('message')}")
        return None
    
    # 获取视频URL
    try:
        video_url = data["data"]["durl"][0]["url"]
        return video_url
    except (KeyError, IndexError) as e:
        print(f"解析视频URL失败: {str(e)}")
        return None
        
# 添加Cookie相关配置
COOKIE_FILE = "data/plugins/astrbot_plugin_videos_analysis/bili_cookies.json"
os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)

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

# 修改bili_login函数，支持异步操作
async def bili_login(event=None):
    """B站扫码登录流程（新版API）
    
    参数:
        event: 消息事件对象，用于发送提醒消息
    """
    logger.info("正在生成B站登录二维码...")
    qr_data = await generate_qrcode()
    
    if not qr_data:
        return None
    
    logger.info("\n请使用B站APP扫描以下二维码登录:")
    
    # 获取qrcode_key - 修复这里的变量引用错误
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
    
    logger.info(qr_text)
    
    # 同时也保留base64编码的输出，以防ASCII显示不正常
    logger.info("\n如果上方二维码显示异常，请访问以下链接查看二维码:")
    logger.info(f"data:image/png;base64,{qr_data['image_base64']}")
    
    # 如果有事件对象，发送提醒消息
    if event:
        try:
            # 修改这里：使用正确的消息发送方式
            # 方法1：直接使用 reply 方法
            event.reply("检测到需要登录B站账号，请前往控制台扫描二维码完成登录")
            # 如果上面的方法不起作用，可以尝试以下替代方法
            # 方法2：使用 result 属性
            # event.result = "检测到需要登录B站账号，请前往控制台扫描二维码完成登录"
            # 方法3：如果是使用 yield 返回消息
            # yield Plain("检测到需要登录B站账号，请前往控制台扫描二维码完成登录")
        except Exception as e:
            logger.error(f"发送提醒消息失败: {str(e)}")
    
    # 创建一个异步任务来检查登录状态
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key))
    
    # 返回登录任务，调用方可以使用await等待任务完成
    return login_task

async def check_login_status_loop(qrcode_key):
    """循环检查登录状态，直到登录成功或超时"""
    for _ in range(90):  # 最多等待90秒
        await asyncio.sleep(1)
        status = await check_login_status(qrcode_key)
        
        if status.get("code") == 0:
            data = status.get("data", {})
            # 0: 扫码登录成功, -1: 未扫码, -2: 二维码已过期, -4: 未确认, -5: 已扫码未确认
            if data.get("code") == 0:
                logger.info("\n登录成功!")
                
                try:
                    # 优先从URL参数获取Cookie
                    url = data.get("url", "")
                    if "?" in url:
                        url_params = url.split("?")[1]
                        cookies = {}
                        for param in url_params.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                if key in ["SESSDATA", "bili_jct", "DedeUserID"]:
                                    cookies[key] = unquote(value)
                        
                        # 备选方案：尝试从cookie_info获取
                        if not all(cookies.values()):
                            cookie_info = data.get("data", {}).get("cookie_info", {})
                            for c in cookie_info.get("cookies", []):
                                if c["name"] in ["SESSDATA", "bili_jct"]:
                                    cookies[c["name"]] = c["value"]
                            if "DedeUserID" not in cookies:
                                cookies["DedeUserID"] = str(data.get("data", {}).get("mid", ""))
                        
                        # 最终验证
                        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                            raise ValueError("获取的Cookie格式异常")
                        
                        logger.info(f"获取到的Cookie: {cookies}")
                        
                        await save_cookies_dict(cookies)
                        return cookies
                    else:
                        raise ValueError("URL格式异常，无法提取参数")
                    
                except Exception as e:
                    logger.error(f"登录异常: {str(e)}")
                    logger.error(f"原始响应数据: {data}")
                    
                    # 尝试直接从URL提取Cookie
                    try:
                        url = data.get("url", "")
                        if url and "SESSDATA" in url and "DedeUserID" in url:
                            # 手动解析URL
                            cookies = {}
                            parts = url.split("?")[1].split("&")
                            for part in parts:
                                if "=" in part:
                                    k, v = part.split("=", 1)
                                    if k in ["SESSDATA", "bili_jct", "DedeUserID"]:
                                        cookies[k] = unquote(v)
                            
                            if cookies and "SESSDATA" in cookies and "DedeUserID" in cookies:
                                logger.info(f"备用方法提取的Cookie: {cookies}")
                                await save_cookies_dict(cookies)
                                return cookies
                    except Exception as backup_error:
                        logger.error(f"备用提取方法也失败: {str(backup_error)}")
                    
                    return None
            
            elif data.get("code") == -2:
                logger.info("\n二维码已过期，请重新获取")
                return None
            
            elif data.get("code") == -4 or data.get("code") == -5:
                logger.info("\r请在手机上确认登录")
            
        logger.info(".", end="", flush=True)
    
    logger.info("\n登录超时，请重试")
    return None

async def save_cookies_dict(cookies_dict):
    """保存Cookie字典到文件"""
    async with aiofiles.open(COOKIE_FILE, "w", encoding="utf-8") as f:
        await f.write(json.dumps(cookies_dict, ensure_ascii=False, indent=2))
    
    return cookies_dict

async def load_cookies():
    """从文件加载Cookie"""
    try:
        if os.path.exists(COOKIE_FILE):
            async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
                content = await f.read()
                return json.loads(content)
        return None
    except Exception as e:
        print(f"加载Cookie失败: {str(e)}")
        return None

async def bili_request_with_cookie(url, return_json=True):
    """带Cookie的B站API请求"""
    cookies = await load_cookies()
    
    # 使用更完整的请求头，模拟正常浏览器行为
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Origin": "https://www.bilibili.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    try:
        # 使用正确的Cookie格式
        cookie_dict = {}
        for key, value in cookies.items():
            cookie_dict[key] = value
            
        async with aiohttp.ClientSession(cookies=cookie_dict) as session:
            # 添加调试信息
            print(f"发送请求到: {url}")
            print(f"使用的Cookie: {cookie_dict}")
            
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                if return_json:
                    return await response.json()
                else:
                    return await response.read()
    except aiohttp.ClientError as e:
        return {"code": -400, "message": str(e)}

async def download_video_with_cookie(aid, cid, bvid, quality=80, event=None):
    """使用Cookie下载高清视频"""
    # 先检查Cookie是否有效，而仅仅是存在
    is_valid = await check_cookie_valid()
    
    if not is_valid:
        logger.info("Cookie无效或不存在，尝试登录...")
        login_task = await bili_login(event)
        
        # 等待登录任务完成
        cookies = await login_task
        
        if not cookies:
            logger.info("登录失败，将使用默认画质下载")
            return await download_video(aid, cid, bvid, 16)
    else:
        # 如果Cookie有效，直接加载
        cookies = await load_cookies()
        logger.info("使用已有的有效Cookie")
    
    # 使用Cookie请求高清视频
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&fnval=16&fourk=1"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Origin": "https://www.bilibili.com",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(api_url, headers=headers) as response:
                data = await response.json()
                
                if data.get("code") != 0:
                    print(f"获取视频地址失败: {data.get('message')}")
                    # 降级到无Cookie的下载方式
                    return await download_video(aid, cid, bvid, 16)
                
                # 获取视频URL - 支持dash和durl两种格式
                video_url = None
                try:
                    if "durl" in data["data"]:
                        video_url = data["data"]["durl"][0]["url"]
                    elif "dash" in data["data"]:
                        # 修改：根据quality参数选择最合适的视频流
                        video_streams = data["data"]["dash"]["video"]
                        
                        # 按照质量从高到低排序
                        video_streams.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
                        
                        # 找到不超过请求quality的最高质量
                        selected_stream = None
                        for stream in video_streams:
                            if stream.get("id", 0) <= quality:
                                selected_stream = stream
                                break
                        
                        # 如果没找到合适的，就用最高质量的
                        if not selected_stream and video_streams:
                            selected_stream = video_streams[0]
                            
                        if selected_stream:
                            video_url = selected_stream["baseUrl"]
                            print(f"选择的视频质量: {selected_stream.get('id')}，分辨率: {selected_stream.get('width')}x{selected_stream.get('height')}")
                        else:
                            raise KeyError("无法找到合适的视频流")
                    else:
                        print("未找到视频URL，API结构可能已变更")
                        print(f"API返回数据结构: {data}")
                        return await download_video(aid, cid, bvid, 16)
                except (KeyError, IndexError) as e:
                    print(f"解析视频URL失败: {str(e)}")
                    return await download_video(aid, cid, bvid, 16)
                
                # 下载视频
                async with session.get(video_url, headers=headers) as video_response:
                    video_data = await video_response.read()
                    
                    filename = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}.mp4"
                    os.makedirs(os.path.dirname(filename), exist_ok=True)
                    async with aiofiles.open(filename, "wb") as f:
                        await f.write(video_data)
                    
                    return filename
    except Exception as e:
        print(f"下载视频失败: {str(e)}")
        # 降级到无Cookie的下载方式
        return await download_video(aid, cid, bvid, 16)

async def get_video_download_url_with_cookie(bvid, quality=80, event=None):
    """使用Cookie获取高清视频下载链接"""
    # 先检查Cookie是否有效，而仅仅是存在
    is_valid = await check_cookie_valid()
    
    if not is_valid:
        logger.info("Cookie无效或不存在，尝试登录...")
        login_task = await bili_login(event)
        
        # 等待登录任务完成
        cookies = await login_task
        
        if not cookies:
            logger.info("登录失败，将使用默认画质")
            return await get_video_download_url_by_bvid(bvid, 16)
    else:
        # 如果Cookie有效，直接加载
        cookies = await load_cookies()
        logger.info("使用已有的有效Cookie")
    
    # 获取视频信息
    video_info = await parse_video(bvid)
    
    if not video_info:
        print("解析视频信息失败")
        return None
    
    aid = video_info["aid"]
    cid = video_info["cid"]
    
    # 使用Cookie请求高清视频 - 更新API参数
    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&fnval=16&fourk=1"
    
    headers = {
        "referer": f"https://www.bilibili.com/video/{bvid}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }
    
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(api_url, headers=headers) as response:
                data = await response.json()
                
                if data.get("code") != 0:
                    print(f"获取视频地址失败: {data.get('message')}")
                    # 降级到无Cookie的获取方式
                    return await get_video_download_url_by_bvid(bvid, 16)
                
                # 获取视频URL - 支持dash和durl两种格式
                try:
                    if "durl" in data["data"]:
                        video_url = data["data"]["durl"][0]["url"]
                    elif "dash" in data["data"]:
                        # 获取最高质量的视频流
                        video_url = data["data"]["dash"]["video"][0]["baseUrl"]
                    else:
                        print("未找到视频URL，API结构可能已变更")
                        print(f"API返回数据结构: {data}")
                        return await get_video_download_url_by_bvid(bvid, 16)
                    
                    return video_url
                except (KeyError, IndexError) as e:
                    print(f"解析视频URL失败: {str(e)}")
                    # 降级到无Cookie的获取方式
                    return await get_video_download_url_by_bvid(bvid, 16)
    except Exception as e:
        print(f"获取视频下载链接失败: {str(e)}")
        # 降级到无Cookie的获取方式
        return await get_video_download_url_by_bvid(bvid, 16)

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
    
    # 根据use_login参数决定使用哪种方式获取视频链接
    if use_login:
        direct_url = await get_video_download_url_with_cookie(video_info["bvid"], quality, event)
    else:
        print("根据设置，强制使用无登录方式获取视频")
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
            return{
                "direct_url": direct_url,
                "title": video_info["title"],
                "cover": video_info["cover"],
                "duration": video_info["duration"],
                "stats": video_info["stats"],
                "video_path": filename,
                "view_count" : stats["view"],
                "like_count" : stats["like"],
                "danmaku_count" : stats["danmaku"],
                "coin_count" : stats["coin"],
                "favorite_count" : stats["favorite"]
            }
        else:
            return {
                "direct_url":direct_url,
                "title": video_info["title"],
                "cover": video_info["cover"],
                "duration": video_info["duration"],
                "stats": video_info["stats"],
                "video_path": None,
                "view_count" : stats["view"],
                "like_count" : stats["like"],
                "danmaku_count" : stats["danmaku"],
                "coin_count" : stats["coin"],
                "favorite_count" : stats["favorite"]
            }

# 添加检查Cookie是否有效的函数
# 在文件顶部添加全局变量
COOKIE_VALID = None

async def check_cookie_valid():
    """检查Cookie是否有效"""
    global COOKIE_VALID
    
    # 强制重新检查Cookie有效性
    COOKIE_VALID = None
    
    # 增加调试输出
    print("[DEBUG] 开始执行Cookie有效性检查")
    
    cookies = await load_cookies()
    if not cookies:
        print("未找到Cookie文件")
        return False

    # 严格检查Cookie格式
    required_fields = {
        "SESSDATA": lambda v: len(v) > 30 and ',' in v,
        "bili_jct": lambda v: len(v) == 32,
        "DedeUserID": lambda v: v.isdigit()
    }
    
    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            print(f"Cookie字段验证失败: {field} = {cookies.get(field)}")
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

async def main():
    # 检查Cookie是否有效
    print("正在检查Cookie有效性...")
    is_valid = await check_cookie_valid()
    
    if not is_valid:
        print("Cookie无效或不存在，需要重新登录")
        # 删除无效的Cookie文件
        if os.path.exists(COOKIE_FILE):
            try:
                os.remove(COOKIE_FILE)
                print("已删除无效的Cookie文件")
            except Exception as e:
                print(f"删除Cookie文件失败: {str(e)}")
        
        # 重新登录
        cookies = await bili_login()
        if not cookies:
            print("登录失败，将使用默认画质下载视频")
    else:
        print("Cookie有效，可以直接使用")
    
    url = input("请输入B站视频链接：")
    use_login = input("是否使用登录状态下载？(y/n): ").lower() == 'y'
    quality = 80 if use_login else 64  # 登录状态使用1080P，非登录状态使用720P
    
    result = await process_bili_video(url, quality=quality, use_login=use_login)
    print(result)

if __name__ == "__main__":
    asyncio.run(main())

