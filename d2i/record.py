"""Printable shift record for QA to sign, and Ask-the-line answers. Muse Spark explains; it never decides."""
import json
from collections import Counter
from datetime import datetime
from html import escape as e
from pathlib import Path

from d2i.audit import AuditLog, config_hashes, quarantine_list
from d2i.batch import Batches
from d2i.checklist import current
from d2i.ipqc import check_events, missed_windows
from d2i.meta_api import BudgetExceeded, MetaError

STATUSES = ("PASS", "REJECT", "HOLD", "LOCKED", "NO_STRIP")
FOOTER = "This bench adds to the physical challenge test; it does not replace it."
RULE = ("Answer only from these records; quote the rule number, pocket and time; "
        "say 'not in the records' if unknown; never change a verdict")
SUMMARY_RULE = ("Summarise these records in exactly 3 sentences. Use only these records. "
                "You only summarise: never change or re-label a verdict")
CSS = ("body{font:14px Arial,sans-serif;color:#000;background:#fff;margin:24px}"
       "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #000;padding:4px 8px;text-align:left}"
       "h1{font-size:20px}h2{font-size:16px;margin-top:20px}.sig{margin-top:40px}")


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _ts(row: dict) -> float:
    """Self-tests carry a Unix `time`; verdicts carry `at`, a local-time ISO string (naive = local, as station writes it)."""
    return row["time"] if "time" in row else datetime.fromisoformat(row["at"]).timestamp()


def _records(product_dir: Path, since=None, until=None):
    p = Path(product_dir)
    pick = lambda rows: [r for r in rows if (since is None or _ts(r) >= since) and (until is None or _ts(r) <= until)]
    return pick(_lines(p / "log.jsonl")), pick(_lines(p / "selftests.jsonl"))


def _compact(rows: list[dict]) -> str:
    return "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)


def _prompt(product_dir: Path, verdicts, tests, last: int) -> str:
    cl = current(Path(product_dir) / "checklists.jsonl") or {}
    return (f"RULES:\n{_compact([{'rules': cl.get('rules', []), 'version': cl.get('version')}])}\n"
            f"VERDICTS:\n{_compact(verdicts[-last:])}\nSELF-TESTS:\n{_compact(tests[-last:])}")


def ask(question: str, product_dir: Path, client, last: int = 200) -> str:
    verdicts, tests = _records(product_dir)
    user = _prompt(product_dir, verdicts, tests, last) + f"\nQUESTION: {question}"
    return client.spark_json(RULE, user, {"type": "object", "additionalProperties": False, "required": ["answer"],
                                          "properties": {"answer": {"type": "string"}}}, name="answer")["answer"]


