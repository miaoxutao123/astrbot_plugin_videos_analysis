from .base import Comment, CreatorPlatform, OpResult, Work
from .douyin import DouyinCreator

REGISTRY: dict[str, type[CreatorPlatform]] = {
    "douyin": DouyinCreator,
}
ALIASES = {"dy": "douyin", "抖音": "douyin"}


def normalize_platform(name: str) -> str | None:
    name = (name or "").strip().lower()
    name = ALIASES.get(name, name)
    return name if name in REGISTRY else None


def get_creator(plugin, platform: str) -> CreatorPlatform:
    return REGISTRY[platform](plugin)


__all__ = ["Comment", "CreatorPlatform", "OpResult", "Work", "REGISTRY", "get_creator", "normalize_platform"]
