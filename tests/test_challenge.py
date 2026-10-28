"""Task 37: verified physical challenge run (fake judge: no model is asked, no paid call)."""
import json
import time

from d2i.audit import AuditLog, quarantine_list
from d2i.checklist import CHECKS
from d2i.ipqc import release_blockers
from d2i.record import _lines, shift_record
from d2i.station import Verdict
from tests.conftest import make_strip
from tests.test_app import _audit, bench, login, up

CHAL = "Physical challenge for this batch"


def _judge_queue(c, *verdicts):
    """Each inspection gets the next (status, [checks]) as its verdict; every check is a named rule (Rule = index + 2)."""
    st = c.app.state.station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    q = list(verdicts)

    def judge(image, sid, name):
        status, checks = q.pop(0)
        return Verdict(status, [st._reason(k, 0) for k in checks], strip_id=sid, image_name=name)
    st._judge = judge


def _register(c, label, kind, rule=""):
    return c.post("/challenge/register", data={"label": label, "kind": kind, "rule": rule},
                  files=up(make_strip(missing=(0,), seed=3))).json()


def _set_bench(tmp_path, *strips, verdicts=()):
    """Released bench; QA registers the strips; then the operator is logged in for the run."""
    c, p = bench(tmp_path)
    _judge_queue(c, *verdicts)
    login(c, "qa1", "pw-qa")
    for label, kind in strips:
        assert "error" not in _register(c, label, kind)
    login(c, "op", "pw-op")
    return c, p


def _run(c, *ejected):
    out = []
    for e in ejected:
        out.append(c.post("/challenge/inspect", files=up(make_strip(seed=15))).json())
        out.append(c.post("/challenge/eject", json={"ejected": e}).json())
    return out


def test_register_needs_qa_and_writes_the_set(tmp_path):
    c, p = bench(tmp_path)
    _judge_queue(c)
    assert _register(c, "C1", "empty_pocket") == {"error": "Log in as a QA user first"}
    login(c, "op", "pw-op")
    assert _register(c, "C1", "empty_pocket") == {"error": "Log in as a QA user first"}
    login(c, "qa1", "pw-qa")
    assert "kind" in _register(c, "C1", "no_such_kind")["error"]
    assert "label" in _register(c, "../x", "empty_pocket")["error"]
    assert _register(c, "c1", "empty_pocket")["strips"][0] == {"label": "C1", "kind": "empty_pocket", "rule": 2,
                                                               "photo": "challenge/C1.jpg"}
    s = _register(c, "C2", "broken_tablet", "3")
    assert [x["label"] for x in s["strips"]] == ["C1", "C2"] and s["registered_by"] == "qa1" and s["at"]
    assert json.loads((p / "challenge_set.json").read_text(encoding="utf-8")) == s
    assert (p / "challenge" / "C1.jpg").is_file() and (p / "challenge" / "C2.jpg").is_file()
    assert c.get("/challenge/photo/C1").headers["content-type"] == "image/jpeg"
    assert c.get("/challenge/photo/C9").status_code == 404
    assert _register(c, "C1", "foil_damage", "7")["strips"][0]["kind"] == "foil_damage"  # same label: replaced in place


def test_pass_run_clears_the_gate_and_is_recorded(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), ("C2", "broken_tablet"),
                      verdicts=[("REJECT", ["empty_pocket"]), ("REJECT", ["broken_tablet", "tablet_spot"])])
    assert CHAL in c.get("/status").json()["blockers"]
    assert c.get("/challenge").json()["next"] == "Place C1 (empty pocket, expect Rule 2)"
    insp, ej1, insp2, ej2 = _run(c, True, True)
    assert insp["caught"] is True and insp["label"] == "C1" and insp["rules"] == [2]
    assert ej1["next"] == "Place C2 (broken tablet, expect Rule 3)" and insp2["rules"] == [3, 6]
    run = _lines(p / "challenge_runs.jsonl")[-1]
    assert ej2["run"] == run and run["passed"] is True and run["by"] == "op" and run["batch"] == "B1" and run["at"]
    assert run["results"] == [
        {"label": "C1", "expected_rule": 2, "status": "REJECT", "rules": [2], "passed": True, "ejected": True},
        {"label": "C2", "expected_rule": 3, "status": "REJECT", "rules": [3, 6], "passed": True, "ejected": True}]
    rows = [e for e in _audit(p) if e["action"] == "physical_challenge"]
    assert [(e["user"], e["details"]["strip_label"], e["details"]["passed"]) for e in rows] == [
        ("op", "C1", True), ("op", "C2", True)]
    s = c.get("/status").json()
    assert s["blockers"] == [] and s["locked"] is False
    assert c.get("/challenge").json()["next"] == "Place C1 (empty pocket, expect Rule 2)"  # a new run can start
    html = shift_record(p, time.time() - 3600, time.time() + 60, "B1", "op", audit=AuditLog(p / "audit.jsonl"))
    sec = html.split("<h2>Physical challenge</h2>")[1].split("<h2>")[0]
    assert "C1" in sec and "Rule 2" in sec and "Passed" in sec and "op" in sec
    assert "<td>Physical challenge for this batch</td>" in html


