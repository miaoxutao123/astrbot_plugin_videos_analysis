from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video
from astrbot.api.event import filter, AstrMessageEvent
import re
from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_get import process_douyin

@register("hybird_videos_analysis", "喵喵", "可以解析抖音和bili视频", "0.0.1")
class hybird_videos_analysis(Star):
    def __init__(self, context: Context,config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")
    
    @filter.event_message_type(EventMessageType.ALL)
    async def auto_parse_dy(self, event: AstrMessageEvent):
        """
        自动检测消息中是否包含抖音分享链接，并解析。
        """
        # result = {
        # "type": None,  # 图片或视频
        # "is_multi_part": False,  # 是否为分段内容
        # "count": 0,  # 图片或视频数量"
        # "save_path": [] , # 无水印保存路径
        # "title": None,  # 标题
        # }
        message_str = event.message_str
        match = re.search(r'(https?://v\.douyin\.com/[a-zA-Z0-9]+)', message_str)
        if match:
            url = match.group(1)
            print(f"检测到抖音链接: {url}")  # 添加日志记录
            result = process_douyin(url)
            if result:
                if result['type'] == "video":
                    if result['is_multi_part']:
                        if self.nap_server_address !="localhost":
                            ns = Nodes([])
                            for i in range(result['count']-1):
                                file_path = result['save_path'][i]
                                nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin = event.get_self_id(),
                                    name = "喵喵",
                                    content = [Video(file=nap_file_path)]
                                )
                                ns.nodes.append(node)
                        else :
                            ns = Nodes([])
                            for i in range(result['count']-1):
                                file_path = result['save_path'][i]
                                node = Node(
                                    uin = event.get_self_id(),
                                    name = "喵喵",
                                    content = [Video(file=file_path)]
                                )
                                ns.nodes.append(node)
                        yield event.chain_result(ns)
                    else:
                        file_path = result['save_path'][0]
                        if self.nap_server_address !="localhost":
                            nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                            # print(nap_file_path)
                        else :
                            nap_file_path = file_path
                        yield event.chain_result([
                           Video(file=nap_file_path)
                        ])
                elif result['type'] == "image":
                    if result['is_multi_part']:
                        if self.nap_server_address !="localhost":
                            ns = Nodes([])
                            for i in range(result['count']-1):
                                file_path = result['save_path'][i]
                                nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                                node = Node(
                                    uin = event.get_self_id(),
                                    name = "喵喵",
                                    content = [Image(file=nap_file_path)]
                                )
                                ns.nodes.append(node)
                        else :  
                            ns = Nodes([])
                            for i in range(result['count']-1):
                                file_path = result['save_path'][i]
                                node = Node(
                                    uin = event.get_self_id(),
                                    name = "喵喵",
                                    content = [Image(file=file_path)]
                                )
                                ns.nodes.append(node)
                        yield event.chain_result(ns)
                    else:
                        file_path = result['save_path'][0]
                        if self.nap_server_address !="localhost":
                            nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                            # print(nap_file_path)
                        else :
                            nap_file_path = file_path
                        yield event.chain_result([
                            Image(file=nap_file_path)
                        ])
                else:
                    print("解析失败，请检查链接是否正确。")
                # file_path = result['video_path']


                # if self.nap_server_address !="localhost":

                #     nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                #     print(nap_file_path)
                # else :
                #     nap_file_path = file_path
                # yield event.chain_result([
                #     Plain(f"作者昵称：{result['author_name']}\n视频简介：{result['video_description']}"),
                #     Video(file=nap_file_path)
                # ])
            else:
                print("解析失败，请检查链接是否正确。")  # 添加日志记录
                yield event.plain_result("检测到抖音链接，但解析失败，请检查链接是否正确。")

    async def terminate(self):
        '''可选择实现 terminate 函数，当插件被卸载/停用时会调用。'''
        yield event.plain_result("抖音解析插件已停用。")

    @filter.event_message_type(EventMessageType.ALL)
    async def auto_parse_bili(self, event: AstrMessageEvent):
        """
        自动检测消息中是否包含bili分享链接，并解析。
        """
        message_str = event.message_str
        match = re.search(r'(b23\.tv|bili2233\.cn\/[\w]+|BV1\w{9}|av\d+)', message_str)
        if match:
            url = match.group(1)
            result = process_bili_video(url)
            if result:
                file_path = result['video_path']
                if self.nap_server_address !="localhost":
                    nap_file_path = send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
                    print(nap_file_path)
                else :
                    nap_file_path = file_path
            yield event.chain_result([
                    Plain(f"视频标题：{result['title']}\n观看次数：{result['view_count']}\n点赞次数：{result['like_count']}\n投币次数：{result['coin_count']}"),
                    Image(file=result['cover']),
                    Video(file=nap_file_path)
             ])
    # async def terminate(self):
    #     '''可选择实现 terminate 函数，当插件被卸载/停用时会调用。'''
    #     yield self.event.plain_result("bilibili解析插件已停用。")