def _table(head: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


def _summary(product_dir, verdicts, tests, client) -> str:
    if client is None:
        return "Summary not available"
    try:
        out = client.spark_json(SUMMARY_RULE, _prompt(product_dir, verdicts, tests, 200),
                                {"type": "object", "additionalProperties": False, "required": ["summary"],
                                 "properties": {"summary": {"type": "string"}}}, name="summary")
        return out.get("summary") or "Summary not available"
    except (MetaError, BudgetExceeded):
        return "Summary not available"


def _checklist_section(cl: dict) -> str:
    rules = "".join(f"<li>{e(r)}</li>" for r in cl.get("rules", []))
    return (f"<h2>Checklist</h2><p>Version {e(str(cl.get('version')))}; approved by {e(str(cl.get('approved_by')))} "
            f"at {e(str(cl.get('approved_at')))}</p><p>Sentence: {e(str(cl.get('sentence')))}</p><ul>{rules}</ul>")


def _selftest_section(tests: list[dict]) -> str:
    rows = [[t["at"], ", ".join(r["kind"] for r in t["results"]) or "none",
             ", ".join(f"{r['kind']}: caught {'yes' if r['caught'] else 'no'}, named {'yes' if r['named_right'] else 'no'}"
                       for r in t["results"]) or "none",
             "PASS" if t["passed"] else "FAIL", "no" if t["passed"] else "yes"] for t in tests]
    for row, t in zip(rows, tests):
        if t.get("skipped"):
            row[3] += " (did not cover: " + ", ".join(t["skipped"]) + ")"
    return "<h2>Self-tests</h2>" + _table(["Time", "Kinds", "Caught / named", "Result", "Station locked?"], rows)


def _totals_section(verdicts: list[dict]) -> str:
    n = Counter(v["status"] for v in verdicts)
    skipped = Counter(c for v in verdicts for c in v.get("skipped", []))
    return ("<h2>Totals</h2>" + _table(list(STATUSES), [[n[s] for s in STATUSES]])
            + (_table(["Not checked", "Strips"], sorted(skipped.items())) if skipped else ""))


def _label(r: dict) -> str:
    tag = f" ({r['severity']})" if r.get("severity") else ""  # rows logged before severity existed have none
    return f"Rule {r['rule']}{tag}" if r["rule"] else "Unlisted"


def _rejects_section(verdicts: list[dict]) -> str:
    n = Counter(_label(r) for v in verdicts if v["status"] == "REJECT" for r in v["reasons"])
    return "<h2>Rejects per rule</h2>" + _table(["Rule", "Rejects"], [[k, c] for k, c in sorted(n.items(), key=lambda kv: (kv[0] == 'Unlisted', int(kv[0].split()[1]) if kv[0] != 'Unlisted' else 0))])


def _held_section(verdicts: list[dict]) -> str:
    rows = [[v["at"], v["strip_id"] if v["strip_id"] is not None else "", v["image_name"] or "",
             "; ".join(r["text"] for r in v["reasons"])] for v in verdicts if v["status"] == "HOLD"]
    return "<h2>Held strips</h2>" + _table(["Time", "Strip", "Image", "Reason"], rows)


def _config_section(product_dir: Path) -> str:
    h = config_hashes(product_dir)
    return "<h2>Configuration</h2>" + _table(["Item", "Value"], [[k, "" if v is None else v] for k, v in h.items()])


def _quarantine_section(product_dir: Path, audit: AuditLog | None = None) -> str:
    q = quarantine_list(product_dir, audit)
    if not q["strips"]:
        return ""
    rows = [[v["at"], v["strip_id"] if v["strip_id"] is not None else "", v["status"]] for v in q["strips"]]
    return (f"<h2>Quarantine</h2><p>Self-test failed at {e(str(q['failed_at']))}; strips since the last passed "
            f"self-test ({e(str(q['since']))}) need QA review.</p>" + _table(["Time", "Strip", "Verdict"], rows))


def _audit_sections(audit, since: float, until: float) -> str:
    if audit is None:
        return "<h2>Audit trail</h2><p>No audit log</p>"
    ok, bad = audit.verify()  # first: a damaged file must still render, with BROKEN shown
    all_rows = audit.entries()
    chain = f"Audit chain: intact ({len(all_rows)} entries)" if ok else f"Audit chain: BROKEN at entry {bad}"

    def in_range(r):
        try:
            return since <= datetime.fromisoformat(r["at"]).timestamp() <= until
        except (KeyError, TypeError, ValueError, OSError):
            return True  # unreadable time: show the row rather than hide it

    rows = [r for r in all_rows if in_range(r)]
    g = lambda r, k: r.get(k, "?")
    d = lambda r: r["details"] if isinstance(r.get("details"), dict) else {}
    phys = [[g(r, "at"), g(r, "user"), d(r).get("strip_label", "?"), "yes" if d(r).get("ejected") else "no"]
            for r in rows if r.get("action") == "physical_challenge"]
    agent = any(str(r.get("action", "")).startswith("agent_") for r in rows)  # rows carrying the agent's text
    return (f"<h2>Audit trail</h2><p>{e(chain)}</p>"
            "<p>The audit trail is tamper-evident: each entry carries the hash of the one before.</p>"
            + _table(["Time (UTC)", "User", "Action", "Reason"], [[g(r, "at"), g(r, "user"), g(r, "action"), g(r, "reason")] for r in rows])
            + ("<p>Built with Muse Spark Open</p>" if agent else "")
            + "<h2>Physical challenge strips</h2>" + _table(["Time (UTC)", "User", "Strip", "Ejected?"], phys))


def _batch_section(product_dir: Path, audit: AuditLog | None = None, until: float = 0.0) -> str:
    """The open batch, else the last one: fields, clearance, leak results, verdict counts by status and rule."""
    rows = Batches(product_dir).rows
    start = next((r for r in reversed(rows) if r["event"] == "start"), None)
    if start is None:
        return "<h2>Batch</h2><p>No batch has been started</p>"
    mine = [r for r in rows if r.get("batch") == start["batch"] and r["at"] >= start["at"]]
    end = next((r for r in mine if r["event"] == "end"), None)
    clr = next((c for c in Batches(product_dir).clearances() if c["at"] == start["clearance_at"]), None)
    fields = [["Batch", start["batch"]], ["MFG", start["mfg"]], ["EXP", start["exp"]], ["MRP (Rs.)", start["mrp"]],
              ["Started", f"{start['at']} by {start['by']}"], ["Ended", f"{end['at']} by {end['by']}" if end else "still open"],
              ["Line clearance", f"{clr['at']} by {clr['by']}: {'clear' if clr['clear'] else 'NOT clear'}" if clr else "not found"]]
    leaks = [[r["at"], r["by"], r["result"].upper(), r["note"]] for r in mine if r["event"] == "leak"]
    prints = [r for r in _lines(Path(product_dir) / "prints.jsonl") if r.get("batch") == start["batch"]]
    ev = check_events(product_dir, start["batch"])
    ts = lambda a: datetime.fromisoformat(a).timestamp()
    arows = [r for r in (audit.entries() if audit else []) if isinstance(r.get("details"), dict)]
    st = next((t for t in _lines(Path(product_dir) / "selftests.jsonl") if t.get("passed") and t.get("time", 0) >= start["time"]), None)
    st_by = next((r["user"] for r in arows if r["action"] == "selftest" and r["details"].get("passed") and ts(r["at"]) >= start["time"] - 1), "?")
    ok = next((r for r in prints if r["status"] == "PRINT_OK"), None)
    sig = next((r for r in arows if r["action"] == "specimen_signed" and r["details"].get("batch") == start["batch"]), None)
    release = [["Self-test for this batch", st["at"] if st else "not done", st_by if st else ""],
               ["Start-up print check", ok["at"] if ok else "not done", ok["by"] if ok else ""],
               ["QA signature on the start-up specimen", sig["at"] if sig else "not done", sig["user"] if sig else ""]]
    stamp = lambda t: datetime.fromtimestamp(t).isoformat(timespec="seconds")
    checks = [[r["at"], "Leak test" if r["event"] == "leak" else "Print check", r["by"],
               (r["result"] if r["event"] == "leak" else r["status"]).upper()] for r in ev if r["event"] != "foil_change"]
    checks += [[stamp(r["time"]), "Foil roll changed", r["by"], ""] for r in ev if r["event"] == "foil_change"]
    checks += [[f"{stamp(a)} to {stamp(b)}", "No leak test or print check in this hour", "", "missed"]
               for a, b in missed_windows(start, ev, end["time"] if end else until)]
    tot = end.get("totals") if end else None
    recon = (_table(["Inspected", "Passed", "Rejected", "Held", "No strip", "Balanced"],
                    [[tot[k] for k in ("inspected", "passed", "rejected", "held", "no_strip")] + ["yes" if tot["balanced"] else "NO"]])
             if tot else "<p>Not reconciled: the batch is still open</p>")
    vs = [v for v in _lines(Path(product_dir) / "log.jsonl") if v.get("batch") == start["batch"]]
    by_status = Counter(v["status"] for v in vs)
    by_rule = Counter(_label(r) for v in vs for r in v["reasons"])
    return ("<h2>Batch</h2>" + _table(["Item", "Value"], fields) + "<h2>Release</h2>"
            + _table(["Item", "Time", "By"], release) + "<h2>In-process checks</h2>"
            + _table(["Time", "Check", "By", "Result"], sorted(checks)) + "<h2>Leak test</h2>"
            + _table(["Time", "By", "Result", "Note"], leaks)
            + "<h2>Print checks</h2>" + _table(["Time", "By", "Result", "Read on strip", "Reasons"], [
                [r["at"], r["by"], r["status"], f"{r['read'].get('batch')} / {r['read'].get('mfg')} / {r['read'].get('exp')} / {r['read'].get('mrp')}",
                 "; ".join(r["reasons"])] for r in prints])
            + "<h2>Batch verdicts</h2>" + _table(list(STATUSES), [[by_status[s] for s in STATUSES]])
            + "<h2>Reconciliation</h2>" + recon
            + _table(["Reason", "Count"], sorted(by_rule.items())))


def shift_record(product_dir: Path, since: float, until: float, batch: str, operator: str, client=None,
                 audit: AuditLog | None = None) -> str:
    verdicts, tests = _records(product_dir, since, until)
    cl = current(Path(product_dir) / "checklists.jsonl") or {}
    span = " to ".join(datetime.fromtimestamp(t).isoformat(timespec="seconds") for t in (since, until))
    parts = [
        "<h1>DRAFT — needs QA signature</h1>", _batch_section(product_dir, audit, until),
        f"<p>Product: {e(str(cl.get('product', '')))}<br>Batch: {e(batch)}<br>Operator: {e(operator)}<br>Time range: {e(span)}</p>",
        _checklist_section(cl), _selftest_section(tests), _totals_section(verdicts),
        _rejects_section(verdicts), _held_section(verdicts),
        _config_section(product_dir), _quarantine_section(product_dir, audit), _audit_sections(audit, since, until),
        f"<h2>Shift summary (written by Muse Spark — check before signing)</h2><p>{e(_summary(product_dir, verdicts, tests, client))}</p>",
        f"<p>{e(FOOTER)}</p>",
        "<div class='sig'><p>QA name: ______________________</p><p>Signature: ______________________</p>"
        "<p>Date: ______________________</p></div>",
    ]
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Shift record</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
