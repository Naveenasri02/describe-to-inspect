"""Task 36: a critical hold opens a re-check window (fake judges and chats: no model is asked, no paid call)."""
import json
import time
from datetime import datetime, timezone

from d2i import recheck
from d2i.audit import AuditLog
from d2i.batch import Batches
from d2i.checklist import CHECKS
from d2i.record import _lines, shift_record
from d2i.station import Verdict
from d2i.supervisor import TOOLS
from tests.conftest import make_strip
from tests.test_app import (END_OK, PrintClient, ScriptChat, _audit, _holds, bench, call, login, open_batch, release_batch,
                            up, wait_done)


class FakeAudit:
    def __init__(self, resumed=None):
        self.rows = [] if resumed is None else [
            {"action": "resume_line", "user": "qa1", "details": {"hold": False},
             "at": datetime.fromtimestamp(resumed, timezone.utc).isoformat(timespec="seconds")}]

    def entries(self):
        return self.rows

    def append(self, action, user, details, reason=""):
        self.rows.append({"action": action, "user": user, "details": details, "reason": reason})
        return self.rows[-1]


def _product(tmp_path, t0, seq, prints=()):
    """An open batch B1 started at t0; one verdict row per letter of seq at t0 + 10, + 20, ...; PRINT_OK rows at prints."""
    b = Batches(tmp_path)
    b.clearance({"clear": True, "found": []}, "qa1", t0)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", t0)
    st = {"P": "PASS", "R": "REJECT", "H": "HOLD", "N": "NO_STRIP", "L": "LOCKED"}
    for i, s in enumerate(seq, 1):
        b._write("log.jsonl", {"status": st[s], "reasons": [], "batch": "B1",
                               "at": datetime.fromtimestamp(t0 + 10 * i).isoformat(timespec="seconds")})
    for t in prints:
        b._write("prints.jsonl", {"batch": "B1", "status": "PRINT_OK", "time": t, "at": "x"})
    return tmp_path


def test_window_bounds(tmp_path):
    t0 = time.time() - 1000
    seq = "PPLPRPPNP"  # strip numbers skip the LOCKED row: P1 P2 P3 R4 P5 P6 N7 P8
    p = _product(tmp_path / "a", t0, seq)
    w = recheck.open_window(p, FakeAudit(), "Critical: wrong tablet", "station")  # since batch release: every pass
    assert (w["from_strip"], w["to_strip"], w["total"], w["done"], w["open"]) == (1, 8, 6, 0, True)
    assert w["opened_by"] == "station" and w["cause"] == "Critical: wrong tablet" and w["waived_by"] is None
    assert json.loads((p / "recheck.json").read_text(encoding="utf-8")) == w
    assert recheck.text(w) == f"Re-check 6 strips: no. 1-8 since {datetime.fromtimestamp(t0):%H:%M}"
    assert recheck.progress(w) == "Re-check 0 of 6"
    p = _product(tmp_path / "b", t0, seq, prints=[t0 + 5, t0 + 45])  # the last passed print check wins: no. 5 onward
    w = recheck.open_window(p, FakeAudit(resumed=t0 - 100), "x", "station")
    assert (w["from_strip"], w["to_strip"], w["total"], w["since"]) == (5, 8, 3, t0 + 45)
    p = _product(tmp_path / "c", t0, seq, prints=[t0 + 45])  # a later QA resume wins over the print check
    w = recheck.open_window(p, FakeAudit(resumed=t0 + 65), "x", "station")
    assert (w["from_strip"], w["to_strip"], w["total"]) == (6, 8, 2)
    assert recheck.open_window(_product(tmp_path / "d", t0, "PPR", prints=[t0 + 25]), FakeAudit(), "x", "station") is None
    assert recheck.open_window(p, FakeAudit(), "again", "station") == w  # an open window is kept, never replaced
    assert recheck.active(p, {"time": t0}) == w and recheck.active(p, {"time": t0 + 2000}) is None  # an older batch's


