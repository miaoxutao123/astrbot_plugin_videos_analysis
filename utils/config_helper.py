"""
配置辅助模块：Gemini API 配置获取、LLM 响应发送、通用辅助函数
"""

import json
import os

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.message_components import Node

from .file_utils import send_file, delete_old_files

# 下载目录常量
DOWNLOAD_DIR_DY = "data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"
DOWNLOAD_DIR_BILI = "data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/"
DOWNLOAD_DIR_DIRECT = "data/plugins/astrbot_plugin_videos_analysis/download_videos/direct/"


async def get_gemini_api_config(context):
    """获取 Gemini API 配置（优先框架 → 插件配置回退）

    Args:
        context: AstrBot Context 对象

    Returns:
        (api_key, proxy_url) 元组
    """
    api_key = None
    proxy_url = None

    # 1. 优先尝试从框架的默认 Provider 获取
    provider = context.provider_manager.curr_provider_inst
    if provider and provider.meta().type == "googlegenai_chat_completion":
        logger.info("检测到框架默认LLM为Gemini，将使用框架配置。")
        api_key = provider.get_current_key()
        proxy_url = getattr(provider, "api_base", None) or getattr(provider, "base_url", None)
        if proxy_url:
            logger.info(f"使用框架配置的代理地址：{proxy_url}")

    # 2. 如果默认 Provider 不是 Gemini，尝试查找其他 Gemini Provider
    if not api_key:
        logger.info("默认Provider不是Gemini，搜索其他Provider...")
        for provider_name, provider_inst in context.provider_manager.providers.items():
            if provider_inst and provider_inst.meta().type == "googlegenai_chat_completion":
                logger.info(f"在Provider列表中找到Gemini配置：{provider_name}")
                api_key = provider_inst.get_current_key()
                proxy_url = getattr(provider_inst, "api_base", None) or getattr(provider_inst, "base_url", None)
                break

    return api_key, proxy_url


async def get_gemini_api_config_with_fallback(context, gemini_api_key: str = "", gemini_base_url: str = ""):
    """获取 Gemini API 配置，带插件配置回退

    Args:
        context: AstrBot Context 对象
        gemini_api_key: 插件配置的 API key
        gemini_base_url: 插件配置的 base URL

    Returns:
        (api_key, proxy_url) 元组
    """
    api_key, proxy_url = await get_gemini_api_config(context)

    # 3. 回退到插件自身配置
    if not api_key:
        logger.info("框架中未找到Gemini配置，回退到插件自身配置。")
        api_key = gemini_api_key
        proxy_url = gemini_base_url
        if api_key:
            logger.info("使用插件配置的API Key。")

    return api_key, proxy_url


async def send_llm_response(context, event, video_summary: str, platform: str = "抖音"):
    """将视频摘要提交给框架 LLM 进行评价 — 异步生成器"""
    if not video_summary:
        return

    curr_cid = await context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
    conversation = None
    llm_context = []
    if curr_cid:
        conversation = await context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
        if conversation:
            llm_context = json.loads(conversation.history)

    # 获取当前人格设定
    provider = context.provider_manager.curr_provider_inst
    current_persona = None
    if provider and hasattr(provider, "personality"):
        current_persona = provider.personality
    elif context.provider_manager.selected_default_persona:
        current_persona = context.provider_manager.selected_default_persona

    persona_prompt = ""
    if current_persona and hasattr(current_persona, "prompt"):
        persona_prompt = f"请保持你的人格设定：{current_persona.prompt}\n\n"

    final_prompt = (
        f"{persona_prompt}我刚刚分析了这个{platform}视频的内容：\n\n{video_summary}\n\n"
        f"请基于这个视频内容，结合你的人格特点，自然地发表你的看法或评论。"
        f"不要说这是我转述给你的，请像你亲自观看了这个用户给你分享的来自{platform}的视频一样回应。"
    )

    llm_result = event.request_llm(
        prompt=final_prompt,
        session_id=curr_cid,
        contexts=llm_context,
        conversation=conversation,
    )

    if hasattr(llm_result, "__aiter__"):
        async for result in llm_result:
            yield result
    else:
        yield llm_result


def should_comprehend(event, platform_config: bool = False, private_auto_comprehend: bool = True) -> bool:
    """判断是否应触发 AI 视频理解

    私聊：根据 private_auto_comprehend 配置决定
    群聊：@bot 时触发，或平台全局开关开启时触发
    """
    if event.is_private_chat():
        return private_auto_comprehend
    if getattr(event, "is_at_or_wake_command", False):
        return True
    return platform_config


def create_node(event, content) -> Node:
    """创建合并转发节点"""
    return Node(
        uin=event.get_self_id(),
        name="astrbot",
        content=content,
    )


async def send_file_if_needed(file_path: str, nap_server_address: str, nap_server_port: int) -> str:
    """如果 NAP 服务器不在本地，通过 TCP 发送文件"""
    if nap_server_address != "localhost":
        result = await send_file(file_path, HOST=nap_server_address, PORT=nap_server_port)
        return result or file_path
    return file_path


async def cleanup_old_files(folder_path: str, delete_time: int):
    """清理过期文件"""
    if delete_time > 0:
        delete_old_files(folder_path, delete_time)
