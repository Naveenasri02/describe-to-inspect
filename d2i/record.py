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
_FONT = ('@font-face{{font-family:"IBM Plex Sans";font-weight:{};font-display:swap;'
         'src:url(/static/fonts/IBMPlexSans-{}.woff2) format("woff2")}}')  # absolute: the srcdoc iframe is same-origin
CSS = "".join(_FONT.format(w, n) for w, n in ((400, "Regular"), (500, "Medium"), (600, "SemiBold"), (700, "Bold"))) + """
:root{--foil:#E3E6E9;--panel:#F8F9FA;--ink:#17202A;--steel:#5C6774;--rule:#C8CDD3;
--pass:#10804A;--reject:#C22A2A;--hold:#D4940A;--brand:#5B3FD6;--grey:#7B858F}
*{box-sizing:border-box}
html{background:#fff}
body{max-width:920px;margin:0 auto;padding:32px 36px 40px;background:#fff;color:var(--ink);
font:14px/1.5 "IBM Plex Sans","Segoe UI",system-ui,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--brand)}
header{border-bottom:2px solid var(--ink);padding-bottom:18px}
.title{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{font-size:28px;font-weight:600;margin:0;letter-spacing:-.01em}
.badge{display:inline-block;font-size:13px;font-weight:600;padding:4px 12px;border:1.5px solid;border-radius:4px}
.badge.draft{color:#5e4100;background:#fdf0d5;border-color:var(--hold)}
.meta{display:grid;grid-template-columns:1.4fr 1fr 2fr 1fr;margin:16px 0 0;background:var(--panel);
border:1px solid var(--rule);border-radius:4px}
.meta div{padding:9px 14px;border-right:1px solid var(--rule);min-width:0}
.meta div:last-child{border-right:0}
.meta dt{font-size:12px;color:var(--steel)}
.meta dd{margin:0;font-size:15px;font-weight:600}
.meta dd.cap::first-letter{text-transform:uppercase}
h2{font-size:16px;font-weight:600;color:var(--ink);margin:32px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--rule)}
p{margin:8px 0}
table{width:100%;border-collapse:collapse;margin:6px 0}
th{font-size:11.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--steel);text-align:left;
padding:6px 10px;border-bottom:1px solid var(--rule)}
td{padding:7px 10px;border-bottom:1px solid var(--rule);vertical-align:top}
th:first-child,td:first-child{padding-left:0}
td:first-child{white-space:nowrap}
th:last-child,td:last-child{padding-right:0}
.n{table-layout:fixed}
.cnt{width:auto;min-width:50%}
.n th,.n td,.cnt th:last-child,.cnt td:last-child{text-align:right}
td.none{color:var(--steel)}
.kv{display:grid;grid-template-columns:200px 1fr;margin:6px 0}
.kv dt,.kv dd{margin:0;padding:7px 0;border-bottom:1px solid var(--rule)}
.kv dt{color:var(--steel)}
.kv dd{overflow-wrap:anywhere}
.chip{display:inline-block;font-size:12px;font-weight:600;line-height:18px;padding:0 8px;border:1px solid;
border-radius:4px;white-space:nowrap}
.chip.pass{color:var(--pass);background:#e7f3ec}
.chip.reject{color:var(--reject);background:#fbe9e9}
.chip.hold{color:#8a5f00;border-color:var(--hold);background:#fdf3dd}
.chip.grey{color:var(--steel);border-color:var(--grey);background:#eef0f2}
.rules{margin:10px 0 0;padding:0;list-style:none}
.rules li{padding:6px 0;border-bottom:1px solid var(--rule)}
.chain{font-weight:600;padding:8px 12px;border-left:4px solid var(--pass);background:var(--panel)}
.chain.bad{border-left-color:var(--reject);color:var(--reject)}
.summary{background:var(--panel);border-left:4px solid var(--brand);padding:12px 16px}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin:12px 0}
.shots figure{margin:0;border:1px solid var(--rule);border-radius:4px;overflow:hidden}
.shots img{display:block;width:100%;height:auto;background:var(--foil)}
.shots figcaption{font-size:12px;color:var(--steel);padding:6px 9px 8px}
.shots figcaption b{display:block;color:var(--ink);font-weight:500}
.foot{color:var(--steel);font-size:13px;margin-top:28px}
.sig{display:grid;grid-template-columns:repeat(3,1fr);gap:32px;margin-top:24px}
.sig p{margin:56px 0 0;padding-top:6px;border-top:1px solid var(--ink);font-size:12px;color:var(--steel)}
body>p:last-child{margin-top:28px;font-size:11px;color:var(--steel);overflow-wrap:anywhere}
@media print{
@page{margin:14mm}
body{max-width:none;padding:0;font-size:12px}
*{box-shadow:none!important}
.meta,.chain,.summary,.badge,.chip{background:#fff!important}
tr,figure,.kv dt,.kv dd,.sig{break-inside:avoid}
h2{break-after:avoid}
}"""
PLAIN = {"PASS": "Passed", "REJECT": "Rejected", "HOLD": "Held", "LOCKED": "Locked", "NO_STRIP": "No strip",
         "PRINT_OK": "Print OK", "pass": "Passed", "fail": "Failed"}  # the Run screen's band words