def _judge(c, *verdicts):
    """Each inspection gets the next (status, [checks]) verdict; every check is a named rule."""
    st = c.app.state.station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    q = list(verdicts)
    st._judge = lambda image, sid, name: (lambda s, ks: Verdict(s, [st._reason(k, 0) for k in ks], strip_id=sid))(*q.pop(0))


def _inspect(c, n=1):
    return [c.post("/inspect", files=up(make_strip(seed=15))).json() for _ in range(n)]


PASS, CRIT = ("PASS", []), ("REJECT", ["wrong_tablet"])


def _critical_bench(tmp_path, *after):
    """3 passed strips, then a wrong tablet: held, and a re-check of strips 1-3 is open."""
    c, p = bench(tmp_path)
    _judge(c, PASS, PASS, PASS, CRIT, *after)
    _inspect(c, 4)
    return c, p


def test_critical_hold_opens_window_and_each_inspect_counts_down(tmp_path):
    c, p = _critical_bench(tmp_path, PASS, ("REJECT", ["empty_pocket"]), PASS, PASS)
    w = c.get("/recheck").json()
    assert c.get("/status").json()["held"] is True
    assert (w["open"], w["from_strip"], w["to_strip"], w["total"], w["done"]) == (True, 1, 3, 3, 0)
    assert w["cause"].startswith("Critical: Rule 5 failed") and w["opened_by"] == "station"
    assert w["text"].startswith("Re-check 3 strips: no. 1-3 since ") and w["count"] == "Re-check 0 of 3"
    assert [e["action"] for e in _audit(p)][-2:] == ["hold_line", "recheck_open"]
    out = _inspect(c, 3)
    assert [v["status"] for v in out] == ["PASS", "REJECT", "PASS"]  # real verdicts while held, rejects recorded
    rows = _lines(p / "log.jsonl")[-3:]
    assert all(r["recheck"] is True and r["window_id"] == w["window_id"] for r in rows)
    w = c.get("/recheck").json()
    assert (w["open"], w["done"], w["count"]) == (False, 3, "Re-check 3 of 3")
    assert _inspect(c)[0]["status"] == "HOLD" and "recheck" not in _lines(p / "log.jsonl")[-1]  # done: held again
    t = c.get("/status").json()["totals"]  # each strip once: the re-check reject left the good tray
    assert (t["inspected"], t["passed"], t["rejected"], t["held"]) == (5, 2, 2, 1)


def test_resume_needs_the_recheck_done_or_a_qa_waiver(tmp_path):
    c, p = _critical_bench(tmp_path, PASS)
    _inspect(c)
    login(c, "qa1", "pw-qa")
    r = c.post("/resume", json={"password": "pw-qa", "reason": "wrong tablet removed"}).json()
    assert r["error"].startswith("Re-check not complete: Re-check 1 of 3") and c.get("/status").json()["held"] is True
    assert "error" in c.post("/resume", json={"password": "wrong", "reason": "x", "waiver": "y"}).json()
    assert c.get("/recheck").json()["open"] is True
    r = c.post("/resume", json={"password": "pw-qa", "reason": "wrong tablet removed",
                                "waiver": "strips 2-3 checked by hand at the bench"}).json()
    assert r["resumed"] is True and c.get("/status").json()["held"] is False
    w = c.get("/recheck").json()
    assert (w["open"], w["waived_by"], w["waiver_reason"]) == (False, "qa1", "strips 2-3 checked by hand at the bench")
    e = _audit(p)[-1]
    assert (e["action"], e["user"], e["reason"]) == ("recheck_waived", "qa1", "strips 2-3 checked by hand at the bench")
    html = shift_record(p, time.time() - 3600, time.time() + 60, "B1", "op", audit=AuditLog(p / "audit.jsonl"))
    sec = html.split("<h2>Re-check</h2>")[1].split("<h2>")[0]
    assert "no. 1-3" in sec and "Waived" in sec and "strips 2-3 checked by hand" in sec and "Critical: Rule 5" in sec


