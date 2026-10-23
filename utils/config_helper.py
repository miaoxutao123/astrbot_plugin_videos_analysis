"""
配置辅助模块：Gemini API 配置获取、LLM 响应发送、通用辅助函数
"""

from astrbot.api.message_components import Node

from ..services.gemini_service import DEFAULT_MODEL, GeminiConfig
from .file_utils import send_file, delete_old_files

from .paths import DOWNLOAD_DIR_BILI, DOWNLOAD_DIR_DIRECT, DOWNLOAD_DIR_DY  # noqa: F401


def _find_framework_gemini(context):
    """在框架 Provider 中查找 Gemini（优先当前默认 Provider）。"""
    pm = context.provider_manager
    candidates = [pm.curr_provider_inst, *getattr(pm, "provider_insts", [])]
    for prov in candidates:
        try:
            if prov and prov.meta().type == "googlegenai_chat_completion":
                return prov
        except Exception:
            continue
    return None


def resolve_gemini_config(plugin) -> GeminiConfig | None:
    """解析 Gemini 配置：未开启 gemini_enabled 时返回 None；插件配置的 key 优先，其次复用框架里的 Gemini Provider。"""
    if not getattr(plugin, "gemini_enabled", False):
        return None
    model = plugin.gemini_model or ""
    fallbacks = [m.strip() for m in str(plugin.config.get("gemini_fallback_models", "") or "").replace("，", ",").split(",") if m.strip()]
    common = {"inline_limit_mb": plugin.gemini_inline_limit_mb, "fallback_models": fallbacks}
    if plugin.gemini_api_key:
        return GeminiConfig(
            api_key=plugin.gemini_api_key,
            base_url=plugin.gemini_base_url,
            model=model or DEFAULT_MODEL,
            source="plugin",
            **common,
        )
    prov = _find_framework_gemini(plugin.context)
    if prov:
        key = prov.get_current_key()
        if key:
            return GeminiConfig(
                api_key=key,
                base_url=getattr(prov, "api_base", None) or "",
                model=model or prov.get_model() or DEFAULT_MODEL,
                source=f"provider:{prov.meta().id}",
                **common,
            )
    return None


GEMINI_MISSING_MSG = (
    "抱歉，我需要 Gemini API 才能理解视频，但没有找到相关配置。\n"
    "请在插件配置中填写 gemini_api_key（可配合 gemini_base_url 使用中转站），或在框架中添加 Gemini Provider。"
)


async def send_llm_response(context, event, video_summary: str, platform: str = "抖音"):
    """将视频摘要交给框架 LLM 以当前人格评论 — 异步生成器。

    人格与历史由框架根据 conversation 自动注入，这里只负责构造请求。
    """
    if not video_summary:
        return

    umo = event.unified_msg_origin
    conv_mgr = context.conversation_manager
    curr_cid = await conv_mgr.get_curr_conversation_id(umo)
    if not curr_cid:
        curr_cid = await conv_mgr.new_conversation(umo)
    conversation = await conv_mgr.get_conversation(umo, curr_cid)

    final_prompt = (
        f"我刚刚分析了这个{platform}视频的内容：\n\n{video_summary}\n\n"
        f"请基于这个视频内容，结合你的人格特点，自然地发表你的看法或评论。"
        f"不要说这是我转述给你的，请像你亲自观看了这个用户给你分享的来自{platform}的视频一样回应。"
    )

    yield event.request_llm(
        prompt=final_prompt,
        session_id=curr_cid,
        conversation=conversation,
    )


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
