"""
测试环境：
- 仓库目录名不一定是 astrbot_plugin_videos_analysis（比如 *-fix），手动把它注册成这个包
- 没装 AstrBot 时提供最小的替身模块（logger、消息组件、MessageChain），装了就用真的
- 插件的数据路径都是相对 cwd 的 data/plugin_data/...，整个测试会话切到临时目录，不碰真实数据
"""

import importlib.machinery
import logging
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "astrbot_plugin_videos_analysis"

os.chdir(tempfile.mkdtemp(prefix="va-test-"))


def _stub_astrbot() -> None:
    try:
        import astrbot.api  # noqa: F401
        return
    except ImportError:
        pass

    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class _Seg:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
            self.chain = kwargs.get("chain", [])

    class Image(_Seg):
        @classmethod
        def fromFileSystem(cls, path):
            return cls(file=path)

        async def convert_to_file_path(self):
            return self.kwargs.get("file", "")

    class Reply(_Seg):
        pass

    class Plain(_Seg):
        pass

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(text)
            return self

    comps = dict(Image=Image, Reply=Reply, Plain=Plain, Video=_Seg, File=_Seg, Record=_Seg, Node=_Seg, Nodes=_Seg, At=_Seg)
    mod("astrbot")
    mod("astrbot.api", logger=logging.getLogger("astrbot"))
    mod("astrbot.api.message_components", **comps)
    mod("astrbot.core")
    mod("astrbot.core.message")
    mod("astrbot.core.message.components", **comps)
    mod("astrbot.core.message.message_event_result", MessageChain=MessageChain)


def _register_package() -> None:
    if PKG in sys.modules:
        return
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [ROOT]
    pkg.__spec__ = importlib.machinery.ModuleSpec(PKG, None, is_package=True)
    pkg.__spec__.submodule_search_locations = [ROOT]
    sys.modules[PKG] = pkg


_stub_astrbot()
_register_package()


class FakeEvent:
    """AstrBot 消息事件里插件用到的那几个方法。"""

    def __init__(self, uid="10001", name="管理员", admin=True, private=True, umo=None, panel=False):
        self.uid, self.name, self.admin, self.private = str(uid), name, admin, private
        self.unified_msg_origin = umo or (f"qq:FriendMessage:{uid}" if private else "qq:GroupMessage:888")
        if panel:
            self._panel = True

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name

    def get_platform_name(self):
        return "aiocqhttp"

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return self.private
