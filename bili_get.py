import asyncio
import aiohttp
import re
import os
import aiofiles

# 配置参数
CONFIG = {
    "VIDEO": {
        "enable": True,
        "send_link": False,
        "send_video": True
    }
}

# 正则表达式
REG_B23 = re.compile(r'(b23\.tv|bili2233\.cn)\/[\w]+')
REG_BV = re.compile(r'BV1\w{9}')
REG_AV = re.compile(r'av\d+', re.I)

# AV转BV算法参数·
AV2BV_TABLE = 'fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF'
AV2BV_TR = {c: i for i, c in enumerate(AV2BV_TABLE)}
AV2BV_S = [11, 10, 3, 8, 4, 6]
AV2BV_XOR = 177451812
AV2BV_ADD = 8728348608

def format_number(num):
    """格式化数字显示"""
    num = int(num)
    if num < 1e4:
        return str(num)
    elif num < 1e8:
        return f"{num/1e4:.1f}万"
    else:
        return f"{num/1e8:.1f}亿"

def av2bv(av):
    """AV号转BV号"""
    av_num = re.search(r'\d+', av)
    if not av_num:
        return None

    try:
        x = (int(av_num.group()) ^ AV2BV_XOR) + AV2BV_ADD
    except:
        return None

    r = list('BV1 0 4 1 7  ')
    for i in range(6):
        idx = (x // (58**i)) % 58
        r[AV2BV_S[i]] = AV2BV_TABLE[idx]

    return ''.join(r).replace(' ', '0')

async def bili_request(url, return_json=True):
    """发送B站API请求"""
    headers = {
        "referer": "https://www.bilibili.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                if return_json:
                    return await response.json()
                else:
                    return await response.read()
    except aiohttp.ClientError as e:
        return {"code": -400, "message": str(e)}

async def parse_b23(short_url):
    """解析b23短链接"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(f"https://{short_url}", allow_redirects=True) as response:
                real_url = str(response.url)

                if REG_BV.search(real_url):
                    return await parse_video(REG_BV.search(real_url).group())
                elif REG_AV.search(real_url):
                    return await parse_video(av2bv(REG_AV.search(real_url).group()))
                return None
    except aiohttp.ClientError:
        return None

async def parse_video(bvid):
    """解析视频信息"""
    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        return None

    info = data["data"]
    return {
        "aid": info["aid"],
        "cid": info["cid"],
        "bvid": bvid,
        "title": info["title"],
        "cover": info["pic"],
        "duration": info["duration"],
        "stats": {
            "view": format_number(info["stat"]["view"]),
            "like": format_number(info["stat"]["like"]),
            "danmaku": format_number(info["stat"]["danmaku"]),
            "coin": format_number(info["stat"]["coin"]),
            "favorite": format_number(info["stat"]["favorite"])
        }
    }

async def download_video(aid, cid, bvid, quality=16):
    """下载视频"""

    api_url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={quality}&type=mp4&platform=html5"
    data = await bili_request(api_url)

    if data.get("code") != 0:
        return None

    video_url = data["data"]["durl"][0]["url"]
    video_data = await bili_request(video_url, return_json=False)

    if isinstance(video_data, dict):
        return None

    filename = f"data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/{bvid}.mp4"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    async with aiofiles.open(filename, "wb") as f:
        await f.write(video_data)

    return filename

async def process_bili_video(url):
    """主处理函数"""
    # 判断链接类型
    if REG_B23.search(url):
        video_info = await parse_b23(REG_B23.search(url).group())
    elif REG_BV.search(url):
        video_info = await parse_video(REG_BV.search(url).group())
    elif REG_AV.search(url):
        bvid = av2bv(REG_AV.search(url).group())
        video_info = await parse_video(bvid) if bvid else None
    else:
        print("不支持的链接格式")
        return

    if not video_info:
        print("解析视频信息失败")
        return

    stats = video_info["stats"]
    # 下载视频
    if CONFIG["VIDEO"]["send_video"]:
        print("\n开始下载视频...")
        filename = await download_video(
            video_info["aid"],
            video_info["cid"],
            video_info["bvid"]
        )

        if filename:
            print(f"视频已保存为：{filename}")
        else:
            print("下载视频失败")
    return{
        "title": video_info["title"],
        "cover": video_info["cover"],
        "duration": video_info["duration"],
        "stats": video_info["stats"],
        "video_path": filename,
        "view_count" : stats["view"],
        "like_count" : stats["like"],
        "danmaku_count" : stats["danmaku"],
        "coin_count" : stats["coin"],
        "favorite_count" : stats["favorite"]

    }

# async def main():
#     url = input("请输入B站视频链接：")
#     result = await process_bili_video(url)
#     print(result)

# if __name__ == "__main__":
#     asyncio.run(main())