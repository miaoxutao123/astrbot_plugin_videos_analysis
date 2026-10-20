"""
创作者操作的平台无关接口

新平台（B站、小红书…）只需实现 CreatorPlatform 并在 creator/__init__.py 的 REGISTRY 里登记，
命令、二次确认、评论挑选与 LLM 起草都复用 services/creator_ops.py 里的通用流程。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Work:
    id: str
    title: str
    create_time: int = 0
    digg: int = 0
    comments: int = 0
    url: str = ""


@dataclass
class Comment:
    id: str
    text: str
    digg: int
    create_time: int
    user: str
    user_id: str = ""
    handle: str = ""  # 平台账号 ID（抖音号等），用于按人指定回复


@dataclass
class OpResult:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)
    screenshot: str = ""  # 浏览器自动化失败时的截图，便于排查页面改版


class CreatorPlatform(ABC):
    name = ""
    label = ""

    def __init__(self, plugin):
        self.plugin = plugin

    @abstractmethod
    async def check_login(self) -> tuple[bool, str]:
        """创作者后台是否处于登录状态。"""

    @abstractmethod
    async def list_works(self, limit: int = 10) -> list[Work]:
        """当前账号最近发布的作品（新→旧）。"""

    @abstractmethod
    async def resolve_work(self, ref: str) -> str | None:
        """把链接 / ID 解析成作品 ID。"""

    @abstractmethod
    async def work_context(self, work_id: str) -> dict:
        """作品标题/文案等，供 LLM 回复时参考：{"title", "desc", "media_url"}。"""

    @abstractmethod
    async def upload(self, video_path: str, title: str, tags: list[str]) -> OpResult:
        """发布视频（危险操作，只能由二次确认流程调用）。"""

    @abstractmethod
    async def delete(self, work_id: str, title: str = "") -> OpResult:
        """删除作品（危险操作，只能由二次确认流程调用）。"""

    @abstractmethod
    async def list_comments(self, work_id: str, max_count: int = 60) -> list[Comment]:
        """作品的一级评论。"""

    @abstractmethod
    async def reply_comment(self, work_id: str, comment_id: str, text: str) -> OpResult:
        """回复一条评论（只能由二次确认流程调用）。"""
