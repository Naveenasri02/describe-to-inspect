"""What waits for a person, oldest first: the line board's "Waiting for a person" zone, and the sign gate.
Reads the product's files only; a missing file means nothing of that kind is waiting, an unreadable one is shown as an
item (never hidden)."""
import json
from datetime import datetime
from pathlib import Path

from d2i.audit import AuditLog, quarantine_list
from d2i.batch import Batches, batch_no
from d2i.ipqc import check_events, missed_windows, window_id  # noqa: F401 (window_id: the reviews.jsonl key, used here)
from d2i.linestate import holder
from d2i.supervisor import hold_state, pending_requests

KINDS = ("line_held", "quarantine", "request", "recheck", "reconciliation", "missed_check", "challenge_failed",
         "record_unsigned")
_NAMES = {"line_held": "hold", "quarantine": "quarantine", "request": "supervisor request", "recheck": "re-check",
          "reconciliation": "reconciliation", "missed_check": "in-process check", "challenge_failed": "challenge",
          "record_unsigned": "batch"}


def _t(at) -> float:
    """Unix time of an ISO string: naive = station-local (verdicts, batches), aware = UTC (audit, hold.json)."""
    return datetime.fromisoformat(at).timestamp()


def _hm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def missed_checks(product_dir: Path, now: float, batch: str | None = None) -> list[dict]:
    """A batch's missed hourly windows with no QA review note yet (default: the last started batch):
    {kind ("leak" | "print"), text, start, end, window (the reviews.jsonl key), batch}, oldest first."""
    p, batch = Path(product_dir), batch and batch_no(batch)
    rows = Batches(p).rows
    start = next((r for r in reversed(rows) if r["event"] == "start" and (not batch or r["batch"] == batch)), None)
    if not start:
        return []
    end = next((r for r in rows if r["event"] == "end" and r["batch"] == start["batch"] and r["time"] >= start["time"]), None)
    reviewed = {r.get("window") for r in _rows(p / "reviews.jsonl")}
    out = []
    for text, a, b in missed_windows(start, check_events(p, start["batch"]), end["time"] if end else now):
        kind = "leak" if text.startswith("Leak") else "print"
        if window_id(kind, a) not in reviewed:
            out.append({"kind": kind, "text": text, "start": a, "end": b, "window": window_id(kind, a),
                        "batch": start["batch"]})
    return out


def missed_text(m: dict) -> str:
    return f"{m['text']} {_hm(m['start'])}–{_hm(m['end'])} — QA review note needed"


def last_started(product_dir: Path) -> str | None:
    return next((r["batch"] for r in reversed(Batches(product_dir).rows) if r["event"] == "start"), None)


def challenge_failure(product_dir: Path, audit_rows: list[dict]) -> dict | None:
    """The physical challenge failure still waiting for QA: {text, since, evidence}; None when the last run passed or a
    QA close note (an ended batch's failure) came at or after it."""
    p = Path(product_dir)
    runs = _rows(p / "challenge_runs.jsonl")
    miss = next((t for t in reversed(_rows(p / "selftests.jsonl")) if t.get("challenge")), None)
    if miss and not miss.get("passed") and (not runs or miss["time"] > runs[-1]["time"]):
        # a miss locks at once; its run row is written only when the Ejected tick is recorded
        f = {"text": "Physical challenge failed: " + "; ".join(miss.get("failed") or [miss["challenge"]]),
             "since": miss["time"], "evidence": "record: Self-tests"}
    elif runs and runs[-1].get("passed") is False:
        bad = [f"{r['label']} (expected Rule {r['expected_rule']})" for r in runs[-1].get("results", []) if not r.get("passed")]
        f = {"text": "Physical challenge failed" + (": " + ", ".join(bad) if bad else ""), "since": runs[-1]["time"],
             "evidence": "record: Physical challenge"}
    else:
        return None
    closed = any(e.get("action") == "challenge_closed" and _t(e["at"]) >= int(f["since"]) for e in audit_rows)
    return None if closed else f


def signed(e: dict, batch: str) -> bool:
    """A signature on that batch's record."""
    return e.get("action") == "sign" and isinstance(e.get("details"), dict) and e["details"].get("batch") == batch