TONE = {"Passed": "pass", "Print OK": "pass", "Clear": "pass", "Yes": "pass", "Rejected": "reject", "Failed": "reject",
        "Locked": "reject", "Not clear": "reject", "No": "reject", "Held": "hold", "Missed": "hold", "Not done": "hold"}
NAMES = {"model_sha256": "Model SHA-256", "checklist_version": "Checklist version", "checklist_sha256": "Checklist SHA-256"}


class _Html(str):
    """Markup made here (a chip): a table cell takes it as is."""


def _cell(c) -> str:
    return c if isinstance(c, _Html) else "–" if c is None else e(str(c))


def _chip(word: str) -> _Html:
    """A result word as a coloured chip; the border and the word carry it in black-and-white print."""
    word = PLAIN.get(word, word)
    return _Html(f'<span class="chip {TONE.get(word, "grey")}">{e(word)}</span>')


def _when(x) -> str:
    """A shown time, station-local: '28 Oct 2026, 13:29:14'. Naive ISO strings are local already; the audit log's UTC
    ones are converted. Display only: stored, hashed and signed values never change. Unreadable values show as they are."""
    if x is None:
        return "–"
    try:
        d = (datetime.fromtimestamp(x) if isinstance(x, (int, float)) else datetime.fromisoformat(x)).astimezone()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(x)
    return f"{d.day} {d:%b %Y, %H:%M:%S}"


def _span(a, b) -> str:
    x, y = _when(a), _when(b)
    return f"{x} to {y.removeprefix(x.split(', ')[0] + ', ')}"  # same day: the date once


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


def _table(head: list[str], rows: list[list], cls: str = "") -> str:
    """cls: 'n' right-aligns every column (counts), 'cnt' the last one."""
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    body = ("".join("<tr>" + "".join(f"<td>{_cell(c)}</td>" for c in r) + "</tr>" for r in rows)
            or f'<tr><td class="none" colspan="{len(head)}">Nothing recorded</td></tr>')
    attr = f' class="{cls}"' if cls else ""
    return f"<table{attr}><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _kv(pairs: list[list]) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{e(k)}</dt><dd>{_cell(v)}</dd>" for k, v in pairs) + "</dl>"


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
    return ("<h2>Checklist</h2>" + _kv([["Version", cl.get("version")], ["Approved by", cl.get("approved_by")],
                                       ["Approved at", _when(cl.get("approved_at"))], ["Sentence", cl.get("sentence")]])
            + f'<ul class="rules">{rules}</ul>')


def _selftest_section(tests: list[dict]) -> str:
    plain = lambda k: k.replace("_", " ")  # empty_pocket -> empty pocket, as the Run screen says it
    rows = [[_when(t["at"]), ", ".join(plain(r["kind"]) for r in t["results"]) or "none",
             "; ".join(f"{plain(r['kind'])}: caught {'yes' if r['caught'] else 'no'}, named {'yes' if r['named_right'] else 'no'}"
                       for r in t["results"]) or "none",
             _chip("Passed" if t["passed"] else "Failed"), "no" if t["passed"] else "yes"] for t in tests]
    for row, t in zip(rows, tests):
        if t.get("skipped"):
            row[3] = _Html(row[3] + e(" (did not cover: " + ", ".join(map(plain, t["skipped"])) + ")"))
    return "<h2>Self-tests</h2>" + _table(["Time", "Kinds", "Caught / named", "Result", "Station locked?"], rows)


def _totals_section(verdicts: list[dict]) -> str:
    n = Counter(v["status"] for v in verdicts)
    skipped = Counter(c for v in verdicts for c in v.get("skipped", []))
    return ("<h2>Totals</h2>" + _table([PLAIN[s] for s in STATUSES], [[n[s] for s in STATUSES]], "n")
            + (_table(["Not checked", "Strips"], sorted(skipped.items()), "cnt") if skipped else ""))


def _label(r: dict) -> str:
    tag = f" ({r['severity']})" if r.get("severity") else ""  # rows logged before severity existed have none
    return f"Rule {r['rule']}{tag}" if r["rule"] else "Unlisted"


def _rejects_section(verdicts: list[dict]) -> str:
    n = Counter(_label(r) for v in verdicts if v["status"] == "REJECT" for r in v["reasons"])
    return "<h2>Rejects per rule</h2>" + _table(["Rule", "Rejects"], [[k, c] for k, c in sorted(n.items(), key=lambda kv: (kv[0] == 'Unlisted', int(kv[0].split()[1]) if kv[0] != 'Unlisted' else 0))], "cnt")


