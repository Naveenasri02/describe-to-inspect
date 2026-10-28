"""What waits for a person, oldest first: the line board's "Waiting for a person" zone, and the sign gate.
Reads the product's files only (plus the live supervisor requests the app passes in); a missing file means nothing
of that kind is waiting, an unreadable one is shown as an item (never hidden)."""
import json
from datetime import datetime
from pathlib import Path

from d2i.audit import AuditLog, quarantine_list
from d2i.batch import Batches
from d2i.ipqc import check_events, missed_windows
from d2i.linestate import holder
from d2i.supervisor import hold_state

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


def window_id(kind: str, start: float) -> str:
    """The id of one missed hourly window, as reviews.jsonl names it: "leak|print <window start, local ISO>"."""
    return f"{kind} {datetime.fromtimestamp(start).isoformat(timespec='seconds')}"


def record_unsigned(batch_rows: list[dict], audit_rows: list[dict]) -> dict | None:
    """The end row of the last batch when it ended and no record was signed since; else None."""
    end = batch_rows[-1] if batch_rows and batch_rows[-1]["event"] == "end" else None
    if end and not any(e.get("action") == "sign" and _t(e["at"]) >= int(end["time"]) for e in audit_rows):
        return end
    return None


def open_items(product_dir: Path, now: float, requests=()) -> list[dict]:
    """Each item: {kind, text, since, who ("QA" | "Operator"), evidence}. `requests`: the Shift Supervisor's pending
    requests ({id, kind, reason}) — they live in the app, not in a file, so a caller that has none passes nothing."""
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
        for r in requests:
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
        rows = Batches(p).rows
        start = next((r for r in reversed(rows) if r["event"] == "start"), None)
        if not start:
            return
        end = next((r for r in rows if r["event"] == "end" and r["batch"] == start["batch"] and r["time"] >= start["time"]), None)
        reviewed = {r.get("window") for r in _rows(p / "reviews.jsonl")}
        for text, a, b in missed_windows(start, check_events(p, start["batch"]), end["time"] if end else now):
            if window_id("leak" if text.startswith("Leak") else "print", a) not in reviewed:
                add("missed_check", f"{text} {_hm(a)}–{_hm(b)} — QA review note needed", b,
                    evidence="record: In-process checks")

    def challenge_failed():
        runs = _rows(p / "challenge_runs.jsonl")
        if runs and runs[-1].get("passed") is False:
            bad = [f"{r['label']} (expected Rule {r['expected_rule']})" for r in runs[-1].get("results", []) if not r.get("passed")]
            add("challenge_failed", "Physical challenge failed" + (": " + ", ".join(bad) if bad else ""), runs[-1]["time"],
                evidence="record: Physical challenge")

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
