"""
自然语言配置：给小白用户用的设置目录 + 体检

LLM 通过 video_plugin_settings 工具读取这份目录（中文名、说明、当前值），把用户的自然语言
（“帮我把语音转写换成硅基流动”）对应到具体设置；修改一律走审批，管理员确认后写入配置并重载插件。
目录只在工具结果里出现，不放进工具描述，避免撑大每次请求的上下文、也不影响缓存。
"""

import json

# key: (中文名, 类型, 说明, 是否密钥)
CATALOG: dict[str, tuple[str, str, str, bool]] = {
    # 常用
    "auto_parse_enabled": ("自动解析链接", "bool", "群里发抖音/B站链接时自动解析并发视频", False),
    "show_progress_messages": ("显示进度提示", "bool", "处理时发“正在解析…”之类的提示", False),
    "max_video_size": ("视频大小上限(MB)", "int", "超过就改成发文件", False),
    "delete_time": ("下载文件保留(分钟)", "int", "过期自动删除，0 为不删", False),
    # 浏览（抽帧 + 语音转写）
    "browse_asr_api_base": ("语音转写地址", "str", "OpenAI 兼容的 /audio/transcriptions 地址，如硅基流动 https://api.siliconflow.cn/v1", False),
    "browse_asr_api_key": ("语音转写密钥", "str", "转写/语音识别/ASR 的 API Key", True),
    "browse_asr_model": ("语音转写模型", "str", "硅基流动免费：FunAudioLLM/SenseVoiceSmall", False),
    "browse_asr_max_minutes": ("转写最长(分钟)", "int", "超出部分不转写", False),
    "browse_max_frames": ("看视频时抽几帧", "int", "抽帧数/帧数，越多越细但越费 token，建议 4~8", False),
    "browse_max_minutes": ("看视频最长(分钟)", "int", "更长的视频只看开头", False),
    "browse_sync_max_minutes": ("超过几分钟转后台看", "int", "长视频后台看完再推送总结", False),
    "browse_report_history": ("浏览记入观看历史", "bool", "管理员让机器人看B站视频时写入账号观看记录", False),
    # B站
    "bili_quality": ("B站下载清晰度", "int", "画质/分辨率：16=360P 32=480P 64=720P 80=1080P", False),
    "bili_use_login": ("B站用登录态下载", "bool", "登录后可下高清", False),
    "bili_upload_tid": ("B站投稿分区", "int", "分区 ID：21=日常 17=单机游戏 138=搞笑 201=科学科普 95=数码", False),
    # 账号运维与审批
    "llm_allow_non_admin_requests": ("允许普通用户申请操作", "bool", "普通用户让 AI 发评论等，会推送给管理员审批", False),
    "max_pending_per_user": ("每人待审批上限", "int", "", False),
    "risk_cooldown_minutes": ("风控冷却(分钟)", "int", "账号触发风控后暂停写操作的时长", False),
    "health_check_interval_minutes": ("账号巡检间隔(分钟)", "int", "定时检查登录状态并自动续期，0 为关闭", False),
    "creator_reply_style": ("评论回复风格", "str", "AI 起草评论/回复时的语气", False),
    "reply_max_comments": ("每次最多回复几条", "int", "1~5", False),
    # 抖音
    "douyin_browser_signer": ("抖音签名浏览器", "bool", "抖音解析必须开启", False),
    "douyin_proxy": ("抖音代理", "str", "如 http://127.0.0.1:7890，留空直连", False),
    # 可选的第三方模型（默认关闭）
    "video_understand_method": ("视频理解方式", "str", "llm=抽帧+转写交给当前对话模型（默认）；gemini/mimo 需先开启", False),
    "gemini_enabled": ("启用 Gemini", "bool", "默认关闭", False),
    "gemini_api_key": ("Gemini 密钥", "str", "", True),
    "gemini_base_url": ("Gemini 地址", "str", "中转站根地址，留空为官方", False),
    "gemini_model": ("Gemini 模型", "str", "", False),
    "mimo_enabled": ("启用 MiMo", "bool", "默认关闭", False),
    "mimo_api_key": ("MiMo 密钥", "str", "", True),
}

PRESETS = {
    "siliconflow_asr": ({"browse_asr_api_base": "https://api.siliconflow.cn/v1", "browse_asr_model": "FunAudioLLM/SenseVoiceSmall"},
                        "硅基流动免费语音转写（还需要 browse_asr_api_key）"),
}