def test_resume_after_a_finished_recheck_needs_no_waiver(tmp_path):
    c, p = _critical_bench(tmp_path, PASS, PASS, PASS)
    _inspect(c, 3)
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "re-check done"}).json()["resumed"] is True


def test_wrong_print_opens_a_window(tmp_path):
    read = {"batch": "B9", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = bench(tmp_path, client=PrintClient(read=read))
    _judge(c, PASS, PASS)
    _inspect(c, 2)
    login(c, "op", "pw-op")
    assert c.post("/print", files=up(make_strip(seed=1))).json()["status"] == "HOLD"
    w = c.get("/recheck").json()
    assert w["open"] and (w["from_strip"], w["to_strip"]) == (1, 2) and w["cause"].startswith("Critical: wrong print")


def test_agent_requests_a_recheck_and_qa_approves_it(tmp_path):
    assert "request_recheck" in [t["function"]["name"] for t in TOOLS]
    chat = ScriptChat(call("request_recheck", {"reason": "two wrong tablets this hour"}),
                      call("request_resume", {"reason": "camera checks out"}), {"content": "Mix-up risk."})
    c, p = bench(tmp_path, chat)
    _judge(c, PASS, PASS)
    _inspect(c, 2)
    c.post("/investigate", json={"trigger": "wrong tablet"})
    rc, res = wait_done(c)["pending"]
    assert rc["kind"] == "recheck" and not (p / "recheck.json").exists()  # a request only
    login(c, "op", "pw-op")
    assert "error" in c.post("/approve-request", json={"id": rc["id"], "password": "pw-op"}).json()
    login(c, "qa1", "pw-qa")
    out = c.post("/approve-request", json={"id": rc["id"], "password": "pw-qa"}).json()
    assert out["approved"] is True and out["message"].startswith("Re-check 2 strips: no. 1-2")
    w = c.get("/recheck").json()
    assert w["open"] and w["opened_by"] == "qa1" and w["cause"] == "Agent request: two wrong tablets this hour"
    out = c.post("/approve-request", json={"id": res["id"], "password": "pw-qa"}).json()
    assert out["approved"] is False and out["message"].startswith("Re-check not complete")
    assert c.get("/status").json()["held"] is True


# --- fix round 1 ---
def test_since_is_the_batch_release(tmp_path):
    t0 = float(int(time.time()) - 1000)
    p = _product(tmp_path, t0, "PPPP", prints=[t0 + 5])
    Batches(p)._write("selftests.jsonl", {"passed": True, "time": t0 + 3})
    signed = FakeAudit()
    signed.rows.append({"action": "specimen_signed", "user": "qa1", "details": {"batch": "B1"},
                        "at": datetime.fromtimestamp(t0 + 7, timezone.utc).isoformat(timespec="seconds")})
    assert recheck.open_window(p, signed, "x", "station")["since"] == t0 + 7  # released at the specimen signature
    (p / "recheck.json").unlink()
    Batches(p)._write("challenge_runs.jsonl", {"passed": True, "batch": "B1", "time": t0 + 15})  # set registered late
    w = recheck.open_window(p, signed, "x", "station")
    assert (w["since"], w["from_strip"], w["total"]) == (t0 + 10, 1, 4)  # never later than the batch's first strip


def test_batch_end_refused_while_a_recheck_is_open(tmp_path):
    c, p = _critical_bench(tmp_path)
    login(c, "qa1", "pw-qa")
    assert c.post("/batch/end", data=END_OK).json() == {"error": "Finish or waive the re-check first."}
    assert Batches(p).open


def test_a_window_belongs_to_its_batch(tmp_path):
    c, p = _critical_bench(tmp_path, PASS, CRIT)
    old = c.get("/recheck").json()
    Batches(p)._write("batches.jsonl", {"event": "end", "batch": "B1", "by": "qa1", "at": "x", "time": time.time(), "totals": {}})
    time.sleep(1.1)
    open_batch(p, "B2")
    release_batch(p, "B2")
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "new batch"}).json()["resumed"] is True  # not blocked
    assert c.get("/recheck").json()["waived_by"] is None  # B1's window is not waived in B2's place
    _inspect(c, 2)  # B2: a pass, then a wrong tablet opens B2's own window
    rows = _lines(p / "log.jsonl")[-2:]
    assert [r.get("recheck") for r in rows] == [None, None] and [r["batch"] for r in rows] == ["B2", "B2"]
    w = c.get("/recheck").json()
    assert w["open"] and w["window_id"] != old["window_id"] and (w["from_strip"], w["total"]) == (1, 1)