def _held_section(verdicts: list[dict]) -> str:
    rows = [[_when(v["at"]), v["strip_id"] if v["strip_id"] is not None else "", v["image_name"] or "",
             "; ".join(r["text"] for r in v["reasons"])] for v in verdicts if v["status"] == "HOLD"]
    return "<h2>Held strips</h2>" + _table(["Time", "Strip", "Image", "Reason"], rows)


def _config_section(product_dir: Path) -> str:
    h = config_hashes(product_dir)
    return "<h2>Configuration</h2>" + _kv([[NAMES.get(k, k), v] for k, v in h.items()])


def _quarantine_section(product_dir: Path, audit: AuditLog | None = None) -> str:
    q = quarantine_list(product_dir, audit)
    if not q["strips"]:
        return ""
    rows = [[_when(v["at"]), v["strip_id"] if v["strip_id"] is not None else "", _chip(v["status"])] for v in q["strips"]]
    return (f"<h2>Quarantine</h2><p>Self-test failed at {e(_when(q['failed_at']))}; strips since the last passed "
            f"self-test ({e(_when(q['since']))}) need QA review.</p>" + _table(["Time", "Strip", "Verdict"], rows))


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
    phys = [[_when(g(r, "at")), g(r, "user"), d(r).get("strip_label", "?"), "yes" if d(r).get("ejected") else "no"]
            for r in rows if r.get("action") == "physical_challenge"]
    agent = any(str(r.get("action", "")).startswith("agent_") for r in rows)  # rows carrying the agent's text
    act = lambda a: str(a).replace("_", " ").replace("selftest", "self-test").capitalize()  # specimen_signed -> Specimen signed
    return (f"<h2>Audit trail</h2><p class=\"chain{'' if ok else ' bad'}\">{e(chain)}</p>"
            "<p>The audit trail is tamper-evident: each entry carries the hash of the one before.</p>"
            + _table(["Time", "User", "Action", "Reason"],
                     [[_when(g(r, "at")), g(r, "user"), act(g(r, "action")), g(r, "reason")] for r in rows])
            + ("<p>Built with Muse Spark Open</p>" if agent else "")
            + "<h2>Physical challenge strips</h2>" + _table(["Time", "User", "Strip", "Ejected?"], phys))


