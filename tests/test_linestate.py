import json
from datetime import datetime

import pytest

from d2i.ipqc import BLOCK_PRINT, BLOCK_SELFTEST, BLOCK_SPECIMEN
from d2i.linestate import finder_down, investigating, line_state, record_change, segments

CRIT = "Critical: Rule 5 failed: pocket 1 has a different tablet"
FINDER_HOLD = {"status": "HOLD", "at": "2026-10-28T10:00:00", "batch": "B1",
               "reasons": [{"rule": 1, "check": "finder", "pocket": None,
                            "text": "Pocket finder (SAM 3.1) did not answer — check this strip by hand"}]}
FINDER_HOLD_TIME = datetime.fromisoformat(FINDER_HOLD["at"]).timestamp()
PASS = {"status": "PASS", "at": "2026-10-28T10:00:05", "batch": "B1", "reasons": []}


def facts(**kw):
    """A taught, self-tested station with a released batch: RUNNING."""
    return dict(dict(checklist=True, taught=True, locked=False, lock_reason="", batch_open=True, blockers=[],
                     held=False, held_by="", hold_reason="", quarantine=0, suspended=False, record_unsigned=False), **kw)


# --- one test per row of the report's state table ---
def test_no_batch_open_is_idle():
    s = line_state(facts(batch_open=False))
    assert (s["state"], s["reason"], s["waiting_for"]) == ("IDLE", "No batch open", None)


def test_batch_open_not_released_is_starting_with_the_release_count():
    s = line_state(facts(blockers=[BLOCK_PRINT, BLOCK_SPECIMEN]))
    assert s["state"] == "STARTING" and s["waiting_for"] == "QA"
    assert s["reason"].startswith("Release 1 of 3") and BLOCK_SPECIMEN in s["reason"]


def test_released_not_held_not_locked_is_running():
    s = line_state(facts())
    assert (s["state"], s["reason"], s["by"], s["waiting_for"], s["also"]) == ("RUNNING", "", None, None, [])


def test_held_line_is_held_with_reason_and_by_whom():
    s = line_state(facts(held=True, held_by="Station", hold_reason=CRIT))
    assert (s["state"], s["reason"], s["by"], s["waiting_for"]) == ("HELD", CRIT, "Station", "QA")


def test_pocket_finder_not_answering_is_suspended():
    s = line_state(facts(suspended=True))
    assert s["state"] == "SUSPENDED" and "pocket finder" in s["reason"].lower() and s["waiting_for"] is None


def test_failed_self_test_with_quarantine_is_stopped():
    s = line_state(facts(locked=True, lock_reason="Self-test missed: empty_pocket", quarantine=4))
    assert (s["state"], s["reason"], s["waiting_for"]) == ("STOPPED", "Self-test failed — quarantine open", "QA")


@pytest.mark.parametrize("why,shown", [
    ("Last self-test was more than 8 hours ago", "Last self-test was more than 8 hours ago"),
    ("Checklist changed — teach the station again", "Checklist changed — teach the station again"),
])
def test_self_test_due_or_reteach_is_stopped(why, shown):
    s = line_state(facts(locked=True, lock_reason=why))
    assert (s["state"], s["reason"], s["waiting_for"]) == ("STOPPED", shown, "QA")


def test_not_taught_is_stopped():
    s = line_state(facts(taught=False, locked=True, lock_reason="No self-test this shift", batch_open=False))
    assert (s["state"], s["reason"]) == ("STOPPED", "Teach the station first")


def test_ended_batch_with_unsigned_record_is_complete():
    s = line_state(facts(batch_open=False, record_unsigned=True))
    assert (s["state"], s["reason"], s["waiting_for"]) == ("COMPLETE", "Record waiting for QA signature", "QA")


# --- the start-up self-test is part of STARTING, not a stop ---
@pytest.mark.parametrize("why", ["No self-test this shift", "Last self-test was more than 8 hours ago",
                                 "Checklist changed — teach the station again"])
def test_any_lock_is_stopped_with_or_without_a_batch(why):
    """The board shows the stack light's colour: a locked station is red (lamp LOCKED, PLC coil 1) in every case."""
    s = line_state(facts(locked=True, lock_reason=why, blockers=[BLOCK_SELFTEST, BLOCK_PRINT, BLOCK_SPECIMEN]))
    assert (s["state"], s["reason"]) == ("STOPPED", why) and s["also"][0].startswith("Release 0 of 3")
    idle = line_state(facts(batch_open=False, locked=True, lock_reason=why))
    assert (idle["state"], idle["reason"], idle["also"]) == ("STOPPED", why, ["No batch open"])


