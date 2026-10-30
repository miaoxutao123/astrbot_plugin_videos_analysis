"""Daily circuit breaker for Bilibili read requests.

Only explicit anti-abuse responses (-352, -412, HTTP 412/429) open this
circuit. A generic 403 is not assumed to be rate limiting. The breaker resets
at the next China-local day and prevents repeated requests after a block.
"""
import json
import os
import time
import threading

from ..utils.paths import ACCOUNTS_DIR

STATE_FILE = os.path.join(ACCOUNTS_DIR, "bili_read_circuit.json")
_LOCK = threading.Lock()


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) and value.get("date") == today() else {}
    except (OSError, ValueError):
        return {}


def is_open() -> bool:
    return bool(state())


def trip(code: int | str, reason: str = "") -> bool:
    """Open once per China-local day; return True only for the first transition."""
    with _LOCK:
        if is_open():
            return False
        os.makedirs(ACCOUNTS_DIR, mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(ACCOUNTS_DIR, 0o700)
        payload = {
            "date": today(),
            "opened_at": time.time(),
            "code": str(code)[:16],
            "reason": " ".join(str(reason or "").split())[:120],
        }
        tmp = STATE_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, STATE_FILE)
            if os.name == "posix":
                os.chmod(STATE_FILE, 0o600)
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
