import json
from datetime import datetime

import pytest

from d2i.checklist import approve
from d2i.meta_api import MetaError
from d2i.audit import AuditLog, config_hashes
from d2i.batch import Batches
from d2i.record import ask, shift_record
from tests.test_station import CHECKLIST

T0 = 1_800_000_000.0
R2 = {"rule": 2, "check": "empty_pocket", "pocket": 7, "text": "Rule 2 failed: pocket 7 is empty"}
R1 = {"rule": 1, "check": "count", "pocket": None, "text": "Rule 1 failed: found 9 pockets, expected 10"}


def _at(t):
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _human(t):
    """How the record shows a time: local, e.g. 28 Oct 2026, 13:29:14."""
    d = datetime.fromtimestamp(t)
    return f"{d.day} {d:%b %Y, %H:%M:%S}"


def _verdict(i, status, reasons=()):
    return {"status": status, "reasons": list(reasons), "pocket_scores": [], "strip_id": i,
            "image_name": f"s{i}.png", "at": _at(T0 + i), "checklist_version": 1}


@pytest.fixture
def product(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    rows = [_verdict(i, "PASS") for i in range(3)] + [_verdict(3, "REJECT", [R2]), _verdict(4, "REJECT", [R2]),
                                                      _verdict(5, "HOLD", [R1])]
    (tmp_path / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    test = {"passed": True, "results": [{"kind": "empty_pocket", "pocket": 3, "caught": True, "named_right": True}],
            "good_passed": True, "failed": [], "time": T0 + 1, "at": _at(T0 + 1), "checklist_version": 1, "taught_at": T0}
    (tmp_path / "selftests.jsonl").write_text(json.dumps(test) + "\n", encoding="utf-8")
    return tmp_path


class Fake:
    def __init__(self, out=None, err=None):
        self.out, self.err, self.calls = out, err, []

    def spark_json(self, system, user, schema, **kw):
        self.calls.append((system, user, schema, kw))
        if self.err:
            raise self.err
        return self.out


def test_record_contents(product):
    html = shift_record(product, T0 - 10, T0 + 100, "B-42", "Asha")
    for s in ["DRAFT — needs QA signature", "Rule 2", "<td>2</td>", "vitamin C strip", "B-42", "Asha",
              "signature", "Priya", "Ten tablets in two rows of five.", "s5.png", "Rule 1 failed",
              "This bench adds to the physical challenge test; it does not replace it."]:
        assert s in html
    assert "Summary not available" in html


def test_record_filters_by_time(product):
    html = shift_record(product, T0 + 3.5, T0 + 100, "B", "A")
    assert "Rule 1 failed" in html  # held strip 5 is in range
    assert "<td>0</td><td>1</td><td>1</td><td>0</td><td>0</td>" in html  # strips 4 (REJECT) and 5 (HOLD) only


def test_ask_sends_records_and_returns_answer(product):
    fake = Fake({"answer": "Rule 2, pocket 7."})
    assert ask("Why was strip 3 rejected?", product, fake) == "Rule 2, pocket 7."
    system, user, schema, kw = fake.calls[0]
    assert "Rule 2 failed: pocket 7 is empty" in user and "Why was strip 3 rejected?" in user
    assert "never change a verdict" in system and "max_out" not in kw


def test_ask_propagates_errors(product):
    with pytest.raises(MetaError):
        ask("q", product, Fake(err=MetaError("down")))


def test_record_without_client_says_not_available(product):
    assert "Summary not available" in shift_record(product, 0, T0 + 100, "B", "A")


def test_record_with_summary_and_escaping(product):
    fake = Fake({"summary": "Three passed. <b>x</b>"})
    html = shift_record(product, 0, T0 + 100, "B<1>", "A", client=fake)
    assert "Shift summary (written by Muse Spark — check before signing)" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "B&lt;1&gt;" in html and "B<1>" not in html


def test_record_summary_error_falls_back(product):
    html = shift_record(product, 0, T0 + 100, "B", "A", client=Fake(err=MetaError("down")))
    assert "Summary not available" in html


def test_record_shows_chain_and_hashes(product):
    (product / "model.pt").write_bytes(b"weights")
    audit = AuditLog(product / "audit.jsonl")
    audit.append("teach", "Asha", {})
    audit.append("sign", "Priya", {})
    sha = config_hashes(product)["model_sha256"]
    html = shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    assert "Audit chain: intact (2 entries)" in html and sha[:12] in html and "tamper-evident" in html
    (product / "audit.jsonl").write_text((product / "audit.jsonl").read_text().replace("Asha", "Mallory"))
    assert "Audit chain: BROKEN at entry 1" in shift_record(product, 0, T0 + 100, "B", "A", audit=audit)


def test_agent_text_in_the_audit_table_carries_the_credit(product):
    audit = AuditLog(product / "audit.jsonl")
    audit.append("teach", "Asha", {})
    assert "Built with Muse Spark Open" not in shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    audit.append("agent_approve", "Priya", {"kind": "reteach"}, "Agent request: lamp moved")
    html = shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    assert "Agent request: lamp moved" in html and "<p>Built with Muse Spark Open</p>" in html


def test_record_builds_when_network_is_down(product):
    import requests
    from d2i.meta_api import Budget, MetaClient
    from tests.conftest import FakeHTTP
    client = MetaClient(Budget(product / "s.json"), key="k",
                        http=FakeHTTP([requests.Timeout("read timed out")] * 2))  # one retry
    html = shift_record(product, 0, T0 + 100, "B", "A", client=client)
    assert "DRAFT — needs QA signature" in html and "Summary not available" in html


def test_record_summary_missing_key_falls_back(product):
    assert "Summary not available" in shift_record(product, 0, T0 + 100, "B", "A", client=Fake({"other": "x"}))


def test_rejects_sorted_by_rule_number(product):
    r10 = dict(R2, rule=10)
    rows = [_verdict(9, "REJECT", [R2]), _verdict(10, "REJECT", [r10])]
    (product / "log.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    html = shift_record(product, 0, T0 + 100, "B", "A")
    assert html.index("Rule 2<") < html.index("Rule 10<")


def test_record_renders_broken_chain_with_stripped_keys(product):
    audit = AuditLog(product / "audit.jsonl")
    for i in range(3):
        audit.append("act", "Asha", {"i": i})
    rows = [json.loads(x) for x in (product / "audit.jsonl").read_text().splitlines()]
    del rows[1]["user"]
    rows[2]["at"] = "garbage"
    (product / "audit.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    html = shift_record(product, 0, T0 + 100, "B", "A", audit=audit)
    assert "Audit chain: BROKEN at entry 2" in html


def test_record_quarantine_open_until_qa_release(product):
    import time
    from d2i.audit import Users, release_quarantine
    now = time.time()  # the release entry carries real time, so the self-tests must too

    def test(t, passed):
        return {"passed": passed, "results": [], "good_passed": passed, "failed": [] if passed else ["empty_pocket"],
                "time": t, "at": _at(t), "checklist_version": 1, "taught_at": T0}
    tests = [test(now - 100, True), test(now - 50, False), test(now - 10, True)]  # failed, then passed again
    strips = [dict(_verdict(i, "PASS"), at=_at(now - 40 + i)) for i in range(3)]
    (product / "selftests.jsonl").write_text("\n".join(json.dumps(r) for r in tests) + "\n")
    (product / "log.jsonl").write_text("\n".join(json.dumps(r) for r in strips) + "\n")
    audit, users = AuditLog(product / "audit.jsonl"), Users(product / "users.json")
    users.add("qa1", "qa", "pw")
    quarantine = "<h2>Quarantine</h2>"
    assert quarantine not in shift_record(product, 0, now + 100, "B", "A")  # no audit: the old rule
    html = shift_record(product, 0, now + 100, "B", "A", audit=audit)
    assert quarantine in html and _human(now - 50) in html.split(quarantine)[1]
    release_quarantine(product, audit, users, "qa1", "pw", "re-checked by hand")
    assert quarantine not in shift_record(product, 0, now + 100, "B", "A", audit=audit)
    rows = (product / "audit.jsonl").read_text().splitlines()
    rows[-1] = json.dumps(dict(json.loads(rows[-1]), at="garbage"))  # an unreadable release does not close it
    (product / "audit.jsonl").write_text("\n".join(rows) + "\n")
    html = shift_record(product, 0, now + 100, "B", "A", audit=audit)
    assert "Audit chain: BROKEN at entry 1" in html and quarantine in html


def test_record_has_batch_section_first(product):
    assert "No batch has been started" in shift_record(product, T0 - 10, T0 + 100, "", "")
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT7", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    b.leak("fail", "qa1", "bubbles", T0 + 70)
    rows = [dict(_verdict(i, st, rs), batch="LOT7") for i, (st, rs) in enumerate([("PASS", []), ("REJECT", [R2])], 10)]
    with (product / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + chr(10) for r in rows))
    html = shift_record(product, T0 - 10, T0 + 100, "", "")
    assert html.index("<h2>Batch</h2>") < html.index("Checklist")
    for t in ("LOT7", "01/2028", "45.00", "still open", "Clear", "Failed", "bubbles", "Rule 2"):
        assert t in html


def test_totals_list_checks_not_run():
    from d2i.record import _totals_section
    html = _totals_section([{"status": "PASS", "skipped": ["tablet measurements"]}, {"status": "PASS"}])
    assert "Not checked" in html and "tablet measurements</td><td>1<" in html


def test_selftest_section_lists_uncovered_kinds():
    from d2i.record import _selftest_section
    t = {"at": "x", "results": [], "passed": True, "skipped": ["double_tablet"]}
    assert "did not cover: double tablet" in _selftest_section([t])


def test_record_lists_missed_hour(product):
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT7", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    b.leak("pass", "qa1", "", T0 + 70, samples=5)
    b.end("qa1", T0 + 60 + 7300, {"inspected": 4, "passed": 3, "rejected": 1, "held": 0, "no_strip": 0, "balanced": True})
    html = shift_record(product, T0 - 10, T0 + 8000, "", "")
    for t in ("<h2>Release</h2>", "<h2>In-process checks</h2>", "<h2>Reconciliation</h2>", "Balanced", "Start-up print check"):
        assert t in html
    assert html.count(">Missed<") == 3 and html.count("Leak test missed") == 1 and html.count("Print check missed") == 2


def test_record_missed_is_per_kind(product):
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT7", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    b.leak("pass", "qa1", "", T0 + 70)
    b.leak("pass", "qa1", "", T0 + 3700)
    b.end("qa1", T0 + 60 + 7300, {"inspected": 0, "passed": 0, "rejected": 0, "held": 0, "no_strip": 0, "balanced": True})
    html = shift_record(product, T0 - 10, T0 + 8000, "", "")
    assert "Leak test missed" not in html and html.count("Print check missed") == 2


def test_record_reconciliation_lines_limits_and_investigation(product):
    from d2i.ipqc import reconcile
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT9", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    counts = {"received": 3, "good_tray": 1, "reject_box": 0, "samples": 0, "unused": 0,
              "foil_issued": 10, "foil_used": 9, "foil_returned": 0.5, "foil_destroyed": 0.5}
    tot = reconcile([], "LOT9", counts, {"strips": 0, "foil": 0.5})
    b.end("qa1", T0 + 100, dict(tot, investigated={"by": "qa1", "reason": "<b>tray</b> miscount"}))
    html = shift_record(product, T0 - 10, T0 + 200, "", "")
    assert "Strip balance" in html and "Foil" in html and "site policy" in html
    assert "Outside" in html and "Within" in html
    assert "Outside limit — investigated by qa1: &lt;b&gt;tray&lt;/b&gt; miscount" in html


def test_rejects_per_rule_counts_each_strip_once(product):
    from d2i.record import _rejects_section
    r3 = {"rule": 3, "severity": "major", "text": "x", "check": "c"}
    vs = [{"status": "REJECT", "reasons": [r3] * 4}, {"status": "REJECT", "reasons": [r3, dict(r3, rule=5, severity="minor")]}]
    html = _rejects_section(vs)
    assert "<td>Rule 3 (major)</td><td>2</td>" in html and "<td>Rule 5 (minor)</td><td>1</td>" in html


def test_record_old_ended_batch_without_totals(product):
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT7", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    b.end("qa1", T0 + 100)
    html = shift_record(product, T0 - 10, T0 + 200, "", "")
    assert "Not reconciled (ended before reconciliation)" in html and "still open" not in html


def test_record_shows_reject_thumbnails(product):
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT8", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    rows = [dict(_verdict(20 + i, "REJECT", [R2]), batch="LOT8", photo=f"20261101-0000{i:02d}-{i}.jpg") for i in range(14)]
    with (product / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + chr(10) for r in rows))
    html = shift_record(product, T0 - 10, T0 + 100, "", "")
    assert html.count("<img") == 12 and 'href="/rejects/20261101-000013-13.jpg"' in html
    assert "20261101-000000-0.jpg" not in html and "pocket 7 is empty" in html  # the latest 12, each with its reason


def _filled(product):
    """A batch with release lines, a print check, a leak test, verdicts and a QA specimen signature (UTC in the audit log)."""
    audit = AuditLog(product / "audit.jsonl")
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT9", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    b.leak("pass", "qa1", "", T0 + 70)
    pr = {"at": _at(T0 + 80), "time": T0 + 80, "batch": "LOT9", "read": {"batch": "LOT9", "mfg": None}, "status": "PRINT_OK",
          "reasons": [], "by": "qa1"}
    (product / "prints.jsonl").write_text(json.dumps(pr) + chr(10), encoding="utf-8")
    sig = audit.append("specimen_signed", "qa1", {"batch": "LOT9", "print_at": pr["at"]})
    rows = [dict(_verdict(30, "NO_STRIP"), batch="LOT9"), dict(_verdict(31, "REJECT", [R2]), batch="LOT9", photo="p.jpg")]
    with (product / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + chr(10) for r in rows))
    return audit, sig


