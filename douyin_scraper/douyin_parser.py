import asyncio
import json
import re
from urllib.parse import urlencode

import httpx

from astrbot.api import logger

from .cookie_extractor import extract_and_format_cookies
from .crawlers.douyin.web.endpoints import DouyinAPIEndpoints
from .crawlers.douyin.web.utils import AwemeIdFetcher, BogusManager


class DouyinParser:
    """
    一个独立的抖音分享链接解析器。
    """
    def __init__(self, cookie: str, proxy: str = ""):
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
            video_list = aweme_detail.get("video", {}).get("play_addr", {}).get("url_list")
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

        # 步骤 2: 从URL中提取 aweme_id
        try:
            aweme_id = await self.id_fetcher.get_aweme_id(
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
            raw_video_data = await self.fetch_video_data(aweme_id)
            logger.info("成功获取视频数据")
            # 步骤 4: 处理原始数据，提取核心信息
            processed_data = self._process_data(raw_video_data)
            return processed_data
        except Exception as e:
            logger.error(f"获取或处理视频数据失败: {e}")
            return {"error": "Failed to fetch or process video data", "details": str(e)}

async def main():
    """
    主函数，用于演示解析器功能。
    """
    # 从配置文件读取Cookie，或者直接在这里粘贴
    # 注意：这里的Cookie是为了演示，实际使用时请确保它是有效的
    user_cookie = "passport_csrf_token=34c03e4b75b7621b16fa1d02fbff4138; passport_csrf_token_default=34c03e4b75b7621b16fa1d02fbff4138; ttwid=1%7C7iJRehNQVuBAsuqhWPBbfKDpr16-VAW7mTyzI2rK_WQ%7C1755565986%7C16c33adc0ae4d3f3bdc462e0539e7e494d70b3e3d37d4af2e8e8f5b8cbce6779; bd_ticket_guard_client_web_domain=2; enter_pc_once=1; UIFID_TEMP=25fbca946115c5de1a71a2f3ab2554e0f974c22280bf68dee7a40800d5de48828e8fa047c34d1c1fa6368e83e8a708864f0aaa1b8b546dd8b0700db02108b0719baf96c9e384a96c4c9fb97727cfb815; hevc_supported=true; IsDouyinActive=true; home_can_add_dy_2_desktop=%220%22; dy_swidth=2560; dy_sheight=1440; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A2560%2C%5C%22screen_height%5C%22%3A1440%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A20%2C%5C%22device_memory%5C%22%3A0%2C%5C%22downlink%5C%22%3A%5C%22%5C%22%2C%5C%22effective_type%5C%22%3A%5C%22%5C%22%2C%5C%22round_trip_time%5C%22%3A0%7D%22; fpk1=U2FsdGVkX1/DMUp5kDCB8UwyTPBhF2z0Sq8+QOnpIkjy0M2/dK6SfnC41KN8weSfFhBLhBg8tcrTwQ+Mwcm7Bg==; fpk2=72c62ac3761e65106bdcc23caec06ba2; strategyABtestKey=%221755565980.268%22; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Atrue%2C%22volume%22%3A0.5%7D; stream_player_status_params=%22%7B%5C%22is_auto_play%5C%22%3A0%2C%5C%22is_full_screen%5C%22%3A0%2C%5C%22is_full_webscreen%5C%22%3A0%2C%5C%22is_mute%5C%22%3A1%2C%5C%22is_speed%5C%22%3A1%2C%5C%22is_visible%5C%22%3A1%7D%22; xgplayer_user_id=534136793350; s_v_web_id=verify_meapqnn2_rVTujbZO_Qb6T_4XPe_8kKk_hW60QbRGBR6T; odin_tt=e261bdd16913632ab644e7bf7f6e1e7856a09bf617b1314ff3c968b9fb1611951c908ece90766ce9a4927d0bc3a44a172cb7612fb97fbd3f2fcb719a18542148; __security_mc_1_s_sdk_crypt_sdk=43b5ab78-4435-a0f5; download_guide=%221%2F20250814%2F0%22; passport_mfa_token=CjcuoOtIpi7fcrpxQtuRxB2x72YsVo%2BwatzBYj%2F4Wffc3vLAWCvGnlARCMVZigog8jNTiT0U0f1mGkoKPAAAAAAAAAAAAABPWRTVYiB4iI%2FWdh4RzqXoNtxceQyaJhdwYuJDqFqMXIt2F6MLKK4ySkYRIG%2FbGqSoNBCZs%2FkNGPax0WwgAiIBA4G%2BPGc%3D; d_ticket=b27dcd263ad670d628e5bc6ee64ced91f192a; passport_assist_user=CkEO3n_PzPoyK1KVaOtm9IVMj_eGEMj4EVVKHlc6Qe8cEQtFnPaux8LIZHBwpRxR6-JgiEU34Mjs3HT6HcM5VJU6FhpKCjwAAAAAAAAAAAAAT1mZQNQCXyC2wknK37Gv-Xa8z5ScvhIlKQTHlzTb2Huh7AQCf291Idd_l5AFRVtxejQQ47L5DRiJr9ZUIAEiAQOMmXx-; n_mh=Y1x7kdpJo4k2DGKfk4rgVX984YWA2SL4gmt7WFVBi9w; sid_guard=29455b68898a48cdea90291220be4a76%7C1755134549%7C5184000%7CMon%2C+13-Oct-2025+01%3A22%3A29+GMT; uid_tt=51aedabeb11d770fbdd2308eca027a29; uid_tt_ss=51aedabeb11d770fbdd2308eca027a29; sid_tt=29455b68898a48cdea90291220be4a76; sessionid=29455b68898a48cdea90291220be4a76; sessionid_ss=29455b68898a48cdea90291220be4a76; session_tlb_tag=sttt%7C17%7CKUVbaImKSM3qkCkSIL5Kdv________-w8K1ByDUfNhJx9CG_m8SKhFib3YuLELd2-H0gdrI_EY0%3D; is_staff_user=false; sid_ucp_v1=1.0.0-KDI3YTJiYTM5YzEyNDVkMzc0Y2U5YzQ4YjdhNzg2NTJmZjA1NTk5ZDEKIQiJ2bCfgs3GBBDV9PTEBhjvMSAMMNX9iKwGOAdA9AdIBBoCbHEiIDI5NDU1YjY4ODk4YTQ4Y2RlYTkwMjkxMjIwYmU0YTc2; ssid_ucp_v1=1.0.0-KDI3YTJiYTM5YzEyNDVkMzc0Y2U5YzQ4YjdhNzg2NTJmZjA1NTk5ZDEKIQiJ2bCfgs3GBBDV9PTEBhjvMSAMMNX9iKwGOAdA9AdIBBoCbHEiIDI5NDU1YjY4ODk4YTQ4Y2RlYTkwMjkxMjIwYmU0YTc2; login_time=1755134549773; _bd_ticket_crypt_cookie=0493c18d6e45ca9f60b3f105d9f6f9c4; __security_mc_1_s_sdk_sign_data_key_web_protect=a8ac2d39-4554-834d; __security_mc_1_s_sdk_cert_key=99d08a3f-49d0-b41d; __security_server_data_status=1; is_dash_user=1; publish_badge_show_info=%220%2C0%2C0%2C1755134550208%22; UIFID=25fbca946115c5de1a71a2f3ab2554e0f974c22280bf68dee7a40800d5de48828e8fa047c34d1c1fa6368e83e8a708869442cb23cdb83619abe8b49ed8a470117b6bbc2159220195dc6f0e5d6c68686440c5af8f6e54bb105e60a9e244c9da22defb260f1d6b54135dea12a178056801f9e9a5724526e2ca050807c437bb98cb234c524fff8cf248d8f87e4af6a4ee432f1f9c2f82836f593d76134390925c6e; SelfTabRedDotControl=%5B%5D; vdg_s=1; __ac_nonce=068a3cf9a00c87b6f3c41; __ac_signature=_02B4Z6wo00f01oX7kwwAAIDCyNqqpL3ii6KFypeAAMnpbb; douyin.com; xg_device_score=8.304698506183575; device_web_cpu_core=20; device_web_memory_size=-1; architecture=amd64; biz_trace_id=88637849; bd_ticket_guard_client_data=eyJiZC10aWNrZXQtZ3VhcmQtdmVyc2lvbiI6MiwiYmQtdGlja2V0LWd1YXJkLWl0ZXJhdGlvbi12ZXJzaW9uIjoxLCJiZC10aWNrZXQtZ3VhcmQtcmVlLXB1YmxpYy1rZXkiOiJCTE56TWY0Y2NJd2dpVkl1WXI3WW5wdk40N3NYNzczNWlGUmtiRGhQL2tnUkhxdkNjR0pTZ0VvVi9rQSsxdXdpR3FwMDJwbElQRWZ4QTBSaUU3M2QwTVk9IiwiYmQtdGlja2V0LWd1YXJkLXdlYi12ZXJzaW9uIjoyfQ%3D%3D; WallpaperGuide=%7B%22showTime%22%3A0%2C%22closeTime%22%3A0%2C%22showCount%22%3A0%2C%22cursor1%22%3A10%2C%22cursor2%22%3A2%7D"

    # 创建解析器实例
    parser = DouyinParser(cookie=user_cookie)

    # 待解析的链接 (请替换为你自己的测试链接)
    # 下面是不同类型的链接示例
    test_urls = {
        "multi_image": "2.51 复制打开抖音，看看【兮兮奈奈子的图文作品】  https://v.douyin.com/UHCPPHpOuC4/ MjP:/ X@Z.ZM 11/19",
        "single_image": "5.82 复制打开抖音，看看【囡囡的图文作品】糟糕 是心动的感觉...# 对镜拍 # 美女 # ... https://v.douyin.com/UNGMxxP0rgA/ m@Q.kP 03/18 MWz:/",
        "video": "1.07 你亖啦 你媽咪話要將你兩蚊雞賣俾我喔 # 我跟你讲  https://v.douyin.com/offWOmYUFdU/ 复制此链接，打开抖音搜索，直接观看视频！ DHI:/ D@u.Sy 04/13"
    }

    for link_type, url in test_urls.items():
        print(f"\n------ 正在解析: {link_type} ------")
        result = await parser.parse(url)
        print("\n------ 解析结果 ------")
        print(json.dumps(result, indent=4, ensure_ascii=False))
        print("--------------------")



if __name__ == "__main__":
    asyncio.run(main())
