"""插件管理员：从 AstrBot 管理员里选，选了之后只有他们能审批。"""

import pytest
from conftest import FakeEvent

from astrbot_plugin_videos_analysis.services import plugin_admins as pa


class Ctx:
    def __init__(self, admins):
        self.cfg = {"admins_id": admins, "platform": [{"id": "qq", "type": "aiocqhttp", "enable": True}]}

    def get_config(self):
        return self.cfg


class Plugin:
    def __init__(self, admins):
        self.context = Ctx(admins)


@pytest.fixture
def admins(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(pa, "ADMINS_FILE", str(tmp_path / "plugin_admins.json"))
    return pa.PluginAdmins(Plugin(["10001", "10002", "10003"]))


def test_unset_means_every_astrbot_admin(admins):
    assert not admins.configured
    assert admins.is_admin(FakeEvent("10002"))
    assert not admins.is_admin(FakeEvent("20000", admin=False))


def test_select_by_index_adds_operator_and_restricts(admins):
    me = FakeEvent("10001")
    msg = admins.select(me, ["2"])
    assert "✅" in msg
    assert admins.ids() == ["10002", "10001"]  # 防止把自己排除后改不回来
    assert not admins.is_admin(FakeEvent("10003"))
    assert admins.is_admin(FakeEvent("10003", panel=True))  # WebUI 面板不受名单限制
    assert admins.umos() == ["qq:FriendMessage:10002", "qq:FriendMessage:10001"]
    # 名单外的 AstrBot 管理员不能改
    assert "只有本插件管理员" in admins.select(FakeEvent("10003"), ["全部"])


def test_removed_astrbot_admin_drops_out(admins):
    admins.select(FakeEvent("10001"), ["全部"])
    admins.plugin.context.cfg["admins_id"] = ["10001"]
    assert admins.ids() == ["10001"]


def test_ask_once_then_skip(admins):
    ev = FakeEvent("10001")
    assert admins.should_ask(ev)
    assert not admins.should_ask(ev)  # 12 小时内不重复问
    assert not admins.should_ask(FakeEvent("20000", admin=False))
    admins.select(ev, ["跳过"])
    admins._data["asked"] = {}
    assert not admins.should_ask(ev)


def test_invalid_index_shows_usage(admins):
    assert "1~3" in admins.select(FakeEvent("10001"), ["9"])
    assert not admins.configured
