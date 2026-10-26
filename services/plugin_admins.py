"""
插件管理员：从 AstrBot 管理员里选出负责本插件的人

- 没选之前与原来一致：所有 AstrBot 管理员都算插件管理员，第一次需要时（用管理员命令、提交审批、体检）问一次要不要选
- 选了之后：只有被选中的人能批准/拒绝危险操作、改插件设置；审批和告警推送到他们的私聊
  （其他 AstrBot 管理员发起的危险操作变成"申请"，交给插件管理员审批）
- AstrBot 管理员列表实时读取：在 AstrBot 里删掉某人的管理员身份，这里立即失效
- 私聊会话靠"记住"：管理员私聊过机器人一次就记下会话；QQ 号在 aiocqhttp 上可以直接推导
"""

import json
import os
import time

from astrbot.api import logger

from ..utils.paths import ACCOUNTS_DIR

ADMINS_FILE = os.path.join(ACCOUNTS_DIR, "plugin_admins.json")
ASK_INTERVAL = 12 * 3600  # 同一位管理员 12 小时内只问一次
# 这些前缀开头的消息算"在用本插件的管理员功能"，需要时顺带提问
COMMAND_PREFIXES = ("va_", "bili_", "dy_")


def _mask(uid: str) -> str:
    uid = str(uid)
    return uid if len(uid) <= 4 else f"{uid[:2]}{'*' * min(4, len(uid) - 4)}{uid[-2:]}"


def _guess_platform(uid: str) -> str:
    uid = str(uid)
    if uid.isdigit() and 5 <= len(uid) <= 11:
        return "QQ"
    if uid == "astrbot":
        return "WebUI"
    if len(uid) == 32:
        return "QQ 官方机器人"
    return ""


