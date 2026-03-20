"""
抖音登录模块 — 全代理架构

支持两种登录方式:
1. Playwright 浏览器扫码登录
   - 所有网络请求通过 httpx 全代理，解决浏览器无法直连抖音服务器的问题
   - 预下载页面所有 JS/CSS 资源，保证 QR 码渲染
2. 手动粘贴 cookie 字符串

cookie 保存/加载功能支持持久化。
"""

import asyncio
import base64
import json
import os
import re
from io import BytesIO

import aiofiles
import httpx

from astrbot.api import logger

from .douyin_scraper.cookie_extractor import extract_and_format_cookies, extract_douyin_cookies

# 路径常量
PLUGIN_DATA_DIR = "data/plugins/astrbot_plugin_videos_analysis"
DOUYIN_COOKIE_FILE = f"{PLUGIN_DATA_DIR}/douyin_cookies.json"
DOUYIN_QR_IMAGE_DIR = f"{PLUGIN_DATA_DIR}/image"

# Cookie有效性缓存（同 bili_get.py 的 COOKIE_VALID）
DOUYIN_COOKIE_VALID = None

# 监控/跟踪域名（直接返回空内容，加速页面加载）
_BLOCK_DOMAINS = (
    "mon.zijieapi.com",
    "ibytedapm.com",
    "mcs.zijieapi.com",
    "frontier-oversea.byteoversea.net",
    "log-api.snssdk.com",
    "analytics.tiktok.com",
)

# httpx 公共 User-Agent
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
    "Gecko/20100101 Firefox/131.0"
)

# SSO 页面 URL
_SSO_URL = (
    "https://sso.douyin.com/get_qrcode/?"
    "service=https://www.douyin.com"
    "&need_logo=false&aid=6383"
    "&account_sdk_source=sso"
    "&sdk_version=2.2.7-beta.6&language=zh"
)

# 进程级资源缓存: url → (bytes, content_type)
_resource_cache: dict[str, tuple[bytes, str]] = {}


def _clear_resource_cache() -> None:
    """清空资源缓存，支持重试时使用干净缓存"""
    _resource_cache.clear()


# ==================== httpx 工具 ====================

async def _httpx_get(
    url: str,
    headers: dict | None = None,
    timeout: int = 25,
    max_retries: int = 3,
) -> httpx.Response | None:
    """带重试的 httpx GET 请求"""
    _headers = {"User-Agent": _UA}
    if headers:
        _headers.update(headers)

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers=_headers)
                return resp
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.debug(
                f"httpx 请求失败 (尝试{attempt+1}/{max_retries}): "
                f"{type(e).__name__} → {url[:80]}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
        except Exception as e:
            logger.debug(f"httpx 未知错误: {type(e).__name__}: {e}")
            break
    return None


async def _httpx_request_like_browser(
    method: str,
    url: str,
    headers: dict | None = None,
    body: bytes | None = None,
) -> tuple[int, dict, bytes] | None:
    """模拟浏览器请求，返回 (status, headers_dict, body)"""
    _headers = {"User-Agent": _UA}
    if headers:
        _headers.update(headers)

    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=True
        ) as client:
            if method.upper() == "POST":
                resp = await client.post(url, headers=_headers, content=body)
            else:
                resp = await client.get(url, headers=_headers)
            resp_headers = dict(resp.headers)
            return resp.status_code, resp_headers, resp.content
    except Exception:
        return None


# ==================== 资源预下载 ====================

def _normalize_url(url: str) -> str:
    """把 // 开头的 URL 补全为 https://"""
    if url.startswith("//"):
        return "https:" + url
    return url


