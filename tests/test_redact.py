"""发给聊天 / LLM / 面板的文本统一脱敏。"""

from astrbot_plugin_videos_analysis.utils.redact import redact


def test_cookie_pairs_and_json_fields():
    s = redact("cookie: SESSDATA=abc123%2Cxyz; bili_jct=0f9e8d; sessionid=deadbeef; other=keep")
    assert "abc123" not in s and "0f9e8d" not in s and "deadbeef" not in s
    assert "SESSDATA=***" in s and "other=keep" in s
    j = redact('{"sid_guard": "a1b2c3d4|1700000000", "msToken": "zzzzzzzz"}')
    assert "a1b2c3d4" not in j and "zzzzzzzz" not in j


def test_keys_tokens_and_proxy_credentials():
    s = redact("GET /v1?key=AIzaSyA1234567890abcdefghijk&token=secret-token-1 Bearer abcdefghijklmn sk-ABCDEFGHIJKLMNOP")
    for leak in ("AIzaSyA1234567890", "secret-token-1", "abcdefghijklmn", "ABCDEFGHIJKLMNOP"):
        assert leak not in s
    assert redact("proxy http://user:p%40ss@10.0.0.1:7890 down") == "proxy http://***:***@10.0.0.1:7890 down"


def test_plain_text_untouched():
    text = "解析失败：作品已删除（status_code=2053）"
    assert redact(text) == text
