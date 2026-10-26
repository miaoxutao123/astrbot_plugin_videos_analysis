"""审批流程：持久化、过期、拒绝、非管理员申请的推送与结果回传。"""

import asyncio

import pytest
from conftest import FakeEvent

from astrbot_plugin_videos_analysis.services import creator_ops as co


class Accounts:
    def __init__(self, targets):
        self.targets = targets

    def active_name(self, platform):
        return ""

    def notify_targets(self):
        return list(self.targets)


class Admins:
    def is_admin(self, event):
        return event.is_admin()

    def should_ask_uid(self, uid):
        return False


class Caller:
    def __init__(self):
        self.results = []

    async def on_publish_result(self, result):
        self.results.append(result)


class Meta:
    def __init__(self, star):
        self.star_cls = star


class Ctx:
    def __init__(self):
        self.sent, self.caller = [], Caller()

    async def send_message(self, umo, chain):
        seg = chain.chain[0]
        self.sent.append((umo, getattr(seg, "text", seg)))
        return True

    def get_registered_star(self, name):
        return Meta(self.caller) if name == "media_gen" else None


class Plugin:
    def __init__(self, targets=()):
        self.config = {"confirm_timeout_seconds": 60, "publish_confirm_minutes": 720}
        self.accounts, self.admins, self.context = Accounts(targets), Admins(), Ctx()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "CREATOR_DIR", str(tmp_path))
    monkeypatch.setattr(co, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(co, "PENDING_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(co, "PUBLISH_LOG", str(tmp_path / "published.jsonl"))


def post_op(ops, event, ttl=None):
    payload = {"title": "晚霞", "tags": ["日常"], "text": "", "images": [], "opts": {"visibility": "private"},
               "caller": "media_gen", "caller_ref": "job-1"}
    return ops._new_op("post", "bilibili", event, payload, ttl=ttl)


async def test_pending_survives_reload():
    ops = co.CreatorOps(Plugin())
    op = post_op(ops, FakeEvent())
    again = co.CreatorOps(Plugin())
    assert again.has_pending(op.code)
    assert "仅自己可见" in again.pending_list()[0]["summary"]


async def test_expired_publish_reports_back_to_caller():
    plugin = Plugin()
    ops = co.CreatorOps(plugin)
    op = post_op(ops, FakeEvent(), ttl=1)
    op.expires = 0
    assert ops.pending_list() == []
    await asyncio.sleep(0.05)
    [res] = plugin.context.caller.results
    assert res["status"] == "expired" and res["caller_ref"] == "job-1" and res["kind"] == "images"
    assert co.read_publish_log()[-1]["code"] == op.code


async def test_cancel_reports_rejected():
    plugin = Plugin()
    ops = co.CreatorOps(plugin)
    op = post_op(ops, FakeEvent())
    assert ops.cancel(op.code, FakeEvent(name="张三")) == f"已取消 {op.code}"
    await asyncio.sleep(0.05)
    assert plugin.context.caller.results[-1]["status"] == "rejected"
    assert not ops.has_pending(op.code)


async def test_non_admin_request_goes_to_admins():
    plugin = Plugin(targets=["qq:FriendMessage:10001"])
    ops = co.CreatorOps(plugin)
    user = FakeEvent("30000", name="路人", admin=False, private=False)
    op = post_op(ops, user)
    res = await ops.submit(op)
    assert res["status"] == "pending_admin_approval"
    to_admin = [t for u, t in plugin.context.sent if u == "qq:FriendMessage:10001"]
    assert to_admin and "权限申请" in to_admin[0] and op.code in to_admin[0]
    assert any(u == user.unified_msg_origin and "已提交管理员审批" in t for u, t in plugin.context.sent)


async def test_non_admin_request_dropped_without_notify_target():
    ops = co.CreatorOps(Plugin(targets=[]))
    op = post_op(ops, FakeEvent("30000", admin=False))
    res = await ops.submit(op)
    assert res["status"] == "rejected" and not ops.has_pending(op.code)


async def test_admin_request_needs_explicit_confirm():
    plugin = Plugin()
    ops = co.CreatorOps(plugin)
    op = post_op(ops, FakeEvent())
    res = await ops.submit(op)
    assert res["status"] == "pending_confirmation" and "尚未执行" in res["message"]
    assert ops.has_pending(op.code)


def test_per_user_pending_limit():
    ops = co.CreatorOps(Plugin())
    ev = FakeEvent("40000")
    for _ in range(ops.max_pending_per_user):
        post_op(ops, ev)
    with pytest.raises(RuntimeError, match="待审批"):
        post_op(ops, ev)
