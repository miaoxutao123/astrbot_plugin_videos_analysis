import requests
import re
import json
import os

def get_real_address(session, url): # Modified to accept session
    """
    获取重定向后的真实地址
    """
    if not url.startswith('http'):
        url = 'https://' + url
    try:
        # Headers are now part of the session, no need to specify them here explicitly
        # unless overriding session headers for this specific request.
        response = session.get(url, allow_redirects=False, timeout=10)
        if response.status_code == 301 or response.status_code == 302:
            return response.headers.get('Location')
        return url 
    except Exception as e:
        print(f"获取真实地址失败: {e}")
        return None

def get_douyin_video_no_watermark(share_url: str, save_path: str = "."):
    """
    从抖音分享链接下载无水印视频
    """
    print(f"开始处理分享链接: {share_url}")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
        'Connection': 'keep-alive'
    })

    try:
        print("尝试访问抖音主页以获取初始cookies...")
        home_response = session.get("https://www.douyin.com/", timeout=10)
        home_response.raise_for_status()
        print(f"访问抖音主页成功。当前Cookies: {session.cookies.get_dict()}")
    except requests.exceptions.RequestException as e:
        print(f"访问抖音主页失败 (这可能影响后续API请求): {e}")

    real_url = get_real_address(session, share_url)
    if not real_url:
        print("无法获取真实视频链接。")
        return None

    print(f"获取到的真实链接: {real_url}")

    video_id_match = re.search(r'/(?:video|note)/(\d+)', real_url)
    if not video_id_match:
        # 尝试从 /discover?modal_id=VIDEO_ID 这样的链接中提取
        video_id_match_discover = re.search(r'modal_id=(\d+)', real_url)
        if not video_id_match_discover:
            print(f"无法从链接中提取视频ID: {real_url}")
            return None
        video_id = video_id_match_discover.group(1)
    else:
        video_id = video_id_match.group(1)
    
    print(f"提取到的视频ID: {video_id}")

    api_url = f"https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/?item_ids={video_id}"
    
    api_headers = {
        'Referer': real_url
    }

    response_text_for_debugging = ""

    try:
        print(f"请求API: {api_url}")
        response = session.get(api_url, headers=api_headers, timeout=15)
        
        response_text_for_debugging = response.text
        # print(f"API响应状态码: {response.status_code}") # 调试时可以取消注释
        # print(f"API响应文本 (前500字符): {response_text_for_debugging[:500]}") # 调试时可以取消注释

        response.raise_for_status()
        
        data = response.json()

        if not data.get("item_list") or not data["item_list"]:
            print("API响应中没有找到 item_list 或 item_list 为空。")
            # print(f"完整API响应: {response_text_for_debugging}") # 调试时可以取消注释
            return None

        video_info = data["item_list"][0]
        
        # aweme_type: 0 或 4 是视频, 2 是单张图片, 68 是图文(图片集)
        aweme_type = video_info.get("aweme_type")
        print(f"作品类型 (aweme_type): {aweme_type}")

        no_watermark_url = None
        media_type = "video" # 默认是视频

        if aweme_type == 0 or aweme_type == 4: # 视频
            video_data = video_info.get("video")
            if video_data:
                # 优先尝试从 play_addr.url_list 替换 playwm
                play_addr = video_data.get("play_addr")
                if play_addr and play_addr.get("url_list"):
                    for url_item in play_addr["url_list"]:
                        if isinstance(url_item, str) and "playwm" in url_item:
                            no_watermark_url = url_item.replace("playwm", "play")
                            print(f"从 play_addr 找到并替换 'playwm' 得到链接: {no_watermark_url}")
                            break
                        elif isinstance(url_item, str) and "play" in url_item: # 如果不含 playwm，可能已经是无水印
                            no_watermark_url = url_item # 暂存，如果后面没找到更好的就用这个
                            print(f"从 play_addr 找到疑似无水印链接: {no_watermark_url}")
                
                # 如果 play_addr 未成功，尝试 bit_rate (通常包含不同清晰度的链接)
                if not no_watermark_url or "playwm" in no_watermark_url: # 如果还是有水印或没找到
                    bit_rate_list = video_data.get("bit_rate")
                    if isinstance(bit_rate_list, list) and bit_rate_list:
                        # 通常选择第一个（可能是最高清，或按特定顺序排列）
                        # Evil0ctal/Douyin_TikTok_Download_API 可能会有更复杂的选择逻辑
                        for br_info in sorted(bit_rate_list, key=lambda x: x.get("bitrate", 0), reverse=True): # 按码率排序尝试
                            if br_info.get("play_addr") and br_info["play_addr"].get("url_list"):
                                br_play_addr_list = br_info["play_addr"]["url_list"]
                                if br_play_addr_list and isinstance(br_play_addr_list[0], str):
                                    potential_url = br_play_addr_list[0].replace("playwm", "play")
                                    if "playwm" not in potential_url: # 确保替换成功
                                        no_watermark_url = potential_url
                                        print(f"从 bit_rate 找到无水印链接: {no_watermark_url}")
                                        break # 找到一个就用
                        if no_watermark_url and "playwm" in no_watermark_url : #如果bit_rate里还是有水印，清空
                            no_watermark_url = None


                # 最后的尝试：如果 play_addr.url_list 存在但之前的逻辑没选好，直接拿第一个替换
                if (not no_watermark_url or "playwm" in no_watermark_url) and play_addr and play_addr.get("url_list"):
                    first_play_addr_url = play_addr["url_list"][0]
                    if isinstance(first_play_addr_url, str):
                        potential_url = first_play_addr_url.replace("playwm", "play")
                        if "playwm" not in potential_url:
                             no_watermark_url = potential_url
                             print(f"最后尝试从 play_addr 第一个链接替换得到: {no_watermark_url}")
                        else: #如果替换了还是有水印，说明这个源不行
                            no_watermark_url = None


            if not no_watermark_url:
                print("未能从视频信息中提取到有效的无水印播放链接。")
                # print(json.dumps(video_info, indent=2, ensure_ascii=False)) # 调试时可以取消注释
                return None
        
        elif aweme_type == 2 or aweme_type == 68: # 图片或图文
            media_type = "image"
            images_data = video_info.get("images")
            if images_data and isinstance(images_data, list):
                # 通常选择 url_list 中的最后一个，可能是最高清的
                # Evil0ctal/Douyin_TikTok_Download_API 可能会有更复杂的选择逻辑
                image_urls = []
                for img in images_data:
                    if img.get("url_list") and isinstance(img["url_list"], list) and img["url_list"]:
                        image_urls.append(img["url_list"][-1]) # 取


            if not no_watermark_url:
                print("API响应中未找到有效的视频播放地址列表。")
                return None
        else:
            print("API响应中视频信息结构不符合预期 (缺少 video.play_addr)。")
            return None
        
        if not no_watermark_url: # Double check after all attempts
            print("未能提取到无水印视频链接。")
            return None

        print(f"获取到的无水印视频链接: {no_watermark_url}")

        print("开始下载视频...")
        video_response = session.get(no_watermark_url, stream=True, timeout=60) # Use session for download too
        video_response.raise_for_status()

        video_title = video_id 
        if video_info.get("desc"):
            video_title = re.sub(r'[\\/*?:"<>|]', "", video_info["desc"])[:50] 
        
        if not video_title: 
            video_title = video_id

        file_name = f"{video_title}.mp4"
        file_path = os.path.join(save_path, file_name)
        os.makedirs(save_path, exist_ok=True)

        with open(file_path, 'wb') as f:
            for chunk in video_response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"视频下载完成，保存在: {file_path}")
        return file_path

    except json.JSONDecodeError as e_json:
        print(f"解析API JSON响应时发生错误: {e_json}")
        print("这通常意味着API没有返回有效的JSON数据。可能是HTML错误页面、验证码或API已更改。")
        print(f"API响应状态码: {response.status_code if 'response' in locals() and hasattr(response, 'status_code') else '未知 (在JSON解析错误前获取)'}")
        print(f"原始API响应文本 (前1000字符):\n{response_text_for_debugging[:1000]}")
        return None
    except requests.exceptions.HTTPError as e_http:
        print(f"API请求返回HTTP错误 (例如 403 Forbidden, 404 Not Found): {e_http}")
        if e_http.response is not None:
            print(f"响应状态码: {e_http.response.status_code}")
            print(f"响应内容 (前500字符): {e_http.response.text[:500]}")
        return None
    except requests.exceptions.RequestException as e_req:
        print(f"请求API或下载视频时发生网络错误 (例如超时、无法连接): {e_req}")
        if hasattr(e_req, 'response') and e_req.response is not None:
            print(f"响应状态码: {e_req.response.status_code}")
            print(f"响应内容 (前500字符): {e_req.response.text[:500]}")
        return None
    except (KeyError, IndexError, TypeError) as e_parse:
        print(f"解析已成功获取的API JSON数据结构时发生错误: {e_parse}")
        if 'data' in locals() and data: # Check if data exists and is not None
             print(f"当前的API响应数据 (部分，可能导致错误): {str(data)[:500]}")
        else:
            print("无法展示API数据，因为它可能未被成功解析或获取，或者内容为空。")
        return None
    except Exception as e_unknown:
        print(f"发生未知错误: {e_unknown}")
        if 'response_text_for_debugging' in locals() and response_text_for_debugging:
             print(f"发生未知错误时的API响应文本 (前1000字符):\n{response_text_for_debugging[:1000]}")
        return None

