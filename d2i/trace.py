"""Model-call trace: which model, which platform, how long, what it cost. Never breaks inspection."""
import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

_events: deque = deque(maxlen=200)
_path: Path | None = None
_lock = threading.Lock()  # calls are recorded from several threads at once


def set_path(path) -> None:
    global _path
    _path = Path(path) if path else None


def clear() -> None:
    _events.clear()


def record(model: str, platform: str, job: str, ms: int, usd: float = 0.0, ok: bool = True, error: str = "") -> None:
    ev = {"at": datetime.now().isoformat(timespec="seconds"), "model": model, "platform": platform,
          "job": job, "ms": int(ms), "usd": usd, "ok": ok}
    if error:
        ev["error"] = error
    with _lock:
        _events.append(ev)
        if _path is not None:
            try:
                with _path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(ev) + "\n")
            except OSError:
                pass


def recent(n: int = 50) -> list[dict]:
    return list(_events)[-n:]
