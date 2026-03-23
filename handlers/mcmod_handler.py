"""
MCMod 被动解析处理器
"""

import re

from astrbot.api import logger
from astrbot.api.message_components import Image, Nodes, Plain

from ..services.mcmod_service import mcmod_parse
from ..utils.config_helper import create_node


async def handle_mcmod_parse(plugin, event):
    """MC百科被动解析核心逻辑"""
    mod_pattern = r"(https?://www\.mcmod\.cn/class/\d+\.html)"
    modpack_pattern = r"(https?://www\.mcmod\.cn/modpack/\d+\.html)"

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    match = (
        re.search(mod_pattern, message_obj_str)
        or re.search(mod_pattern, message_str)
        or re.search(modpack_pattern, message_obj_str)
        or re.search(modpack_pattern, message_str)
    )

    contains_reply = re.search(r"reply", message_obj_str)

    if not match or contains_reply:
        return

    logger.info(f"解析MCmod链接: {match.group(1)}")
    results = await mcmod_parse(match.group(1))

    if not results or not results[0]:
        yield event.plain_result("抱歉，我不能打开这个MC百科链接，请检查一下链接是否正确。")
        return

    result = results[0]

    ns = Nodes([])
    ns.nodes.append(create_node(event, [Plain(f"📦 {result.name}")]))

    if result.icon_url:
        ns.nodes.append(create_node(event, [Image.fromURL(result.icon_url)]))

    if result.categories:
        categories_str = "/".join(result.categories)
        ns.nodes.append(create_node(event, [Plain(f"🏷️ 分类: {categories_str}")]))

    if result.description:
        ns.nodes.append(create_node(event, [Plain(f"📝 描述:\n{result.description}")]))

    if result.description_images:
        for img_url in result.description_images:
            ns.nodes.append(create_node(event, [Image.fromURL(img_url)]))

    yield event.chain_result([ns])
