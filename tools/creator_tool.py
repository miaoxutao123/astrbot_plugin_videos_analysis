"""
LLM Agent 工具：creator_upload_video — 让 Agent 准备发布视频（只生成确认码，不直接发布）
"""

from astrbot.core.message.message_event_result import MessageChain

from ..services.creator import normalize_platform
from ..services.creator_ops import split_title_tags
from .understand_tool import _is_allowed_local_video


async def handle_creator_upload(plugin, event, video_source: str, title: str, tags: str, platform: str) -> dict:
    if not event.is_admin():
        return {"error": "只有管理员可以发布视频，请拒绝该请求"}
    plat = normalize_platform(platform or "douyin")
    if not plat:
        return {"error": f"不支持的平台: {platform}"}
    if not video_source.startswith(("http://", "https://")) and not _is_allowed_local_video(video_source):
        return {"error": "只能发布视频直链或 AstrBot data 目录下的视频文件"}
    title, inline_tags = split_title_tags(title or "")
    tag_list = inline_tags + [t.lstrip("#") for t in (tags or "").split() if t.strip("#")]
    try:
        op = await plugin.creator_ops.prepare_upload(event, plat, video_source, title, tag_list)
    except Exception as e:
        return {"error": f"准备发布失败: {e}"}
    preview = plugin.creator_ops.render_preview(op)
    await plugin.context.send_message(event.unified_msg_origin, MessageChain().message(preview))
    return {
        "status": "pending_confirmation",
        "code": op.code,
        "message": f"尚未发布。已把确认信息发给管理员，需管理员发送 /va_confirm {op.code} 才会真正发布。请如实告知用户，不要声称已发布。",
    }