def test_miss_locks_and_opens_quarantine(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), ("C2", "broken_tablet"), verdicts=[("PASS", [])])
    st = c.app.state.station
    insp = c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()
    assert insp["caught"] is False and "locked" in insp["message"].lower()
    assert st.locked and st.lock_reason == "Physical challenge missed: C1 not rejected"  # locked before the tick
    assert quarantine_list(p, AuditLog(p / "audit.jsonl"))["failed_at"]
    ej = c.post("/challenge/eject", json={"ejected": False}).json()
    assert ej["run"]["passed"] is False and [r["label"] for r in ej["run"]["results"]] == ["C1"]  # the run stops
    assert CHAL in c.get("/status").json()["blockers"] and c.get("/status").json()["state"] == "LOCKED"
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()["error"].startswith("Station locked")


def test_wrong_rule_locks(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), verdicts=[("REJECT", ["broken_tablet"])])
    insp, ej = _run(c, True)
    assert insp["caught"] is False and insp["rules"] == [3]
    assert c.app.state.station.lock_reason == "Physical challenge missed: C1 rejected for Rule 3, expected Rule 2"
    assert ej["result"] == {"label": "C1", "expected_rule": 2, "status": "REJECT", "rules": [3], "passed": False,
                            "ejected": True}


def test_caught_but_not_ejected_locks(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), verdicts=[("REJECT", ["empty_pocket"])])
    insp, ej = _run(c, False)
    assert insp["caught"] is True and ej["run"]["passed"] is False
    assert c.app.state.station.lock_reason == "Physical challenge missed: C1 not ejected"


def test_gate_needs_a_run_only_when_a_set_is_registered(tmp_path):
    c, p = bench(tmp_path)
    assert c.get("/status").json()["blockers"] == []  # no set: the gate is unchanged
    html = shift_record(p, time.time() - 3600, time.time() + 60, "B1", "op", audit=AuditLog(p / "audit.jsonl"))
    assert "No physical challenge set registered" in html
    batch = {"batch": "B1", "time": 1000.0}
    assert release_blockers(batch, [{"passed": True, "time": 1001}], [{"batch": "B1", "status": "PRINT_OK"}], True) == []
    runs = [{"passed": True, "time": 999.0}, {"passed": False, "time": 1002.0}]  # passed before batch start; failed after
    assert release_blockers(batch, [{"passed": True, "time": 1001}], [{"batch": "B1", "status": "PRINT_OK"}], True,
                            runs) == [CHAL]
    assert release_blockers(batch, [{"passed": True, "time": 1001}], [{"batch": "B1", "status": "PRINT_OK"}], True,
                            runs + [{"passed": True, "time": 1003.0}]) == []


def test_challenge_inspections_are_not_batch_counts_or_drift(tmp_path):
    crit = ("REJECT", ["wrong_tablet"])  # critical on a production strip: it would hold the line
    c, p = _set_bench(tmp_path, ("C1", "wrong_tablet"), ("C2", "wrong_tablet"), ("C3", "wrong_tablet"),
                      verdicts=[crit, crit, crit])
    before = c.get("/status").json()["totals"]
    for r in _run(c, True, True, True)[1::2]:
        assert r["result"]["passed"] is True
    s = c.get("/status").json()
    assert s["totals"] == before and s["held"] is False and not (p / "log.jsonl").exists()
    assert not any(e["action"] == "hold_line" for e in _audit(p))


def test_run_needs_login_a_set_and_an_unlocked_station(tmp_path):
    c, p = bench(tmp_path)
    _judge_queue(c, ("REJECT", ["empty_pocket"]))
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json() == {"error": "Log in first"}
    login(c, "op", "pw-op")
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json() == {
        "error": "No physical challenge set registered"}
    assert c.post("/challenge/eject", json={"ejected": True}).json() == {"error": "Inspect the challenge strip first"}
    login(c, "qa1", "pw-qa")
    _register(c, "C1", "empty_pocket")
    c.post("/teach", json={})  # re-taught: locked until a self-test passes
    login(c, "op", "pw-op")
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()["error"].startswith("Station locked")
    assert not (p / "challenge_runs.jsonl").exists()