def record_unsigned(batch_rows: list[dict], audit_rows: list[dict]) -> dict | None:
    """The end row of the last batch when it ended and its record was not signed since; else None."""
    end = batch_rows[-1] if batch_rows and batch_rows[-1]["event"] == "end" else None
    if end and not any(signed(e, end["batch"]) and _t(e["at"]) >= int(end["time"]) for e in audit_rows):
        return end
    return None


def open_items(product_dir: Path, now: float) -> list[dict]:
    """Each item: {kind, text, since, who ("QA" | "Operator"), evidence}."""
    p = Path(product_dir)
    audit = AuditLog(p / "audit.jsonl")
    items = []

    def add(kind, text, since, who="QA", evidence=None):
        items.append({"kind": kind, "text": text, "since": float(since), "who": who, "evidence": evidence})

    def line_held():
        held, by = hold_state(p)
        if not held:
            return
        try:
            since = _t(json.loads((p / "hold.json").read_text(encoding="utf-8"))["at"])
        except Exception:  # unreadable hold.json counts as held; its time is not known
            since = now
        row = next((e for e in reversed(audit.entries()) if e.get("action") == "hold_line" and e.get("user") == by),
                   None) if by != "supervisor" else None  # the supervisor's hold has no hold_line row
        add("line_held", f"Line held by {holder(by)}" + (f" — {row['reason']}" if row and row.get("reason") else ""),
            since, evidence=f"audit #{row['seq']}" if row else None)

    def quarantine():
        q = quarantine_list(p, audit)
        if q["strips"]:
            add("quarantine", f"Quarantine: {len(q['strips'])} strips since the last passed self-test — QA release needed",
                _t(q["failed_at"]), evidence="record: Quarantine")

    def request():
        steps = [e for e in audit.entries() if e.get("action") == "agent_step"]
        for r in pending_requests(p).values():  # the Shift Supervisor's requests waiting for QA
            row = next((e for e in reversed(steps) if r["id"] in str((e.get("details") or {}).get("result_summary"))), None)
            add("request", f"Shift Supervisor asks QA to approve: {r['kind'].replace('_', ' ')} — "
                + str(r.get("reason", "")).removeprefix("Agent request: "),
                _t(row["at"]) if row else now, evidence=f"audit #{row['seq']}" if row else None)

    def recheck():
        rc = json.loads((p / "recheck.json").read_text(encoding="utf-8")) if (p / "recheck.json").exists() else None
        b = Batches(p).open if rc and rc.get("open") else None
        if b and rc["since"] >= b["time"]:  # a window left open by another batch is not this batch's re-check
            add("recheck", f"Re-check {rc['done']} of {rc['total']}: strips no. {rc['from_strip']}–{rc['to_strip']}",
                rc["since"], who="Operator")

    def reconciliation():
        end = next((r for r in reversed(Batches(p).rows) if r["event"] == "end"), None)
        tot = (end or {}).get("totals") or {}
        if (tot.get("status") == "outside" or tot.get("balanced") is False) and not tot.get("investigated"):
            add("reconciliation", f"Reconciliation outside limit — batch {end['batch']}, no investigation reason",
                end["time"], evidence="record: Reconciliation")

    def missed_check():
        for m in missed_checks(p, now):
            add("missed_check", missed_text(m), m["end"], evidence="record: In-process checks")

    def challenge_failed():
        f = challenge_failure(p, audit.entries())
        if f:
            add("challenge_failed", f["text"], f["since"], evidence=f["evidence"])

    def unsigned():
        end = record_unsigned(Batches(p).rows, audit.entries())
        if end:
            add("record_unsigned", f"Record unsigned — batch {end['batch']} ended {_hm(end['time'])}", end["time"],
                evidence="record: Sign")

    for kind, fn in zip(KINDS, (line_held, quarantine, request, recheck, reconciliation, missed_check, challenge_failed,
                                unsigned)):
        try:
            fn()
        except Exception:  # unknown is shown, never hidden
            add(kind, f"Cannot read the {_NAMES[kind]} records — check by hand", now)
    return sorted(items, key=lambda i: i["since"])
