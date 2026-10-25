from .base import Comment, CreatorPlatform, OpResult, Work
from .bilibili import BilibiliCreator
from .douyin import DouyinCreator

REGISTRY: dict[str, type[CreatorPlatform]] = {
    "douyin": DouyinCreator,
    "bilibili": BilibiliCreator,
}
ALIASES = {"dy": "douyin", "抖音": "douyin", "bili": "bilibili", "b站": "bilibili", "B站": "bilibili"}


def normalize_platform(name: str) -> str | None:
    name = (name or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in REGISTRY else None


def get_creator(plugin, platform: str, account: str | None = None) -> CreatorPlatform:
    """account：账号池里的账号名，None 表示当前使用中的账号（抖音只能用当前账号）。"""
    if platform == "bilibili":
        return BilibiliCreator(plugin, account)
    return REGISTRY[platform](plugin)


__all__ = ["Comment", "CreatorPlatform", "OpResult", "Work", "REGISTRY", "get_creator", "normalize_platform"]
