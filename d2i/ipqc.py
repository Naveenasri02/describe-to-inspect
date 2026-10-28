"""Line-quality rules that look across strips: three rejects in a row mean the machine has drifted."""
from datetime import datetime

DRIFT_REJECTS = 3


def reject_run(rows: list[dict], batch: str, since: float = 0.0) -> int:
    """Length of the trailing run of REJECT rows of this batch; PASS ends it, HOLD / NO_STRIP / LOCKED are skipped.
    since: a Unix time; rows logged at or before it (e.g. before a QA resume) are not counted."""
    n = 0
    for r in reversed(rows):
        if r.get("batch") != batch or r["status"] in ("HOLD", "NO_STRIP", "LOCKED"):
            continue
        if since and datetime.fromisoformat(r["at"]).timestamp() <= since:
            break  # log order: everything earlier is older still
        if r["status"] != "REJECT":
            break
        n += 1
    return n
