import asyncio
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_videos_analysis.services import bilibili_service as bili
from astrbot_plugin_videos_analysis.services import bili_read_circuit
from astrbot_plugin_videos_analysis.services.bili_http import close_session, get_session


@pytest.mark.asyncio
async def test_cookie_validation_uses_nav_cache(monkeypatch):
    bili._COOKIE_VALID_CACHE.clear()
    calls = 0

    async def nav(cookies):
        nonlocal calls
        calls += 1
        return {"isLogin": True, "mid": "123"}

    monkeypatch.setattr(bili, "get_login_info", nav)
    cookies = {"SESSDATA": "test-session", "DedeUserID": "123"}
    assert await bili.check_cookie_valid(cookies) is True
    assert await bili.check_cookie_valid(cookies) is True
    assert calls == 1
    assert await bili.check_cookie_valid(cookies, force=True) is True
    assert calls == 2


@pytest.mark.asyncio
async def test_nav_network_failure_is_not_cached_as_invalid(monkeypatch):
    bili._COOKIE_VALID_CACHE.clear()
    bili.COOKIE_VALID = None
    calls = 0

    async def nav(cookies):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(bili, "get_login_info", nav)
    cookies = {"SESSDATA": "test-session", "DedeUserID": "123"}
    assert await bili.check_cookie_valid(cookies) is False
    assert bili.COOKIE_VALID is None
    assert await bili.check_cookie_valid(cookies) is False
    assert calls == 2


@pytest.mark.asyncio
async def test_shared_http_session_is_reused_and_closed():
    session = get_session()
    assert get_session() is session
    await close_session()
    assert session.closed


def test_risk_circuit_opens_once_per_local_day(tmp_path, monkeypatch):
    monkeypatch.setattr(bili_read_circuit, "ACCOUNTS_DIR", str(tmp_path))
    monkeypatch.setattr(bili_read_circuit, "STATE_FILE", str(tmp_path / "circuit.json"))
    assert not bili_read_circuit.is_open()
    assert bili_read_circuit.trip(-352, "rate limited") is True
    assert bili_read_circuit.is_open()
    assert bili_read_circuit.trip(-412, "second response") is False


@pytest.mark.asyncio
async def test_process_reuses_one_playurl_result_for_download(tmp_path, monkeypatch):
    bvid = "BV1AbCdEfGhI"
    info = {
        "aid": 123, "cid": 456, "bvid": bvid, "title": "sample",
        "cover": "", "duration": 10,
        "stats": {"view": "1", "like": "1", "danmaku": "0", "coin": "0", "favorite": "0"},
    }
    monkeypatch.setattr(bili, "BILI_DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(bili, "parse_video", AsyncMock(return_value=info))
    monkeypatch.setattr(bili, "bili_read_circuit_open", lambda: False)
    monkeypatch.setattr(bili, "check_cookie_valid", AsyncMock(return_value=True))
    playurl = AsyncMock(return_value=("https://cdn.example/video", "https://cdn.example/audio"))
    download = AsyncMock(return_value=str(tmp_path / "done.mp4"))
    monkeypatch.setattr(bili, "get_video_download_url_with_cookie", playurl)
    monkeypatch.setattr(bili, "download_video_with_cookie", download)
    monkeypatch.setattr(bili, "_BILI_DOWNLOAD_SEMAPHORE", asyncio.Semaphore(1))

    result = await bili.process_bili_video(bvid, download_flag=True, use_login=True)

    assert playurl.await_count == 1
    assert download.await_count == 1
    assert download.await_args.kwargs["result"] == ("https://cdn.example/video", "https://cdn.example/audio")
    assert result["video_path"] == str(tmp_path / "done.mp4")
    assert result["title"] == "sample"


@pytest.mark.asyncio
async def test_anonymous_download_accepts_previously_resolved_url(tmp_path, monkeypatch):
    monkeypatch.setattr(bili, "BILI_DOWNLOAD_DIR", str(tmp_path))
    async def write_file(url, path, headers):
        with open(path, "wb") as file:
            file.write(b"video")
    monkeypatch.setattr(bili, "bili_request", AsyncMock())
    download = AsyncMock(side_effect=write_file)
    monkeypatch.setattr(bili, "download_file", download)
    result = await bili.download_video(1, 2, "BV1AbCdEfGhI", video_url="https://cdn.example/video")
    assert result == str(tmp_path / "BV1AbCdEfGhI.mp4")
    bili.bili_request.assert_not_awaited()
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_risk_circuit_fails_closed_in_memory_if_disk_write_fails(monkeypatch):
    monkeypatch.setattr(bili, "_RISK_CIRCUIT_LOCAL_OPEN", False)
    monkeypatch.setattr(bili, "_RISK_CIRCUIT_LOCAL_DATE", "")
    async def notify(message):
        notified.append(message)
    notified = []
    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(bili, "trip_bili_read_circuit", fail_write)
    monkeypatch.setattr(bili, "RISK_NOTIFY_HOOK", notify)
    await bili._open_bili_read_circuit(-352, "risk")
    assert bili.bili_read_circuit_open()
    assert len(notified) == 1
    await bili._open_bili_read_circuit(-412, "another risk")
    assert len(notified) == 1


def test_definitive_invalid_cookie_status_expires_with_negative_cache(monkeypatch):
    cookies = {"SESSDATA": "test-session", "DedeUserID": "123"}
    fingerprint = bili.hashlib.sha256(bili.cookie_header(cookies).encode("utf-8")).hexdigest()
    bili._COOKIE_VALID_CACHE[fingerprint] = (False, bili.time.monotonic() - bili._COOKIE_INVALID_TTL - 1)
    assert not bili._cookie_validation_is_definitive(cookies)
    bili._COOKIE_VALID_CACHE[fingerprint] = (False, bili.time.monotonic())
    assert bili._cookie_validation_is_definitive(cookies)
    bili._COOKIE_VALID_CACHE.pop(fingerprint, None)
