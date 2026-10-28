import json
import time
from datetime import datetime, timezone

from d2i.audit import AuditLog
from d2i.ipqc import IPQC_EVERY_S
from d2i.openitems import open_items, window_id

NOW = time.time()


def iso(t):  # local ISO seconds, as the station writes verdict and batch times
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def put(p, name, *rows):
    with (p / name).open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def kinds(items):
    return [i["kind"] for i in items]


def start(p, t, no="B1"):
    put(p, "batches.jsonl", {"event": "start", "batch": no, "time": t, "at": iso(t), "by": "qa1", "clearance_at": iso(t - 60)})


def test_missing_files_mean_nothing_waiting(tmp_path):
    assert open_items(tmp_path, NOW) == []


def test_line_held(tmp_path):
    AuditLog(tmp_path / "audit.jsonl").append("hold_line", "station", {"hold": True}, "Critical: Rule 5 failed: x")
    at = datetime.fromtimestamp(NOW - 300, timezone.utc).isoformat(timespec="seconds")
    (tmp_path / "hold.json").write_text(json.dumps({"held": True, "at": at, "by": "station"}), encoding="utf-8")
    [i] = open_items(tmp_path, NOW)
    assert (i["kind"], i["who"], i["evidence"]) == ("line_held", "QA", "audit #1")
    assert "Critical: Rule 5 failed: x" in i["text"] and abs(i["since"] - (NOW - 300)) < 1.5
    (tmp_path / "hold.json").write_text(json.dumps({"held": False, "at": at, "by": "qa1"}), encoding="utf-8")
    assert open_items(tmp_path, NOW) == []


def test_quarantine_open_until_released(tmp_path):
    put(tmp_path, "selftests.jsonl", {"passed": True, "time": NOW - 900, "at": iso(NOW - 900)},
        {"passed": False, "time": NOW - 300, "at": iso(NOW - 300), "failed": ["empty_pocket"]})
    put(tmp_path, "log.jsonl", *[{"status": "PASS", "at": iso(NOW - 600 + k), "batch": "B1", "reasons": []} for k in range(3)])
    [i] = open_items(tmp_path, NOW)
    assert (i["kind"], i["who"]) == ("quarantine", "QA") and "3 strips" in i["text"]
    AuditLog(tmp_path / "audit.jsonl").append("release_quarantine", "qa1", {}, "checked by hand")
    assert open_items(tmp_path, NOW) == []


def test_pending_supervisor_request(tmp_path):
    req = {"id": "ab12cd34", "kind": "resume", "reason": "Agent request: lamp moved back"}
    AuditLog(tmp_path / "audit.jsonl").append("agent_step", "supervisor", {
        "tool": "request_resume", "args": {}, "result_summary": json.dumps({"id": "ab12cd34", "status": "waiting"})})
    [i] = open_items(tmp_path, NOW, requests=[req])
    assert (i["kind"], i["who"], i["evidence"]) == ("request", "QA", "audit #1") and "lamp moved back" in i["text"]
    assert open_items(tmp_path, NOW) == []  # no live request: nothing waits


def test_recheck_open(tmp_path):
    rc = {"open": True, "window_id": "w1", "since": NOW - 120, "from_strip": 41, "to_strip": 58, "total": 18, "done": 5,
          "opened_by": "station", "cause": "wrong print", "waived_by": None, "waiver_reason": None}
    (tmp_path / "recheck.json").write_text(json.dumps(rc), encoding="utf-8")
    assert open_items(tmp_path, NOW) == []  # no batch open: the window belongs to no batch
    start(tmp_path, NOW - 600)
    [i] = open_items(tmp_path, NOW)
    assert (i["kind"], i["since"]) == ("recheck", NOW - 120) and "5 of 18" in i["text"] and "41" in i["text"]
    (tmp_path / "recheck.json").write_text(json.dumps(dict(rc, open=False)), encoding="utf-8")
    assert open_items(tmp_path, NOW) == []