async def _prefetch_all_resources() -> bytes | None:
    """
    预下载 SSO 页面 HTML 及其引用的所有 JS/CSS 资源。

    Returns:
        SSO 页面 HTML 字节流，失败返回 None
    """
    logger.info("正在预下载抖音登录页面及资源...")

    resp = await _httpx_get(_SSO_URL, max_retries=5)
    if not resp or resp.status_code != 200:
        logger.error("获取抖音SSO页面失败")
        return None

    html_bytes = resp.content
    html_text = resp.text
    ct = resp.headers.get("content-type", "text/html; charset=utf-8")
    _resource_cache[_SSO_URL] = (html_bytes, ct)
    logger.info(f"SSO 页面获取成功 ({len(html_bytes)} bytes)")

    # 提取所有 JS/CSS/资源 URL
    # 策略：从整个 HTML 文本中提取所有看起来像 CDN 资源的 URL
    urls = set()

    # 1. 匹配 src="..." 和 href="..." 属性中的 JS/CSS
    for match in re.findall(
        r'(?:src|href)="((?:https?:)?//[^"]+\.(?:js|css)[^"]*)"', html_text
    ):
        urls.add(_normalize_url(match))

    # 2. 匹配 JS 字符串中的 URL（覆盖 srcList 数组等）
    #    例如: "https://...bytegoofy.com/.../secsdk-lastest.umd.js"
    for match in re.findall(
        r'"((?:https?:)?//[^"]+\.(?:js|css)(?:\?[^"]*)?)"', html_text
    ):
        urls.add(_normalize_url(match))

    # 3. 匹配 window.__publicUrl__ 并找到相关资源
    pub_match = re.search(
        r"window\.__publicUrl__\s*=\s*'(//[^']+)'", html_text
    )
    if pub_match:
        # 从 HTML 中找 static/js/*.js 和 static/css/*.css
        for path_match in re.findall(
            r'(?:src|href)="(//[^"]+/static/(?:js|css)/[^"]+)"', html_text
        ):
            urls.add(_normalize_url(path_match))

    # 过滤掉 data: URL 和过短的 URL
    urls = {u for u in urls if u.startswith("https://") and len(u) > 20}

    logger.info(f"发现 {len(urls)} 个外部资源需要预下载")

    # 并发下载所有资源（增大并发数和重试次数）
    sem = asyncio.Semaphore(8)
    failed_urls = []

    async def _fetch_one(url: str) -> None:
        async with sem:
            if url in _resource_cache:
                return
            r = await _httpx_get(url, max_retries=4, timeout=30)
            if r and r.status_code == 200:
                c = r.headers.get("content-type", "application/javascript")
                _resource_cache[url] = (r.content, c)
                logger.debug(
                    f"预缓存: {url.split('/')[-1][:50]} ({len(r.content)} bytes)"
                )
            else:
                failed_urls.append(url)

    await asyncio.gather(*[_fetch_one(u) for u in urls])
    cached = len(_resource_cache) - 1  # exclude the HTML itself
    logger.info(f"预下载完成: {cached}/{len(urls)} 个资源已缓存")
    if failed_urls:
        for fu in failed_urls:
            logger.warning(f"资源预下载失败: {fu.split('/')[-1][:60]}")

    return html_bytes


# ==================== 全代理路由 ====================

# 标记 QR 码是否已截图（截图前阻止页面跳转）
_qr_captured = False


async def _full_proxy_route_handler(route) -> None:
    """
    Playwright 路由处理：拦截所有请求，通过 httpx 全代理。

    - 缓存命中 → 直接返回
    - 监控域名 → 返回空内容
    - 导航到非SSO页面（QR未截图时）→ 阻止跳转
    - 其他请求 → httpx 代理
    - 代理失败 → 返回空响应（不阻塞页面）
    """
    url = route.request.url

    # 1. 缓存命中
    if url in _resource_cache:
        body, ct = _resource_cache[url]
        await route.fulfill(body=body, content_type=ct, status=200)
        return

    # 2. 拦截监控域名
    if any(d in url for d in _BLOCK_DOMAINS):
        await route.fulfill(
            body=b"", content_type="application/javascript", status=200
        )
        return

    # 3. 拦截 data: URL（浏览器内部生成的，直接放行）
    if url.startswith("data:"):
        try:
            await route.continue_()
        except Exception:
            await route.fulfill(body=b"", status=200)
        return

    # 4. 通过 httpx 代理请求
    method = route.request.method
    req_headers = {}
    # 传递部分浏览器请求头
    for key in ("accept", "accept-language", "content-type", "referer", "cookie"):
        val = route.request.headers.get(key)
        if val:
            req_headers[key] = val

    result = await _httpx_request_like_browser(
        method, url, headers=req_headers,
        body=route.request.post_data_buffer if method == "POST" else None,
    )

    if result:
        status, resp_headers, body = result
        ct = resp_headers.get("content-type", "application/octet-stream")
        # 缓存成功的 GET 请求
        if method == "GET" and status == 200 and len(body) > 0:
            _resource_cache[url] = (body, ct)

        # 构造响应头
        fulfill_headers = {}
        for k in ("access-control-allow-origin", "access-control-allow-credentials",
                   "access-control-allow-headers", "access-control-allow-methods",
                   "set-cookie"):
            if k in resp_headers:
                fulfill_headers[k] = resp_headers[k]

        await route.fulfill(
            body=body,
            content_type=ct,
            status=status,
            headers=fulfill_headers if fulfill_headers else None,
        )
    else:
        # 代理失败，返回空响应（避免阻塞页面）
        logger.debug(f"代理失败（返回空响应）: {url[:80]}")
        await route.fulfill(body=b"", status=200)


