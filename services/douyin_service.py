import asyncio
import os
import re
from urllib.parse import urlparse

import aiofiles
import aiohttp

from astrbot.api import logger

from ..douyin_scraper.cookie_extractor import extract_and_format_cookies
from ..utils import client_identity


# 只有抖音主站接口需要登录 cookie；CDN（douyinvod/douyinpic 等）和任何跳转后的地址都不发，避免登录态外泄
_COOKIE_HOSTS = ("douyin.com", "iesdouyin.com")


def scoped_cookie(url, cookie):
    """仅当 url 属于抖音主站域名时返回 cookie，否则返回 None。"""
    if not cookie:
        return None
    host = (urlparse(url).hostname or "").lower()
    return cookie if any(host == h or host.endswith("." + h) for h in _COOKIE_HOSTS) else None


def clean_cookie(cookie):
    """
    清理cookie字符串，移除无法编码的字符并格式化抖音cookie
    """
    if not cookie:
        return ""

    # 首先格式化抖音cookie
    formatted_cookie = extract_and_format_cookies(cookie)

    # 然后移除无法编码的字符
    return re.sub(r"[^\x00-\x7F]+", "", formatted_cookie)

async def get_location_from_url(url, cookie=None):
    """
    处理单个 URL，获取响应头中的 location，并模拟指定的请求头。

    Args:
        url: 单个 URL。
        cookie: 可选的cookie字符串。

    Returns:
        包含 URL 和 location 的字典。
    """
    headers = client_identity.headers(
        Accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        Sec_Fetch_Dest="document", Sec_Fetch_Mode="navigate", Sec_Fetch_Site="none", Sec_Fetch_User="?1",
        Upgrade_Insecure_Requests="1",
    )

    cookie = scoped_cookie(url, cookie)
    if cookie:
        headers["Cookie"] = clean_cookie(cookie)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status in (301, 302):
                    location = response.headers.get("location")
                    return {"url": url, "location": location}
                else:
                    return {"url": url, "location": None, "status_code": response.status}
    except aiohttp.ClientError as e:
        return {"url": url, "error": str(e)}

