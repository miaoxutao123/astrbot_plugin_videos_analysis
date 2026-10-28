"""Regression tests for credential migration, corruption and execution recovery."""
import asyncio
import json
import os

import pytest
from cryptography.fernet import Fernet

from conftest import FakeEvent
from astrbot_plugin_videos_analysis.utils.secure_storage import (
    CredentialStoreError, read_secure_json, write_secure_json,
)
from astrbot_plugin_videos_analysis.services import creator_ops as co
from test_creator_ops import Plugin, post_op


def test_legacy_credentials_migrate_without_changing_values(tmp_path, monkeypatch):
    monkeypatch.delenv("VA_CREDENTIAL_KEY", raising=False)
    path = tmp_path / "accounts" / "pool.json"
    path.parent.mkdir()
    original = {"accounts": {"bilibili": [{"name": "one", "secret": {"SESSDATA": "sample"}}]}, "active": {}}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert read_secure_json(str(path)) == original
    assert path.read_bytes().startswith(b"VAENC1:")
    assert b"SESSDATA" not in path.read_bytes()
    assert read_secure_json(str(path)) == original
    key = path.parent / ".credentials.key"
    assert key.exists()
    if os.name == "posix":
        assert (key.stat().st_mode & 0o077) == 0
        assert (path.stat().st_mode & 0o077) == 0


def test_wrong_key_must_not_overwrite_existing_account(tmp_path, monkeypatch):
    path = str(tmp_path / "secret.json")
    first = Fernet.generate_key().decode()
    second = Fernet.generate_key().decode()
    monkeypatch.setenv("VA_CREDENTIAL_KEY", first)
    write_secure_json(path, {"token": "original"})
    old = (tmp_path / "secret.json").read_bytes()
    monkeypatch.setenv("VA_CREDENTIAL_KEY", second)
    with pytest.raises(CredentialStoreError):
        read_secure_json(path)
    with pytest.raises(CredentialStoreError):
        write_secure_json(path, {"token": "replacement"})
    assert (tmp_path / "secret.json").read_bytes() == old
    monkeypatch.setenv("VA_CREDENTIAL_KEY", first)
    assert read_secure_json(path) == {"token": "original"}


def test_invalid_legacy_data_fails_closed(tmp_path):
    path = tmp_path / "secret.json"
    path.write_text("{ definitely not json", encoding="utf-8")
    with pytest.raises(CredentialStoreError):
        read_secure_json(str(path))
    with pytest.raises(CredentialStoreError):
        write_secure_json(str(path), {"token": "new"})
    assert path.read_text(encoding="utf-8") == "{ definitely not json"


@pytest.fixture
def journal_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(co, "CREATOR_DIR", str(tmp_path))
    monkeypatch.setattr(co, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(co, "PENDING_FILE", str(tmp_path / "pending.json"))
    monkeypatch.setattr(co, "PUBLISH_LOG", str(tmp_path / "published.jsonl"))
    monkeypatch.setattr(co, "EXECUTION_FILE", str(tmp_path / "executions.json"))


async def test_crash_during_external_post_stays_uncertain(journal_isolated, monkeypatch):
    started = asyncio.Event()

    class SlowCreator:
        async def post_images(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()
    monkeypatch.setattr(co, "get_creator", lambda *args, **kwargs: SlowCreator())
    plugin = Plugin()
    ops = co.CreatorOps(plugin)
    op = post_op(ops, FakeEvent())
    task = asyncio.create_task(ops.confirm(op.code, event=FakeEvent()))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    restored = co.CreatorOps(Plugin())
    unresolved = restored.unresolved_executions()
    assert len(unresolved) == 1
    assert unresolved[0]["code"] == op.code and unresolved[0]["state"] == "uncertain"
    assert not restored.has_pending(op.code)
    assert restored.pending_list() == []
    assert "没有待确认" in await restored.confirm(op.code, event=FakeEvent())


async def test_journal_failure_prevents_external_operation(journal_isolated, monkeypatch):
    plugin = Plugin()
    ops = co.CreatorOps(plugin)
    op = post_op(ops, FakeEvent())
    def fail():
        raise RuntimeError("执行日志持久化失败")
    monkeypatch.setattr(ops, "_save_executions", fail)
    result = await ops.confirm(op.code, event=FakeEvent())
    assert "执行日志" in result
    assert ops.has_pending(op.code)