class PluginAdmins:
    def __init__(self, plugin):
        self.plugin = plugin
        self._data = self._load()

    # ── 存储 ─────────────────────────────────────────────

    @staticmethod
    def _load() -> dict:
        try:
            with open(ADMINS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f"[admins] 读取插件管理员失败，按未设置处理: {e}")
            return {}

    def _save(self) -> None:
        try:
            os.makedirs(ACCOUNTS_DIR, exist_ok=True)
            tmp = ADMINS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, ADMINS_FILE)
        except Exception as e:
            logger.warning(f"[admins] 保存插件管理员失败: {e}")

    # ── 查询 ─────────────────────────────────────────────

    def astrbot_admins(self) -> list[str]:
        try:
            ids = self.plugin.context.get_config().get("admins_id") or []
        except Exception:
            ids = []
        return [str(i).strip() for i in ids if str(i).strip()]

    def ids(self) -> list[str]:
        """生效的插件管理员（已不是 AstrBot 管理员的自动排除）。"""
        astr = set(self.astrbot_admins())
        return [i for i in self._data.get("ids") or [] if i in astr]

    @property
    def configured(self) -> bool:
        return bool(self.ids())

    def is_admin(self, event) -> bool:
        """能否审批/改设置：AstrBot 管理员，且（设置了插件管理员时）在名单里。WebUI 面板不受名单限制。"""
        if getattr(event, "_panel", False):
            return True
        if not event.is_admin():
            return False
        ids = self.ids()
        return not ids or str(event.get_sender_id()) in ids

    def names(self) -> str:
        seen = self._data.get("seen") or {}
        return "、".join((seen.get(i) or {}).get("name") or _mask(i) for i in self.ids())

    def umos(self) -> list[str]:
        """插件管理员的私聊会话：记住的优先，QQ 号推导兜底。"""
        seen = self._data.get("seen") or {}
        out = []
        for uid in self.ids():
            umo = (seen.get(uid) or {}).get("umo") or self._derive_umo(uid)
            if umo and umo not in out:
                out.append(umo)
        return out

    def _derive_umo(self, uid: str) -> str:
        if not uid.isdigit():
            return ""
        try:
            for p in self.plugin.context.get_config().get("platform") or []:
                if p.get("enable", True) and p.get("type") == "aiocqhttp" and p.get("id"):
                    return f"{p['id']}:FriendMessage:{uid}"
        except Exception:
            pass
        return ""

    def candidates(self) -> list[dict]:
        seen = self._data.get("seen") or {}
        chosen = set(self.ids())
        out = []
        for i, uid in enumerate(self.astrbot_admins(), 1):
            s = seen.get(uid) or {}
            out.append({
                "index": i, "id": uid, "masked": _mask(uid), "name": s.get("name", ""),
                "platform": s.get("platform") or _guess_platform(uid),
                "reachable": bool(s.get("umo") or self._derive_umo(uid)), "chosen": uid in chosen,
            })
        return out

    # ── 记录与提问 ───────────────────────────────────────

    def observe(self, event) -> None:
        """AstrBot 管理员发消息时记下昵称和私聊会话（推送审批用）。"""
        try:
            uid = str(event.get_sender_id())
            if uid not in self.astrbot_admins():
                return
            seen = self._data.setdefault("seen", {})
            old = seen.get(uid) or {}
            new = dict(old, name=str(event.get_sender_name() or old.get("name") or ""),
                       platform=str(event.get_platform_name() or old.get("platform") or ""))
            if event.is_private_chat():
                new["umo"] = event.unified_msg_origin
            if new != old:
                seen[uid] = new
                self._save()
        except Exception as e:
            logger.debug(f"[admins] 记录管理员信息失败: {e}")

    def should_ask(self, event) -> bool:
        return event.is_admin() and self.should_ask_uid(str(event.get_sender_id()))

    def should_ask_uid(self, uid: str) -> bool:
        """还没设置、没选过跳过、这位是 AstrBot 管理员、12 小时内没问过。"""
        if self.configured or self._data.get("declined") or uid not in self.astrbot_admins():
            return False
        asked = self._data.setdefault("asked", {})
        if time.time() - asked.get(uid, 0) < ASK_INTERVAL:
            return False
        asked[uid] = time.time()
        self._save()
        return True

    def prompt(self) -> str:
        lines = ["🔐 本插件还没有设置管理员（现在所有 AstrBot 管理员都能批准发布、删除等操作，审批也不知道推给谁）。",
                 "要从 AstrBot 管理员里选几位作为本插件管理员吗？选中的人负责审批，并在私聊里收到审批和告警：", ""]
        lines += [self._row(c) for c in self.candidates()]
        lines += ["", "回复「/va_admins 1 3」选择，「/va_admins 全部」全选，「/va_admins 我」只选自己，"
                      "「/va_admins 跳过」保持现状不再提醒。"]
        return "\n".join(lines)

    @staticmethod
    def _row(c: dict) -> str:
        who = c["name"] or "（还没在机器人这里说过话）"
        plat = f" · {c['platform']}" if c["platform"] else ""
        reach = "私聊可推送" if c["reachable"] else "私聊会话未知，先私聊机器人一次"
        return f"{c['index']}. {who}（{c['masked']}{plat}）· {reach}" + (" ✅" if c["chosen"] else "")

    def status_text(self) -> str:
        if self.configured:
            head = f"本插件管理员：{self.names()}（审批和告警推送到他们的私聊）"
        elif self._data.get("declined"):
            head = "本插件管理员：未设置（已选择跳过，所有 AstrBot 管理员都可审批）"
        else:
            head = "本插件管理员：未设置（所有 AstrBot 管理员都可审批）"
        lines = [head, ""] + [self._row(c) for c in self.candidates()]
        lines += ["", "修改：/va_admins 1 3｜全部｜我｜清空｜跳过"]
        return "\n".join(lines)

    # ── 选择 ─────────────────────────────────────────────

    def select(self, event, args: list[str]) -> str:
        """管理员命令 /va_admins 的参数处理；设置了插件管理员后只有插件管理员能改。"""
        if not self.is_admin(event):
            return f"只有本插件管理员（{self.names()}）可以修改，或在 AstrBot WebUI 的插件页面修改"
        if not args:
            return self.status_text()
        cands = self.candidates()
        word = args[0]
        if word in ("跳过", "skip", "不用", "否"):
            self._data.update(declined=True)
            self._save()
            return "好的，保持现状（所有 AstrBot 管理员都可审批），不再提醒。之后想设置可以发 /va_admins"
        if word in ("清空", "clear", "reset"):
            self._data.update(ids=[], declined=False)
            self._save()
            return "已清空本插件管理员，恢复为所有 AstrBot 管理员都可审批"
        if word in ("全部", "all", "所有"):
            ids = [c["id"] for c in cands]
        elif word in ("我", "me", "自己"):
            ids = [str(event.get_sender_id())]
        else:
            picks = {int(x) for a in args for x in a.replace("，", ",").split(",") if x.strip().isdigit()}
            bad = [p for p in picks if not 1 <= p <= len(cands)]
            if not picks or bad:
                return f"序号要在 1~{len(cands)} 之间，例如「/va_admins 1 3」\n\n" + self.status_text()
            ids = [c["id"] for c in cands if c["index"] in picks]
        me = str(event.get_sender_id())
        if event.is_admin() and me not in ids and me in self.astrbot_admins() and not getattr(event, "_panel", False):
            ids.append(me)  # 防止把自己排除后再也改不回来
        self._data.update(ids=ids, declined=False)
        self._save()
        self.observe(event)
        unreachable = [c for c in self.candidates() if c["chosen"] and not c["reachable"]]
        tip = ("\n⚠️ " + "、".join(c["name"] or c["masked"] for c in unreachable) + " 的私聊会话未知，请他们私聊机器人说一句话后才能收到审批推送"
               if unreachable else "")
        return f"✅ 本插件管理员：{self.names()}。之后审批和告警推送到他们的私聊。{tip}"