# --- precedence: Stopped > Held > Suspended > Starting > Execute; the lesser ones show as "also" ---
def test_precedence_and_also():
    all_on = dict(locked=True, lock_reason="Self-test missed: empty_pocket", quarantine=2, held=True,
                  held_by="Station", hold_reason=CRIT, suspended=True, blockers=[BLOCK_SPECIMEN])
    order = []
    f = facts(**all_on)
    for drop in (dict(locked=False, lock_reason="", quarantine=0), dict(held=False), dict(suspended=False),
                 dict(blockers=[])):
        order.append(line_state(f)["state"])
        f = dict(f, **drop)
    order.append(line_state(f)["state"])
    assert order == ["STOPPED", "HELD", "SUSPENDED", "STARTING", "RUNNING"]
    also = line_state(facts(**all_on))["also"]
    assert also[0] == CRIT and "pocket finder" in also[1].lower() and also[2].startswith("Release 2 of 3")


def test_held_after_the_batch_ended_wins_over_complete():
    s = line_state(facts(batch_open=False, record_unsigned=True, held=True, held_by="qa1", hold_reason="foil"))
    assert s["state"] == "HELD" and s["also"] == ["Record waiting for QA signature"]


# --- Suspended: the last inspection was a finder HOLD and nothing answered since ---
def test_finder_down_until_a_later_answer():
    locked = dict(PASS, status="LOCKED", at="2026-10-28T10:00:09")
    assert finder_down([PASS, FINDER_HOLD], []) is True
    assert finder_down([FINDER_HOLD, locked], []) is True  # a LOCKED row is no answer from the finder
    assert finder_down([FINDER_HOLD, PASS], []) is False
    later = {"passed": True, "time": FINDER_HOLD_TIME + 30}
    assert finder_down([FINDER_HOLD], [later]) is False  # a recorded self-test means the finder answered
    assert finder_down([FINDER_HOLD], [{"passed": True, "time": FINDER_HOLD_TIME + 0.5}]) is True  # same second
    assert finder_down([], []) is False



# --- states.jsonl: one row per change ---
def test_state_log_never_goes_back_in_time(tmp_path):
    path = tmp_path / "states.jsonl"
    record_change(path, 200.0, "HELD", lambda prev: (CRIT, "station", 5, 3))
    assert record_change(path, 199.5, "RUNNING", lambda prev: ("Line running", None, None, 3)) is None  # a stale snapshot
    assert [json.loads(x)["to"] for x in path.read_text(encoding="utf-8").splitlines()] == ["HELD"]


def test_state_log_written_once_per_change(tmp_path):
    path = tmp_path / "states.jsonl"
    why = lambda prev: ("No batch open", None, None, None)
    assert record_change(path, 100.0, "IDLE", why)["from"] is None
    for t in (101.0, 102.0, 103.0):
        assert record_change(path, t, "IDLE", why) is None
    held = record_change(path, 110.0, "HELD", lambda prev: (CRIT, "Station", 5, 12))
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[1] == held
    assert {k: held[k] for k in ("time", "from", "to", "cause", "by", "rule", "strip")} == \
        {"time": 110.0, "from": "IDLE", "to": "HELD", "cause": CRIT, "by": "Station", "rule": 5, "strip": 12}
    assert len(held["at"]) == 19  # local ISO, seconds


# --- the timeline: a held segment lasts from the hold to the QA resume ---
def test_held_to_resume_segment_duration():
    rows = [{"time": 1000.0, "to": "RUNNING", "cause": "Batch released", "by": None},
            {"time": 1300.0, "to": "HELD", "cause": CRIT, "by": "Station"},
            {"time": 1745.0, "to": "RUNNING", "cause": "Resumed: lamp moved back", "by": "qa1"}]
    seg = segments(rows, 1100.0, 2000.0)
    assert [(s["state"], s["start"], s["seconds"]) for s in seg] == [("RUNNING", 1100.0, 200.0), ("HELD", 1300.0, 445.0),
                                                                   ("RUNNING", 1745.0, 255.0)]
    assert (seg[1]["cause"], seg[1]["closed_by"], seg[1]["close_cause"]) == (CRIT, "qa1", "Resumed: lamp moved back")
    assert seg[2]["open"] is True and segments(rows, 3000.0, 3000.0)[-1]["seconds"] == 0


def test_investigating_line():
    assert investigating(False, [], 0) is None
    assert investigating(True, ["image_health", "camera_shift"], 0) == "Investigating: image_health → camera_shift …"
    assert investigating(True, ["image_health"], 1) == "Investigating: image_health … waiting for QA approval"


def test_challenge_miss_is_stopped_with_its_own_reason_and_counts_in_release():
    from d2i.ipqc import BLOCK_CHALLENGE
    s = line_state(facts(locked=True, lock_reason="Physical challenge missed: C1 not rejected", quarantine=3))
    assert (s["state"], s["reason"], s["waiting_for"]) == ("STOPPED", "Physical challenge missed: C1 not rejected", "QA")
    s = line_state(facts(blockers=[BLOCK_CHALLENGE, BLOCK_PRINT], challenge_set=True))
    assert s["state"] == "STARTING" and s["reason"].startswith("Release 2 of 4 — still needed: Physical challenge")
    assert line_state(facts(blockers=[BLOCK_CHALLENGE]))["reason"].startswith("Not released")  # no set: not a gate item
