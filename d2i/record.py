"""Printable shift record for QA to sign, and Ask-the-line answers. Muse Spark explains; it never decides."""
import json
from collections import Counter
from datetime import datetime
from html import escape as e
from pathlib import Path

from d2i.audit import AuditLog, config_hashes, quarantine_list
from d2i.checklist import current
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
    return "<h2>Self-tests</h2>" + _table(["Time", "Kinds", "Caught / named", "Result", "Station locked?"], rows)


def _totals_section(verdicts: list[dict]) -> str:
    n = Counter(v["status"] for v in verdicts)
    return "<h2>Totals</h2>" + _table(list(STATUSES), [[n[s] for s in STATUSES]])


def _rejects_section(verdicts: list[dict]) -> str:
    n = Counter(f"Rule {r['rule']}" if r["rule"] else "Unlisted" for v in verdicts if v["status"] == "REJECT" for r in v["reasons"])
    return "<h2>Rejects per rule</h2>" + _table(["Rule", "Rejects"], [[k, c] for k, c in sorted(n.items(), key=lambda kv: (kv[0] == 'Unlisted', int(kv[0][5:]) if kv[0] != 'Unlisted' else 0))])


def _held_section(verdicts: list[dict]) -> str:
    rows = [[v["at"], v["strip_id"] if v["strip_id"] is not None else "", v["image_name"] or "",
             "; ".join(r["text"] for r in v["reasons"])] for v in verdicts if v["status"] == "HOLD"]
    return "<h2>Held strips</h2>" + _table(["Time", "Strip", "Image", "Reason"], rows)


def _config_section(product_dir: Path) -> str:
    h = config_hashes(product_dir)
    return "<h2>Configuration</h2>" + _table(["Item", "Value"], [[k, "" if v is None else v] for k, v in h.items()])


def _quarantine_section(product_dir: Path) -> str:
    q = quarantine_list(product_dir)
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
    return (f"<h2>Audit trail</h2><p>{e(chain)}</p>"
            "<p>The audit trail is tamper-evident: each entry carries the hash of the one before.</p>"
            + _table(["Time (UTC)", "User", "Action", "Reason"], [[g(r, "at"), g(r, "user"), g(r, "action"), g(r, "reason")] for r in rows])
            + "<h2>Physical challenge strips</h2>" + _table(["Time (UTC)", "User", "Strip", "Ejected?"], phys))


def shift_record(product_dir: Path, since: float, until: float, batch: str, operator: str, client=None,
                 audit: AuditLog | None = None) -> str:
    verdicts, tests = _records(product_dir, since, until)
    cl = current(Path(product_dir) / "checklists.jsonl") or {}
    span = " to ".join(datetime.fromtimestamp(t).isoformat(timespec="seconds") for t in (since, until))
    parts = [
        "<h1>DRAFT — needs QA signature</h1>",
        f"<p>Product: {e(str(cl.get('product', '')))}<br>Batch: {e(batch)}<br>Operator: {e(operator)}<br>Time range: {e(span)}</p>",
        _checklist_section(cl), _selftest_section(tests), _totals_section(verdicts),
        _rejects_section(verdicts), _held_section(verdicts),
        _config_section(product_dir), _quarantine_section(product_dir), _audit_sections(audit, since, until),
        f"<h2>Shift summary (written by Muse Spark — check before signing)</h2><p>{e(_summary(product_dir, verdicts, tests, client))}</p>",
        f"<p>{e(FOOTER)}</p>",
        "<div class='sig'><p>QA name: ______________________</p><p>Signature: ______________________</p>"
        "<p>Date: ______________________</p></div>",
    ]
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Shift record</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
