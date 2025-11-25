import aiohttp

from .douyin_download import (
    download,  # 假设 download 函数也支持异步，或者你需要将其改为异步
)


async def get_douyin_data(url,api_url,minimal=False):
    params = {
        "url": url,
        "minimal": str(minimal)  # 将布尔值转换为字符串
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params) as response:
                response.raise_for_status()
                return await response.json()
    except aiohttp.ClientError as e:
        print(f"Error fetching video data: {e}")
        return None
def parse_douyin_data(data):
    result = {
        "type": None,  # 图片或视频
        "is_multi_part": False,  # 是否为分段内容
        "count": 0,  # 图片或视频数量"
        "download_links": [],  # 无水印下载链接
        "title": None,  # 标题
    }
    title = data["data"]["aweme_id"]
    result["title"] = title
    # 判断内容类型
    media_type = data["data"]["media_type"]

    if media_type == 2:  # 图片
        result["type"] = "image"
        images = data["data"]["images"]
        image_count = len(images)
        result["count"] = image_count
        if image_count > 1:
            result["is_multi_part"] = True
            for image in images:
                download_url = image["url_list"][0]
                result["download_links"].append(download_url)
        else:
            download_url = images[0]["url_list"][0]
            result["download_links"].append(download_url)

    elif media_type == 42:  # 分p视频
        result["type"] = "video"
        result["is_multi_part"] = True
        videos = data["data"]["images"]
        video_count = len(videos)
        result["count"] = video_count
        for video in videos:
            if video["video"]["play_addr_h264"]["url_list"][2]:
                download_url = video["video"]["play_addr_h264"]["url_list"][2]
            else:
                download_url = video["url_list"][2]
            result["download_links"].append(download_url)

    elif media_type == 4:  # 视频
        result["type"] = "video"
        video = data["data"]["video"]
        download_url = video["play_addr"]["url_list"][2]
        result["download_links"].append(download_url)

    return result

async def process_douyin(url,api_url):
    result = {
        "type": None,  # 图片或视频
        "is_multi_part": False,  # 是否为分段内容
        "count": 0,  # 图片或视频数量"
        "save_path": [],  # 无水印保存路径
        "title": None,  # 标题
    }

    video_data = await get_douyin_data(url,api_url,minimal=False)
    opt_path = "data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"
    if video_data:
        data = parse_douyin_data(video_data)
        if data["type"] == "video":
            result["type"] = "video"
            result["count"] = data["count"]
            if data["is_multi_part"]:  # 分段视频
                # print(data["download_links"])
                result["is_multi_part"] = True
                output_path = f"{opt_path}/{data['title']}"
                for i, download_link in enumerate(data["download_links"], 1):
                    print(f"Downloading video part {i}..., url: {download_link}\n")
                    await download(download_link, filename=f"{output_path}-Part{i}.mp4") # 假设download函数也支持异步
                    result["save_path"].append(f"{output_path}-Part{i}.mp4")
            else:  # 单段视频
                # print(data["download_links"])
                output_path = f"{opt_path}/{data['title']}.mp4"
                await download(data["download_links"][0], filename=output_path) # 假设download函数也支持异步
                result["save_path"].append(output_path)
        if data["type"] == "image":
            result["type"] = "image"
            result["count"] = data["count"]
            if data["is_multi_part"]:
                # print(data["download_links"])
                result["is_multi_part"] = True
                output_path = f"{opt_path}/{data['title']}"
                for i, download_link in enumerate(data["download_links"], 1):
                    await download(download_link, filename=f"{output_path}-Part{i}.jpg") # 假设download函数也支持异步
                    result["save_path"].append(f"{output_path}-Part{i}.jpg")
            else:
                # print(data["download_links"])
                output_path = f"{opt_path}/{data['title']}.jpg"
                await download(data["download_links"][0], filename=output_path) # 假设download函数也支持异步
                result["save_path"].append(output_path)
        return result
    return None

# Example usage
# async def main():
#     video_url = " https://v.douyin.com/i5gLT2gs/"
#     result = await process_douyin(video_url)
#     print(result)

# if __name__ == "__main__":
#     asyncio.run(main())
