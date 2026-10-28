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
BLOCK_CHALLENGE = "Physical challenge for this batch"


def release_blockers(batch: dict, selftests: list, prints: list, specimen_signed: bool,
                     challenge_runs: list | None = None) -> list[str]:
    """What still stands between a started batch and inspection; empty = released.
    challenge_runs: None when no physical challenge set is registered (then no challenge run is needed)."""
    out = []
    if not any(t.get("passed") and t.get("time", 0) >= batch["time"] for t in selftests):
        out.append(BLOCK_SELFTEST)
    if challenge_runs is not None and not any(r.get("passed") and r.get("batch") == batch["batch"]
                                              and r.get("time", 0) >= batch["time"] for r in challenge_runs):
        out.append(BLOCK_CHALLENGE)
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


def _done(r, ev) -> bool:
    return r["event"] == ev and (ev != "leak" or r.get("result") == "pass")  # a failed leak test is not a done one


def due_at(batch: dict, rows: list[dict]) -> dict:
    """When each hourly check falls due, {"leak": t, "print": t}; None = due now (none done since its clock started).
    A foil roll change restarts the print-check clock (the leak-test clock keeps running)."""
    foil = max([batch["time"]] + [r["time"] for r in rows if r["event"] == "foil_change"])
    out = {}
    for ev, start in (("leak", batch["time"]), ("print", foil)):
        last = max((r["time"] for r in rows if _done(r, ev) and r["time"] >= start), default=None)
        out[ev] = None if last is None else last + IPQC_EVERY_S
    return out


def due(batch: dict, rows: list[dict], now: float) -> list[str]:
    """Leak test due: none since batch start, or the last pass is over an hour old. Print check: the same, but a foil
    roll change restarts its clock (the leak-test clock keeps running)."""
    at = due_at(batch, rows)
    return [text for ev, text in (("leak", "Leak test due"), ("print", "Print check due")) if at[ev] is None or now > at[ev]]


def window_id(kind: str, start: float) -> str:
    """The id of one missed hourly window, as reviews.jsonl names it: "leak|print <window start, local ISO>"."""
    return f"{kind} {datetime.fromtimestamp(start).isoformat(timespec='seconds')}"


def missed_windows(batch: dict, rows: list[dict], until: float) -> list[tuple[str, float, float]]:
    """Whole IPQC_EVERY_S windows up to `until`, per kind (the print windows restart at each foil change, the leak
    windows do not), with no leak test / no print check: (text, window start, window end), matching due()."""
    foils = [r["time"] for r in rows if r["event"] == "foil_change"]
    out = []
    for ev, text, marks in (("leak", "Leak test missed", [batch["time"], until]),
                            ("print", "Print check missed", [batch["time"]] + foils + [until])):
        for s, stop in zip(marks, marks[1:]):
            w = s
            while w + IPQC_EVERY_S <= stop:
                if not any(_done(r, ev) and w <= r["time"] < w + IPQC_EVERY_S for r in rows):
                    out.append((text, w, w + IPQC_EVERY_S))
                w += IPQC_EVERY_S
    return sorted(out, key=lambda m: m[1])  # stable: leak before print in the same window


def strip_counts(rows: list[dict]) -> Counter:
    """Verdict counts with each strip once: a re-checked strip was counted already as PASS; found bad on re-check it
    moved to the reject box, so it counts as that instead."""
    n = Counter(r["status"] for r in rows if not r.get("recheck"))
    moved = Counter(r["status"] for r in rows if r.get("recheck") and r["status"] in ("REJECT", "HOLD"))
    n["PASS"] -= min(n["PASS"], sum(moved.values()))  # a record's time range may start after the first verdicts
    n.update(moved)
    return n


def reconcile(rows: list[dict], batch: str, counts: dict | None = None, limits: dict | None = None) -> dict:
    """Log counts of this batch (LOCKED rows are not inspections). With the counts QA typed at End batch (strips at the
    bench, trays, foil in metres) each line is checked against QA's limits: status within / outside; without them, open."""
    n = strip_counts([r for r in rows if r.get("batch") == batch and r["status"] != "LOCKED"])
    out = {"inspected": sum(n.values()), "passed": n["PASS"], "rejected": n["REJECT"], "held": n["HOLD"],
           "no_strip": n["NO_STRIP"]}
    if counts is None:
        return dict(out, status="open")
    c, p, r, h = counts, n["PASS"], n["REJECT"], n["HOLD"]
    raw = [("Strip balance", c["received"] - (p + r + h + c["samples"] + c["unused"]), "strips"),
           ("Good tray", c["good_tray"] - p, "strips"),
           ("Reject box", c["reject_box"] - (r + h), "strips"),
           ("Foil", c["foil_issued"] - (c["foil_used"] + c["foil_returned"] + c["foil_destroyed"]), "foil")]
    lines = [{"name": k, "difference": round(d, 3), "limit": limits[u], "unit": "m" if u == "foil" else "strips",
              "outside": abs(round(d, 3)) > limits[u]} for k, d, u in raw]
    return dict(out, counts=counts, limits=limits, lines=lines,
                status="outside" if any(l["outside"] for l in lines) else "within")
