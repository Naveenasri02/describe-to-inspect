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
