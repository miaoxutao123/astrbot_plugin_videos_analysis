import asyncio
import json
import re
from urllib.parse import urlencode

import httpx

from astrbot.api import logger

from .cookie_extractor import extract_and_format_cookies
from .crawlers.douyin.web.endpoints import DouyinAPIEndpoints
from .crawlers.douyin.web.utils import AwemeIdFetcher, BogusManager


_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
_ROUTER_DATA = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})</script>", re.S)
# 抖音分享短链、作品长链（video/note/图集/分享页/modal_id 弹窗）
DOUYIN_LINK = re.compile(r"https?://(?:v\.douyin\.com/[\w\-]+/?|(?:www\.)?(?:ies)?douyin\.com/(?:video|note|slides|share/(?:video|note))/\d+[^\s]*|www\.douyin\.com/\S*?[?&]modal_id=\d+\S*)")
_DIRECT_ID = re.compile(r"douyin\.com/(?:share/)?(?:video|note|slides)/(\d{15,21})|[?&](?:modal_id|aweme_id|vid)=(\d{15,21})")


def _upgrade_share_item(item: dict) -> dict:
    """分享页给的是带水印、720p 的播放地址：换成无水印 play 接口并请求 1080p。"""
    play = (item.get("video") or {}).get("play_addr") or {}
    urls = []
    for u in play.get("url_list") or []:
        u = u.replace("/playwm/", "/play/")
        u = re.sub(r"ratio=[^&]*", "ratio=1080p", u) if "ratio=" in u else u + "&ratio=1080p"
        urls.append(u)
    if urls:
        play["url_list"] = urls
    return item


