import json
import sqlite3

from astrbot_plugin_videos_analysis.services import analysis_cache as cache


def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CREATOR_DIR", str(tmp_path))
    monkeypatch.setattr(cache, "DB_FILE", str(tmp_path / "analysis_cache.sqlite3"))
    monkeypatch.setattr(cache, "LEGACY_FILE", str(tmp_path / "video_context.json"))


def test_legacy_json_is_migrated_once(tmp_path, monkeypatch):
    _isolate_cache(tmp_path, monkeypatch)
    with open(cache.LEGACY_FILE, "w", encoding="utf-8") as file:
        json.dump({"bilibili:BV1AbCdEfGhI": "legacy summary"}, file)
    assert cache.get_summary("bilibili", "BV1AbCdEfGhI", 7) == "legacy summary"
    cache.put_summary("bilibili", "BV1AbCdEfGhI", "new summary")
    assert cache.get_summary("bilibili", "BV1AbCdEfGhI", 7) == "new summary"


def test_expired_summary_is_deleted(tmp_path, monkeypatch):
    _isolate_cache(tmp_path, monkeypatch)
    cache.put_summary("bilibili", "BV1AbCdEfGhI", "old summary")
    with sqlite3.connect(cache.DB_FILE) as db:
        db.execute("UPDATE analysis_cache SET created_ts=0")
    assert cache.get_summary("bilibili", "BV1AbCdEfGhI", 7) == ""
    with sqlite3.connect(cache.DB_FILE) as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0] == 0


def test_cache_ttl_is_configurable_including_zero():
    assert cache.configured_ttl_days({"analysis_cache_ttl_days": 3}) == 3
    assert cache.configured_ttl_days({"analysis_cache_ttl_days": 0}) == 0
    assert cache.configured_ttl_days({"analysis_cache_ttl_days": "invalid"}) == 7
