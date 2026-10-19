"""
抖音浏览器签名引擎

核心原理:
- 启动 Chromium 加载 douyin.com, 页面中的 webmssdk 会自动为所有 fetch() 请求签名
- 通过 page.evaluate() 在页面上下文中调用 fetch(), SDK 自动注入 msToken/a_bogus/x-secsdk-web-signature
- 使用持久化 userDataDir 保存登录态, 下次启动可跳过扫码

平台支持:
- Windows: 非无头模式直接可用
- Linux:   需要 Xvfb 虚拟显示器, 本模块会自动检测和启动
- macOS:   非无头模式直接可用

验证码说明:
- 抖音对无头浏览器有浏览器指纹级验证码检测 (与登录状态无关)
- 通过 Xvfb/真实显示器运行的非无头 Chromium 不会被检测
- 公开视频无需登录即可通过 SDK 签名获取

架构:
┌─────────────┐    page.evaluate(fetch)    ┌──────────────┐
│  Python 代码 │ <─────────────────────────> │  Chromium    │
│  (解析短链,  │                              │  (douyin.com) │
│   下载媒体)  │                              └──────────────┘
└─────────────┘                                     │
                                              SDK 自动签名
                                              msToken/a_bogus
                                              x-secsdk-web-signature
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
import sys
from urllib.parse import urlencode

_PLUGIN_DATA_DIR = "data/plugin_data/astrbot_plugin_videos_analysis/accounts"
_USER_DATA_DIR = os.path.join(_PLUGIN_DATA_DIR, "dy_browser_profile")
_COOKIE_FILE = os.path.join(_PLUGIN_DATA_DIR, "douyin_cookies.json")

# 平台检测
_IS_LINUX = sys.platform.startswith("linux")
_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"


def _find_xvfb() -> str | None:
    """查找 Xvfb 可执行文件路径。"""
    for path in ("/usr/bin/Xvfb", "/usr/X11R6/bin/Xvfb"):
        if os.path.exists(path):
            return path
    return shutil.which("Xvfb")


_XVFB_PROC: subprocess.Popen | None = None


def _start_xvfb(display: int = 99) -> int | None:
    """
    尝试启动 Xvfb 虚拟显示器（已存在则复用）。
    返回 display 编号, 失败返回 None。
    """
    global _XVFB_PROC
    socket_path = f"/tmp/.X11-unix/X{display}"
    if os.path.exists(socket_path):
        os.environ["DISPLAY"] = f":{display}"
        return display

    xvfb = _find_xvfb()
    if not xvfb:
        return None

    try:
        _XVFB_PROC = subprocess.Popen(
            [xvfb, f":{display}", "-screen", "0", "1920x1080x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 等待 socket 出现，最多 5 秒
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            if _XVFB_PROC.poll() is not None:
                return None
            time.sleep(0.1)
        else:
            return None
        os.environ["DISPLAY"] = f":{display}"
        return display
    except Exception:
        return None


def _stop_xvfb() -> None:
    """只停止本模块自己启动的 Xvfb，并撤销对应的 DISPLAY。"""
    global _XVFB_PROC
    if _XVFB_PROC is None:
        return
    if _XVFB_PROC.poll() is None:
        _XVFB_PROC.terminate()
        try:
            _XVFB_PROC.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _XVFB_PROC.kill()
    os.environ.pop("DISPLAY", None)
    _XVFB_PROC = None


def _check_display() -> tuple[bool, str]:
    """
    检查显示器环境。
    返回 (是否可用, 原因描述)。
    """
    display = os.environ.get("DISPLAY", "")

    # 已有 DISPLAY 环境变量 → 直接可用
    if display:
        return True, f"DISPLAY={display}"

    # Windows/macOS 通常原生有显示器
    if _IS_WINDOWS or _IS_MACOS:
        return True, "Windows/macOS 原生显示"

    # Linux 无 DISPLAY → 尝试 Xvfb
    display_num = _start_xvfb()
    if display_num is not None:
        return True, f"Xvfb :{display_num} 已启动"

    # Xvfb 未安装
    return False, _xvfb_install_hint()


def _xvfb_install_hint() -> str:
    """生成 Xvfb 安装提示。"""
    return (
        "Linux 服务器缺少显示器环境。请安装 Xvfb:\n"
        "  Ubuntu/Debian: sudo apt install xvfb\n"
        "  CentOS/RHEL:   sudo yum install xorg-x11-server-Xvfb\n"
        "  Alpine:        sudo apk add xvfb\n"
        "安装后重启插件即可。或手动启动: Xvfb :99 -screen 0 1920x1080x24 &"
    )


class BrowserSigner:
    """
    抖音 API 浏览器签名引擎。

    用法:
        signer = BrowserSigner()
        status = await signer.start()
        # status['ok']=True   → SDK 就绪
        # status['ok']=False  → 查看 status['reason']

        result = await signer.parse_video("7644535719297355023")
        await signer.stop()
    """

    def __init__(self, headless: bool = False, user_data_dir: str | None = None):
        self._headless = headless
        self._user_data_dir = user_data_dir or _USER_DATA_DIR
        self._playwright = None
        self._context = None
        self._page = None
        self._sdk_ready = False
        self._logged_in = False
        self._lock = asyncio.Lock()

    # ── 生命周期 ─────────────────────────────────────────

    async def start(self) -> dict:
        """
        启动浏览器并导航到 douyin.com。

        Returns:
            {
                "ok": bool,             # 是否成功
                "reason": str,          # 失败原因或成功描述
                "logged_in": bool,      # 是否已登录
                "need_qr_login": bool,  # 是否需要扫码登录
            }
        """
        async with self._lock:
            if self._context and self._page:
                return self._make_status(True, "已运行")

            # 1. 检查显示器环境
            display_ok, display_msg = _check_display()
            if not display_ok:
                return self._make_status(False, display_msg)

            # 2. 检查 Playwright
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return self._make_status(
                    False,
                    "playwright 未安装。请运行:\n"
                    "  pip install playwright\n"
                    "  playwright install chromium",
                )

            os.makedirs(self._user_data_dir, exist_ok=True)

            # 3. 启动浏览器
            self._playwright = await async_playwright().start()

            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                "--window-size=1400,900",
            ]
            # Linux 特有参数
            if _IS_LINUX:
                launch_args += [
                    "--disable-gpu",
                    "--no-zygote",
                    "--disable-software-rasterizer",
                ]

            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    self._user_data_dir,
                    headless=self._headless,
                    args=launch_args,
                    viewport={"width": 1400, "height": 900},
                    locale="zh-CN",
                )
            except Exception as e:
                msg = str(e)
                await self._playwright.stop()
                self._playwright = None
                if "Executable doesn't exist" in msg:
                    return self._make_status(
                        False,
                        "Chromium 浏览器未安装。请运行: playwright install --with-deps chromium",
                    )
                if "Missing X server" in msg or "cannot open display" in msg.lower():
                    return self._make_status(
                        False,
                        f"无法打开显示器。{_xvfb_install_hint()}",
                    )
                if "error while loading shared libraries" in msg or "Host system is missing dependencies" in msg:
                    return self._make_status(
                        False,
                        "Chromium 缺少系统依赖。请运行: playwright install-deps chromium",
                    )
                return self._make_status(False, f"浏览器启动失败: {msg[:300]}")

            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()

            # 4. 注入反检测脚本
            await self._page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => false});
                window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};
            """)

            # 5. 导航到 douyin.com
            self._log(f"正在加载 douyin.com (display: {display_msg}) ...")
            try:
                await self._page.goto(
                    "https://www.douyin.com",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                self._log(f"导航超时 (可能网络问题): {e}")

            # 6. 检测验证码
            title = await self._page.title()
            if "验证码" in title:
                if self._headless:
                    await self._context.close()
                    self._context = None
                    return self._make_status(
                        False,
                        "无头浏览器被抖音验证码拦截。\n"
                        "抖音不支持 headless 模式, 请使用 display 模式:\n"
                        + (_xvfb_install_hint() if _IS_LINUX else "将 headless 设为 False"),
                    )
                # 非无头但仍有验证码 → 可能是页面异常, 重试
                self._log("检测到验证码, 3秒后重试...")
                await asyncio.sleep(3)
                try:
                    await self._page.goto(
                        "https://www.douyin.com",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    title = await self._page.title()
                except Exception:
                    pass

            if "验证码" in title:
                return self._make_status(
                    False,
                    "抖音页面持续返回验证码, 可能是网络环境被标记。\n"
                    "尝试以下方法:\n"
                    "1. 更换网络/IP\n"
                    "2. 使用 dy_cookie 命令手动设置 cookie\n"
                    "3. 等待一段时间后重试",
                )

            # 7. 等待 SDK
            self._sdk_ready = await self._wait_for_sdk(timeout=30)
            if not self._sdk_ready:
                return self._make_status(
                    False,
                    "SDK 加载超时。可能原因:\n"
                    "1. 网络连接问题\n"
                    "2. douyin.com 被屏蔽\n"
                    "3. 页面加载不完整",
                )

            self._log("SDK 就绪")

            # 8. 检查登录状态
            self._logged_in = await self._check_logged_in()

            if self._logged_in:
                return self._make_status(True, "已登录, SDK 就绪", logged_in=True)
            else:
                return self._make_status(
                    True,
                    "SDK 就绪, 未登录 (公开视频可正常解析)",
                    logged_in=False,
                    need_qr_login=True,
                )

    async def stop(self, save: bool = True) -> None:
        """关闭浏览器（默认保存登录状态）。"""
        async with self._lock:
            if self._context:
                if save:
                    try:
                        cookies = await self._context.cookies()
                        await self._save_cookies(cookies)
                    except Exception:
                        pass
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None

            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

            self._page = None
            self._sdk_ready = False
            self._logged_in = False
            _stop_xvfb()

    @property
    def is_ready(self) -> bool:
        return self._sdk_ready and self._page is not None

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    # ── 登录 ─────────────────────────────────────────────

    async def get_page_screenshot(self) -> bytes | None:
        """截取当前浏览器页面 (PNG bytes), 用于发送扫码截图。"""
        if not self._page:
            return None
        try:
            return await self._page.screenshot(type="png", full_page=False)
        except Exception as e:
            self._log(f"截图失败: {e}")
            return None

    async def trigger_login(self) -> bool:
        """点击 douyin.com 页面的登录按钮, 弹出登录对话框/二维码。"""
        if not self._page:
            return False

        try:
            await self._page.goto(
                "https://www.douyin.com",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            clicked = await self._page.evaluate("""
                (function() {
                    const btns = document.querySelectorAll('span, div, button, a');
                    for (const b of btns) {
                        if (b.textContent.trim() === '登录' || b.textContent.trim() === '我') {
                            b.click();
                            return 'clicked:' + b.textContent.trim();
                        }
                    }
                    return 'not found';
                })()
            """)
            self._log(f"触发登录: {clicked}")
            await asyncio.sleep(2)
            return True
        except Exception as e:
            self._log(f"触发登录失败: {e}")
            return False

    async def wait_for_login(self, timeout_seconds: int = 120) -> bool:
        """
        轮询等待用户完成登录。
        返回 True=已登录, False=超时。
        """
        for i in range(timeout_seconds):
            await asyncio.sleep(1)
            if await self._check_logged_in():
                self._logged_in = True
                cookies = await self._context.cookies()
                await self._save_cookies(cookies)
                self._log("登录成功!")
                return True
            if i % 15 == 0 and i > 0:
                self._log(f"等待登录... ({i}s)")
        return False

    async def set_manual_cookies(self, cookie_string: str) -> bool:
        """
        手动设置 cookie。
        支持: name=value; name2=value2; ... 或 JSON 数组 [{"name":"x","value":"y"},...]
        """
        if not self._context:
            return False

        cookie_string = cookie_string.strip()
        pairs = []

        if cookie_string.startswith("["):
            try:
                arr = json.loads(cookie_string)
                for item in arr:
                    if isinstance(item, dict) and "name" in item:
                        pairs.append({
                            "name": item["name"], "value": item.get("value", ""),
                            "domain": ".douyin.com", "path": "/",
                        })
            except json.JSONDecodeError:
                pass

        if not pairs:
            for part in cookie_string.replace(" ", "").split(";"):
                if "=" in part:
                    name, value = part.split("=", 1)
                    pairs.append({
                        "name": name.strip(), "value": value.strip(),
                        "domain": ".douyin.com", "path": "/",
                    })

        if pairs:
            await self._context.add_cookies(pairs)
            await self._save_cookies(pairs)
            return True
        return False

    # ── API 签名调用 ─────────────────────────────────────

    async def fetch_signed_api(
        self, endpoint: str, params: dict, method: str = "GET"
    ) -> dict | None:
        """
        在浏览器页面上下文中调用抖音 API, SDK 自动签名。

        Args:
            endpoint: API 路径, 如 "aweme/v1/web/aweme/detail/"
            params: 参数字典
            method: GET 或 POST

        Returns:
            API 响应 JSON, 失败返回 None
        """
        if not self._page:
            self._log("浏览器未启动")
            return None

        if not self._sdk_ready:
            await self._wait_for_sdk(timeout=5)

        base_url = "https://www.douyin.com/" + endpoint.lstrip("/")

        if method.upper() == "POST":
            url = base_url
            body_json = json.dumps(params)
            js_code = f"""
            (async function() {{
                try {{
                    const resp = await fetch({json.dumps(url)}, {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: {json.dumps(body_json)}
                    }});
                    const text = await resp.text();
                    return {{ok: resp.ok, status: resp.status, body: text}};
                }} catch(e) {{
                    return {{ok: false, status: 0, error: e.message}};
                }}
            }})()
            """
        else:
            query = urlencode(params)
            url = f"{base_url}?{query}"
            js_code = f"""
            (async function() {{
                try {{
                    const resp = await fetch({json.dumps(url)});
                    const text = await resp.text();
                    return {{ok: resp.ok, status: resp.status, body: text}};
                }} catch(e) {{
                    return {{ok: false, status: 0, error: e.message}};
                }}
            }})()
            """

        try:
            result = await self._page.evaluate(js_code)
            if not result or not result.get("ok"):
                self._log(f"API 请求失败: status={result and result.get('status')} {(result or {}).get('error') or (result or {}).get('body', '')[:200]}")
                return None
            if not result.get("body"):
                self._log("API 返回空响应（可能触发风控，可尝试 /dy_login 登录或 /dy_restart）")
                return None
            return json.loads(result["body"])
        except Exception as e:
            self._log(f"API 请求异常: {e}")
            return None

    async def refresh(self) -> bool:
        """重新加载 douyin.com 并等待 SDK，用于风控导致的空响应后重试。"""
        if not self._page:
            return False
        try:
            await self._page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            self._log(f"刷新页面失败: {e}")
        self._sdk_ready = await self._wait_for_sdk(timeout=20)
        return self._sdk_ready

    # ── 视频/图文解析 ────────────────────────────────────

    async def parse_video(self, aweme_id: str) -> dict | None:
        """
        解析单个 aweme_id, 返回媒体信息 (兼容旧 DouyinParser 格式)。
        """
        params = {
            "aweme_id": aweme_id,
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "149.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "149.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "16",
            "device_memory": "8",
            "platform": "PC",
        }
        raw = await self.fetch_signed_api("aweme/v1/web/aweme/detail/", params)
        if not raw:
            return None

        aweme_detail = raw.get("aweme_detail")
        if not aweme_detail:
            filter_msg = raw.get("filter_detail", {}).get("detail_msg", "")
            if filter_msg:
                self._log(f"视频不可用: {filter_msg}")
            return None

        return self._extract_media_info(aweme_detail)

    @staticmethod
    def _extract_media_info(aweme_detail: dict) -> dict:
        """从 aweme_detail 提取媒体信息, 选最高画质。"""
        media_type = "unknown"
        media_urls = []

        if aweme_detail.get("images") and len(aweme_detail["images"]) > 0:
            images = aweme_detail["images"]
            has_video = any(item.get("video") for item in images)
            media_type = "multi_video" if has_video else "image"

            for item in images:
                if item.get("video"):
                    vl = item.get("video", {}).get("play_addr", {}).get("url_list")
                    if vl:
                        media_urls.append(vl[0])
                elif item.get("url_list"):
                    media_urls.append(item["url_list"][-1])

        elif aweme_detail.get("video"):
            media_type = "video"
            video = aweme_detail["video"]
            bit_rates = video.get("bit_rate", [])

            best = bit_rates[0] if bit_rates else None
            for br in bit_rates:
                if "1080" in br.get("gear_name", ""):
                    best = br
                    break
            if best:
                url_list = best.get("play_addr", {}).get("url_list", [])
                if url_list:
                    media_urls.append(url_list[0])

            if not media_urls:
                pa_urls = video.get("play_addr", {}).get("url_list", [])
                if pa_urls:
                    media_urls.append(pa_urls[0])

        return {
            "aweme_id": aweme_detail.get("aweme_id"),
            "type": media_type,
            "desc": aweme_detail.get("desc"),
            "create_time": aweme_detail.get("create_time"),
            "author_nickname": aweme_detail.get("author", {}).get("nickname"),
            "media_urls": media_urls,
        }

    # ── Cookie 导出 ───────────────────────────────────────

    async def get_cookie_string(self) -> str:
        """导出所有 cookie 为 Python 可用的字符串, 用于下载媒体。"""
        if not self._context:
            return ""
        cookies = await self._context.cookies()
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    # ── 多账号 / 创作者操作支持 ─────────────────────────

    async def export_cookies(self) -> list[dict]:
        """导出完整 cookie 列表（含 domain/expires），用于账号池快照。"""
        if not self._context:
            return []
        return await self._context.cookies()

    async def replace_cookies(self, cookies: list[dict]) -> bool:
        """清空当前浏览器 cookie 并换成另一账号的快照（账号切换）。"""
        if not self._context:
            return False
        await self._context.clear_cookies()
        if cookies:
            await self._context.add_cookies(cookies)
        await self._save_cookies(cookies)
        self._logged_in = await self._check_logged_in()
        await self.refresh()
        return True

    async def new_page(self):
        """在同一登录态下打开新标签页（创作者中心操作用，调用方负责关闭）。"""
        if not self._context:
            raise RuntimeError("浏览器未启动")
        return await self._context.new_page()

    async def post_form(self, endpoint: str, query: dict, form: dict) -> dict | None:
        """在 douyin.com 页面里发 x-www-form-urlencoded POST（评论发布等写接口），SDK 自动签名。"""
        if not self._page:
            return None
        url = "https://www.douyin.com/" + endpoint.lstrip("/") + "?" + urlencode(query)
        js_code = f"""
        (async function() {{
            try {{
                const resp = await fetch({json.dumps(url)}, {{
                    method: 'POST', credentials: 'include',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}},
                    body: {json.dumps(urlencode(form))}
                }});
                return {{ok: resp.ok, status: resp.status, body: await resp.text()}};
            }} catch(e) {{
                return {{ok: false, status: 0, error: e.message}};
            }}
        }})()
        """
        try:
            result = await self._page.evaluate(js_code)
        except Exception as e:
            self._log(f"POST 异常: {e}")
            return None
        if not result or not result.get("body"):
            status = result and result.get("status")
            self._log(f"POST 失败: status={status} {(result or {}).get('error', '')}")
            return {"status_code": -1, "status_msg": f"HTTP {status} 空响应（未登录或触发风控）"}
        try:
            return json.loads(result["body"])
        except json.JSONDecodeError:
            return {"status_code": -1, "status_msg": result["body"][:200]}

    # ── 内部方法 ─────────────────────────────────────────

    def _make_status(self, ok: bool, reason: str, **extra) -> dict:
        return {
            "ok": ok,
            "reason": reason,
            "logged_in": self._logged_in,
            "need_qr_login": not self._logged_in and ok,
            **extra,
        }

    async def _wait_for_sdk(self, timeout: int = 30) -> bool:
        if not self._page:
            return False
        check = """
        (function() {
            const ac = window.byted_acrawler;
            if (ac && typeof ac.frontierSign === 'function') return 'ok';
            return 'waiting';
        })()
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self._page.evaluate(check) == "ok":
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
        return False

    async def _check_logged_in(self) -> bool:
        if not self._context:
            return False
        try:
            names = {c["name"] for c in await self._context.cookies()}
            return "sessionid" in names and "uid_tt" in names
        except Exception:
            return False

    async def _save_cookies(self, cookies: list) -> None:
        try:
            os.makedirs(os.path.dirname(_COOKIE_FILE), exist_ok=True)
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            with open(_COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookie_dict, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._log(f"保存 cookie 失败: {e}")

    @staticmethod
    def _log(msg: str) -> None:
        try:
            if "astrbot.api" in sys.modules:
                from astrbot.api import logger as a_logger
                a_logger.info(f"[BrowserSigner] {msg}")
                return
        except Exception:
            pass
        print(f"[BrowserSigner] {msg}")
