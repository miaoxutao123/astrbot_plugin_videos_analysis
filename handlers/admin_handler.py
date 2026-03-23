"""
管理员命令处理器：B站登录、抖音登录、抖音Cookie手动设置
"""

import base64 as b64mod
import os

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..services.bilibili_service import (
    generate_qrcode as bili_generate_qrcode,
    check_login_status_loop as bili_check_login_status_loop,
    check_cookie_valid as bili_check_cookie_valid,
)
from ..douyin_login import (
    generate_douyin_qrcode_playwright,
    douyin_login_loop_playwright,
    save_manual_douyin_cookie,
    check_douyin_cookie_valid,
)
from ..utils.config_helper import (
    DOWNLOAD_DIR_BILI,
    send_file_if_needed,
)


async def handle_bili_login(plugin, event):
    """管理员指令：B站扫码登录"""
    is_valid = await bili_check_cookie_valid()
    if is_valid:
        yield event.plain_result("B站cookie仍然有效，无需重新登录。如需强制刷新，请先删除cookie文件后重试。")
        return

    yield event.plain_result("正在生成B站登录二维码，请稍候...")

    qr_data = await bili_generate_qrcode()
    if not qr_data:
        yield event.plain_result("生成B站二维码失败，请稍后重试。")
        return

    image_dir = DOWNLOAD_DIR_BILI
    os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, "bili_login_qrcode.png")
    with open(image_path, "wb") as f:
        f.write(b64mod.b64decode(qr_data["image_base64"]))

    nap_path = await send_file_if_needed(image_path, plugin.nap_server_address, plugin.nap_server_port)
    yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])
    yield event.plain_result("请使用B站APP扫描上方二维码登录（40秒内有效）")

    cookies = await bili_check_login_status_loop(qr_data["qrcode_key"])
    if cookies:
        yield event.plain_result("B站登录成功！cookie已自动保存，后续将使用登录状态获取高清视频。")
    else:
        yield event.plain_result("B站登录超时或失败，请重试。")


async def handle_douyin_login(plugin, event):
    """管理员指令：抖音扫码登录"""
    is_valid = await check_douyin_cookie_valid()
    if is_valid:
        yield event.plain_result("抖音cookie仍然有效，无需重新登录。\n如需强制刷新，请先删除cookie文件后重试。")
        return

    yield event.plain_result("正在打开抖音登录页面，首次加载可能需要1-2分钟，请耐心等待...")

    pw_data = await generate_douyin_qrcode_playwright(timeout_seconds=180)
    if not pw_data:
        yield event.plain_result(
            "抖音登录页面加载失败。\n"
            "替代方案：使用 dy_cookie 命令手动粘贴cookie。\n"
            "方法：在浏览器登录抖音后，按F12打开开发者工具，"
            "在Console中输入 document.cookie 复制结果，"
            "然后发送: dy_cookie <粘贴的cookie>"
        )
        return

    nap_path = await send_file_if_needed(pw_data["screenshot_path"], plugin.nap_server_address, plugin.nap_server_port)
    yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])
    yield event.plain_result("请使用抖音APP扫描上方二维码登录（120秒内有效）")

    cookie_str = await douyin_login_loop_playwright(pw_data, timeout_seconds=120)
    if cookie_str:
        plugin._douyin_cookie_from_file = cookie_str
        yield event.plain_result("抖音登录成功！cookie已自动保存，后续解析将使用新cookie。")
    else:
        yield event.plain_result("登录超时或失败。\n可尝试使用 dy_cookie 命令手动粘贴cookie。")


async def handle_douyin_cookie(plugin, event):
    """管理员指令：手动粘贴抖音cookie"""
    message_str = event.message_str.strip()
    for prefix in ("dy_cookie", "/dy_cookie"):
        if message_str.startswith(prefix):
            message_str = message_str[len(prefix):].strip()
            break

    if not message_str or "=" not in message_str:
        yield event.plain_result(
            "用法: dy_cookie <cookie字符串>\n\n"
            "获取方法:\n"
            "1. 在浏览器中登录 www.douyin.com\n"
            "2. 按F12打开开发者工具\n"
            "3. 切换到Console标签\n"
            "4. 输入 document.cookie 并回车\n"
            "5. 复制输出的内容\n"
            "6. 发送: dy_cookie <粘贴的内容>"
        )
        return

    cookie_str = await save_manual_douyin_cookie(message_str)
    if cookie_str:
        plugin._douyin_cookie_from_file = cookie_str
        yield event.plain_result("抖音cookie已保存！后续解析将使用此cookie。")
    else:
        yield event.plain_result("cookie格式无效，请检查后重试。")
