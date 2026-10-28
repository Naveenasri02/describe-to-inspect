from d2i.ipqc import DRIFT_REJECTS, reject_run


def rows(seq, batch="B1"):
    return [{"status": {"R": "REJECT", "P": "PASS", "H": "HOLD", "N": "NO_STRIP", "L": "LOCKED"}[s], "batch": batch} for s in seq]


def test_reject_run_counts_trailing_rejects():
    assert DRIFT_REJECTS == 3
    assert reject_run(rows("RRPRHR"), "B1") == 2
    assert reject_run(rows("RRR"), "B1") == 3
    assert reject_run(rows("RNRLR"), "B1") == 3  # NO_STRIP / LOCKED neither count nor reset
    assert reject_run(rows("RR", "B0") + rows("P") + rows("R"), "B1") == 1  # other batch rows are ignored
    assert reject_run([], "B1") == 0


def test_reject_run_ignores_rows_before_since():
    from datetime import datetime
    rs = [dict(r, at=datetime.fromtimestamp(1_700_000_000 + i).isoformat()) for i, r in enumerate(rows("RRRR"))]
    assert reject_run(rs, "B1") == 4
    assert reject_run(rs, "B1", since=1_700_000_001) == 2  # only the rows after the resume count
    assert reject_run(rs, "B1", since=1_800_000_000) == 0


# --- Task 28: release gate, hourly checks, reconciliation ---
from d2i.ipqc import IPQC_EVERY_S, due, missed_windows, reconcile, release_blockers

BATCH = {"batch": "B1", "time": 1000.0}
ST_OK = {"passed": True, "time": 1001.0}
PR_OK = {"batch": "B1", "status": "PRINT_OK"}


def test_release_blockers_all_three():
    assert release_blockers(BATCH, [], [], False) == [
        "Self-test for this batch", "Start-up print check", "QA signature on the start-up specimen"]


def test_release_blockers_cleared():
    assert release_blockers(BATCH, [ST_OK], [PR_OK], True) == []
    assert release_blockers(BATCH, [ST_OK], [dict(PR_OK, batch="B0"), dict(PR_OK, status="HOLD")], True) == ["Start-up print check"]
    assert release_blockers(BATCH, [dict(ST_OK, passed=False)], [PR_OK], True) == ["Self-test for this batch"]


def test_selftest_before_batch_start_does_not_count():
    assert release_blockers(BATCH, [{"passed": True, "time": 999.0}], [PR_OK], True) == ["Self-test for this batch"]


def ev(kind, t, result="pass"):
    return {"event": kind, "time": t, "result": result}  # print rows carry no result; harmless here


def test_due_after_an_hour():
    rows = [ev("leak", 1100.0), ev("print", 1100.0)]
    assert IPQC_EVERY_S == 3600
    assert due(BATCH, rows, 1100.0 + 3600) == []
    assert due(BATCH, rows, 1100.0 + 3601) == ["Leak test due", "Print check due"]
    assert due(BATCH, [], 1000.0) == ["Leak test due", "Print check due"]


def test_due_after_foil_change():
    rows = [ev("leak", 1100.0), ev("print", 1100.0), ev("foil_change", 1200.0)]
    assert due(BATCH, rows, 1300.0) == ["Print check due"]  # only the print clock restarts
    assert due(BATCH, [ev("foil_change", 1200.0)], 1300.0) == ["Leak test due", "Print check due"]


def test_not_due_after_recent_checks():
    assert due(BATCH, [ev("leak", 5000.0), ev("print", 6000.0)], 7000.0) == []


def test_missed_windows():
    assert missed_windows(BATCH, [ev("leak", 1010.0), ev("print", 1010.0)], 1000.0 + 2 * 3600 + 5) == [
        ("Leak test missed", 4600.0, 8200.0), ("Print check missed", 4600.0, 8200.0)]
    assert missed_windows(BATCH, [ev("leak", 1010.0), ev("leak", 4700.0)], 8205.0) == [  # per kind, like due()
        ("Print check missed", 1000.0, 4600.0), ("Print check missed", 4600.0, 8200.0)]
    assert missed_windows(BATCH, [ev("foil_change", 2000.0)], 2000.0 + 3600) == [
        ("Leak test missed", 1000.0, 4600.0), ("Print check missed", 2000.0, 5600.0)]  # a foil change restarts the print windows only


def test_foil_change_makes_only_the_print_check_due():
    rows = [ev("leak", 1100.0), ev("print", 1100.0), ev("foil_change", 1200.0)]
    assert due(BATCH, rows, 1300.0) == ["Print check due"]


def test_failed_leak_test_does_not_count():
    rows = [ev("leak", 1100.0, "fail"), ev("print", 1100.0)]
    assert due(BATCH, rows, 1200.0) == ["Leak test due"]
    assert missed_windows(BATCH, rows, 4600.0) == [("Leak test missed", 1000.0, 4600.0)]
    assert due(BATCH, rows + [ev("leak", 1150.0)], 1200.0) == []


C0 = {"received": 5, "good_tray": 2, "reject_box": 2, "samples": 1, "unused": 0,
      "foil_issued": 10, "foil_used": 8, "foil_returned": 1.5, "foil_destroyed": 0.5}
L0 = {"strips": 0, "foil": 0.5}


def test_reconcile_open_without_typed_counts():
    r = reconcile(rows("PPRHNL") + rows("P", "B0"), "B1")
    assert r == {"inspected": 5, "passed": 2, "rejected": 1, "held": 1, "no_strip": 1, "status": "open"}  # LOCKED excluded


def test_reconcile_typed_counts_within():
    r = reconcile(rows("PPRH"), "B1", C0, L0)
    assert r["status"] == "within" and [l["difference"] for l in r["lines"]] == [0, 0, 0, 0]
    assert r["lines"][0]["name"] == "Strip balance" and r["limits"] == L0


def test_reconcile_old_tautology_is_gone():
    r = reconcile(rows("P" * 10), "B1", dict(C0, received=10, good_tray=9, reject_box=0, samples=0), L0)
    tray = next(l for l in r["lines"] if l["name"] == "Good tray")
    assert tray["difference"] == -1 and tray["outside"] and r["status"] == "outside"


def test_reconcile_foil_line_and_limit():
    assert reconcile(rows("PPRH"), "B1", dict(C0, foil_used=7.6), L0)["status"] == "within"  # 0.4 m, limit 0.5
    r = reconcile(rows("PPRH"), "B1", dict(C0, foil_used=7), L0)
    assert next(l for l in r["lines"] if l["name"] == "Foil")["difference"] == 1 and r["status"] == "outside"