class DouyinParser:
    """
    一个独立的抖音分享链接解析器。

    支持两种模式:
    1. 浏览器签名 (推荐): 传入 browser_signer, 由 Chromium SDK 自动签名
    2. 纯 Python (回退): 使用内置 A-Bogus 算法 (可能因反爬更新而失效)
    """
    def __init__(self, cookie: str, proxy: str = "", browser_signer=None):
        # 使用cookie_extractor格式化cookie
        self.cookie = extract_and_format_cookies(cookie) if cookie else ""
        self.id_fetcher = AwemeIdFetcher()
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
        self.headers = {
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "User-Agent": self.user_agent,
            "Referer": "https://www.douyin.com/",
            "Cookie": self.cookie,
        }
        # 代理配置（httpx 0.28+ 使用 proxy 单数参数）
        self.proxy = proxy or None
        # 浏览器签名引擎 (可选)
        self._browser_signer = browser_signer

    async def fetch_video_data(self, aweme_id: str) -> dict:
        """
        直接请求抖音API以获取视频数据。
        """
        params = {
            "aweme_id": aweme_id,
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Edge",
            "browser_version": "117.0.2045.47",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "117.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "16",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
            "webid": "7318500000000000000",
            "msToken": "",
        }

        a_bogus = BogusManager.ab_model_2_endpoint(params, self.user_agent)
        endpoint = f"{DouyinAPIEndpoints.POST_DETAIL}?{urlencode(params)}&a_bogus={a_bogus}"

        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(
            transport=transport, timeout=30, proxy=self.proxy
        ) as client:
            response = await client.get(endpoint, headers=self.headers)
            response.raise_for_status()

            # Check if response is empty
            if not response.text:
                raise ValueError(
                    f"Empty response from Douyin API (aweme_id={aweme_id}). "
                    "This may indicate rate limiting, invalid cookie, or blocked request."
                )

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                snippet = response.text[:200]
                content_type = response.headers.get("Content-Type", "")
                raise ValueError(
                    f"Invalid JSON response from Douyin API (aweme_id={aweme_id}, content_type={content_type}, snippet={snippet})"
                ) from exc

    def _process_data(self, raw_data: dict) -> dict:
        """
        处理原始API数据，提取关键信息和下载链接。
        """
        if not raw_data or "aweme_detail" not in raw_data:
            return {"error": "无效的原始数据格式"}

        aweme_detail = raw_data["aweme_detail"]

        media_type = "unknown"
        media_urls = []

        # 最可靠的判断方式：检查是否存在 images 列表并且其不为空
        if aweme_detail.get("images") and len(aweme_detail["images"]) > 0:
            # 默认为图文，但如果发现视频片段，则更新类型
            media_type = "image"
            images = aweme_detail.get("images")
            has_video_segment = False
            for item in images:
                # 检查每个item是图片还是视频片段
                if item.get("video"):
                    has_video_segment = True
                    video_list = item.get("video", {}).get("play_addr", {}).get("url_list")
                    if video_list:
                        media_urls.append(video_list[0])
                elif item.get("url_list"):
                    # 提取最高清的图片链接
                    media_urls.append(item["url_list"][-1])
            if has_video_segment:
                media_type = "multi_video"
        # 否则，当作普通单视频处理
        elif aweme_detail.get("video"):
            media_type = "video"
            video = aweme_detail["video"]

            # 优先从 bit_rate 获取最高画质
            bit_rates = video.get("bit_rate", [])
            if bit_rates:
                best = bit_rates[0]
                for br in bit_rates:
                    if "1080" in br.get("gear_name", ""):
                        best = br
                        break
                url_list = best.get("play_addr", {}).get("url_list", [])
                if url_list:
                    media_urls.append(url_list[0])

            # 回退: 顶层 play_addr
            if not media_urls:
                video_list = video.get("play_addr", {}).get("url_list")
                if video_list:
                    media_urls.append(video_list[0])

        # 提取基础信息
        processed_data = {
            "aweme_id": aweme_detail.get("aweme_id"),
            "type": media_type,
            "desc": aweme_detail.get("desc"),
            "create_time": aweme_detail.get("create_time"),
            "author_nickname": aweme_detail.get("author", {}).get("nickname"),
            "media_urls": media_urls,
        }

        return processed_data

    async def parse(self, share_url: str) -> dict:
        """
        解析单个抖音分享链接，并返回处理后的核心数据。

        Args:
            share_url: 抖音分享链接 (短链/长链/口令均可).

        Returns:
            包含核心视频信息的字典。
        """
        logger.info(f"正在解析链接: {share_url}")

        # 步骤 1: 从分享文案中提取有效的URL
        url_match = re.search(r"(https?://[^\s]+)", share_url)
        if not url_match:
            logger.warning("未能在分享文案中找到有效的URL")
            return {"error": "No valid URL found in the share text"}

        extracted_url = url_match.group(1)
        logger.info(f"成功提取URL: {extracted_url}")

        # 步骤 2: 从URL中提取 aweme_id（长链接直接取，短链接才请求跳转）
        try:
            m = _DIRECT_ID.search(extracted_url)
            aweme_id = (m.group(1) or m.group(2)) if m else await self.id_fetcher.get_aweme_id(
                extracted_url, proxy=self.proxy
            )
            if not aweme_id:
                raise ValueError("未能从链接中提取到 aweme_id")
            logger.info(f"成功提取 aweme_id: {aweme_id}")
        except Exception as e:
            logger.error(f"提取 aweme_id 失败: {e}")
            return {"error": "Failed to extract aweme_id", "details": str(e)}

        # 步骤 3: 使用 aweme_id 获取视频详情
        try:
            # 优先走公开分享页（纯 HTTP、不带登录态、约 0.5 秒）；私密/受限作品再用浏览器签名
            raw_video_data = await self._fetch_via_share(aweme_id)
            if raw_video_data:
                logger.info("通过分享页获取视频数据")
            elif self._browser_signer:
                logger.info("使用浏览器签名获取视频数据")
                raw_video_data = await self._fetch_via_browser(aweme_id)
            else:
                logger.info("使用纯 Python 签名获取视频数据")
                raw_video_data = await self._fetch_via_python(aweme_id)

            if not raw_video_data:
                return {"error": "Failed to fetch video data", "details": "API returned empty response"}

            logger.info("成功获取视频数据")
            # 步骤 4: 处理原始数据，提取核心信息
            processed_data = self._process_data(raw_video_data)
            return processed_data
        except Exception as e:
            logger.error(f"获取或处理视频数据失败: {e}")
            return {"error": "Failed to fetch or process video data", "details": str(e)}

    async def _fetch_via_share(self, aweme_id: str) -> dict | None:
        """公开分享页 iesdouyin.com/share/{note|video}/{id} 的 SSR 数据，不需要签名和 cookie。"""
        try:
            async with httpx.AsyncClient(timeout=10, proxy=self.proxy, follow_redirects=True) as client:
                for kind in ("note", "video", "note"):  # note 路径对视频同样有效；偶尔返回空数据，换路径重试
                    resp = await client.get(f"https://www.iesdouyin.com/share/{kind}/{aweme_id}/",
                                            headers={"User-Agent": _MOBILE_UA})
                    m = _ROUTER_DATA.search(resp.text)
                    if not m:
                        continue
                    for page in (json.loads(m.group(1)).get("loaderData") or {}).values():
                        items = ((page or {}).get("videoInfoRes") or {}).get("item_list") if isinstance(page, dict) else None
                        if items:
                            return {"aweme_detail": _upgrade_share_item(items[0])}
        except Exception as e:
            logger.info(f"分享页解析失败，改用浏览器签名: {e}")
        return None

    async def _fetch_via_browser(self, aweme_id: str) -> dict | None:
        """通过浏览器签名引擎获取视频数据。直接返回 API 原始响应。"""
        params = {
            "aweme_id": aweme_id,
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "platform": "PC",
            # 设备参数（屏幕/系统/浏览器版本等）由 fetch_signed_api 按浏览器真实环境填充
        }
        endpoint = "aweme/v1/web/aweme/detail/"
        raw = await self._browser_signer.fetch_signed_api(endpoint, params)
        if not raw:
            logger.info("浏览器签名请求无结果，刷新页面后重试一次")
            if await self._browser_signer.refresh():
                raw = await self._browser_signer.fetch_signed_api(endpoint, params)
        return raw

    async def _fetch_via_python(self, aweme_id: str) -> dict | None:
        """通过纯 Python A-Bogus 签名获取视频数据 (旧方法, 可能失效)。"""
        return await self.fetch_video_data(aweme_id)

async def main():
    """
    主函数，用于演示解析器功能。
    推荐配合 BrowserSigner 使用:
        signer = BrowserSigner(headless=False)
        await signer.start()
        parser = DouyinParser(cookie="", browser_signer=signer)
        result = await parser.parse("https://v.douyin.com/xxxxx/")
        await signer.stop()
    """
    test_urls = [
        "https://v.douyin.com/offWOmYUFdU/",
    ]

    for url in test_urls:
        print(f"\n------ 正在解析: {url} ------")
        parser = DouyinParser(cookie="")
        result = await parser.parse(url)
        print("\n------ 解析结果 ------")
        print(json.dumps(result, indent=4, ensure_ascii=False))
        print("--------------------")



if __name__ == "__main__":
    asyncio.run(main())
