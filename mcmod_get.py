import asyncio
import logging
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def fix_url(url: str) -> str:
    """修复URL中的双斜杠问题"""
    if not url:
        return url

    # 如果以//开头，添加https:
    if url.startswith("//"):
        return "https:" + url
    # 如果已经是完整URL，直接返回
    elif url.startswith("http"):
        return url
    # 否则添加https://
    else:
        return "https://" + url


@dataclass
class ModInfo:
    """模组/整合包信息数据类"""

    name: str
    categories: list[str]
    icon_url: str | None
    description_images: list[str]
    description: str | None


class MCModSpider:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        self.base_url = "https://www.mcmod.cn"

    async def get_page(
        self, session: aiohttp.ClientSession, url: str
    ) -> BeautifulSoup | None:
        """异步获取页面内容并解析"""
        try:
            async with session.get(url, headers=self.headers, ssl=False) as response:
                response.raise_for_status()
                html = await response.text()
                return BeautifulSoup(html, "lxml")
        except Exception as e:
            logger.error(f"获取页面失败: {url}, 错误: {str(e)}")
            return None

    async def get_mod_info(self, url: str) -> ModInfo | None:
        """异步获取模组/整合包信息"""
        async with aiohttp.ClientSession() as session:
            soup = await self.get_page(session, url)
            if not soup:
                return None

            try:
                # 获取名称
                title_element = soup.find("div", class_="class-title")
                name = title_element.get_text(strip=True) if title_element else ""

                # 获取类别
                categories = []
                category_element = soup.find("div", class_="class-category")
                if category_element:
                    categories = [
                        cat.get_text(strip=True)
                        for cat in category_element.find_all("a")
                    ]

                # 获取图标URL
                icon_url = None
                icon_element = soup.find("div", class_="class-cover-image")
                if icon_element:
                    img = icon_element.find("img")
                    if img and img.get("src"):
                        src = str(img.get("src"))
                        # 修复URL中的双斜杠问题
                        icon_url = fix_url(src)

                # 获取描述和图片
                description = None
                description_images = []
                element = soup.find(
                    "li",
                    attrs={
                        "data-id": "1",
                        "class": "text-area common-text font14",
                        "style": "display:block",
                    },
                )

                if element:
                    # 获取所有图片URL
                    images = element.find_all("img")
                    for img in images:
                        img_url = img.get("data-src")
                        if img_url:
                            # 修复URL拼接问题，避免双斜杠
                            description_images.append(fix_url(img_url))

                    # 获取描述文本
                    description = element.get_text(strip=True, separator="\n")

                return ModInfo(
                    name=name,
                    categories=categories,
                    icon_url=icon_url,
                    description_images=description_images,
                    description=description,
                )

            except Exception as e:
                logger.error(f"解析信息失败: {url}, 错误: {str(e)}")
                return None


async def mcmod_parse(url: str):
    spider = MCModSpider()

    # 并发获取信息
    tasks = [
        spider.get_mod_info(url),
    ]

    results = await asyncio.gather(*tasks)

    return results


if __name__ == "__main__":
    # 示例URL
    modpack_url = "https://www.mcmod.cn/modpack/1004.html"
    mod_url = "https://www.mcmod.cn/class/260.html"
    url = modpack_url
    asyncio.run(mcmod_parse(url))