# ==================== Playwright 扫码登录 ====================

async def _cleanup_playwright(pw, browser=None) -> None:
    """安全清理 Playwright 资源。"""
    try:
        if browser:
            await browser.close()
    except Exception:
        pass
    try:
        await pw.stop()
    except Exception:
        pass


async def generate_douyin_qrcode_playwright(timeout_seconds: int = 180) -> dict | None:
    """
    使用 Playwright 打开抖音 SSO 登录页面，等待 QR 码渲染后截图。

    通过全代理架构，所有请求通过 httpx 转发，解决浏览器网络不可达问题。
    内置重试机制：网络不稳定时自动重试最多3次。

    Returns:
        包含 screenshot_path 和 browser/context/page 句柄的字典，失败返回 None。
        调用方需在登录完成后关闭 browser。
    """
    global _qr_captured
    _qr_captured = False

    # ---- 阶段 0: 外层重试（网络不稳定时最多重试 3 次）----
    for retry_round in range(3):
        if retry_round > 0:
            _clear_resource_cache()
            logger.info(f"第 {retry_round + 1} 次重试：清除缓存并重新预下载...")
            await asyncio.sleep(5)

        result = await _generate_qrcode_inner(
            timeout_seconds=timeout_seconds
        )
        if result is not None:
            return result

        logger.warning(f"QR码生成失败（第 {retry_round + 1}/3 次）")

    logger.error("QR码生成失败（3次重试均失败）")
    return None