def test_recheck_from_another_batch_is_not_waiting(tmp_path):
    start(tmp_path, NOW - 600, "B2")
    rc = {"open": True, "since": NOW - 900, "from_strip": 1, "to_strip": 9, "total": 9, "done": 2}  # opened in batch B1
    (tmp_path / "recheck.json").write_text(json.dumps(rc), encoding="utf-8")
    assert open_items(tmp_path, NOW) == []


def test_reconciliation_outside_without_reason_and_record_unsigned(tmp_path):
    start(tmp_path, NOW - 1800)
    end = {"event": "end", "batch": "B1", "time": NOW - 600, "at": iso(NOW - 600), "by": "qa1",
           "totals": {"status": "outside", "lines": []}}
    put(tmp_path, "batches.jsonl", end)
    assert kinds(open_items(tmp_path, NOW)) == ["reconciliation", "record_unsigned"]
    AuditLog(tmp_path / "audit.jsonl").append("sign", "qa1", {"record_sha256": "x"})
    assert kinds(open_items(tmp_path, NOW)) == ["reconciliation"]


def test_reconciliation_outside_with_investigation_is_not_waiting(tmp_path):
    start(tmp_path, NOW - 1800)
    put(tmp_path, "batches.jsonl", {"event": "end", "batch": "B1", "time": NOW - 600, "at": iso(NOW - 600), "by": "qa1",
                                    "totals": {"status": "outside", "investigated": {"by": "qa1", "reason": "recount"}}})
    assert kinds(open_items(tmp_path, NOW)) == ["record_unsigned"]


def test_missed_check_closed_by_a_review_note(tmp_path):
    t0 = NOW - 1.5 * IPQC_EVERY_S
    start(tmp_path, t0)
    items = open_items(tmp_path, NOW)
    assert kinds(items) == ["missed_check", "missed_check"] and items[0]["since"] == t0 + IPQC_EVERY_S
    assert items[0]["text"].startswith("Leak test missed") and items[1]["text"].startswith("Print check missed")
    put(tmp_path, "reviews.jsonl", {"time": NOW, "at": iso(NOW), "kind": "missed_check", "window": window_id("leak", t0),
                                    "by": "qa1", "note": "tested at 10:05, entered late"})
    [i] = open_items(tmp_path, NOW)
    assert i["text"].startswith("Print check missed") and window_id("print", t0) == "print " + iso(t0)


def test_failed_challenge_until_a_passed_run(tmp_path):
    run = {"time": NOW - 200, "at": iso(NOW - 200), "by": "op", "batch": "B1", "passed": False,
           "results": [{"label": "C1", "expected_rule": 2, "status": "PASS", "rules": [], "passed": False, "ejected": False}]}
    put(tmp_path, "challenge_runs.jsonl", run)
    [i] = open_items(tmp_path, NOW)
    assert (i["kind"], i["who"]) == ("challenge_failed", "QA") and "C1" in i["text"]
    put(tmp_path, "challenge_runs.jsonl", dict(run, time=NOW - 100, passed=True))
    assert open_items(tmp_path, NOW) == []


def test_oldest_first_and_unreadable_is_shown(tmp_path):
    rc = {"open": True, "since": NOW - 50, "from_strip": 1, "to_strip": 2, "total": 2, "done": 0}
    (tmp_path / "recheck.json").write_text(json.dumps(rc), encoding="utf-8")
    put(tmp_path, "challenge_runs.jsonl", {"time": NOW - 500, "passed": False, "results": []})
    (tmp_path / "batches.jsonl").write_text("not json\n", encoding="utf-8")
    items = open_items(tmp_path, NOW)
    assert [i["since"] for i in items] == sorted(i["since"] for i in items)
    assert kinds(items)[0] == "challenge_failed" and any("cannot read" in i["text"].lower() for i in items)
