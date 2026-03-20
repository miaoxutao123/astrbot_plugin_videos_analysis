import asyncio
import os
import re

import aiofiles
import aiohttp

from astrbot.api import logger

from ..douyin_scraper.cookie_extractor import extract_and_format_cookies


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
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Connection": "keep-alive",
        "Host": "www.douyin.com",
        "Priority": "u=0, i",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "TE": "trailers",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0"
    }

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
    if os.path.exists(filename):
        logger.info(f"文件已存在，跳过下载: {filename}")
        return True

    max_retries = 5
    retry_strategies = [
        # 策略1: 完整桌面端请求头
        {
            "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "DNT": "1",
            "Pragma": "no-cache",
            "Referer": "https://www.douyin.com/",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        },
        # 策略2: iPhone移动端请求头
        {
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Connection": "keep-alive",
            "Referer": "https://www.douyin.com/",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
        },
        # 策略3: Android移动端请求头
        {
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Referer": "https://www.douyin.com/",
            "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
        },
        # 策略4: 模拟抖音APP请求头
        {
            "Accept": "*/*",
            "Connection": "keep-alive",
            "User-Agent": "com.ss.android.ugc.aweme/180400 (Linux; U; Android 11; zh_CN; SM-G973F; Build/RP1A.200720.012; Cronet/TTNetVersion:36a9da4a 2021-11-26 QuicVersion:8d8b5b0e 2021-11-23)",
            "X-Requested-With": "com.ss.android.ugc.aweme"
        },
        # 策略5: 最简请求头
        {
            "User-Agent": "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)",
            "Accept": "*/*"
        }
    ]

    strategy_names = ["桌面端", "iPhone", "Android", "抖音APP", "爬虫"]

    for attempt in range(max_retries):
        current_headers = retry_strategies[attempt % len(retry_strategies)].copy()
        strategy_name = strategy_names[attempt % len(retry_strategies)]

        if cookie and strategy_name not in ["抖音APP", "爬虫"]:
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

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
                            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
                            "Referer": "https://www.douyin.com/",
                            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                        }
                        if cookie:
                            mobile_headers["Cookie"] = clean_cookie(cookie)

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
