"""A critical hold (wrong tablet, wrong print) opens a re-check of the strips passed since the last good check:
each Inspect while it is open re-checks one of them; resume needs it finished or a QA waiver reason."""
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from d2i.batch import Batches

FILE = "recheck.json"


def load(product_dir) -> dict | None:
    """The last window, open or not; None when none was ever opened. Unreadable: raises (resume stays refused)."""
    p = Path(product_dir) / FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def pending(product_dir) -> dict | None:
    w = load(product_dir)
    return w if w and w["open"] else None


def active(product_dir, batch: dict | None) -> dict | None:
    """The open window when it belongs to this batch (it starts at or after the batch start). Another batch's window
    is ignored: it never blocks this batch's resume or end, and it is never waived in this batch's place."""
    w = pending(product_dir)
    return w if w and batch and w["since"] >= batch["time"] else None


def current(product_dir) -> dict | None:
    """The open window of the open batch."""
    return active(product_dir, Batches(product_dir).open)


def _write(product_dir, w: dict) -> dict:
    p = Path(product_dir) / FILE
    tmp = p.with_name(FILE + ".tmp")
    tmp.write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)  # one object, rewritten whole
    return w


def _ts(r) -> float:
    return r.get("time") or datetime.fromisoformat(r["at"]).timestamp()


def _rows(path: Path) -> list[dict]:  # verdict rows; a damaged line is skipped, as the app's own reader does
    out = []
    for x in (path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []):
        try:
            r = json.loads(x)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("status"), str):
            out.append(r)
    return out


def resumed_at(audit) -> float:
    """Unix time of the last QA resume: the Resume line button or an approved agent resume request."""
    for e in reversed(audit.entries()):
        d = e.get("details") if isinstance(e.get("details"), dict) else {}
        if e.get("action") == "resume_line" or (e.get("action") == "agent_approve" and d.get("kind") == "resume"
                                                 and d.get("approved")):
            return datetime.fromisoformat(e["at"]).timestamp()
    return 0.0


def released_at(product_dir, batch: dict, audit, first_strip: float | None) -> float:
    """When the batch was released: the latest of its first passed self-test, first PRINT_OK, QA specimen signature and
    first passed challenge run; batch start when one is missing (older records). Never after the batch's first strip
    (a gate item added later, e.g. a challenge set registered mid-batch, must not shrink the re-check)."""
    b, t0, no = Batches(product_dir), batch["time"], batch["batch"]
    signed = [datetime.fromisoformat(e["at"]).timestamp() for e in audit.entries() if e.get("action") == "specimen_signed"
              and isinstance(e.get("details"), dict) and e["details"].get("batch") == no]
    firsts = [min([t["time"] for t in b._read("selftests.jsonl") if t.get("passed") and t.get("time", 0) >= t0], default=None),
              min([_ts(p) for p in b._read("prints.jsonl") if p.get("batch") == no and p.get("status") == "PRINT_OK"], default=None),
              min(signed, default=None)]
    if None in firsts:
        return t0
    runs = [r["time"] for r in b._read("challenge_runs.jsonl") if r.get("passed") and r.get("batch") == no]
    t = max(firsts + [min(runs, default=t0)])  # no challenge run (no set): t0 never wins
    return min(t, first_strip) if first_strip is not None else t


def open_window(product_dir, audit, cause: str, by: str) -> dict | None:
    """The passed strips of the open batch since the later of batch release, the last passed print check and the last
    QA resume (rejected and held strips are already out of the good tray), after any finished re-check of this batch.
    This batch's open window is kept, not replaced; another batch's is replaced. None when there is nothing to
    re-check. Strip numbers count as the Run screen does (LOCKED and re-check rows are not strips)."""
    b = Batches(product_dir)
    batch = b.open
    if not batch:
        return None
    w = active(product_dir, batch)
    if w:
        return w
    rows = [r for r in _rows(Path(product_dir) / "log.jsonl")
            if r.get("batch") == batch["batch"] and r["status"] != "LOCKED" and not r.get("recheck")]
    prints = [_ts(p) for p in b._read("prints.jsonl") if p.get("batch") == batch["batch"] and p.get("status") == "PRINT_OK"]
    since = max([released_at(product_dir, batch, audit, _ts(rows[0]) if rows else None), resumed_at(audit)] + prints)
    last = load(product_dir)
    done = last["to_strip"] if last and not last["open"] and last["since"] >= batch["time"] else 0  # already re-checked
    nos = [n for n, r in enumerate(rows, 1)
           if n > done and r["status"] == "PASS" and _ts(r) >= int(since)]  # same second: re-check it
    if not nos:
        return None
    w = _write(product_dir, {"open": True, "window_id": uuid.uuid4().hex[:8], "since": since, "from_strip": nos[0],
                             "to_strip": nos[-1], "total": len(nos), "done": 0, "opened_by": by, "cause": cause,
                             "waived_by": None, "waiver_reason": None})
    audit.append("recheck_open", by, {k: w[k] for k in ("window_id", "from_strip", "to_strip", "total", "since")}, cause)
    return w


def count(product_dir, w: dict, status: str) -> dict:
    """One strip re-checked; LOCKED and NO_STRIP are not inspections. The window closes when all are done."""
    if status in ("LOCKED", "NO_STRIP"):
        return w
    done = w["done"] + 1
    return _write(product_dir, dict(w, done=done, open=done < w["total"]))


def waive(product_dir, audit, w: dict, by: str, reason: str) -> dict:
    out = _write(product_dir, dict(w, open=False, waived_by=by, waiver_reason=reason))
    audit.append("recheck_waived", by, {"window_id": w["window_id"], "done": w["done"], "total": w["total"]}, reason)
    return out


def text(w: dict) -> str:
    return (f"Re-check {w['total']} strips: no. {w['from_strip']}-{w['to_strip']} "
            f"since {datetime.fromtimestamp(w['since']):%H:%M}")


def progress(w: dict) -> str:
    return f"Re-check {w['done']} of {w['total']}"


def not_done(w: dict) -> str:
    return f"Re-check not complete: {progress(w)}. Finish it, or resume with a QA waiver reason"
