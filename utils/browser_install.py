"""
浏览器自动安装：Playwright 的 Chromium 走测速后最快的源，正式版 Google Chrome 的安装包缓存到数据目录。

实测（2026-10，国内宽带）：
- cdn.playwright.dev（微软 CDN）直连/走代理都只有约 0.12 MB/s，Chromium 约 170MB 要二十多分钟
- cdn.npmmirror.com/binaries/playwright 约 9~12 MB/s，文件和官方一致（Playwright 安装时会校验）
- dl.google.com 的 Chrome 安装包约 0.2 MB/s，国内没有镜像；所以下载一次就缓存在数据目录，
  容器重建后从缓存安装，不用再等
"""

import asyncio
import os
import sys
import time

import aiohttp

from astrbot.api import logger

from .paths import DATA_DIR

OFFICIAL_HOST = "https://cdn.playwright.dev"
MIRROR_HOSTS = ("https://cdn.npmmirror.com/binaries/playwright",)
# 测速用的小文件（ffmpeg 约 2MB），各源路径一致
_PROBE_PATH = "/builds/ffmpeg/1013/ffmpeg-linux.zip"
CHROME_DEB_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
CACHE_DIR = os.path.join(DATA_DIR, "browser_cache")
CHROME_DEB = os.path.join(CACHE_DIR, "google-chrome-stable_current_amd64.deb")
CHROME_DEB_MAX_AGE = 14 * 86400  # 缓存超过两周就重新下载，跟上 Chrome 正式版


async def _speed(host: str, seconds: float = 4.0) -> float:
    """下载几秒测速（字节/秒），失败返回 0。"""
    got, start = 0, time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=seconds + 4, sock_connect=4)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
            async with s.get(host + _PROBE_PATH, headers={"Range": "bytes=0-3000000"}) as r:
                if r.status not in (200, 206):
                    return 0.0
                async for chunk in r.content.iter_chunked(65536):
                    got += len(chunk)
                    if time.monotonic() - start > seconds:
                        break
    except Exception:
        pass
    return got / max(time.monotonic() - start, 0.1)


async def pick_download_host() -> str | None:
    """返回最快的 PLAYWRIGHT_DOWNLOAD_HOST；用户自己设置过环境变量时尊重它（返回 None）。"""
    if os.environ.get("PLAYWRIGHT_DOWNLOAD_HOST") or os.environ.get("PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST"):
        return None
    hosts = (OFFICIAL_HOST, *MIRROR_HOSTS)
    speeds = await asyncio.gather(*(_speed(h) for h in hosts))
    best = max(zip(speeds, hosts))
    logger.info("[setup] 浏览器下载源测速: " + "，".join(f"{h} {s / 1e6:.1f}MB/s" for s, h in zip(speeds, hosts)))
    return None if best[1] == OFFICIAL_HOST or best[0] <= 0 else best[1]


async def _run(*args: str, env: dict | None = None, timeout: int = 1200) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "超时"
    return proc.returncode, out.decode(errors="ignore")


async def install_chromium() -> tuple[bool, str]:
    """playwright install chromium：选最快的源，不装用不到的 headless shell（省约 100MB）。"""
    env = dict(os.environ)
    if host := await pick_download_host():
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = host
        logger.info(f"[setup] 使用镜像下载 Chromium: {host}")
    code, out = await _run(sys.executable, "-m", "playwright", "install", "--no-shell", "chromium", env=env)
    return code == 0, out[-500:]


async def _download(url: str, dest: str) -> bool:
    """断点续传下载到 dest（先写 .part，完成后改名）。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    timeout = aiohttp.ClientTimeout(total=3600, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as s:
        async with s.get(url, headers=headers) as r:
            if r.status == 416:  # 已经下完
                os.replace(part, dest)
                return True
            if r.status not in (200, 206):
                logger.warning(f"[setup] 下载 {url} 失败: HTTP {r.status}")
                return False
            with open(part, "ab" if r.status == 206 else "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
    os.replace(part, dest)
    return True


async def install_chrome() -> tuple[bool, str]:
    """
    安装正式版 Google Chrome（Debian/Ubuntu）。安装包缓存在数据目录，下次（含容器重建后）直接用。
    缺系统依赖时 apt-get update 后重试一次。
    """
    fresh = os.path.exists(CHROME_DEB) and time.time() - os.path.getmtime(CHROME_DEB) < CHROME_DEB_MAX_AGE
    if not fresh:
        logger.info("[setup] 下载 Google Chrome 安装包（约 120MB，dl.google.com 国内较慢，约 10 分钟，只需一次）...")
        try:
            if not await _download(CHROME_DEB_URL, CHROME_DEB):
                return False, "下载 Chrome 安装包失败"
        except Exception as e:
            return False, f"下载 Chrome 安装包失败: {e}"
    deb = os.path.abspath(CHROME_DEB)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    code, out = await _run("apt-get", "install", "-y", "--no-install-recommends", deb, env=env)
    if code != 0:
        await _run("apt-get", "update", env=env)
        code, out = await _run("apt-get", "install", "-y", "--no-install-recommends", deb, env=env)
    if code != 0:
        # 安装包可能损坏，删掉下次重新下载
        try:
            os.remove(CHROME_DEB)
        except OSError:
            pass
    return code == 0, out[-500:]