async def download_douyin_image(url, filename, cookie=None):
    """
    专门用于下载抖音图片的函数

    Args:
        url (str): 抖音图片URL
        filename (str): 保存文件名
        cookie (str): 可选的Cookie

    Returns:
        bool: 下载是否成功
    """
    cookie = scoped_cookie(url, cookie)
    if os.path.exists(filename):
        logger.info(f"文件已存在，跳过下载: {filename}")
        return True

    max_retries = 5
    # 只用两种身份：和登录浏览器一致的桌面端；手机端兜底且不带 cookie。
    # 之前还会轮换 Android、抖音 App、百度爬虫的 UA——同一个 IP 上一会儿是爬虫一会儿是 App，
    # 这个 IP 本身会被标记，而登录账号也在这个 IP 上
    retry_strategies = [
        client_identity.headers(
            Accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            Referer="https://www.douyin.com/", Sec_Fetch_Dest="image", Sec_Fetch_Mode="no-cors",
            Sec_Fetch_Site="cross-site",
        ),
        {
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
        },
    ]

    strategy_names = ["桌面端", "iPhone"]

    for attempt in range(max_retries):
        current_headers = retry_strategies[attempt % len(retry_strategies)].copy()
        strategy_name = strategy_names[attempt % len(retry_strategies)]

        if cookie and strategy_name == "桌面端":
            current_headers["Cookie"] = clean_cookie(cookie)

        try:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info(f"图片下载尝试 {attempt + 1}: 使用 {strategy_name} 策略")

                async with session.get(url, headers=current_headers, allow_redirects=True) as response:
                    logger.debug(f"图片下载尝试 {attempt + 1}: 状态码 {response.status}")

                    if response.status == 200:
                        os.makedirs(os.path.dirname(filename), exist_ok=True)

                        content = await response.read()
                        if len(content) > 1000:  # 确保不是错误页面
                            async with aiofiles.open(filename, "wb") as f:
                                await f.write(content)

                            logger.info(f"图片下载成功 ({strategy_name}): {filename} ({len(content)} bytes)")
                            return True
                        else:
                            logger.warning(f"下载内容过小 ({len(content)} bytes)，可能是错误页面")
                            continue
                    elif response.status == 404:
                        logger.warning("图片未找到 (404)，跳过重试")
                        return False
                    else:
                        logger.warning(f"图片下载失败，状态码: {response.status}")

        except aiohttp.ClientResponseError as e:
            logger.error(f"HTTP错误 ({strategy_name}): {e.status} {e.message}")
        except aiohttp.ClientError as e:
            logger.error(f"网络错误 ({strategy_name}): {e}")
        except Exception as e:
            logger.error(f"未知错误 ({strategy_name}): {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(2)

    logger.error(f"图片下载失败，已重试 {max_retries} 次")
    return False

async def download_video(url, filename="video.mp4", cookie=None):
    """
    异步下载抖音视频。

    Args:
        url (str): 视频URL
        filename (str): 保存文件名
        cookie (str): 可选的Cookie

    Returns:
        bool: 下载是否成功
    """
    cookie = scoped_cookie(url, cookie)
    if cookie:
        # 主站播放地址先在本地解析跳转，CDN 地址不带 cookie
        loc = (await get_location_from_url(url, cookie)).get("location")
        if loc:
            url, cookie = loc, scoped_cookie(loc, cookie)
    if os.path.exists(filename):
        logger.info(f"文件已存在，跳过下载: {filename}")
        return True

    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Connection": "keep-alive",
        "Referer": "https://www.douyin.com/",
        "Sec-Fetch-Dest": "video",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }
    headers.update(client_identity.headers())

    if cookie:
        headers["Cookie"] = clean_cookie(cookie)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as response:
                    logger.info(f"视频下载尝试 {attempt + 1}: 状态码 {response.status}")

                    if response.status == 403:
                        logger.warning("403 Forbidden，尝试使用移动端请求头...")
                        mobile_headers = {
                            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
                            "Referer": "https://www.douyin.com/",
                            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                        }
                        # 手机端兜底不带 cookie：登录态只出现在和登录浏览器一致的请求里

                        async with session.get(url, headers=mobile_headers, allow_redirects=True) as retry_response:
                            response = retry_response
                            logger.info(f"移动端重试状态码: {response.status}")

                    response.raise_for_status()

                    if response.status == 304:
                        logger.info("视频未修改，无需下载")
                        return True

                    os.makedirs(os.path.dirname(filename), exist_ok=True)

                    total_size = int(response.headers.get("content-length", 0))
                    block_size = 8192

                    async with aiofiles.open(filename, "wb") as file:
                        downloaded = 0
                        async for data in response.content.iter_chunked(block_size):
                            await file.write(data)
                            downloaded += len(data)

                    if total_size:
                        logger.info(f"视频下载完成: {filename} ({total_size} bytes)")
                    else:
                        logger.info(f"视频下载完成: {filename} (大小未知)")

                    # 验证文件是否成功下载
                    if os.path.exists(filename) and os.path.getsize(filename) > 0:
                        logger.info(f"文件验证成功: {os.path.getsize(filename)} bytes")
                        return True
                    else:
                        logger.warning("下载的文件为空或不存在，重试中...")
                        if os.path.exists(filename):
                            os.remove(filename)
                        continue

        except aiohttp.ClientError as e:
            logger.error(f"尝试 {attempt + 1} 网络错误: {e}")
            if attempt == max_retries - 1:
                logger.error(f"全部 {max_retries} 次尝试均失败: {e}")
                return False
        except OSError as e:
            logger.error(f"尝试 {attempt + 1} 文件错误: {e}")
            if attempt == max_retries - 1:
                logger.error(f"全部 {max_retries} 次尝试均失败: {e}")
                return False
        except Exception as e:
            logger.error(f"尝试 {attempt + 1} 未知错误: {e}")
            if attempt == max_retries - 1:
                logger.error(f"全部 {max_retries} 次尝试均失败: {e}")
                return False

        await asyncio.sleep(1)

    logger.error("视频下载失败，已用尽所有重试次数")
    return False

async def download(url, filename="video.mp4", cookie=None):
    """
    异步下载抖音视频或图片。

    Args:
        url (str): 媒体URL
        filename (str): 保存文件名
        cookie (str): 可选的Cookie

    Returns:
        bool: 下载是否成功
    """
    # 检查是否是抖音图片URL
    if "douyinpic.com" in url and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", "image"]):
        logger.info("检测到抖音图片URL，使用专用下载方法")
        return await download_douyin_image(url, filename, cookie)

    # 对于视频或其他媒体，使用原来的逻辑
    location_data = await get_location_from_url(url, cookie)

    if location_data and location_data.get("location"):
        download_url = location_data.get("location")
        return await download_video(download_url, filename, cookie)
    else:
        return await download_video(url, filename, cookie)
