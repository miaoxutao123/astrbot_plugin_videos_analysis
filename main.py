from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import re
import json  # 添加json模块导入
from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_get import process_douyin
from .auto_delate import delete_old_files

@register("hybird_videos_analysis", "喵喵", "可以解析抖音和bili视频", "0.2.1","https://github.com/miaoxutao123/astrbot_plugin_videos_analysis")
class hybird_videos_analysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")
        self.douyin_api_url = config.get("douyin_api_url")
        self.delate_time = config.get("delate_time")
        self.max_video_size = config.get("max_video_size")
        self.bili_quality = config.get("bili_quality")
        self.bili_reply_mode = config.get("bili_reply_mode")
        self.bili_url_mode = config.get("bili_url_mode")
        self.Merge_and_forward = config.get("Merge_and_forward")
        self.bili_use_login = config.get("bili_use_login")
@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dy(self, event: AstrMessageEvent, context: Context, *args, **kwargs):
    """
    自动检测消息中是否包含抖音分享链接，并解析。
    """
    api_url = self.douyin_api_url
    # print(f"解析链接：{api_url}")
    message_str = event.message_str
    match = re.search(r'(https?://v\.douyin\.com/[a-zA-Z0-9_]+)', message_str)
    
    if self.delate_time != 0:
        delete_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/dy", self.delate_time)
    if match:
        url = match.group(1)
        # print(f"检测到抖音链接: {url}")  # 添加日志记录
        result = await process_douyin(url,api_url)  # 使用 await 调用异步函数
        if result:
            # print(f"解析结果: {result}")  # 添加日志记录
            if result['type'] == "video":
                if result['is_multi_part']:
                    if self.nap_server_address != "localhost":
                        ns = Nodes([])
                        for i in range(result['count']):
                            file_path = result['save_path'][i]
                            if file_path.endswith('.jpg'):
                                nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image.fromFileSystem(nap_file_path)]
                                )
                            else:
                                nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Video.fromFileSystem(nap_file_path)]
                                )
                            # file_path = result['save_path'][i]
                            # nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                            # node = Node(
                            #     uin=event.get_self_id(),
                            #     name="喵喵",
                            #     content=[Video.fromFileSystem(nap_file_path)]
                            # )
                            ns.nodes.append(node)
                        # print(f"发送多段视频: {ns}")  # 添加日志记录
                    else:
                        ns = Nodes([])
                        for i in range(result['count']):
                            if file_path.endswith('.jpg'):
                                nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image.fromFileSystem(nap_file_path)]
                                )
                            else:
                                nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Video.fromFileSystem(nap_file_path)]
                                )
                            ns.nodes.append(node)
                        # print(f"发送多段视频: {ns}")  # 添加日志记录
                    yield event.chain_result([ns])
                else:
                    file_path = result['save_path'][0]
                    if self.nap_server_address != "localhost":
                        nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                    else:
                        nap_file_path = file_path
                    # print(f"发送单段视频: {nap_file_path}")  # 添加日志记录
                    yield event.chain_result([
                        Video.fromFileSystem(nap_file_path)
                    ])
            elif result['type'] == "image":
                if result['is_multi_part']:
                    if self.nap_server_address != "localhost":
                        ns = Nodes([])
                        for i in range(result['count']):
                            file_path = result['save_path'][i]
                            nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                            node = Node(
                                uin=event.get_self_id(),
                                name="喵喵",
                                content=[Image.fromFileSystem(nap_file_path)]
                            )
                            ns.nodes.append(node)
                    else:
                        ns = Nodes([])
                        for i in range(result['count']):
                            file_path = result['save_path'][i]
                            node = Node(
                                uin=event.get_self_id(),
                                name="喵喵",
                                content=[Image.fromFileSystem(file_path)]
                            )
                            ns.nodes.append(node)
                    # print(f"发送多段图片: {ns}")  # 添加日志记录
                    yield event.chain_result([ns])
                else:
                    file_path = result['save_path'][0]
                    if self.nap_server_address != "localhost":
                        nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                    else:
                        nap_file_path = file_path
                    print(f"发送单段图片: {nap_file_path}")  # 添加日志记录
                    yield event.chain_result([
                        Image.fromFileSystem(nap_file_path)
                    ])
            else:
                print("解析失败，请检查链接是否正确。")
        else:
            print("解析失败，请检查链接是否正确。")  # 添加日志记录
            yield event.plain_result("检测到抖音链接，但解析失败，请检查链接是否正确。")

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_bili(self, event: AstrMessageEvent, context: Context, *args, **kwargs):
    """
    自动检测消息中是否包含bili分享链接，并解析。
    """
    qulity = self.bili_quality
    reply_mode = self.bili_reply_mode
    url_mode = self.bili_url_mode
    use_login = self.bili_use_login
    if reply_mode == 0 or reply_mode == 1 :
        videos_download = False
    else:
        videos_download = True
    zhuanfa = self.Merge_and_forward

    message_str = event.message_str
    message_obj = event.message_obj 
    message_obj = str(message_obj)
    
    contains_reply = re.search(r'reply', message_obj)
    match_json = re.search(r'https:\\\\/\\\\/b23\.tv\\\\/[a-zA-Z0-9]+', message_obj)
    match = re.search(r'(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)', message_str)

    if self.delate_time != 0:
        delete_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/", self.delate_time)  # 删除过期文件

    if match or match_json:
        if match:
            url = match.group(1)
        elif match_json:
            url = match_json.group(0).replace('\\\\', '\\')
            url = url.replace('\\\\', '\\').replace('\\/', '/')
        if not contains_reply:
            # 检查是否需要登录B站账号
            need_login = False
            
            # 传递event对象给process_bili_video函数，但不在bili_get.py中发送消息
            result = await process_bili_video(url, download_flag=videos_download, quality=qulity, use_login=use_login, event=None)
            
            # 如果需要登录，在这里发送提醒消息
            if need_login:
                yield event.plain_result("检测到需要登录B站账号，请前往控制台扫描二维码完成登录")
            
            if result:
                file_path = result['video_path']
                if self.nap_server_address != "localhost":
                    nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                    print(nap_file_path)
                else:
                    nap_file_path = file_path
                with_url = (
                    f"📜 视频标题：{result['title']}\n"
                    f"👀 观看次数：{result['view_count']}\n"
                    f"👍 点赞次数：{result['like_count']}\n"
                    f"💰 投币次数：{result['coin_count']}\n"
                    f"📂 收藏次数：{result['favorite_count']}\n"
                    f"💬 弹幕量：{result['danmaku_count']}\n"
                    f"⏳ 视频时长：{int(result['duration'] / 60)}分{result['duration'] % 60}秒\n"
                    f"🎥 视频直链：{result['direct_url']}\n"
                    f"🧷 原始链接：https://www.bilibili.com/video/{result['bvid']}"
                )
                without_url = (
                    f"📜 视频标题：{result['title']}\n"
                    f"👀 观看次数：{result['view_count']}\n"
                    f"👍 点赞次数：{result['like_count']}\n"
                    f"💰 投币次数：{result['coin_count']}\n"
                    f"📂 收藏次数：{result['favorite_count']}\n"
                    f"💬 弹幕量：{result['danmaku_count']}\n"
                    f"⏳ 视频时长：{int(result['duration'] / 60)}分{result['duration'] % 60}秒\n"
                    f"🧷 原始链接：https://www.bilibili.com/video/{result['bvid']}"
                )
                match reply_mode :
                    case 0: #纯文本回复
                        if url_mode:
                            if zhuanfa :
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Plain(with_url)]
                                )
                                yield event.chain_result([node])
                            else:
                                yield event.chain_result([
                                Plain(with_url),
                                ])
                        else:
                            if zhuanfa :
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Plain(without_url)]
                                )
                                yield event.chain_result([node])
                            else:
                                yield event.chain_result([
                                Plain(without_url),
                                ])
                    case 1: #带图片回复
                        if url_mode:
                            if zhuanfa :
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image(file=result['cover']),Plain(with_url)]
                                )
                                yield event.chain_result([node])
                            else:
                                yield event.chain_result([
                                Image(file=result['cover']),
                                Plain(with_url),
                                ])
                        else:
                            if zhuanfa :
                                node = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image(file=result['cover']),Plain(without_url)]
                                )
                                yield event.chain_result([node])
                            else:
                                yield event.chain_result([
                                Image(file=result['cover']),
                                Plain(without_url),
                                ])
                    case 2: #不带图片带视频回复
                        if url_mode:
                            if zhuanfa :
                                ns = Nodes([])
                                
                                node1 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Video.fromFileSystem(nap_file_path)]
                                )
                                node2 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Plain(with_url)]
                                )
                                ns.nodes.append(node1)
                                ns.nodes.append(node2)
                                yield event.chain_result([ns])
                            else:
                                yield event.chain_result([
                                Video.fromFileSystem(nap_file_path),
                                Plain(with_url),
                                ])
                        else:
                            if zhuanfa :
                                ns = Nodes([])
                                
                                node1 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Video.fromFileSystem(nap_file_path)]
                                )
                                node2 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Plain(without_url)]
                                )
                                ns.nodes.append(node1)
                                ns.nodes.append(node2)
                                yield event.chain_result([ns])
                            else:
                                yield event.chain_result([
                                Video.fromFileSystem(nap_file_path),
                                Plain(without_url),
                                ])

                    case 3: #完整回复
                        if url_mode:
                            if zhuanfa :
                                ns = Nodes([])
                                node1 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Video.fromFileSystem(nap_file_path)]
                                )
                                node2 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image(file=result['cover']),Plain(with_url)]
                                )
                                ns.nodes.append(node1)
                                ns.nodes.append(node2)
                                yield event.chain_result([ns])
                            else:
                                yield event.chain_result([
                                Video.fromFileSystem(nap_file_path),
                                Image(file=result['cover']),
                                Plain(with_url),
                                ])
                        else:
                            if zhuanfa :
                                ns = Nodes([])
                                ns.nodes.append(node)
                                node1 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Image(file=result['cover']),Video.fromFileSystem(nap_file_path)]
                                )
                                node2 = Node(
                                    uin=event.get_self_id(),
                                    name="喵喵",
                                    content=[Plain(without_url)]
                                )
                                ns.nodes.append(node1)
                                ns.nodes.append(node2)
                                yield event.chain_result([ns])
                            else:
                                yield event.chain_result([
                                Image(file=result['cover']),
                                Video.fromFileSystem(nap_file_path),
                                Plain(without_url),
                                ])

