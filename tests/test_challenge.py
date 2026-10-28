"""Task 37: verified physical challenge run (fake judge: no model is asked, no paid call)."""
import json
import time

import cv2

from d2i.audit import AuditLog, quarantine_list
from d2i.batch import Batches
from d2i.checklist import CHECKS
from d2i.plc import Plc
from d2i.ipqc import release_blockers
from d2i.record import _lines, shift_record
from d2i.station import Verdict
from tests.conftest import make_strip
from tests.test_app import _audit, bench, login, open_batch, release_batch, up

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
    assert run["source"] == "upload"  # no camera on this bench: the photo came with the request
    assert run["results"] == [
        {"label": "C1", "expected_rule": 2, "status": "REJECT", "rules": [2], "passed": True, "ejected": True},
        {"label": "C2", "expected_rule": 3, "status": "REJECT", "rules": [3, 6], "passed": True, "ejected": True}]
    rows = [e for e in _audit(p) if e["action"] == "physical_challenge"]
    assert [(e["user"], e["details"]["strip_label"], e["details"]["passed"]) for e in rows] == [
        ("op", "C1", True), ("op", "C2", True)]
    s = c.get("/status").json()
    assert s["blockers"] == [] and s["locked"] is False
    assert s["last_verdict"] is None and s["state"] == "READY"  # the production banner never shows a challenge strip
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
    runs = [{"passed": True, "batch": "B1", "time": 999.0}, {"passed": False, "batch": "B1", "time": 1002.0},
            {"passed": True, "batch": "B0", "time": 1003.0}]  # passed before batch start; failed; another batch's
    assert release_blockers(batch, [{"passed": True, "time": 1001}], [{"batch": "B1", "status": "PRINT_OK"}], True,
                            runs) == [CHAL]
    assert release_blockers(batch, [{"passed": True, "time": 1001}], [{"batch": "B1", "status": "PRINT_OK"}], True,
                            runs + [{"passed": True, "batch": "B1", "time": 1003.0}]) == []


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
    c, p = bench(tmp_path, as_user=None)  # logged out: bench() logs the operator in by default
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


# --- fix round 1 ---
def test_miss_is_audited_at_once_and_ejected_without_counting(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    _judge_queue(c, ("PASS", []))
    login(c, "qa1", "pw-qa")
    _register(c, "C1", "empty_pocket")
    login(c, "op", "pw-op")
    insp = c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()
    assert "Take C1 out — it is a known-bad strip" in insp["message"]
    rows = [e for e in _audit(p) if e["action"] == "physical_challenge"]  # written at the miss, before any tick
    assert len(rows) == 1 and rows[0]["details"]["strip_label"] == "C1" and rows[0]["details"]["passed"] is False
    assert plc._reject_until > 0 and plc.regs == [0, 0, 0, 0]  # the reject output pulsed; no verdict counter moved
    assert c.get("/status").json()["last_verdict"] is None


def test_run_needs_an_open_batch_and_ends_with_its_batch(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), ("C2", "broken_tablet"),
                      verdicts=[("REJECT", ["empty_pocket"])] * 3)
    _run(c, True)  # C1 done, C2 next
    assert c.get("/challenge").json()["results"]
    Batches(p)._write("batches.jsonl", {"event": "end", "batch": "B1", "by": "qa1", "at": "x", "time": time.time(), "totals": {}})
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()["error"] == "Start a batch first (QA)"
    time.sleep(1.1)
    open_batch(p, "B2")
    release_batch(p, "B2")
    s = c.get("/challenge").json()
    assert s["results"] == [] and s["next"] == "Place C1 (empty pocket, expect Rule 2)"  # B1's half run is dropped
    assert c.get("/status").json()["blockers"] == [CHAL]


def test_registering_mid_run_restarts_the_run(tmp_path):
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), verdicts=[("REJECT", ["empty_pocket"])])
    c.post("/challenge/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    assert "run the new set in full" in _register(c, "C2", "broken_tablet")["message"]
    login(c, "op", "pw-op")
    assert c.post("/challenge/eject", json={"ejected": True}).json() == {"error": "Inspect the challenge strip first"}
    assert c.get("/challenge").json()["next"] == "Place C1 (empty pocket, expect Rule 2)"
    assert not (p / "challenge_runs.jsonl").exists()


def test_with_a_camera_the_run_uses_the_live_frame(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    cv2.imwrite(str(photos / "a.jpg"), make_strip(missing=(0,), seed=3))
    c, p = bench(tmp_path, source=str(photos))
    try:
        _judge_queue(c, ("REJECT", ["empty_pocket"]))
        login(c, "qa1", "pw-qa")
        assert "error" not in _register(c, "C1", "empty_pocket")
        login(c, "op", "pw-op")
        assert "camera" in c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()["error"]
        for _ in range(100):
            if c.app.state.source.get() is not None:
                break
            time.sleep(0.05)
        assert c.post("/challenge/inspect").json()["caught"] is True
        run = c.post("/challenge/eject", json={"ejected": True}).json()["run"]
        assert run["passed"] is True and run["source"] == "camera"
        assert [e["details"]["source"] for e in _audit(p) if e["action"] == "physical_challenge"] == ["camera"]
    finally:
        c.app.state.source.stop.set()


def test_challenge_inspect_shows_the_station_step(tmp_path):
    """The Challenge Inspect button is a long action like Inspect: /status names the step while the strip is judged."""
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"))
    st, seen = c.app.state.station, []

    def judge(image, sid, name):
        seen.append(st.step)
        return Verdict("REJECT", [st._reason("empty_pocket", 0)], strip_id=sid, image_name=name)
    st._judge = judge
    assert c.post("/challenge/inspect", files=up(make_strip(seed=15))).json()["caught"]
    assert seen and seen[0].startswith("Finding the strip") and st.step == ""


def test_line_board_shows_the_challenge_gate_and_a_miss(tmp_path):
    """The board's release list has the challenge run when a set is registered; a miss is STOPPED with its own reason
    and waits for QA as a failed challenge."""
    c, p = _set_bench(tmp_path, ("C1", "empty_pocket"), verdicts=[("PASS", [])])
    j = c.get("/line").json()
    assert [(r["item"], r["done"]) for r in j["release"]] == [
        ("Self-test for this batch", True), (CHAL, False), ("Start-up print check", True),
        ("QA signature on the start-up specimen", True)]
    assert (j["state"], j["reason"]) == ("STARTING", "Release 3 of 4 — still needed: " + CHAL)
    c.post("/challenge/inspect", files=up(make_strip(seed=15)))  # missed: locked at once, before any tick
    j = c.get("/line").json()
    assert (j["state"], j["reason"]) == ("STOPPED", "Physical challenge missed: C1 not rejected")
    [w] = [i for i in j["waiting"] if i["kind"] == "challenge_failed"]
    assert w["text"] == "Physical challenge failed: C1 not rejected" and w["who"] == "QA"
    row = json.loads((p / "states.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (row["to"], row["cause"]) == ("STOPPED", "Physical challenge missed: C1 not rejected")