if __name__ == '__main__':
    # 测试链接，请替换为你自己的抖音分享链接
    # 格式通常是 "v.douyin.com/xxxxxxx/" 或包含 "douyin.com" 的长链接
    full_share_text = "8.48 J@I.vs 03/04 GVL:/ 三角洲行动鼠鼠直播日常 # 三角洲行动 # 猛攻三角洲4月新赛季  https://v.douyin.com/nIa7fsdDdrw/ 复制此链接，打开Dou音搜索，直接观看视频！" # 示例链接
    
    # 从分享文本中提取URL
    url_match = re.search(r'(https?://[^\s]+)', full_share_text)
    
    if not url_match:
        print(f"无法从分享文本中提取有效的URL: {full_share_text}")
    else:
        test_share_link = url_match.group(1)
        print(f"从文本中提取到的链接: {test_share_link}")

        # 检查是否是提醒用户替换的默认链接(虽然现在不太可能匹配，但保留逻辑)
        if test_share_link == "v.douyin.com/iYtXKXQP/": # Placeholder, unlikely to be hit with current full_share_text
            print("请在脚本中替换 `full_share_text` 为一个有效的抖音分享链接进行测试。")
        else:
            print(f"正在测试链接: {test_share_link}")
            downloaded_file = get_douyin_video_no_watermark(test_share_link, save_path="downloaded_videos")
            if downloaded_file:
                print(f"测试成功，视频已下载到: {downloaded_file}")
            else:
                print("测试失败。")

    # 另一个测试示例 (如果需要可以取消注释并修改)
    # test_share_link_2 = "【小猫咪的心思你别猜#内容过于真实 #小猫咪有什么坏心眼 #萌宠出道计划】 https://v.douyin.com/iYtXKXQP/ 复制此链接，打开Douyin搜索，直接观看视频!"
    # extracted_url = re.search(r'(https?://[^\s]+)', test_share_link_2)
    # if extracted_url:
    #     url_to_test = extracted_url.group(1)
    #     print(f"\n正在测试从文本中提取的链接: {url_to_test}")
    #     downloaded_file_2 = get_douyin_video_no_watermark(url_to_test, save_path="downloaded_videos")
    #     if downloaded_file_2:
    #         print(f"测试成功，视频已下载到: {downloaded_file_2}")
    #     else:
    #         print("测试失败。")
    # else:
    #     print("\n无法从示例分享文本中提取链接。")