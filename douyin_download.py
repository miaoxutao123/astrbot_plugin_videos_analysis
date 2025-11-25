import asyncio
import os
import re

import aiofiles
import aiohttp

from .douyin_scraper.cookie_extractor import extract_and_format_cookies


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
                if response.status == 302 or response.status == 301:
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
    """
    if os.path.exists(filename):
        print(f"File '{filename}' already exists. Skipping download.")
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

    for attempt in range(max_retries):
        current_headers = retry_strategies[attempt % len(retry_strategies)].copy()
        strategy_name = ["桌面端", "iPhone", "Android", "抖音APP", "爬虫"][attempt % len(retry_strategies)]

        if cookie and strategy_name not in ["抖音APP", "爬虫"]:
            current_headers["Cookie"] = clean_cookie(cookie)

        try:
            timeout = aiohttp.ClientTimeout(total=30, connect=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                print(f"Attempt {attempt + 1}: Trying {strategy_name} strategy")

                async with session.get(url, headers=current_headers, allow_redirects=True) as response:
                    print(f"Image download attempt {attempt + 1}: Status {response.status}")

                    if response.status == 200:
                        os.makedirs(os.path.dirname(filename), exist_ok=True)

                        content = await response.read()
                        if len(content) > 1000:  # 确保不是错误页面
                            async with aiofiles.open(filename, "wb") as f:
                                await f.write(content)

                            print(f"Image download successful with {strategy_name}: {filename} ({len(content)} bytes)")
                            return True
                        else:
                            print(f"Downloaded content too small ({len(content)} bytes), likely an error page")
                            continue
                    elif response.status == 404:
                        print("Image not found (404), skipping retries")
                        return False
                    else:
                        print(f"Image download failed with status {response.status}")

        except aiohttp.ClientResponseError as e:
            print(f"HTTP error with {strategy_name}: {e.status} {e.message}")
        except aiohttp.ClientError as e:
            print(f"Network error with {strategy_name}: {e}")
        except Exception as e:
            print(f"Unexpected error with {strategy_name}: {e}")

        if attempt < max_retries - 1:
            await asyncio.sleep(2)  # 增加等待时间

    print(f"Failed to download image after {max_retries} attempts")
    return False

async def download_video(url, filename="video.mp4", cookie=None):
    """
    Downloads a video from the given URL asynchronously.

    Args:
        url (str): The URL of the video.
        filename (str): The filename to save the video as.
        cookie (str): Optional cookie string for authentication.
    """
    # Check if the file already exists
    if os.path.exists(filename):
        print(f"File '{filename}' already exists. Skipping download.")
        return

    # 抖音视频下载需要的完整请求头
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

    # 添加Cookie如果提供了
    if cookie:
        headers["Cookie"] = clean_cookie(cookie)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 设置更长的超时时间
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, allow_redirects=True) as response:
                    print(f"Attempt {attempt + 1}: Status {response.status}")

                    if response.status == 403:
                        print("403 Forbidden - trying with mobile headers...")
                        # 尝试移动端请求头
                        mobile_headers = {
                            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1",
                            "Referer": "https://www.douyin.com/",
                            "Accept": "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,audio/*;q=0.6,*/*;q=0.5"
                        }
                        if cookie:
                            mobile_headers["Cookie"] = clean_cookie(cookie)

                        async with session.get(url, headers=mobile_headers, allow_redirects=True) as retry_response:
                            response = retry_response
                            print(f"Mobile retry status: {response.status}")

                    response.raise_for_status()

                    if response.status == 304:
                        print("Video not modified. No download needed.")
                        return

                    os.makedirs(os.path.dirname(filename), exist_ok=True)

                    total_size = int(response.headers.get("content-length", 0))
                    block_size = 8192  # 增大块大小提高下载效率

                    async with aiofiles.open(filename, "wb") as file:
                        downloaded = 0
                        async for data in response.content.iter_chunked(block_size):
                            await file.write(data)
                            downloaded += len(data)
                            if total_size and downloaded % (block_size * 10) == 0:  # 减少打印频率
                                progress = (downloaded / total_size) * 100
                                print(f"\rDownloading: {progress:.1f}% ({downloaded}/{total_size})", end="")

                    if total_size:
                        print(f"\nDownload complete! File saved: {filename}")
                    else:
                        print(f"Download complete! File saved: {filename} (Size unknown)")

                    # 验证文件是否成功下载
                    if os.path.exists(filename) and os.path.getsize(filename) > 0:
                        print(f"File verified successfully: {os.path.getsize(filename)} bytes")
                        return
                    else:
                        print("Downloaded file is empty or doesn't exist, retrying...")
                        if os.path.exists(filename):
                            os.remove(filename)
                        continue

        except aiohttp.ClientError as e:
            print(f"Attempt {attempt + 1} failed with network error: {e}")
            if attempt == max_retries - 1:
                print(f"All {max_retries} attempts failed. Error: {e}")
                return
        except OSError as e:
            print(f"Attempt {attempt + 1} failed with file error: {e}")
            if attempt == max_retries - 1:
                print(f"All {max_retries} attempts failed. Error: {e}")
                return
        except Exception as e:
            print(f"Attempt {attempt + 1} failed with unexpected error: {e}")
            if attempt == max_retries - 1:
                print(f"All {max_retries} attempts failed. Error: {e}")
                return

        # 等待一秒后重试
        await asyncio.sleep(1)

    print("Download failed after all retries")

async def download(url, filename="video.mp4", cookie=None):
    """
    Downloads videos or images from the given URL asynchronously.

    Args:
        url (str): The URL of the media.
        filename (str): The base filename to save the media as.
        cookie (str): Optional cookie string for authentication.
    """
    # 检查是否是抖音图片URL
    if "douyinpic.com" in url and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".webp", "image"]):
        print("Detected Douyin image, using specialized download method")
        success = await download_douyin_image(url, filename, cookie)
        return success

    # 对于视频或其他媒体，使用原来的逻辑
    location_data = await get_location_from_url(url, cookie)

    if location_data and location_data.get("location"):
        download_url = location_data.get("location")
        await download_video(download_url, filename, cookie)
    else:
        await download_video(url, filename, cookie)


# if __name__ == "__main__":
#     url = "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/oMAnCVBQBAEwiiwI8Td2SMAIJPQY0hADhiAPZ~tplv-dy-aweme-images:q75.jpeg?lk3s=138a59ce&x-expires=1744963200&x-signature=Cgf9pS1Fne0tvRujCHt6htkHP%2BI%3D&from=327834062&s=PackSourceEnum_AWEME_DETAIL&se=false&sc=image&biz_tag=aweme_images&l=202503191654145A604C96898653070E42"
#     asyncio.run(download(url))
