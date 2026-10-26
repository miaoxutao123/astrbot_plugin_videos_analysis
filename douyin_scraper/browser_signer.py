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
import random
import shutil
import socket
import subprocess
import time
import sys
from urllib.parse import urlencode

from ..utils import client_identity

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


_XVFB_DISPLAY = 99


def _x_display_alive(display: str) -> bool:
    """
    DISPLAY 对应的 X server 是否真的能连上。

    只看 /tmp/.X11-unix/X<n> 是否存在不够：容器重启后 socket 文件还在，
    但 Xvfb 进程已经没了，Chromium 会报 "Missing X server"。
    """
    host, sep, rest = display.rpartition(":")
    if not sep:
        return False
    num = rest.split(".", 1)[0]
    if not num.isdigit():
        return False
    try:
        if host in ("", "unix"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(f"/tmp/.X11-unix/X{num}")
        else:
            with socket.create_connection((host, 6000 + int(num)), timeout=1):
                pass
        return True
    except OSError:
        return False


def _remove_stale_x_files(display: int) -> None:
    """删除残留的 X 锁和 socket（调用前须确认该 display 已连不上）。"""
    for path in (f"/tmp/.X{display}-lock", f"/tmp/.X11-unix/X{display}"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _start_xvfb(display: int = _XVFB_DISPLAY) -> int | None:
    """
    确保 :display 上有一个可连接的 Xvfb（已在运行则复用）。
    返回 display 编号, 失败返回 None。

    启动的 Xvfb 不随插件停止而关闭：插件热重载时新旧模块各有一份全局状态，
    旧实例 terminate 时若关掉 Xvfb，会把新实例正在用的显示器一起杀掉。
    Xvfb 常驻的开销很小，下次启动直接复用。
    """
    name = f":{display}"
    if _x_display_alive(name):
        os.environ["DISPLAY"] = name
        return display

    xvfb = _find_xvfb()
    if not xvfb:
        return None

    # 连不上却有锁/socket → 上次的 Xvfb 已死（如容器重启），清掉残留，
    # 否则 Xvfb 会因锁文件拒绝启动
    _remove_stale_x_files(display)
    try:
        os.makedirs("/tmp/.X11-unix", mode=0o1777, exist_ok=True)
    except OSError:
        pass

    try:
        proc = subprocess.Popen(
            [xvfb, name, "-screen", "0", "1920x1080x24", "-ac", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return None

    # 等待 X server 可连接，最多 5 秒
    for _ in range(50):
        if _x_display_alive(name):
            os.environ["DISPLAY"] = name
            return display
        if proc.poll() is not None:
            return None
        time.sleep(0.1)
    proc.kill()
    return None


def _check_display() -> tuple[bool, str]:
    """
    检查显示器环境（每次启动浏览器前都重新检查）。
    返回 (是否可用, 原因描述)。
    """
    # Windows/macOS 通常原生有显示器
    if _IS_WINDOWS or _IS_MACOS:
        return True, "Windows/macOS 原生显示"

    display = os.environ.get("DISPLAY", "")
    if display and _x_display_alive(display):
        return True, f"DISPLAY={display}"

    # Linux 无 DISPLAY，或 DISPLAY 已失效（Xvfb 被杀/容器重启）→ 启动 Xvfb
    display_num = _start_xvfb()
    if display_num is not None:
        if display and display != f":{display_num}":
            return True, f"DISPLAY={display} 不可用，已改用 Xvfb :{display_num}"
        return True, f"Xvfb :{display_num} 就绪"

    if display:
        os.environ.pop("DISPLAY", None)
    return False, _xvfb_install_hint()


def _clear_stale_profile_lock(user_data_dir: str) -> bool:
    """
    清理残留的 Chromium 配置锁。

    SingletonLock 指向 "<hostname>-<pid>"。容器重建后 hostname 改变，或进程已退出，
    Chromium 会认为配置正被"另一台电脑"使用而直接退出，这里把这种残留锁删掉。
    返回是否清理了锁。
    """
    lock = os.path.join(user_data_dir, "SingletonLock")
    try:
        target = os.readlink(lock)
    except OSError:
        return False
    host, _, pid = target.rpartition("-")
    alive = False
    if host == socket.gethostname() and pid.isdigit():
        try:
            os.kill(int(pid), 0)
            alive = True
        except OSError:
            alive = False
    if alive:
        return False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(user_data_dir, name))
        except OSError:
            pass
    return True


_CHROME_PATHS = (
    "/opt/google/chrome/chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def _major(version: str) -> int:
    head = version.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def _chrome_channel(user_data_dir: str) -> str | None:
    """
    装了正式版 Google Chrome 就用它（channel="chrome"），否则用 Playwright 自带的 Chromium。

    Chromium 的 UA-CH 品牌里没有 "Google Chrome"，普通用户几乎都是正式版 Chrome，
    换成正式版后浏览器身份和真人一致。安装: playwright install chrome

    配置目录被更新版本的浏览器用过时不切换：低版本 Chrome 打开高版本配置会拒绝或损坏它，
    登录态就没了。
    """
    path = next((p for p in _CHROME_PATHS if os.path.exists(p)), None)
    if not path:
        return None
    try:
        with open(os.path.join(user_data_dir, "Last Version"), encoding="utf-8") as f:
            profile_major = _major(f.read())
    except OSError:
        return "chrome"
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=15).stdout
        chrome_major = _major(out.strip().rsplit(" ", 1)[-1])
    except Exception:
        chrome_major = 0
    return "chrome" if chrome_major and chrome_major >= profile_major else None


def _mark_profile_clean_exit(user_data_dir: str) -> None:
    """
    把配置标记为正常退出。容器重启/崩溃时浏览器是被杀掉的，
    下次启动会一直挂着 "Restore pages?" 提示，真人的浏览器不会这样。
    """
    pref = os.path.join(user_data_dir, "Default", "Preferences")
    try:
        with open(pref, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    profile = data.setdefault("profile", {})
    if profile.get("exit_type") == "Normal" and profile.get("exited_cleanly"):
        return
    profile["exit_type"] = "Normal"
    profile["exited_cleanly"] = True
    try:
        with open(pref, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _disable_secure_dns(user_data_dir: str) -> None:
    """
    关闭 Chrome 的安全 DNS（DoH），改用系统 DNS。

    容器的 resolv.conf 里有 8.8.8.8 时，Chrome 启动十几秒后会自动升级到 dns.google 的 DoH。
    宿主机走代理时这个查询从海外出口发出，抖音 CDN 被解析到美国节点
    （实测 lf-fe-creator.douyinstatic.com → 弗吉尼亚的阿里云 CDN），
    浏览器直连跨洋节点，创作者中心的静态 JS 每 2 秒才十几 KB，goto 30s 超时；
    国内用户访问海外节点本身也是风控信号。系统 DNS 返回的是国内节点。
    """
    path = os.path.join(user_data_dir, "Local State")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if state.get("dns_over_https", {}).get("mode") == "off":
        return
    state["dns_over_https"] = {"mode": "off", "templates": ""}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


# Playwright 默认会加的启动参数里，这些会让浏览器和普通用户的 Chrome 不一样，去掉：
# - --enable-automation：显示"受自动化软件控制"、navigator.webdriver=true
# - --disable-popup-blocking：无用户手势的 window.open 也能弹出，风控脚本会拿它探测自动化
# - --disable-extensions / --disable-component-extensions-with-background-pages / --disable-default-apps /
#   --disable-component-update：真人的 Chrome 都有内置组件扩展和组件更新
# - --disable-field-trial-config / --force-color-profile=srgb：关掉了 Chrome 的实验分组、强制色彩配置
_IGNORED_DEFAULT_ARGS = [
    "--enable-automation",
    "--disable-popup-blocking",
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-field-trial-config",
    "--force-color-profile=srgb",
]

# 抖音风控页 / 验证码的特征（接口响应头、响应体、页面 DOM）
_RISK_HEADERS = ("x-vc-bdturing-parameters", "bdturing-verify")
_RISK_BODY_MARKS = ("verify_check", "\"verify_type\"", "bdturing")
_RISK_COOLDOWN = 30 * 60
# 两次签名请求之间的最小间隔（秒），再加随机抖动，避免连续高频调用
_API_MIN_INTERVAL = 1.5
_API_JITTER = 1.0
# 单个签名请求的超时（秒）
_API_TIMEOUT = 20


def _xvfb_install_hint() -> str:
    """生成 Xvfb 安装提示。"""
    return (
        "Linux 服务器缺少显示器环境。请安装 Xvfb:\n"
        "  Ubuntu/Debian: sudo apt install xvfb\n"
        "  CentOS/RHEL:   sudo yum install xorg-x11-server-Xvfb\n"
        "  Alpine:        sudo apk add xvfb\n"
        "安装后重启插件即可。或手动启动: Xvfb :99 -screen 0 1920x1080x24 &"
    )


# 打开/关闭主页面的"空闲省电"样式；返回调用前是否已处于空闲
_IDLE_JS = """(on) => {
    const old = document.getElementById('va-idle');
    if (on && !old) {
        const s = document.createElement('style');
        s.id = 'va-idle';
        s.textContent = 'html{display:none!important} *{animation:none!important;transition:none!important}';
        document.documentElement.appendChild(s);
        document.querySelectorAll('video,audio').forEach(v => { try { v.pause(); v.muted = true; } catch (e) {} });
    } else if (!on && old) {
        old.remove();
    }
    return !!old;
}"""


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

    def __init__(
        self,
        headless: bool = False,
        user_data_dir: str | None = None,
        timezone: str = "Asia/Shanghai",
        low_cpu: bool = True,
    ):
        self._headless = headless
        self._low_cpu = low_cpu
        self._user_data_dir = user_data_dir or _USER_DATA_DIR
        self._timezone = timezone
        self._client_env: dict | None = None
        self._last_api_at = 0.0
        self._api_lock = asyncio.Lock()
        self._risk_until = 0.0
        self._risk_reason = ""
        self._playwright = None
        self._context = None
        self._page = None
        self._sdk_ready = False
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._origin_pages: dict = {}
        self.browser_name = ""

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
            if _clear_stale_profile_lock(self._user_data_dir):
                self._log("已清理残留的浏览器配置锁（上次未正常退出或容器已重建）")
            _mark_profile_clean_exit(self._user_data_dir)
            _disable_secure_dns(self._user_data_dir)

            # 3. 启动浏览器
            self._playwright = await async_playwright().start()
            self._client_env = None

            # 可信环境：尽量让浏览器和真人桌面 Chrome 一致，不靠 JS 注入去"伪装"
            # （注入的 webdriver/window.chrome 本身就能被检测出来）
            # - 不禁用 GPU：禁用后 WebGL 直接不可用，是很明显的机器特征；无显卡时走 SwiftShader
            # - 不固定 viewport：否则 screen 会被改成 1400x900，窗口反而比屏幕大
            # - 去掉 --enable-automation，时区/语言与国内用户一致
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1400,900",
                "--window-position=0,0",
                "--lang=zh-CN",
                "--accept-lang=zh-CN,zh",
            ]
            if _IS_LINUX:
                launch_args += ["--no-zygote", "--enable-unsafe-swiftshader"]

            launch_kwargs = dict(
                headless=self._headless,
                args=launch_args,
                ignore_default_args=_IGNORED_DEFAULT_ARGS,
                timezone_id=self._timezone,
            )
            if self._headless:
                launch_kwargs["viewport"] = {"width": 1400, "height": 900}
                launch_kwargs["locale"] = "zh-CN"
            else:
                launch_kwargs["no_viewport"] = True
            if _IS_LINUX:
                # Intl.DateTimeFormat().resolvedOptions().locale 跟随进程语言环境
                launch_kwargs["env"] = {**os.environ, "LANG": "zh_CN.UTF-8", "LANGUAGE": "zh_CN:zh"}
            channel = _chrome_channel(self._user_data_dir)
            if channel:
                launch_kwargs["channel"] = channel

            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    self._user_data_dir, **launch_kwargs
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
            self.browser_name = "Google Chrome" if channel else "Chromium"
            self._log(f"浏览器: {self.browser_name}，时区 {self._timezone}")

            # 4. 导航到 douyin.com
            self._log(f"正在加载 douyin.com (display: {display_msg}) ...")
            try:
                await self._page.goto(
                    "https://www.douyin.com",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                self._log(f"导航超时 (可能网络问题): {e}")

            # 5. 检测验证码
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

            # 6. 等待 SDK
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
            await self._record_identity()

            # 7. 检查登录状态
            self._logged_in = await self._check_logged_in()
            await self._set_idle(True)

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
            self._origin_pages = {}
            self._sdk_ready = False
            self._logged_in = False

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
            await self._set_idle(False)
            return await self._page.screenshot(type="png", full_page=False)
        except Exception as e:
            self._log(f"截图失败: {e}")
            return None

    async def get_login_qr_screenshot(self, timeout: float = 30) -> bytes | None:
        """
        等登录弹窗里的二维码真正渲染出来再截图（只截弹窗）。

        点击登录后弹窗先显示抖音 logo 占位，二维码（data: PNG）要几秒后才出现，
        固定 sleep 后截图会拿到扫不了的空图。超时则退回整页截图。
        """
        if not self._page:
            return None
        try:
            handle = await self._page.wait_for_function(
                """() => {
                    const panel = document.querySelector('[id^=login-full-panel]') || document;
                    let img = panel.querySelector('img[aria-label="二维码"]');
                    if (!img) {
                        img = [...panel.querySelectorAll('img')].find(i => (i.src || '').startsWith('data:image')
                            && i.naturalWidth >= 200 && Math.abs(i.naturalWidth - i.naturalHeight) < 4);
                    }
                    if (!img || !img.complete || !img.naturalWidth || img.getBoundingClientRect().width < 50) return null;
                    // 向上找到弹窗本体（足够宽的祖先），截图只截这一块
                    let box = img.getBoundingClientRect();
                    for (let el = img.parentElement; el && el !== document.body; el = el.parentElement) {
                        const r = el.getBoundingClientRect();
                        if (r.width >= 500 && r.height >= 300 && r.width < innerWidth) { box = r; break; }
                    }
                    return {x: box.x, y: box.y, width: box.width, height: box.height};
                }""",
                timeout=timeout * 1000,
            )
            clip = await handle.json_value()
            await asyncio.sleep(0.5)
            return await self._page.screenshot(type="png", clip=clip)
        except Exception as e:
            self._log(f"等待登录二维码失败，改为整页截图: {e}")
        return await self.get_page_screenshot()

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
        try:
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
        finally:
            await self._set_idle(True)

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

    # ── 风控 ─────────────────────────────────────────────

    @property
    def risk_status(self) -> str:
        """处于风控冷却期时返回原因，否则返回空串。"""
        if time.time() < self._risk_until:
            left = int(self._risk_until - time.time()) // 60 + 1
            return f"{self._risk_reason}（约 {left} 分钟后自动恢复，或在浏览器里完成验证后 /dy_restart）"
        return ""

    def trip_risk(self, reason: str) -> None:
        """
        触发风控熔断：冷却期内不再发签名请求。
        被要求验证后继续高频重试只会让账号/设备的风险分越来越高。
        """
        self._risk_until = time.time() + _RISK_COOLDOWN
        self._risk_reason = reason
        self._log(f"检测到抖音风控: {reason}，暂停签名请求 {_RISK_COOLDOWN // 60} 分钟")

    async def has_captcha(self, page=None) -> bool:
        """页面（默认主页面）是否弹出了抖音验证码（验证中心 iframe / 验证码容器 / 验证码中间页）。"""
        page = page or self._page
        if not page:
            return False
        idle = page is self._page and await self._set_idle(False)
        try:
            if "验证码" in (await page.title()):
                return True
            # 页面里常驻一个 0x0 的 rmc-nocaptcha 验证中心 iframe（无感风控 SDK），只算真正显示出来的
            return await page.evaluate("""() => {
                const shown = el => {
                    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                    return r.width > 50 && r.height > 50 && cs.display !== 'none' && cs.visibility !== 'hidden';
                };
                const els = [...document.querySelectorAll('#captcha_container, .captcha_verify_container, [id^=captcha-verify]')];
                const frames = [...document.querySelectorAll('iframe')].filter(f => /verifycenter|bdturing|captcha/i.test(f.src || ''));
                return els.concat(frames).some(shown);
            }""")
        except Exception:
            return False
        finally:
            if idle:
                await self._set_idle(True)

    async def _client_params(self) -> dict:
        """
        用浏览器真实环境生成抖音 web 接口的设备参数。

        之前写死成 Windows / Chrome 149 / 16 核，而 SDK 采集到的是真实的 Linux Chrome，
        同一请求里两套设备信息互相矛盾，是很强的风控信号。
        """
        if self._client_env is None:
            env = await self._page.evaluate("""() => {
                const ua = navigator.userAgent;
                const ver = (ua.match(/Chrome\\/([\\d.]+)/) || [])[1] || '';
                const plat = navigator.platform || '';
                let os_name = 'Linux', os_version = '';
                if (/Win/.test(plat)) { os_name = 'Windows'; os_version = (ua.match(/Windows NT ([\\d.]+)/) || [])[1] || '10'; }
                else if (/Mac/.test(plat)) { os_name = 'Mac OS'; os_version = ((ua.match(/Mac OS X ([\\d_]+)/) || [])[1] || '').replace(/_/g, '.'); }
                return {
                    screen_width: String(screen.width), screen_height: String(screen.height),
                    browser_language: navigator.language, browser_platform: plat,
                    browser_name: 'Chrome', browser_version: ver,
                    engine_name: 'Blink', engine_version: ver,
                    os_name, os_version,
                    cpu_core_num: String(navigator.hardwareConcurrency || 8),
                    device_memory: String(navigator.deviceMemory || 8),
                    browser_online: String(navigator.onLine), cookie_enabled: String(navigator.cookieEnabled),
                };
            }""")
            self._client_env = env
        return self._client_env

    async def fetch_signed_api(
        self, endpoint: str, params: dict, method: str = "GET", form: dict | None = None
    ) -> dict | None:
        """
        在浏览器页面上下文中调用抖音 API, SDK 自动签名。

        - 设备参数统一用浏览器真实环境覆盖（调用方传的同名参数会被替换）
        - 请求之间保持最小间隔 + 随机抖动
        - 检测到验证码/风控后熔断一段时间，期间直接返回 None

        Args:
            endpoint: API 路径, 如 "aweme/v1/web/aweme/detail/"
            params: 参数字典
            method: GET 或 POST（JSON body）；传 form 时为 x-www-form-urlencoded POST，params 作为 query

        Returns:
            API 响应 JSON, 失败返回 None
        """
        if not self._page:
            self._log("浏览器未启动")
            return None
        if self.risk_status:
            self._log(f"风控冷却中，跳过请求: {self.risk_status}")
            return None

        if not self._sdk_ready:
            await self._wait_for_sdk(timeout=5)

        try:
            params = {**params, **(await self._client_params())}
        except Exception as e:
            self._log(f"读取浏览器环境失败（沿用调用方参数）: {e}")

        async with self._api_lock:
            wait = self._last_api_at + _API_MIN_INTERVAL + random.uniform(0, _API_JITTER) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._do_fetch(endpoint, params, method, form)
            finally:
                self._last_api_at = time.time()

    async def _do_fetch(self, endpoint: str, params: dict, method: str, form: dict | None = None) -> dict | None:
        base_url = "https://www.douyin.com/" + endpoint.lstrip("/")
        if form is not None:
            url, init = f"{base_url}?{urlencode(params)}", {
                "method": "POST",
                "headers": {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                "body": urlencode(form),
            }
        elif method.upper() == "POST":
            url, init = base_url, {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(params),
            }
        else:
            url, init = f"{base_url}?{urlencode(params)}", {}

        risk_headers = json.dumps(list(_RISK_HEADERS))
        js_code = f"""
        (async function() {{
            try {{
                // 没有超时的话，某个接口一直不返回会把整个签名引擎（持有请求锁）卡死
                const resp = await fetch({json.dumps(url)}, Object.assign({{credentials: 'include', signal: AbortSignal.timeout({_API_TIMEOUT * 1000})}}, {json.dumps(init)}));
                const text = await resp.text();
                const risk = {risk_headers}.filter(h => resp.headers.get(h));
                return {{ok: resp.ok, status: resp.status, body: text, risk}};
            }} catch(e) {{
                return {{ok: false, status: 0, error: e.message}};
            }}
        }})()
        """
        try:
            result = await asyncio.wait_for(self._page.evaluate(js_code), _API_TIMEOUT + 10)
        except asyncio.TimeoutError:
            self._log(f"API 请求超时（{_API_TIMEOUT + 10}s）: {endpoint}")
            return None
        except Exception as e:
            self._log(f"API 请求异常: {e}")
            return None

        body = (result or {}).get("body") or ""
        if (result or {}).get("risk") or any(m in body for m in _RISK_BODY_MARKS):
            self.trip_risk("接口要求验证（verify_check）")
            return None
        if not result or not result.get("ok"):
            self._log(f"API 请求失败: status={result and result.get('status')} {(result or {}).get('error') or body[:200]}")
            return None
        if not body:
            if await self.has_captcha():
                self.trip_risk("页面弹出验证码")
            else:
                self._log("API 返回空响应（可能触发风控，可尝试 /dy_login 登录或 /dy_restart）")
            return None
        try:
            return json.loads(body)
        except ValueError as e:
            self._log(f"API 响应不是 JSON: {e} {body[:200]}")
            return None

    async def refresh(self) -> bool:
        """重新加载 douyin.com 并等待 SDK，用于风控导致的空响应后重试。"""
        if not self._page or self.risk_status:
            return False
        try:
            await self._page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            self._log(f"刷新页面失败: {e}")
        self._sdk_ready = await self._wait_for_sdk(timeout=20)
        await self._set_idle(True)
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
            "platform": "PC",
            # 设备参数（屏幕/系统/浏览器版本等）由 fetch_signed_api 按浏览器真实环境填充
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
            "duration": int((aweme_detail.get("video") or {}).get("duration") or 0) // 1000,
            "media_urls": media_urls,
        }

    # ── Cookie 导出 ───────────────────────────────────────

    async def get_cookie_string(self) -> str:
        """导出所有 cookie 为 Python 可用的字符串, 用于下载媒体。"""
        if not self._context:
            return ""
        cookies = await self._context.cookies()
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async def _record_identity(self) -> None:
        """记下浏览器真实的 UA / Client Hints，纯 HTTP 请求统一用它（见 utils/client_identity）。"""
        try:
            ident = await self._page.evaluate("""() => ({ua: navigator.userAgent,
                brands: (navigator.userAgentData && navigator.userAgentData.brands) || [],
                platform: (navigator.userAgentData && navigator.userAgentData.platform) || ''})""")
            client_identity.save(ident["ua"], ident["brands"], ident["platform"])
        except Exception as e:
            self._log(f"读取浏览器身份失败: {e}")

    _ORIGIN_FETCH_JS = """async (r) => {
        try {
            const resp = await fetch(r.url, {method: r.method, credentials: 'include',
                headers: r.body !== null ? {'content-type': 'application/json'} : {},
                body: r.body !== null ? JSON.stringify(r.body) : undefined});
            return await resp.json();
        } catch (e) { return null; }
    }"""

    async def origin_fetch(self, origin: str, landing: str, url: str, method: str = "GET", body=None) -> dict | None:
        """
        在 origin 下的空白标签页里 fetch 接口，不打开业务页面。

        请求由真实 Chrome 发出：TLS 指纹、UA、Client Hints、cookie 都和用户自己浏览时一样，
        纯 Python 请求做不到这点（aiohttp 的 TLS 指纹一眼就能认出不是浏览器）。
        标签页停在 landing（该域名下一个返回 JSON 的接口）上，没有渲染开销，打开后复用。
        不能用 robots.txt 之类：抖音这些站点对未知路径一律返回整个前端应用，一直开着 CPU 很高。
        """
        if not self._context:
            return None
        page = self._origin_pages.get(origin)
        if page is None or page.is_closed():
            page = await self._context.new_page()
            try:
                await page.goto(origin + landing, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                await page.close()
                raise
            self._origin_pages[origin] = page
        return await asyncio.wait_for(
            page.evaluate(self._ORIGIN_FETCH_JS, {"url": url, "method": method, "body": body}), _API_TIMEOUT)

    async def cookies_for(self, url: str) -> str:
        """只导出会发给 url 的 cookie（按域名过滤），用于纯 HTTP 请求。"""
        if not self._context:
            return ""
        cookies = await self._context.cookies(url)
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
        """
        在 douyin.com 页面里发 x-www-form-urlencoded POST（评论发布等写接口），SDK 自动签名。
        和读接口走同一套：真实设备参数、请求节流、风控检测与熔断。
        """
        if self.risk_status:
            return {"status_code": -1, "status_msg": f"风控冷却中：{self.risk_status}"}
        data = await self.fetch_signed_api(endpoint, query, form=form)
        if data is None:
            reason = self.risk_status or "空响应（未登录或触发风控）"
            return {"status_code": -1, "status_msg": reason}
        return data

    # ── 内部方法 ─────────────────────────────────────────

    def _make_status(self, ok: bool, reason: str, **extra) -> dict:
        return {
            "ok": ok,
            "reason": reason,
            "logged_in": self._logged_in,
            "need_qr_login": not self._logged_in and ok,
            **extra,
        }

    async def _set_idle(self, on: bool) -> bool:
        """
        主页面空闲时停掉渲染：抖音首页的推荐视频、Lottie 动画会让渲染进程常驻 40%~100% CPU，
        而签名只需要页面里的 SDK 脚本。隐藏整页 + 停动画后约 10%，签名与页面内 fetch 不受影响。
        需要看页面（扫码截图、验证码检测）时先恢复显示。返回之前是否处于空闲状态。
        """
        if not self._low_cpu or not self._page:
            return False
        try:
            return bool(await self._page.evaluate(_IDLE_JS, on))
        except Exception:
            return False

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
