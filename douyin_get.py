import requests
import os
from .douyin_download import *
def get_douyin_data(url, minimal=False):
    api_url = "http://123.56.185.74:1478/api/hybrid/video_data"
    params = {
        "url": url,
        "minimal": minimal
    }
    
    try:
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching video data: {e}")
        return None

# def download(url, output_path):
#     headers = {
#         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
#         "Referer": "https://www.douyin.com",  # 伪装来源
#         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
#         "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6"
#     }

#     os.makedirs(os.path.dirname(output_path), exist_ok=True)

#     response = requests.get(url, headers=headers, stream=True)
#     if response.status_code == 200:
#         video_url = response.headers.get('location')
#         response = requests.get(video_url, headers=headers, stream=True)
#         with open(output_path, 'wb') as f:
#             for chunk in response.iter_content(chunk_size=8192):
#                 if chunk:
#                     f.write(chunk)
#         print(f"视频\图片已保存到：{output_path}")
#     else:
#         print(f"下载失败，状态码：{response.status_code}，原因：{response.reason}")

def parse_douyin_data(data):
    result = {
        "type": None,  # 图片或视频
        "is_multi_part": False,  # 是否为分段内容
        "count": 0,  # 图片或视频数量"
        "download_links": [] , # 无水印下载链接
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
    
    elif media_type == 42:  # 分p视频
        result["type"] = "video"
        result["is_multi_part"] = True
        videos = data["data"]["images"]
        video_count = len(videos)
        result["count"] = video_count
        for video in videos:
            download_url = video["video"]["play_addr_h264"]["url_list"][2]
            result["download_links"].append(download_url)

    elif media_type == 4:   # 视频
        result["type"] = "video"
        video = data["data"]["video"]
        download_url = video["play_addr"]["url_list"][2]
        result["download_links"].append(download_url)        

    return result

def process_douyin(url):
    result = {
        "type": None,  # 图片或视频
        "is_multi_part": False,  # 是否为分段内容
        "count": 0,  # 图片或视频数量"
        "save_path": [] , # 无水印保存路径
        "title": None,  # 标题
    }

    video_data = get_douyin_data(url, minimal=False)
    # /data/plugin/astrbot_plugin_videos_analysis
    opt_path = "/data/plugin/astrbot_plugin_videos_analysis/download_videos/dy"
    if video_data:
        data = parse_douyin_data(video_data)
        if data["type"] == "video":
            result["type"] = "video"
            if data["is_multi_part"]:  # 分段视频
                print(data["download_links"])
                result["is_multi_part"] = True
                output_path = f"{opt_path}/{data['title']}"
                for i, download_link in enumerate(data["download_links"], 1):
                    print(f"Downloading video part {i}..., url: {download_link}\n")
                    download(download_link, filename = f"{output_path} - Part {i}.mp4")
                result['save_path'].append(f"{output_path} - Part {i}.mp4")
            else:  # 单段视频
                print(data["download_links"])
                output_path = f"{opt_path}/{data['title']}.mp4"
                download(data["download_links"], filename = output_path)
                result['save_path'].append(output_path)
        if data["type"] == "image":
            result["type"] = "image"
            if data["is_multi_part"]:
                print(data["download_links"])
                result["is_multi_part"] = True
                output_path = f"{opt_path}/{data['title']}"
                for i, download_link in enumerate(data["download_links"], 1):
                    download(download_link, filename = f"{output_path} - Part {i}.jpg")
                result['save_path'].append(f"{output_path} - Part {i}.jpg")
            else:
                print(data["download_links"])
                output_path = f"{opt_path}/{data['title']}.jpg"
                download(data["download_links"][0], filename = output_path)
                result['save_path'].append(output_path)
        return result
    return None


# Example usage
if __name__ == "__main__":
    video_url = " https://v.douyin.com/i5gLT2gs/"
    result = process_douyin(video_url)