def test_record_shows_local_human_times_only(product):
    import re
    audit, sig = _filled(product)
    html = shift_record(product, T0 - 10, T0 + 100, "LOT9", "Asha", audit=audit)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", html) and "+00:00" not in html
    local = datetime.fromisoformat(sig["at"]).astimezone()  # the audit log stores UTC; the record shows station time
    assert f"{local.day} {local:%b %Y, %H:%M:%S}" in html and _human(T0 + 70) in html
    assert "+00:00" in (product / "audit.jsonl").read_text()  # display only: the stored value is unchanged


def test_record_uses_plain_words_not_codes(product):
    audit, _ = _filled(product)
    html = shift_record(product, 0, T0 + 100, "LOT9", "Asha", audit=audit)
    assert "Print OK" in html and "No strip" in html and "Specimen signed" in html
    for code in ("PRINT_OK", "NO_STRIP", "empty_pocket", "specimen_signed", "None"):
        assert code not in html, code
    assert 'href="/rejects/p.jpg" target="_blank" rel="noopener"' in html


def test_record_look_draft_badge_and_fonts(product):
    html = shift_record(product, T0 - 10, T0 + 100, "B", "A")
    assert "DRAFT — needs QA signature</span>" in html and "<h1>Shift record</h1>" in html
    for w in ("Regular", "Medium", "SemiBold", "Bold"):
        assert f"url(/static/fonts/IBMPlexSans-{w}.woff2)" in html
    assert "Arial" not in html and "<script" not in html


