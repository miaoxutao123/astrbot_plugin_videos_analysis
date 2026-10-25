"""
操作审计日志：每个 PendingOp 从申请到结束的全过程

一行一条 JSON（JSONL），存在 plugin_data 下，插件更新/重载不丢：
- created   申请人、平台、账号、操作摘要
- approved  批准人 + 执行结果（ok/failed/partial）+ 失败原因
- rejected  拒绝人
- expired   超时未审批
- dropped   提交后无人可审批（未设置 /va_notify）被自动取消

只记录操作摘要（不含 cookie、图片内容等），文件超过上限时保留最近一半。
"""

import json
import os
import threading
import time

from ..utils.paths import DATA_DIR

AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")
MAX_LINES = 5000
_lock = threading.Lock()

STATUS_LABEL = {
    "created": "申请", "approved": "已执行", "rejected": "已拒绝", "expired": "已过期", "dropped": "已取消",
}
RESULT_LABEL = {"ok": "✅成功", "failed": "❌失败", "partial": "⚠️部分成功"}


def _ts(t: float) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(t + 8 * 3600))


def record(event: str, op, **extra) -> None:
    """追加一条审计记录；写盘失败不影响业务。"""
    entry = {
        "t": round(time.time(), 1), "event": event, "code": op.code, "kind": op.kind,
        "platform": op.platform, "account": op.account,
        "requester": op.requester, "requester_name": op.requester_name,
        **{k: v for k, v in extra.items() if v not in (None, "")},
    }
    try:
        with _lock:
            os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
            with open(AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if os.path.getsize(AUDIT_FILE) > MAX_LINES * 400:
                _trim()
    except OSError:
        pass


def _trim() -> None:
    with open(AUDIT_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) > MAX_LINES:
        tmp = AUDIT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[-MAX_LINES // 2:])
        os.replace(tmp, AUDIT_FILE)


def _load() -> list[dict]:
    try:
        with open(AUDIT_FILE, encoding="utf-8") as f:
            out = []
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
    except OSError:
        return []


def recent(limit: int = 10, code: str = "", platform: str = "") -> list[dict]:
    """
    按申请合并后的最近记录（新→旧）：一个申请码一条，带上最终状态。
    申请码会复用，所以按“同一个码最近一次 created 之后的事件”合并。
    """
    ops: list[dict] = []
    open_by_code: dict[str, dict] = {}
    for e in _load():
        if code and e.get("code") != code:
            continue
        if platform and e.get("platform") != platform:
            continue
        if e["event"] == "created" or e["code"] not in open_by_code:
            item = {**e, "status": "created", "created": e["t"]}
            ops.append(item)
            open_by_code[e["code"]] = item
            if e["event"] == "created":
                continue
        item = open_by_code[e["code"]]
        item["status"] = e["event"]
        for k in ("approver", "approver_name", "by", "by_name", "result", "message", "error", "summary"):
            if k in e:
                item[k] = e[k]
        item["finished"] = e["t"]
    return list(reversed(ops))[:limit]


def compact(items: list[dict]) -> list[dict]:
    """给 LLM 的精简视图：只留必要字段并截断长文本。"""
    out = []
    for it in items:
        row = {
            "code": it["code"], "time": _ts(it["created"]), "op": it["kind"], "platform": it["platform"],
            "account": it.get("account", ""), "by": it.get("requester_name") or it.get("requester"),
            "status": STATUS_LABEL.get(it["status"], it["status"]),
        }
        if it.get("approver_name") or it.get("by_name"):
            row["approver"] = it.get("approver_name") or it.get("by_name")
        if it.get("result"):
            row["result"] = it["result"]
        if it.get("error"):
            row["error"] = it["error"][:80]
        out.append(row)
    return out


def render(items: list[dict]) -> str:
    if not items:
        return "还没有操作记录"
    lines = []
    for it in items:
        who = it.get("requester_name") or it.get("requester")
        head = f"[{it['code']}] {_ts(it['created'])} {it['platform']}·{it.get('account', '')} {it.get('summary', it['kind'])}"
        status = STATUS_LABEL.get(it["status"], it["status"])
        if it["status"] == "approved":
            status += f"（{it.get('approver_name') or it.get('approver')}批准）{RESULT_LABEL.get(it.get('result', ''), '')}"
        elif it["status"] == "rejected":
            status += f"（{it.get('by_name') or it.get('by') or '管理员'}）"
        line = f"{head}\n   申请人 {who} · {status}"
        if it.get("error"):
            line += f"\n   原因: {it['error'][:120]}"
        lines.append(line)
    return "\n".join(lines)
