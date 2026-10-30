"""
管理员命令处理器：B站登录、抖音登录、抖音Cookie手动设置
"""

import asyncio
import base64 as b64mod
import os
import uuid

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..services.bilibili_service import (
    generate_qrcode as bili_generate_qrcode,
    check_login_status_loop as bili_check_login_status_loop,
    check_cookie_valid as bili_check_cookie_valid,
)
from ..douyin_login import (
    save_manual_douyin_cookie,
)
from ..utils.config_helper import send_file_if_needed
from ..utils.paths import IMAGE_DIR

# 登录二维码 / cookie 只在私聊里处理：群里任何人都能抢先扫码（机器人会登录到对方账号），
# 粘贴的 cookie 等同于账号密码
_PRIVATE_ONLY = "为了账号安全，{what}只能私聊机器人进行（群里其他人能看到{obj}）。请私聊我发送 /{cmd}"


def _group_guard(event, what: str, obj: str, cmd: str) -> str:
    return "" if event.is_private_chat() else _PRIVATE_ONLY.format(what=what, obj=obj, cmd=cmd)


async def handle_bili_login(plugin, event):
    """管理员指令：B站扫码登录"""
    if msg := _group_guard(event, "扫码登录", "并抢先扫描二维码", "bili_login"):
        yield event.plain_result(msg)
        return
    is_valid = await bili_check_cookie_valid()
    if is_valid:
        yield event.plain_result("B站cookie仍然有效，无需重新登录。如需切换账号，请先发送 /va_logout bilibili。")
        return

    yield event.plain_result("正在生成B站登录二维码，请稍候...")

    qr_data = await bili_generate_qrcode()
    if not qr_data:
        yield event.plain_result("生成B站二维码失败，请稍后重试。")
        return

    image_dir = IMAGE_DIR
    os.makedirs(image_dir, mode=0o700, exist_ok=True)
    image_path = os.path.join(image_dir, f"bili_login_qrcode_{uuid.uuid4().hex[:10]}.png")
    try:
        fd = os.open(image_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(b64mod.b64decode(qr_data["image_base64"]))
        if os.name == "posix":
            os.chmod(image_path, 0o600)

        nap_path = await send_file_if_needed(image_path, plugin.nap_server_address, plugin.nap_server_port)
        yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])
        yield event.plain_result("请使用B站APP扫描上方二维码登录（2 分钟内有效）")

        cookies = await bili_check_login_status_loop(qr_data["qrcode_key"])
        if cookies:
            yield event.plain_result("B站登录成功！cookie已自动保存，后续将使用登录状态获取高清视频。")
        else:
            yield event.plain_result("B站登录超时或失败，请重试。")
    finally:
        # QR carries a one-time login credential; remove it after delivery/timeout.
        try:
            os.remove(image_path)
        except OSError:
            pass



async def handle_douyin_login(plugin, event):
    """管理员指令：抖音扫码登录 (使用浏览器签名引擎)"""
    if msg := _group_guard(event, "扫码登录", "并抢先扫描二维码", "dy_login"):
        yield event.plain_result(msg)
        return
    signer = plugin.browser_signer

    if not signer or not signer.is_ready:
        reason = plugin.browser_signer_error or "未知原因"
        yield event.plain_result(
            f"抖音浏览器签名引擎未就绪：{reason}\n\n"
            "需要: pip install playwright && PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright python -m playwright install --with-deps --no-shell chromium\n"
            "Linux 无桌面还需: apt install xvfb\n"
            "装好后发送 /dy_restart 重启签名引擎。"
        )
        return

    if signer.is_logged_in:
        yield event.plain_result("抖音已登录，无需重复扫码。如需切换账号，请先发送 /va_logout douyin。")
        return

    # 触发登录 (点击页面登录按钮)
    yield event.plain_result("正在触发抖音登录页面...")
    await signer.trigger_login()

    # 等二维码渲染完成后截图发送给用户
    screenshot = await signer.get_login_qr_screenshot()
    if not screenshot:
        yield event.plain_result("截图失败，请尝试使用 dy_cookie 命令手动粘贴cookie。")
        return

    os.makedirs(IMAGE_DIR, exist_ok=True)
    ss_path = os.path.join(IMAGE_DIR, "douyin_login_qrcode.png")
    with open(ss_path, "wb") as f:
        f.write(screenshot)

    nap_path = await send_file_if_needed(ss_path, plugin.nap_server_address, plugin.nap_server_port)
    yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])
    yield event.plain_result("请在浏览器窗口中扫码或使用手机抖音扫描二维码登录（120秒内有效）")

    # 等待登录
    logged_in = await signer.wait_for_login(timeout_seconds=120)
    if logged_in:
        cookie_str = await signer.get_cookie_string()
        plugin._douyin_cookie_from_scan = cookie_str
        yield event.plain_result("抖音登录成功！cookie已自动保存。")
    else:
        yield event.plain_result(
            "登录超时或失败。\n"
            "可尝试以下替代方案:\n"
            "1. 在浏览器中登录 www.douyin.com\n"
            "2. 发送: dy_cookie <从浏览器复制的cookie>"
        )


async def handle_douyin_cookie(plugin, event):
    """管理员指令：手动粘贴抖音cookie"""
    if not event.is_private_chat():
        has_cookie = len(event.message_str.split(maxsplit=1)) > 1
        yield event.plain_result(
            "为了账号安全，抖音 cookie 只能私聊机器人设置，本次没有保存。"
            + ("\n⚠️ 刚才的 cookie 已经发在群里，相当于公开了登录凭证：请在抖音 App「设置 → 账号与安全 → 登录设备管理」"
               "里移除这台设备，或退出网页登录使其失效，然后私聊我重新设置。" if has_cookie else "")
        )
        return
    signer = plugin.browser_signer

    message_str = event.message_str.strip()
    for prefix in ("dy_cookie", "/dy_cookie"):
        if message_str.startswith(prefix):
            message_str = message_str[len(prefix):].strip()
            break

    if not message_str:
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

    # 优先通过 BrowserSigner 设置 cookie
    if signer and signer.is_ready:
        ok = await signer.set_manual_cookies(message_str)
        if ok:
            plugin._douyin_cookie_from_scan = await signer.get_cookie_string()
            yield event.plain_result("抖音cookie已注入浏览器并保存！")
            return

    # 回退: 使用旧的方式
    cookie_str = await save_manual_douyin_cookie(message_str)
    if cookie_str:
        plugin._douyin_cookie_from_file = cookie_str
        yield event.plain_result("抖音cookie已保存！")
    else:
        yield event.plain_result("cookie格式无效，请检查后重试。")


async def handle_accounts_status(plugin, event, deep: bool = False):
    """管理员指令：查看各平台账号与 AI 服务状态"""
    if deep:
        yield event.plain_result("正在检测各平台连通性...")
    statuses = await plugin.accounts.status_all(deep=deep)
    text = "\n".join(st.render() for st in statuses)
    text += "\n\n命令: /bili_login /dy_login /dy_cookie /dy_restart /va_logout <douyin|bilibili> /va_accounts deep\n运维: /va_check /va_notify /va_pool /va_works /va_upload /va_delete /va_reply /va_pending"
    yield event.plain_result(text)
