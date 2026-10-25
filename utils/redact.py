"""
敏感信息脱敏：发给聊天、LLM、面板的报错文本统一过一遍，避免 cookie / 密钥 / 代理账号外泄。

只做"兜底"——正常路径本来就不应该把这些值拼进返回文本。
"""

import re

# 登录态 cookie 名（B站 / 抖音）
_COOKIE_NAMES = (
    "SESSDATA", "bili_jct", "DedeUserID__ckMd5", "refresh_token", "ac_time_value",
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt", "uid_tt_ss", "sid_ucp_v1",
    "ssid_ucp_v1", "odin_tt", "passport_auth_status", "passport_assist_user", "msToken", "ttwid",
    "s_v_web_id", "__ac_signature", "bd_ticket_guard_client_data",
)
_RULES = [
    # name=value（cookie 串、查询参数）
    (re.compile(r"(?i)\b(" + "|".join(map(re.escape, _COOKIE_NAMES)) + r")=([^;&\s\"']+)"), r"\1=***"),
    # "name": "value"（JSON 形式）
    (re.compile(r"(?i)([\"'](?:" + "|".join(map(re.escape, _COOKIE_NAMES)) + r")[\"']\s*:\s*[\"'])([^\"']+)"), r"\1***"),
    # API key 查询参数 / 头
    (re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|key|token|csrf)=)([^&\s\"']{6,})"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***"),
    (re.compile(r"(?i)(x-goog-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "AIza***"),
    # 代理 / URL 里的账号密码 scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1***:***@"),
]


def redact(text) -> str:
    """把文本中的 cookie 值、密钥、代理账号替换成 ***。"""
    s = str(text)
    for pat, rep in _RULES:
        s = pat.sub(rep, s)
    return s