def _batch_section(product_dir: Path, audit: AuditLog | None = None, until: float = 0.0) -> str:
    """The open batch, else the last one: fields, clearance, leak results, verdict counts by status and rule."""
    rows = Batches(product_dir).rows
    start = next((r for r in reversed(rows) if r["event"] == "start"), None)
    if start is None:
        return "<h2>Batch</h2><p>No batch has been started</p>"
    mine = [r for r in rows if r.get("batch") == start["batch"] and r["at"] >= start["at"]]
    end = next((r for r in mine if r["event"] == "end"), None)
    clr = next((c for c in Batches(product_dir).clearances() if c["at"] == start["clearance_at"]), None)
    fields = [["Batch", start["batch"]], ["MFG", start["mfg"]], ["EXP", start["exp"]], ["MRP", f"₹ {start['mrp']}"],
              ["Started", f"{_when(start['at'])} by {start['by']}"],
              ["Ended", f"{_when(end['at'])} by {end['by']}" if end else "still open"],
              ["Line clearance", _Html(f"{e(_when(clr['at']))} by {e(clr['by'])} " + _chip("Clear" if clr["clear"] else "Not clear"))
               if clr else "not found"]]
    leaks = [[_when(r["at"]), r["by"], _chip(r["result"]), r["note"]] for r in mine if r["event"] == "leak"]
    prints = [r for r in _lines(Path(product_dir) / "prints.jsonl") if r.get("batch") == start["batch"]]
    ev = check_events(product_dir, start["batch"])
    ts = lambda a: datetime.fromisoformat(a).timestamp()
    arows = [r for r in (audit.entries() if audit else []) if isinstance(r.get("details"), dict)]
    st = next((t for t in _lines(Path(product_dir) / "selftests.jsonl") if t.get("passed") and t.get("time", 0) >= start["time"]), None)
    st_by = next((r["user"] for r in arows if r["action"] == "selftest" and r["details"].get("passed") and ts(r["at"]) >= start["time"] - 1), "?")
    ok = next((r for r in prints if r["status"] == "PRINT_OK"), None)
    sig = next((r for r in arows if r["action"] == "specimen_signed" and r["details"].get("batch") == start["batch"]), None)
    release = [["Self-test for this batch", _when(st["at"]) if st else _chip("Not done"), st_by if st else ""],
               ["Start-up print check", _when(ok["at"]) if ok else _chip("Not done"), ok["by"] if ok else ""],
               ["QA signature on the start-up specimen", _when(sig["at"]) if sig else _chip("Not done"), sig["user"] if sig else ""]]
    checks = [(r["time"], [_when(r["at"]), "Leak test" if r["event"] == "leak" else "Print check", r["by"],
                           _chip(r["result"] if r["event"] == "leak" else r["status"])]) for r in ev if r["event"] != "foil_change"]
    checks += [(r["time"], [_when(r["time"]), "Foil roll changed", r["by"], ""]) for r in ev if r["event"] == "foil_change"]
    checks += [(a, [_span(a, b), text, "", _chip("Missed")]) for text, a, b in missed_windows(start, ev, end["time"] if end else until)]
    checks = [row for _, row in sorted(checks, key=lambda c: (c[0], c[1][1]))]  # by time: shown times do not sort as text
    tot = end.get("totals") if end else None
    recon = (_table(["Inspected", "Passed", "Rejected", "Held", "No strip", "Balanced"],
                    [[tot[k] for k in ("inspected", "passed", "rejected", "held", "no_strip")]
                     + [_chip("Yes" if tot["balanced"] else "No")]], "n")
             if tot else "<p>Not reconciled (ended before reconciliation)</p>" if end
             else "<p>Not reconciled: the batch is still open</p>")
    vs = [v for v in _lines(Path(product_dir) / "log.jsonl") if v.get("batch") == start["batch"]]
    by_status = Counter(v["status"] for v in vs)
    by_rule = Counter(_label(r) for v in vs for r in v["reasons"])
    shots = [v for v in vs if v.get("photo")][-12:]  # the 12 latest reject photos, each with its reasons
    photos = "".join(f'<figure><a href="/rejects/{e(v["photo"])}" target="_blank" rel="noopener">'
                     f'<img src="/rejects/{e(v["photo"])}" alt="Reject photo"></a>'
                     f'<figcaption><b>{e(_when(v["at"]))}</b>{e("; ".join(r["text"] for r in v["reasons"]))}</figcaption></figure>'
                     for v in shots)
    return ("<h2>Batch</h2>" + _kv(fields) + "<h2>Release</h2>"
            + _table(["Item", "Time", "By"], release) + "<h2>In-process checks</h2>"
            + _table(["Time", "Check", "By", "Result"], checks) + "<h2>Leak test</h2>"
            + _table(["Time", "By", "Result", "Note"], leaks)
            + "<h2>Print checks</h2>" + _table(["Time", "By", "Result", "Read on strip", "Reasons"], [
                [_when(r["at"]), r["by"], _chip(r["status"]),
                 " / ".join(str(r["read"].get(k) or "–") for k in ("batch", "mfg", "exp", "mrp")), "; ".join(r["reasons"])]
                for r in prints])
            + "<h2>Batch verdicts</h2>" + _table([PLAIN[s] for s in STATUSES], [[by_status[s] for s in STATUSES]], "n")
            + "<h2>Reconciliation</h2>" + recon
            + _table(["Reason", "Count"], sorted(by_rule.items()), "cnt")
            + ('<h2>Reject photos</h2><div class="shots">' + photos + "</div>" if photos else ""))


def shift_record(product_dir: Path, since: float, until: float, batch: str, operator: str, client=None,
                 audit: AuditLog | None = None) -> str:
    verdicts, tests = _records(product_dir, since, until)
    cl = current(Path(product_dir) / "checklists.jsonl") or {}
    shown = batch or next((r["batch"] for r in reversed(Batches(product_dir).rows) if r["event"] == "start"), "")  # as the Batch section
    meta = (("Product", cl.get("product"), " class='cap'"), ("Batch", shown, ""), ("Shift", _span(since, until), ""),
            ("Operator", operator, ""))
    parts = [
        "<header><div class='title'><h1>Shift record</h1><span class='badge draft'>DRAFT — needs QA signature</span></div>"
        "<dl class='meta'>" + "".join(f"<div><dt>{k}</dt><dd{c}>{e(str(v or '–'))}</dd></div>" for k, v, c in meta) + "</dl></header>",
        _batch_section(product_dir, audit, until),
        _checklist_section(cl), _selftest_section(tests), _totals_section(verdicts),
        _rejects_section(verdicts), _held_section(verdicts),
        _config_section(product_dir), _quarantine_section(product_dir, audit), _audit_sections(audit, since, until),
        "<h2>Shift summary (written by Muse Spark — check before signing)</h2>"
        f"<p class='summary'>{e(_summary(product_dir, verdicts, tests, client))}</p>",
        f"<p class='foot'>{e(FOOTER)}</p>",
        "<div class='sig'><p>QA name</p><p>Signature</p><p>Date</p></div>",
    ]
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Shift record</title><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"
