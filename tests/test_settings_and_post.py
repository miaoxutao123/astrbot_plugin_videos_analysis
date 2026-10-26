"""自然语言设置解析、发布选项与图文命令解析。"""

import pytest

from astrbot_plugin_videos_analysis.handlers.account_ops_handler import parse_post_text
from astrbot_plugin_videos_analysis.services.creator_ops import _publish_opts, split_title_tags
from astrbot_plugin_videos_analysis.services.settings_service import CATALOG, parse_value, resolve_key


def test_resolve_key_accepts_key_and_chinese_name():
    assert resolve_key("douyin_proxy") == "douyin_proxy"
    assert resolve_key("抖音代理") == "douyin_proxy"
    assert resolve_key("抖音浏览器空闲省电") == "douyin_browser_low_cpu"
    assert resolve_key("") is None
    assert resolve_key("完全无关的东西xyz") is None


def test_parse_value_bool_and_int():
    assert parse_value("douyin_browser_low_cpu", "打开") is True
    assert parse_value("douyin_browser_low_cpu", "关闭") is False
    with pytest.raises(ValueError):
        parse_value("douyin_browser_low_cpu", "也许")
    int_key = next(k for k, v in CATALOG.items() if v[1] == "int")
    assert parse_value(int_key, "6") == 6 and parse_value(int_key, "2.0") == 2
    with pytest.raises(ValueError):
        parse_value(int_key, "六个")


def test_publish_opts_normalizes_visibility_and_ai():
    assert _publish_opts({"visibility": "仅自己可见", "ai_generated": True}) == {"visibility": "private", "ai_generated": True}
    assert _publish_opts({"visibility": "好友"}) == {"visibility": "friends"}
    assert _publish_opts({"visibility": "公开", "ai_generated": "否"}) == {}  # 公开是默认值，不用传
    assert _publish_opts(None) == {}


def test_split_title_tags():
    title, tags = split_title_tags("秋天的第一杯奶茶 #日常 #治愈")
    assert title == "秋天的第一杯奶茶" and tags == ["日常", "治愈"]


def test_parse_post_text_flags_title_tags_body():
    title, tags, text, opts = parse_post_text("仅自己可见 AI 秋天的第一杯奶茶 #日常\n今天路过街角那家店 #治愈")
    assert title == "秋天的第一杯奶茶"
    assert tags == ["日常", "治愈"]
    assert text == "今天路过街角那家店"
    assert opts == {"visibility": "private", "ai_generated": True}
