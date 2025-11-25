import asyncio

import aiohttp

api = "https://api.kxzjoker.cn/api/jiexi_video?url="
api2 = "https://api.kxzjoker.cn/api/jiexi_video_2?url="
async def fetch(session, url):
    async with session.get(url) as response:
        print(f"Status: {response.status}")  # 输出 HTTP 状态码
        raw_content = await response.text()  # 获取响应的原始内容
        print(f"Raw Response: {raw_content}")  # 输出原始内容

        # 检查 Content-Type 是否为 JSON
        if "application/json" in response.headers.get("Content-Type", ""):
            return await response.json()
        else:
            raise ValueError(f"Unexpected Content-Type: {response.headers.get('Content-Type')}, cannot parse as JSON.")

async def fetch_head(session, url):
    async with session.head(url) as response:
        return response.headers.get("Content-Length", 0)

async def xhs_parse(url):
    """
    小红书分享链接解析，使用看戏仔api
    """
    async with aiohttp.ClientSession() as session:
        try:
            # 尝试使用 api
            result = await fetch(session, api + url)
            if result.get("code") == 201:  # 如果接口返回维护状态
                print("API 维护中，切换到 API2")
                result = await fetch(session, api2 + url)  # 切换到 api2
        except Exception as e:
            print(f"Error using API: {e}")
            print("尝试切换到 API2")
            result = await fetch(session, api2 + url)  # 切换到 api2

        if result.get("success") == 1:
            data = result["data"]
            if "images" in data:
                images = data["images"]
                if isinstance(images, str):  # 如果是单个字符串，转换为列表
                    images = [images]
                return {
                    "title": data["title"],
                    "result_type": "image",
                    "count": len(images),
                    "urls": images
                }
            elif "download_url" in data:
                video_urls = data["download_url"]
                if isinstance(video_urls, str):  # 如果是单个字符串，转换为列表
                    video_urls = [video_urls]
                video_sizes = []
                for current_video_url in video_urls:
                    video_size = await fetch_head(session, current_video_url)
                    video_sizes.append(int(video_size))
                return {
                    "title": data["video_title"],
                    "result_type": "video",
                    "count": len(video_urls),
                    "video_sizes": video_sizes,
                    "urls": video_urls,
                    "cover": data.get("image_url", ""),
                    "size": sum(video_sizes)
                }
        return {"error": "解析失败或数据格式不正确"}

if __name__ == "__main__":
    async def main():
        # print(await xhs_parse("https://www.xiaohongshu.com/discovery/item/6808886f000000001c01f52f"))
        print(await xhs_parse("http://xhslink.com/a/20O4Hwe5YYXab"))

    asyncio.run(main())