def test_in_process_checks_sort_by_time_not_text(product):
    b = Batches(product)
    b.clearance({"clear": True, "found": []}, "qa1", T0)
    b.start("LOT7", "01/2026", "01/2028", "45", "qa1", T0 + 60)
    late = T0 + 17 * 86400  # a later month: "1 Feb" sorts before "15 Jan" as text
    b.leak("pass", "qa1", "", T0 + 70)
    b.leak("pass", "qa1", "", late)
    html = shift_record(product, T0 - 10, late + 10, "", "").split("<h2>In-process checks</h2>")[1]
    assert html.index(_human(T0 + 70)) < html.index(_human(late))


def test_signed_record_shows_green_badge_manifest_and_names(product):
    import re
    from d2i.audit import Users, sign_record
    audit, _ = _filled(product)
    users = Users(product / "users.json")
    users.add("qa1", "qa", "pw")
    html0 = shift_record(product, T0 - 10, T0 + 100, "LOT9", "Asha", audit=audit)
    assert "DRAFT — needs QA signature</span>" in html0 and "Signed by" not in html0
    sign_record(html0, audit, users, "qa1", "pw", meaning="Approved", batch="LOT9")
    html = shift_record(product, T0 - 10, T0 + 100, "LOT9", "Asha", audit=audit)
    assert "DRAFT" not in html
    m = re.search(r"<span class='badge signed'>Signed by qa1 — Approved — (\d+ \w+ \d{4}, \d\d:\d\d:\d\d UTC[+-]\d\d:\d\d)</span>", html)
    assert m, html[:1500]
    assert "<h2>Signature manifest</h2>" in html and "<td>Approved</td>" in html and "<p>qa1</p>" in html
    other = shift_record(product, T0 - 10, T0 + 100, "OTHER", "Asha", audit=audit)  # another batch is not signed
    assert "DRAFT — needs QA signature</span>" in other


def test_config_block_has_thresholds_version_and_camera(product):
    html = shift_record(product, T0 - 10, T0 + 100, "B", "A",
                        config=[["Pocket threshold", "0.412"], ["Border threshold", "0.388"], ["Camera source", "0"]])
    cfg = html.split("<h2>Configuration</h2>")[1].split("<h2>")[0]
    for s in ("Pocket threshold", "0.412", "Border threshold", "0.388", "Camera source", "App version"):
        assert s in cfg


def test_app_version_is_a_git_hash_or_unknown():
    from d2i.record import app_version
    v = app_version()
    assert v == "unknown" or (4 <= len(v) <= 12 and all(c in "0123456789abcdef" for c in v))