def test_a_critical_on_a_held_line_still_opens_a_window(tmp_path):
    c, p = bench(tmp_path)
    _judge(c, PASS, PASS, CRIT)
    _inspect(c, 2)
    c.post("/hold", json={"reason": "operator"})
    assert _inspect(c)[0]["status"] == "REJECT"
    w = c.get("/recheck").json()
    assert w["open"] and (w["from_strip"], w["to_strip"]) == (1, 2) and w["cause"].startswith("Critical: Rule 5")
    assert len(_holds(p)) == 1  # the operator's hold stays the only one


def test_approved_agent_recheck_holds_a_running_line_first(tmp_path):
    chat = ScriptChat(call("request_recheck", {"reason": "mix-up seen"}), {"content": "done"})
    c, p = bench(tmp_path, chat)
    _judge(c, *[PASS] * 8)
    _inspect(c, 2)
    c.post("/investigate", json={"trigger": "t"})
    rc, = wait_done(c)["pending"]
    login(c, "qa1", "pw-qa")
    time.sleep(1.1)  # verdict and audit times have one-second resolution
    assert c.post("/resume", json={"password": "pw-qa", "reason": "ok"}).json()["resumed"] is True
    time.sleep(1.1)
    _inspect(c, 2)  # production on a running line
    out = c.post("/approve-request", json={"id": rc["id"], "password": "pw-qa"}).json()
    assert out["approved"] is True and c.get("/status").json()["held"] is True
    assert _holds(p)[-1]["user"] == "qa1" and "Re-check" in _holds(p)[-1]["reason"]
    assert c.get("/recheck").json()["total"] == 2
    _inspect(c, 3)  # two re-checked strips, then the held line holds the next one
    t = c.get("/status").json()["totals"]
    assert (t["inspected"], t["passed"], t["held"]) == (5, 4, 1)


def test_rows_are_marked_only_on_a_held_line(tmp_path):
    c, p = bench(tmp_path)
    _judge(c, PASS, PASS, PASS)
    _inspect(c, 2)
    assert recheck.open_window(p, AuditLog(p / "audit.jsonl"), "odd", "qa1")["open"]  # an open window, line running
    _inspect(c)
    assert "recheck" not in _lines(p / "log.jsonl")[-1] and c.get("/status").json()["totals"]["inspected"] == 3


def test_record_totals_and_quarantine_count_each_strip_once(tmp_path):
    from d2i.audit import quarantine_list
    c, p = _critical_bench(tmp_path, PASS, ("REJECT", ["empty_pocket"]), PASS, PASS)
    _inspect(c, 4)
    html = shift_record(p, time.time() - 3600, time.time() + 60, "B1", "op", audit=AuditLog(p / "audit.jsonl"))
    tot = html.split("<h2>Totals</h2>")[1].split("<h2>")[0]
    assert "<td>2</td><td>2</td><td>1</td><td>0</td><td>0</td>" in tot  # passed, rejected, held, locked, no strip
    bv = html.split("<h2>Batch verdicts</h2>")[1].split("<h2>")[0]
    assert "<td>2</td><td>2</td><td>1</td><td>0</td><td>0</td>" in bv
    Batches(p)._write("selftests.jsonl", dict(c.app.state.station._selftests()[-1], passed=False, failed=["x"],
                                              time=time.time() + 1))
    q = quarantine_list(p, AuditLog(p / "audit.jsonl"))["strips"]
    assert q and not any(v.get("recheck") for v in q)  # a re-checked strip is listed once, by its first verdict