def _mask(v) -> str:
    v = str(v or "")
    return f"已设置(***{v[-4:]})" if v else "未设置"


def _score(query: str, key: str) -> float:
    """中文名/说明/键名与用户说法的相似度（字符重合 + 序列相似）。"""
    import difflib

    cn, _, desc, _ = CATALOG[key]
    best = 0.0
    for target in (cn, key, desc):
        if not target:
            continue
        overlap = len(set(query) & set(target)) / max(1, len(set(query)))
        best = max(best, 0.6 * overlap + 0.4 * difflib.SequenceMatcher(None, query, target).ratio())
    return best


def resolve_key(name: str) -> str | None:
    """接受配置键或中文名，模糊匹配到最像的一项（太不像则返回 None）。"""
    name = (name or "").strip()
    if not name:
        return None
    if name in CATALOG:
        return name
    for k, (cn, *_rest) in CATALOG.items():
        if name == cn or name in cn or cn in name:
            return k
    ranked = sorted(CATALOG, key=lambda k: -_score(name, k))
    return ranked[0] if _score(name, ranked[0]) >= 0.55 else None


def suggest(name: str, n: int = 3) -> list[str]:
    ranked = sorted(CATALOG, key=lambda k: -_score(name or "", k))[:n]
    return [f"{CATALOG[k][0]}（{k}）" for k in ranked]


def parse_value(key: str, raw):
    typ = CATALOG[key][1]
    s = str(raw).strip()
    if typ == "bool":
        if s.lower() in ("1", "true", "yes", "on", "开", "开启", "打开", "是", "启用"):
            return True
        if s.lower() in ("0", "false", "no", "off", "关", "关闭", "否", "停用", "禁用"):
            return False
        raise ValueError(f"「{CATALOG[key][0]}」只能是开/关")
    if typ == "int":
        try:
            return int(float(s))
        except ValueError as e:
            raise ValueError(f"「{CATALOG[key][0]}」需要一个数字") from e
    return s


def show(config) -> dict:
    items = []
    for k, (cn, typ, desc, secret) in CATALOG.items():
        v = config.get(k)
        items.append({"k": k, "名": cn, "值": _mask(v) if secret else v, **({"说明": desc[:30]} if desc else {})})
    return {"settings": items, "presets": {k: d for k, (_, d) in PRESETS.items()},
            "注意": "这只是查看。要修改必须再调用 action=set（多项可一次传 JSON），没调用 set 不算提交，不要对用户说已提交"}


async def check(plugin, event) -> list[str]:
    """体检：告诉小白还缺什么、哪里不对。只返回有问题或值得注意的项。"""
    cfg, notes = plugin.config, []
    if not (cfg.get("browse_asr_api_base") and cfg.get("browse_asr_api_key")):
        notes.append("没有配置线上语音转写：看视频时只能靠B站字幕或画面。可以说“用硅基流动的免费语音转写，密钥是 xxx”")
    try:
        from .browse_service import _current_provider, _provider_sees_images

        _, prov = await _current_provider(plugin, event)
        if not prov:
            notes.append("没有找到当前对话模型，浏览视频无法总结")
        elif not _provider_sees_images(prov):
            notes.append(f"当前对话模型 {prov.get_model()} 在 AstrBot 里没有勾选“图像”能力，看视频时只能读转写不看画面")
    except Exception:
        pass
    bili = await plugin.accounts.bili_cookies_for(None)
    if not (bili or {}).get("SESSDATA"):
        notes.append("B站未登录：评论、观看记录、字幕等需要登录，管理员发送 /bili_login 扫码")
    if plugin.browser_signer is None:
        notes.append(f"抖音签名引擎未就绪：{plugin.browser_signer_error or '未知原因'}")
    if not plugin.accounts.notify_targets():
        notes.append("还没有设置审批/告警会话：普通用户的操作申请无处推送。管理员私聊机器人发送 /va_notify")
    return notes or ["一切正常"]


def diff_text(changes: dict, config) -> str:
    lines = []
    for k, v in changes.items():
        cn, _, _, secret = CATALOG[k]
        old = config.get(k)
        lines.append(f"· {cn}：{_mask(old) if secret else json.dumps(old, ensure_ascii=False)} → {_mask(v) if secret else json.dumps(v, ensure_ascii=False)}")
    return "\n".join(lines)
