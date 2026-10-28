"""Line-quality rules that look across strips: three rejects in a row mean the machine has drifted."""
from collections import Counter
from datetime import datetime

from d2i.batch import Batches

DRIFT_REJECTS = 3


def reject_run(rows: list[dict], batch: str, since: float = 0.0) -> int:
    """Length of the trailing run of REJECT rows of this batch; PASS ends it, HOLD / NO_STRIP / LOCKED are skipped.
    since: a Unix time; rows logged at or before it (e.g. before a QA resume) are not counted."""
    n = 0
    for r in reversed(rows):
        if r.get("batch") != batch or r["status"] in ("HOLD", "NO_STRIP", "LOCKED"):
            continue
        if since and datetime.fromisoformat(r["at"]).timestamp() <= since:
            break  # log order: everything earlier is older still
        if r["status"] != "REJECT":
            break
        n += 1
    return n


IPQC_EVERY_S = 3600
BLOCK_SELFTEST, BLOCK_PRINT, BLOCK_SPECIMEN = ("Self-test for this batch", "Start-up print check",
                                               "QA signature on the start-up specimen")


def release_blockers(batch: dict, selftests: list, prints: list, specimen_signed: bool) -> list[str]:
    """What still stands between a started batch and inspection; empty = released."""
    out = []
    if not any(t.get("passed") and t.get("time", 0) >= batch["time"] for t in selftests):
        out.append(BLOCK_SELFTEST)
    if not any(p.get("batch") == batch["batch"] and p.get("status") == "PRINT_OK" for p in prints):
        out.append(BLOCK_PRINT)
    if not specimen_signed:
        out.append(BLOCK_SPECIMEN)
    return out


def check_events(product_dir, batch: str) -> list[dict]:
    """This batch's leak / print / foil_change events, oldest first, each with a Unix `time`."""
    b = Batches(product_dir)
    ev = [r for r in b.rows if r.get("batch") == batch and r["event"] in ("leak", "foil_change")]
    ev += [dict(r, event="print", time=r.get("time") or datetime.fromisoformat(r["at"]).timestamp())
           for r in b._read("prints.jsonl") if r.get("batch") == batch]
    return sorted(ev, key=lambda r: r["time"])


def due(batch: dict, rows: list[dict], now: float) -> list[str]:
    """Leak test / print check due: none since the later of batch start and last foil change, or the last is over an hour old."""
    start = max([batch["time"]] + [r["time"] for r in rows if r["event"] == "foil_change"])
    out = []
    for ev, text in (("leak", "Leak test due"), ("print", "Print check due")):
        last = max((r["time"] for r in rows if r["event"] == ev and r["time"] >= start), default=None)
        if last is None or now - last > IPQC_EVERY_S:
            out.append(text)
    return out


def missed_windows(batch: dict, rows: list[dict], until: float) -> list[tuple[float, float]]:
    """Whole IPQC_EVERY_S windows (restarted by each foil change) up to `until` with no leak or print check."""
    marks = [batch["time"]] + [r["time"] for r in rows if r["event"] == "foil_change"] + [until]
    checks = [r["time"] for r in rows if r["event"] in ("leak", "print")]
    out = []
    for s, stop in zip(marks, marks[1:]):
        w = s
        while w + IPQC_EVERY_S <= stop:
            if not any(w <= t < w + IPQC_EVERY_S for t in checks):
                out.append((w, w + IPQC_EVERY_S))
            w += IPQC_EVERY_S
    return out


def reconcile(rows: list[dict], batch: str) -> dict:
    """Strip counts of this batch; LOCKED rows are not inspections."""
    n = Counter(r["status"] for r in rows if r.get("batch") == batch and r["status"] != "LOCKED")
    out = {"inspected": sum(n.values()), "passed": n["PASS"], "rejected": n["REJECT"], "held": n["HOLD"],
           "no_strip": n["NO_STRIP"]}
    return dict(out, balanced=out["inspected"] == out["passed"] + out["rejected"] + out["held"] + out["no_strip"])
