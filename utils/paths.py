"""
插件持久化数据路径

账号凭据、浏览器配置、下载缓存统一放在 data/plugin_data/<插件名>/ 下，
避免插件更新/重装（会覆盖 data/plugins/<插件名>/）时丢失登录状态。
"""

import os
import shutil

from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_videos_analysis"
LEGACY_DIR = f"data/plugins/{PLUGIN_NAME}"
DATA_DIR = f"data/plugin_data/{PLUGIN_NAME}"

ACCOUNTS_DIR = f"{DATA_DIR}/accounts"
BILI_COOKIE_FILE = f"{ACCOUNTS_DIR}/bili_cookies.json"
DOUYIN_COOKIE_FILE = f"{ACCOUNTS_DIR}/douyin_cookies.json"
DOUYIN_BROWSER_PROFILE = f"{ACCOUNTS_DIR}/dy_browser_profile"

DOWNLOAD_DIR = f"{DATA_DIR}/download_videos"
DOWNLOAD_DIR_DY = f"{DOWNLOAD_DIR}/dy"
DOWNLOAD_DIR_BILI = f"{DOWNLOAD_DIR}/bili"
DOWNLOAD_DIR_DIRECT = f"{DOWNLOAD_DIR}/direct"
IMAGE_DIR = f"{DATA_DIR}/image"

_LEGACY_MOVES = {
    f"{LEGACY_DIR}/bili_cookies.json": BILI_COOKIE_FILE,
    f"{LEGACY_DIR}/douyin_cookies.json": DOUYIN_COOKIE_FILE,
    f"{LEGACY_DIR}/dy_browser_profile": DOUYIN_BROWSER_PROFILE,
}


def migrate_legacy_data() -> None:
    """把旧版本存放在插件目录里的账号数据迁移到 plugin_data（只迁移一次，不覆盖）。"""
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    for old, new in _LEGACY_MOVES.items():
        if os.path.exists(old) and not os.path.exists(new):
            try:
                shutil.move(old, new)
                logger.info(f"[videos_analysis] 已迁移旧数据 {old} -> {new}")
            except OSError as e:
                logger.warning(f"[videos_analysis] 迁移 {old} 失败: {e}")
