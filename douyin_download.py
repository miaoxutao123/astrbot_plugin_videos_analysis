import requests
import re
import os
def clean_cookie(cookie):
    # 使用正则表达式移除无法编码的字符
    return re.sub(r'[^\x00-\x7F]+', '', cookie)

def get_location_from_url(url):
    """
    处理单个 URL，获取响应头中的 location，并模拟指定的请求头。

    Args:
        url: 单个 URL。

    Returns:
        包含 URL 和 location 的字典。
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
        "Connection": "keep-alive",
        "Cookie": clean_cookie("ttwid=REDACTED; UIFID_TEMP=REDACTED; fpk1=REDACTED; fpk2=REDACTED; UIFID=REDACTED; download_guide=REDACTED; __ac_signature=REDACTED; bd_ticket_guard_client_data=REDACTED"),
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

    try:
        response = requests.get(url, headers=headers, allow_redirects=False)
        if response.status_code == 302 or response.status_code == 301:
            location = response.headers.get('location')
            return {'url': url, 'location': location}
        else:
            return {'url': url, 'location': None, 'status_code': response.status_code}
    except requests.exceptions.RequestException as e:
        return {'url': url, 'error': str(e)}

def download_video(url, filename="video.mp4"):
    """
    Downloads a video from the given URL.

    Args:
        url (str): The URL of the video.
        filename (str): The filename to save the video as.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0'
    }

    try:
        response = requests.get(url, headers=headers, stream=True)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)

        # Check if the server responded with 304 Not Modified
        if response.status_code == 304:
            print("Video not modified. No download needed.")
            return

        # Ensure the directory exists
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        total_size = int(response.headers.get('content-length', 0))
        block_size = 1024  # 1 Kilobyte

        with open(filename, 'wb') as file:
            downloaded = 0
            for data in response.iter_content(block_size):
                file.write(data)
                downloaded += len(data)
                if total_size:  # only show progress if total_size is known.
                    print(f"\rDownloaded: {downloaded / total_size * 100:.2f}%", end="")
        if total_size:
            print("\nDownload complete!")
        else:
            print("Download complete. Content-Length header missing, progress not shown.")

    except requests.exceptions.RequestException as e:
        print(f"Error downloading video: {e}")
    except IOError as e:
        print(f"Error writing file: {e}")

    except requests.exceptions.RequestException as e:
        print(f"Error downloading video: {e}")
    except IOError as e:
        print(f"Error writing file: {e}")

def download(url, filename="video.mp4"):
    """
    Downloads videos from the given list of URLs.

    Args:
        urls (list): A list of URLs of the videos.
        filename (str): The base filename to save the videos as.
    """
    download_url = get_location_from_url(url)['location']
    download_video(download_url, filename) 

# # Example usage:
# # 示例 URL 列表
# urls = [
#     "https://www.douyin.com/aweme/v1/play/?video_id=v0d00fg10000c9hmfhjc77ue0hf19et0&line=0&file_id=b3d3fe9b8b484225932abe8960294724&sign=affd1a80b827dc66324dcc7a0fa24924&is_play_url=1&source=PackSourceEnum_AWEME_DETAIL",
#     "https://www.douyin.com/aweme/v1/play/?video_id=v0300fg10000cv64i0vog65u35u8r91g&line=0&file_id=2f45b2fcad8f4aaa83e54a470864c35e&sign=85220d7da12fc91b8779b88189f4a62b&is_play_url=1&source=PackSourceEnum_AWEME_DETAIL"
# ]
# # 获取 location
# locations = get_location_from_urls(urls)
# video_url = locations[1]['location']
# # video_url = "https://v26-daily-e.douyinvod.com/6555ae20c855f1758d7cd3dc45958242/67d3b1c6/video/tos/cn/tos-cn-ve-15c001-alinc2/dea619737fbc437196d9bb7fc414330c/?a=1128&ch=0&ch=0&cr=0&cr=0&dr=0&dr=0&er=0&er=0&cd=0|0|0|0&cd=0|0|0|0&cv=1&cv=1&br=1174&br=1174&bt=1174&bt=1174&cs=0&cs=0&ds=3&ds=3&ft=XV-6aF3UUmf.cdP_02D1YmAo6kItG..vuP9eF1IfdvV12nzXT&ft=XV-6aF3UUmf.cdP_02D1YmAo6kItG..vuP9eF1IfdvV12nzXT&mime_type=video_mp4&mime_type=video_mp4&qs=0&qs=0&rc=Nzo8MzxoZDY4NTtlZWg2NkBpM2g0a2g6ZmtwPDMzNGkzM0AvMS8zNV8vNi8xMTU2Y2FfYSNxNmMtcjRnY2VgLS1kLWFzcw==&rc=Nzo8MzxoZDY4NTtlZWg2NkBpM2g0a2g6ZmtwPDMzNGkzM0AvMS8zNV8vNi8xMTU2Y2FfYSNxNmMtcjRnY2VgLS1kLWFzcw==&btag=80000e000a8000&btag=80000e000a8000&cquery=100y&cquery=100y&dy_q=1741923183&dy_q=1741923183&l=20250314113303B3F7E9F0AD4C2C46FAF7https://v26-daily-e.douyinvod.com/6555ae20c855f1758d7cd3dc45958242/67d3b1c6/video/tos/cn/tos-cn-ve-15c001-alinc2/dea619737fbc437196d9bb7fc414330c/?a=1128&l=20250314113303B3F7E9F0AD4C2C46FAF7"
# download_video(video_url)