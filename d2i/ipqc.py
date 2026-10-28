"""Line-quality rules that look across strips: three rejects in a row mean the machine has drifted."""
DRIFT_REJECTS = 3


def reject_run(rows: list[dict], batch: str) -> int:
    """Length of the trailing run of REJECT rows of this batch; PASS ends it, HOLD / NO_STRIP / LOCKED are skipped."""
    n = 0
    for r in reversed(rows):
        if r.get("batch") != batch or r["status"] in ("HOLD", "NO_STRIP", "LOCKED"):
            continue
        if r["status"] != "REJECT":
            break
        n += 1
    return n
