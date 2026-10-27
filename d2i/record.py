"""Printable shift record for QA to sign, and Ask-the-line answers. Muse Spark explains; it never decides."""
import json
from collections import Counter
from datetime import datetime
from html import escape as e
from pathlib import Path

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
        return out["summary"]
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
    return "<h2>Rejects per rule</h2>" + _table(["Rule", "Rejects"], [[k, c] for k, c in sorted(n.items())])


def _held_section(verdicts: list[dict]) -> str:
    rows = [[v["at"], v["strip_id"] if v["strip_id"] is not None else "", v["image_name"] or "",
             "; ".join(r["text"] for r in v["reasons"])] for v in verdicts if v["status"] == "HOLD"]
    return "<h2>Held strips</h2>" + _table(["Time", "Strip", "Image", "Reason"], rows)


def shift_record(product_dir: Path, since: float, until: float, batch: str, operator: str, client=None) -> str:
    verdicts, tests = _records(product_dir, since, until)
    cl = current(Path(product_dir) / "checklists.jsonl") or {}
    span = " to ".join(datetime.fromtimestamp(t).isoformat(timespec="seconds") for t in (since, until))
    parts = [
        "<h1>DRAFT — needs QA signature</h1>",
        f"<p>Product: {e(str(cl.get('product', '')))}<br>Batch: {e(batch)}<br>Operator: {e(operator)}<br>Time range: {e(span)}</p>",
        _checklist_section(cl), _selftest_section(tests), _totals_section(verdicts),
        _rejects_section(verdicts), _held_section(verdicts),
        f"<h2>Shift summary (written by Muse Spark — check before signing)</h2><p>{e(_summary(product_dir, verdicts, tests, client))}</p>",
        f"<p>{e(FOOTER)}</p>",
        "<div class='sig'><p>QA name: ______________________</p><p>Signature: ______________________</p>"
        "<p>Date: ______________________</p></div>",
    ]
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Shift record</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
