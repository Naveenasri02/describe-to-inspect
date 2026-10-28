"""Line board: the station's facts as one PackML state (the stack light, with reasons), and states.jsonl, the
append-only log written once per state change. Read-only: nothing here acts on the line."""
import json
import threading
from datetime import datetime
from pathlib import Path

from d2i.ipqc import BLOCK_PRINT, BLOCK_SELFTEST, BLOCK_SPECIMEN

IDLE, STARTING, RUNNING, HELD, SUSPENDED, STOPPED, COMPLETE = WORDS = (
    "IDLE", "STARTING", "RUNNING", "HELD", "SUSPENDED", "STOPPED", "COMPLETE")
RELEASE = (BLOCK_SELFTEST, BLOCK_PRINT, BLOCK_SPECIMEN)
NO_SELFTEST = "No self-test this shift"  # the station's own lock text; on an untaught station it means teach first
TEACH = "Teach the station first"
FINDER = "Pocket finder (SAM 3.1) not answering — each strip held for a hand check"
_WRITE = threading.Lock()  # read the last row and append as one step: one row per change


def holder(by: str) -> str:
    """Who holds the line, as the board says it (hold.json keeps "supervisor" / "station" / a user name)."""
    return {"supervisor": "the Shift Supervisor", "station": "the station", "": "unknown"}.get(by or "", by)


def line_state(f: dict) -> dict:
    """f: checklist, taught, locked, lock_reason, batch_open, blockers, held, held_by, hold_reason, quarantine (strips
    waiting for QA), suspended, record_unsigned. The most serious condition wins (STOPPED > HELD > SUSPENDED >
    STARTING > RUNNING; IDLE or COMPLETE when no batch is open); the lesser ones come back in `also`. Any lock is
    STOPPED, batch or not: the board shows the stack light's colour (the lamp and PLC coil 1 say LOCKED)."""
    found = []  # (state, reason, by, waiting_for), most serious first
    lr = f["lock_reason"] or ""
    if f["locked"]:
        if f["checklist"] and not f["taught"] and lr.startswith(NO_SELFTEST):
            lr = TEACH
        if lr.startswith("Self-test missed"):
            lr = "Self-test failed" + (" — quarantine open" if f["quarantine"] else "")
        found.append((STOPPED, lr, None, "QA"))
    if f["held"]:
        found.append((HELD, f["hold_reason"] or "Line held", f["held_by"] or None, "QA"))
    if f["suspended"]:
        found.append((SUSPENDED, FINDER, None, None))
    b = f["blockers"]
    if f["batch_open"] and b:
        known = all(x in RELEASE for x in b)  # else an unreadable record: shown as it is
        found.append((STARTING, (f"Release {len(RELEASE) - len(b)} of {len(RELEASE)} — still needed: " if known
                                 else "Not released — ") + "; ".join(b), None, "QA"))
    elif f["batch_open"]:
        found.append((RUNNING, "", None, None))
    elif f["record_unsigned"]:
        found.append((COMPLETE, "Record waiting for QA signature", None, "QA"))
    else:
        found.append((IDLE, "No batch open", None, None))
    state, reason, by, waiting = found[0]
    also = [r for _, r, _, _ in found[1:] if r]
    return {"state": state, "reason": reason, "by": by, "waiting_for": waiting, "also": also}


def finder_down(rows: list[dict], selftests: list[dict]) -> bool:
    """SUSPENDED: the last inspection (a LOCKED row is none) was held because the pocket finder (SAM 3.1) did not
    answer, and no self-test was recorded since (a recorded self-test means the finder answered)."""
    last = next((r for r in reversed(rows) if r.get("status") != "LOCKED"), None)
    if not last or not any(isinstance(x, dict) and x.get("check") == "finder" for x in last.get("reasons") or []):
        return False
    t = datetime.fromisoformat(last["at"]).timestamp() + 1  # verdict times are whole seconds: the same second is not later
    return not any(s.get("time", 0) >= t for s in selftests)


def investigating(running: bool, tools: list[str], pending: int) -> str | None:
    """The state band's extra line while the Shift Supervisor investigates."""
    if not running:
        return None
    return ("Investigating: " + (" → ".join(tools) or "starting") + " …"
            + (" waiting for QA approval" if pending else ""))


def read_rows(path) -> list[dict]:
    """states.jsonl rows; an unreadable line is skipped so the board never errors."""
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("to") in WORDS and isinstance(row.get("time"), (int, float)):
                out.append(row)
    except OSError:
        pass
    return out


def record_change(path, now: float, to: str, why) -> dict | None:
    """Append one row when `to` differs from the last row's state; why(previous row or None) -> (cause, by, rule,
    strip) is asked only then. The first row ever has from = null (the state before the log began is not known).
    A snapshot older than the last row is never appended: the log only moves forward."""
    path = Path(path)
    with _WRITE:
        rows = read_rows(path)
        prev = rows[-1] if rows else None
        if prev and (prev["to"] == to or now < prev["time"]):
            return None
        cause, by, rule, strip = why(prev)
        row = {"time": now, "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
               "from": prev and prev["to"], "to": to, "cause": cause, "by": by, "rule": rule, "strip": strip}
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def segments(rows: list[dict], start: float, now: float) -> list[dict]:
    """The state rows as time segments from `start` to `now`; each says who and what closed it."""
    out = []
    for r, nxt in zip(rows, rows[1:] + [None]):
        end = nxt["time"] if nxt else now
        if end <= start and nxt:
            continue
        s = min(max(r["time"], start), end)
        out.append({"state": r["to"], "start": s, "end": end, "seconds": round(end - s, 1), "cause": r.get("cause"),
                    "by": r.get("by"), "rule": r.get("rule"), "strip": r.get("strip"), "open": nxt is None,
                    "closed_by": nxt and nxt.get("by"), "close_cause": nxt and nxt.get("cause")})
    return out