async def _generate_qrcode_inner(timeout_seconds: int = 180) -> dict | None:
    """QR码生成内部实现（单次尝试）"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error(
            "playwright 未安装，无法使用扫码登录。"
            "请运行: pip install playwright && playwright install chromium"
        )
        return None

    # ---- 阶段 1: 预下载所有资源 ----
    html_bytes = await _prefetch_all_resources()
    if not html_bytes:
        logger.error("预下载 SSO 资源失败")
        return None

    # ---- 阶段 2: 启动浏览器 ----
    pw_obj = await async_playwright().start()

    browser = None
    for browser_type_name in ("chromium", "firefox"):
        try:
            bt = getattr(pw_obj, browser_type_name)
            launch_args = {"headless": True}
            if browser_type_name == "chromium":
                launch_args["args"] = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ]
            browser = await bt.launch(**launch_args)
            logger.info(f"浏览器已启动: {browser_type_name}")
            break
        except Exception as e:
            logger.warning(f"{browser_type_name} 启动失败: {e}")

    if not browser:
        logger.error("所有浏览器引擎均启动失败")
        await pw_obj.stop()
        return None

    try:
        context = await browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 720},
            locale="zh-CN",
        )
        page = await context.new_page()

        # ---- 阶段 3: 注册全代理路由 → 导航 ----
        await page.route("**/*", _full_proxy_route_handler)

        logger.info("正在打开抖音登录页面（全代理模式）...")

        nav_ok = False
        for attempt in range(3):
            try:
                await page.goto(_SSO_URL, timeout=90000, wait_until="commit")
                nav_ok = True
                logger.info("页面导航成功")
                break
            except Exception as e:
                logger.warning(
                    f"导航尝试 {attempt+1}/3 失败: {type(e).__name__}"
                )
                if attempt < 2:
                    await asyncio.sleep(3)

        if not nav_ok:
            logger.error("页面导航失败（3次重试均超时）")
            await _cleanup_playwright(pw_obj, browser)
            return None

        # ---- 阶段 4: 等待 QR 码渲染 ----
        # SSO 页面渲染 QR 码后可能自动跳转到 www.douyin.com/login_page。
        # 策略：每秒截图一次，一旦截图文件 > 20KB（包含渲染内容），
        # 立即认为 QR 码已渲染。DOM查询可能因跳转而失败，不影响结果。
        os.makedirs(DOUYIN_QR_IMAGE_DIR, exist_ok=True)
        screenshot_path = os.path.join(
            DOUYIN_QR_IMAGE_DIR, "douyin_login_qrcode.png"
        )

        qr_found = False
        last_screenshot_size = 0

        for i in range(timeout_seconds):
            await asyncio.sleep(1)

            # 每秒截图一次
            try:
                await page.screenshot(path=screenshot_path, timeout=5000)
                sz = os.path.getsize(screenshot_path)
                if sz != last_screenshot_size:
                    last_screenshot_size = sz
                    # 渲染好的 QR 码页面截图通常 > 20KB
                    if sz > 20000:
                        qr_found = True
                        logger.info(
                            f"抖音登录二维码已渲染 (截图大小={sz} bytes)"
                        )
                        break
            except Exception:
                # 页面可能已跳转，检查最后的截图
                if (
                    os.path.exists(screenshot_path)
                    and os.path.getsize(screenshot_path) > 20000
                ):
                    qr_found = True
                    logger.info("使用跳转前保存的截图")
                    break

            # 尝试 DOM 检测（可能失败，不影响结果）
            try:
                qr_img = await page.query_selector('img[src*="data:image"]')
                if qr_img:
                    qr_found = True
                    logger.info("抖音登录二维码已渲染（DOM检测）")
                    try:
                        await page.screenshot(
                            path=screenshot_path, timeout=5000
                        )
                    except Exception:
                        pass
                    break
            except Exception:
                pass  # DOM查询可能因跳转失败

            if i > 0 and i % 15 == 0:
                try:
                    html_len = len(await page.content())
                except Exception:
                    html_len = -1
                logger.debug(
                    f"等待 QR 码渲染... ({i}s, 截图={last_screenshot_size}B, html={html_len})"
                )


        if not qr_found:
            logger.error("抖音登录页面加载超时，QR码未渲染。")
            # 保存调试截图
            try:
                os.makedirs(DOUYIN_QR_IMAGE_DIR, exist_ok=True)
                debug_path = os.path.join(DOUYIN_QR_IMAGE_DIR, "debug_page.png")
                await page.screenshot(path=debug_path, timeout=10000)
                logger.info(f"调试截图已保存: {debug_path}")
            except Exception:
                pass
            await _cleanup_playwright(pw_obj, browser)
            return None

        # ---- 阶段 5: 标记 QR 已截图，返回结果 ----
        _qr_captured = True

        return {
            "screenshot_path": screenshot_path,
            "_pw": pw_obj,
            "_browser": browser,
            "_context": context,
            "_page": page,
        }

    except Exception as e:
        logger.error(f"Playwright 抖音登录异常: {e}")
        await _cleanup_playwright(pw_obj, browser)
        return None


async def douyin_login_loop_playwright(
    pw_data: dict, timeout_seconds: int = 120
) -> str | None:
    """
    等待用户扫码，监测登录状态（URL 变化或 cookie 出现）。

    Args:
        pw_data: generate_douyin_qrcode_playwright 返回的字典
        timeout_seconds: 最长等待时间

    Returns:
        成功时返回格式化的 cookie 字符串
    """
    page = pw_data["_page"]
    context = pw_data["_context"]
    browser = pw_data["_browser"]
    pw = pw_data["_pw"]

    try:
        logger.info(f"等待抖音扫码登录...（最多{timeout_seconds}秒）")

        for i in range(timeout_seconds):
            await asyncio.sleep(1)

            # 检查 URL 是否跳转到 douyin.com（登录成功）
            current_url = page.url
            if (
                "www.douyin.com" in current_url
                and "sso.douyin.com" not in current_url
                and "login_page" not in current_url
                and "login" not in current_url.split("?")[0]
            ):
                logger.info(f"抖音登录成功，已跳转到: {current_url}")
                break

            # 检查 cookie 是否包含登录标识
            cookies = await context.cookies()
            cookie_names = {c["name"] for c in cookies}
            if any(
                k in cookie_names
                for k in ("sessionid", "sessionid_ss", "uid_tt")
            ):
                logger.info("检测到抖音登录 cookie")
                break

            if i % 15 == 0 and i > 0:
                logger.info(f"仍在等待抖音扫码确认... ({i}s)")
        else:
            logger.info("抖音扫码登录超时")
            return None

        # 等待 cookie 完全设置
        await asyncio.sleep(2)
        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        if not cookie_dict:
            logger.warning("登录跳转成功但未提取到 cookie")
            return None

        # 保存并格式化
        await save_douyin_cookies(cookie_dict)
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
        formatted = extract_and_format_cookies(cookie_str)
        return formatted

    finally:
        await _cleanup_playwright(pw, browser)


# ==================== 手动 Cookie 粘贴 ====================

def _try_parse_json_cookies(raw: str) -> str | None:
    """
    尝试将 JSON 数组格式的 cookie 转换为标准 name=value; 字符串。

    支持浏览器 Cookie 编辑器导出的格式：
    [{"name": "sessionid", "value": "xxx", ...}, ...]

    Returns:
        转换后的 cookie 字符串，非 JSON 格式返回 None
    """
    raw = raw.strip()
    if not raw.startswith("["):
        return None

    try:
        cookies_list = json.loads(raw)
        if not isinstance(cookies_list, list):
            return None

        pairs = []
        for item in cookies_list:
            if isinstance(item, dict):
                name = item.get("name", "").strip()
                value = item.get("value", "")
                if name:  # 跳过空 name 的条目
                    pairs.append(f"{name}={value}")

        if not pairs:
            return None

        cookie_str = "; ".join(pairs)
        logger.info(f"已从 JSON 数组格式解析出 {len(pairs)} 个 cookie 字段")
        return cookie_str

    except (json.JSONDecodeError, TypeError):
        return None


async def save_manual_douyin_cookie(cookie_string: str) -> str | None:
    """
    解析用户手动粘贴的 cookie，自动提取抖音所需的 12 个关键字段并保存。

    支持两种输入格式：
    1. 标准字符串: name=value; name2=value2; ...
    2. JSON 数组: [{"name": "xxx", "value": "yyy"}, ...]（Cookie 编辑器导出）

    Args:
        cookie_string: cookie 字符串或 JSON 数组

    Returns:
        格式化的 cookie 字符串，失败返回 None
    """
    cookie_string = cookie_string.strip()
    if not cookie_string:
        logger.error("cookie 输入为空")
        return None

    # 自动检测 JSON 数组格式（Cookie 编辑器导出）
    json_converted = _try_parse_json_cookies(cookie_string)
    if json_converted:
        cookie_string = json_converted
    elif "=" not in cookie_string:
        logger.error("cookie 格式无效：既非 name=value 字符串也非 JSON 数组")
        return None

    # 使用 extract_douyin_cookies 自动提取 12 个所需字段
    formatted, is_valid, extracted = extract_douyin_cookies(cookie_string)

    # 构建只包含有效值的字典（过滤掉缺失的 "xxx" 占位值）
    clean_dict = {k: v for k, v in extracted.items() if v != "xxx" and v}

    if not clean_dict:
        logger.error("未从 cookie 中提取出任何有效的抖音字段")
        return None

    # 统计并记录结果
    found_fields = list(clean_dict.keys())
    missing_fields = [k for k, v in extracted.items() if v == "xxx" or not v]

    if is_valid:
        logger.info(
            f"cookie 验证通过，已提取 {len(found_fields)}/12 个字段: "
            f"{', '.join(found_fields)}"
        )
    else:
        logger.warning(
            f"cookie 已提取 {len(found_fields)}/12 个字段，"
            f"缺少: {', '.join(missing_fields)}（部分功能可能受限）"
        )

    # 仅保存提取后的关键字段（而非原始的几十个字段）
    await save_douyin_cookies(clean_dict)
    return formatted


# ==================== Cookie 持久化 ====================

async def save_douyin_cookies(cookies: dict) -> bool:
    """保存cookie到JSON文件"""
    try:
        os.makedirs(os.path.dirname(DOUYIN_COOKIE_FILE), exist_ok=True)
        async with aiofiles.open(DOUYIN_COOKIE_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(cookies, ensure_ascii=False, indent=2))
        logger.info(f"抖音cookie已保存到: {DOUYIN_COOKIE_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存抖音cookie失败: {e}")
        return False


async def load_douyin_cookies() -> str | None:
    """
    从文件加载cookie。

    Returns:
        格式化的 cookie 字符串，如果文件不存在或无效返回 None
    """
    if not os.path.exists(DOUYIN_COOKIE_FILE):
        return None

    try:
        async with aiofiles.open(DOUYIN_COOKIE_FILE, encoding="utf-8") as f:
            content = await f.read()
            if not content.strip():
                return None
            cookies = json.loads(content)
            if not isinstance(cookies, dict) or not cookies:
                return None

            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            formatted = extract_and_format_cookies(cookie_str)

            _, is_valid, _ = extract_douyin_cookies(cookie_str)

            if not is_valid:
                logger.warning("抖音cookie文件中缺少关键字段，可能已失效")

            return formatted

    except json.JSONDecodeError:
        logger.error("抖音cookie文件格式错误")
        return None
    except Exception as e:
        logger.error(f"加载抖音cookie失败: {e}")
        return None


def save_qrcode_image(image_base64: str) -> str:
    """保存二维码图片到本地"""
    os.makedirs(DOUYIN_QR_IMAGE_DIR, exist_ok=True)
    image_path = os.path.join(DOUYIN_QR_IMAGE_DIR, "douyin_login_qrcode.png")
    with open(image_path, "wb") as f:
        f.write(base64.b64decode(image_base64))
    return image_path


# ==================== Cookie 有效性检查 ====================

async def check_douyin_cookie_valid() -> bool:
    """
    检查抖音Cookie是否有效（类似 bili_get.py 的 check_cookie_valid）。

    策略：用保存的cookie请求抖音用户页面，根据响应判断登录状态。

    Returns:
        True = cookie有效, False = 无效或不存在
    """
    global DOUYIN_COOKIE_VALID

    # 先加载cookie
    cookie_str = await load_douyin_cookies()
    if not cookie_str:
        logger.debug("抖音cookie不存在或为空")
        DOUYIN_COOKIE_VALID = False
        return False

    # 检查关键字段
    _, is_valid, extracted = extract_douyin_cookies(cookie_str)
    if not is_valid:
        logger.debug("抖音cookie缺少关键字段")
        DOUYIN_COOKIE_VALID = False
        return False

    # 用cookie请求抖音API验证登录状态
    check_url = "https://www.douyin.com/aweme/v1/web/query/user/"
    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie_str,
    }

    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False
        ) as client:
            resp = await client.get(check_url, headers=headers)

            # 200且返回有效JSON = cookie可用
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # 一般有效cookie会返回含data字段的响应
                    if data.get("status_code") == 0 or "data" in data:
                        logger.info("抖音cookie验证通过")
                        DOUYIN_COOKIE_VALID = True
                        return True
                except Exception:
                    pass

            # 备用检测：尝试访问用户主页
            resp2 = await client.get(
                "https://www.douyin.com/",
                headers=headers,
            )
            # 如果cookie中有sessionid且访问正常，认为有效
            if resp2.status_code == 200 and "sessionid" in cookie_str:
                logger.info("抖音cookie验证通过（会话检测）")
                DOUYIN_COOKIE_VALID = True
                return True

    except Exception as e:
        logger.debug(f"抖音cookie验证异常: {type(e).__name__}: {e}")

    logger.info("抖音cookie验证失败，可能已过期")
    DOUYIN_COOKIE_VALID = False
    return False
