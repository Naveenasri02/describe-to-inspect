import json
import shutil
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from d2i import trace
from d2i.audit import AuditLog, Users
from d2i.checklist import approve
from d2i.judge import Judge
from d2i.llm import ToolChat
from d2i.meta_api import Budget
from d2i.plc import Plc
from d2i.station import Station
from d2i.pockets import Layout
from d2i.supervisor import (GOOD_STRIP_FIRST, MAX_STEPS, MODEL_DOWN, STEP_LIMIT, TOOLS, Supervisor, baseline_from,
                            resume_line)
from tests.conftest import STRIP_BOX, FakeHTTP, make_strip
from tests.test_station import CHECKLIST, T0, Clock

GOOD = [make_strip(seed=i, shift=(i, 0)) for i in range(6)]
_ids = count(1)


def calls(*pairs):
    """One model step asking for one or more tools (Spark Open often asks for two at once)."""
    return {"content": None, "tool_calls": [
        {"id": f"call_{next(_ids)}", "type": "function",
         "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}
        for name, args in pairs]}


def say(text):
    return {"content": text}


class ScriptedChat:
    last_platform = "Baseten (open weights)"

    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages, tools):
        self.seen.append(list(messages))
        return self.replies.pop(0)


def budget(tmp_path):
    return Budget(tmp_path / "spend.json")


@pytest.fixture(scope="module")
def taught(tmp_path_factory):
    base = tmp_path_factory.mktemp("product")
    approve(CHECKLIST, "Priya", base / "checklists.jsonl")
    judge = Judge()
    Station(base, judge=judge, clock=lambda: T0).teach(GOOD)
    good = tmp_path_factory.mktemp("good")
    for i, im in enumerate(GOOD):
        cv2.imwrite(str(good / f"good{i}.jpg"), im)
    return base, judge, good


@pytest.fixture
def bench(taught, tmp_path):
    base, judge, good = taught
    shutil.copytree(base, tmp_path / "p")
    users = Users(tmp_path / "users.json")
    users.add("op", "operator", "pw-op")
    users.add("qa1", "qa", "pw-qa")
    clock = Clock()
    return SimpleNamespace(st=Station(tmp_path / "p", judge=judge, clock=clock), clock=clock, good=good,
                           users=users, plc=Plc(host="127.0.0.1", port=0), audit=AuditLog(tmp_path / "audit.jsonl"))


GOOD_Q = {"question": "Is a known-good strip under the camera now?"}


def supervisor(b, chat, frame, answer="no"):
    """answer: the operator's reply; a question about a known-good strip gets "yes" unless answer is a function."""
    ask = answer if callable(answer) else (lambda q: "yes" if "good strip" in q.lower() else answer)
    return Supervisor(b.st, b.plc, b.audit, chat, ask, baseline_from(GOOD), STRIP_BOX, lambda: frame, b.good)


def self_test_chat(*after):
    """The operator confirms a known-good strip, then the agent runs the self-test."""
    return ScriptedChat(calls(("ask_operator", GOOD_Q)), calls(("run_self_test", {})), *after)


def test_lamp_drift_ends_with_pending_reteach(bench):
    # Lamp moved closer: brighter, with glare. (A darkened good synthetic strip fails the self-test on the foil border.)
    frame = np.clip(make_strip(seed=12).astype(np.float32) * 1.2, 0, 255).astype(np.uint8)
    chat = ScriptedChat(calls(("image_health", {}), ("camera_shift", {})),
                        calls(("ask_operator", GOOD_Q)),
                        calls(("run_self_test", {})),
                        calls(("ask_operator", {"question": "Did anyone move the lamp?"})),
                        calls(("request_reteach", {"reason": "lighting changed since teaching"})),
                        say("The lamp moved: brightness and glare are up on the teaching photos and the self-test passed."))
    taught_at, model = bench.st.taught_at, (bench.st.dir / "model.pt").stat().st_mtime_ns
    out = supervisor(bench, chat, frame, "Yes, I moved the lamp").investigate("rejects rose: 6 of the last 10 strips")
    assert [s["tool"] for s in out["steps"]] == ["image_health", "camera_shift", "ask_operator", "run_self_test",
                                                  "ask_operator", "request_reteach"]
    health, shift, _, test, answer, _ = (s["result"] for s in out["steps"])
    assert health["drift"] is True and shift["drift"] is False
    assert test["passed"] is True
    assert answer == "Yes, I moved the lamp"
    assert [p["kind"] for p in out["pending"]] == ["reteach"]
    assert out["conclusion"].startswith("The lamp moved") and out["fallback"] is False
    assert out["platform"] == "Baseten (open weights)"
    assert bench.st.taught_at == taught_at and (bench.st.dir / "model.pt").stat().st_mtime_ns == model  # no teach
    assert bench.plc.coils[2] is True
    step2 = chat.seen[1]  # both parallel calls answered, in order, before the next model step
    assert step2[2]["role"] == "assistant" and len(step2[2]["tool_calls"]) == 2
    assert [m["tool_call_id"] for m in step2[3:5]] == [c["id"] for c in step2[2]["tool_calls"]]
    assert [e["action"] for e in bench.audit.entries()] == ["agent_step"] * 6 + ["agent_conclusion"]


def test_real_defects_keep_hold_no_reteach(bench):
    st = bench.st
    st.self_test(make_strip(seed=12), rng_seed=5)
    for i in range(3):
        st.inspect(make_strip(seed=20 + i, missing=(6,)))
    chat = ScriptedChat(calls(("image_health", {}), ("reject_stats", {"window": 20})),
                        calls(("compare_to_last_good", {"pocket": 7})),
                        calls(("hold_line", {"reason": "pocket 7 empty"}), ("draft_deviation", {"summary": "Pocket 7 empty."})),
                        say("Real defects: pocket 7 is empty on 3 strips under rule 2 and the image is healthy."))
    out = supervisor(bench, chat, make_strip(seed=23, missing=(6,))).investigate("3 rejects in a row")
    health, stats, cmp, held, dev = (s["result"] for s in out["steps"])
    assert health["drift"] is False
    assert stats["status"] == {"REJECT": 3} and stats["reject_rules"]["rule 2"] == 3
    assert cmp["score"] > cmp["threshold"]
    assert Path(cmp["evidence"]).parent == st.dir / "evidence" and Path(cmp["evidence"]).exists()
    assert Path(dev["path"]).parent == st.dir / "deviations"
    note = Path(dev["path"]).read_text(encoding="utf-8")
    assert note.startswith("# DRAFT — needs QA review") and "Built with Muse Spark Open" in note
    assert out["conclusion"] == "Real defects: pocket 7 is empty on 3 strips under rule 2 and the image is healthy."
    assert not any(p["kind"] == "reteach" for p in out["pending"])
    assert bench.plc.coils[2] is True


def test_chat_error_falls_back_to_hold(bench, tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "b")
    monkeypatch.setenv("MODEL_API_KEY", "m")
    chat = ToolChat(budget(tmp_path), http=FakeHTTP([(500, "Baseten down"), (503, "Meta down"), (503, "Meta down")]))
    out = supervisor(bench, chat, make_strip(seed=12)).investigate("self-test failed")
    assert out["fallback"] is True and out["conclusion"] == MODEL_DOWN and "call QA" in out["conclusion"]
    assert out["pending"] == [] and bench.plc.coils[2] is True and chat.last_platform is None
    assert bench.audit.entries()[-1]["action"] == "agent_conclusion"


def test_primary_error_uses_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "b")
    monkeypatch.setenv("MODEL_API_KEY", "m")
    trace.clear()
    asks = calls(("image_health", {}))
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    http = FakeHTTP([{"choices": [{"message": asks}], "usage": usage}, (500, "Baseten down"),
                     {"choices": [{"message": {"content": "Lamp moved."}}], "usage": usage}])
    chat = ToolChat(budget(tmp_path), http=http)
    msgs, tools = [{"role": "user", "content": "go"}], [{"type": "function", "function": {"name": "image_health"}}]
    assert chat.complete(msgs, tools) == asks and chat.last_platform == "Baseten (open weights)"
    assert chat.complete(msgs, tools)["content"] == "Lamp moved." and chat.last_platform == "Meta API"
    base, meta = http.calls[1], http.calls[2]
    assert base["url"] == "https://inference.baseten.co/v1/chat/completions"
    assert base["json"]["model"] == "meta-models/muse-spark-open-1026" and base["json"]["reasoning_effort"] == "low"
    assert base["json"]["tools"] == tools
    assert meta["url"] == "https://api.meta.ai/v1/chat/completions" and meta["json"]["model"] == "muse-spark-1.3"
    assert "tool_choice" not in meta["json"]
    ev = trace.recent(3)
    assert [(e["platform"], e["ok"]) for e in ev] == [("Baseten (open weights)", True), ("Baseten (open weights)", False),
                                                      ("Meta API", True)]
    assert ev[0]["model"] == "muse-spark-open-1026" and ev[0]["usd"] == pytest.approx(1000 * 1.25e-6 + 100 * 4.25e-6)
    assert ev[1]["error"] == "HTTP 500" and "error" not in ev[0]
    monkeypatch.delenv("BASETEN_API_KEY")  # no Baseten key at all: straight to the Meta API
    monkeypatch.setattr("d2i.llm.BASETEN_KEY_FILE", tmp_path / "missing.txt")
    http.responses.append({"choices": [{"message": {"content": "ok"}}], "usage": usage})
    assert chat.complete(msgs, tools)["content"] == "ok" and http.calls[-1]["url"].startswith("https://api.meta.ai")


class Endless:
    last_platform = "Baseten (open weights)"

    def __init__(self, *pairs):
        self.n, self.pairs = 0, pairs

    def complete(self, messages, tools):
        self.n += 1
        return calls(*self.pairs)


def test_step_cap_12(bench):
    chat = Endless(("no_such_tool", {}))
    out = supervisor(bench, chat, make_strip(seed=12)).investigate("rejects rose")
    assert chat.n == MAX_STEPS == 12 and out["conclusion"] == STEP_LIMIT
    assert len(out["steps"]) == 12 and all("error" in s["result"] for s in out["steps"])
    assert bench.plc.coils[2] is True


def test_tool_call_cap_20(bench):
    chat = Endless(("no_such_tool", {}), ("compare_to_last_good", "{not json"))  # two calls per step
    out = supervisor(bench, chat, make_strip(seed=12)).investigate("rejects rose")
    assert out["conclusion"] == "investigation stopped: too many tool calls — call QA"
    assert len(out["steps"]) == 20 and chat.n == 11 and bench.plc.coils[2] is True
    assert bench.audit.entries()[-1]["details"]["conclusion"] == out["conclusion"]


def test_resume_refused_while_locked(bench):
    chat = ScriptedChat(calls(("request_resume", {"reason": "camera checks out"})), say("Camera is fine."))
    sup = supervisor(bench, chat, make_strip(seed=12))
    (req,) = sup.investigate("operator pressed stop")["pending"]
    assert bench.st.locked
    out = sup.approve(req["id"], bench.users, "qa1", "pw-qa")
    assert out["approved"] is False and out["message"] == "Station locked: " + bench.st.lock_reason
    assert bench.plc.coils[2] is True
    assert bench.st.self_test(make_strip(seed=12), rng_seed=5)["passed"]
    assert sup.approve(req["id"], bench.users, "qa1", "pw-qa")["approved"] is True
    assert bench.plc.coils[2] is False
    with pytest.raises(ValueError):
        sup.approve(req["id"], bench.users, "qa1", "pw-qa")


def test_approve_requires_qa_password(bench):
    bench.st = st = Station(bench.st.dir, clock=bench.clock)  # its own judge: re-teaching must not touch the shared one
    chat = ScriptedChat(calls(("request_reteach", {"reason": "lamp moved"}),
                              ("request_release_quarantine", {"reason": "strips re-checked by hand"})),
                        say("Lamp moved; asking QA."))
    sup = supervisor(bench, chat, make_strip(seed=12))
    reteach, release = sup.investigate("rejects rose")["pending"]
    for name, pw in [("op", "pw-op"), ("qa1", "wrong")]:
        with pytest.raises(PermissionError):
            sup.approve(reteach["id"], bench.users, name, pw)
    assert st.taught_at == T0
    bench.clock.t += 60
    assert sup.approve(reteach["id"], bench.users, "qa1", "pw-qa")["approved"] is True
    assert st.taught_at == T0 + 60 and st.lock_reason == "No self-test this shift"
    e = bench.audit.entries()[-1]
    assert e["action"] == "agent_approve" and e["user"] == "qa1" and e["details"]["kind"] == "reteach"
    with pytest.raises(ValueError):
        sup.approve(reteach["id"], bench.users, "qa1", "pw-qa")
    sup.approve(release["id"], bench.users, "qa1", "pw-qa")
    assert [(e["action"], e["reason"]) for e in bench.audit.entries()[-2:]] == [
        ("release_quarantine", "Agent request: strips re-checked by hand"),
        ("agent_approve", "Agent request: strips re-checked by hand")]
    assert bench.audit.entries()[-1]["details"]["approved_by"] == "qa1"


def test_agent_self_test_pass_does_not_record_or_unlock(bench):
    chat = self_test_chat(say("Self-test looks fine."))
    out = supervisor(bench, chat, make_strip(seed=12)).investigate("shift start")
    assert out["steps"][1]["result"] == {"passed": True, "missed": [],
                                         "note": "diagnostic only — does not unlock the station"}
    assert not (bench.st.dir / "selftests.jsonl").exists() and bench.st.lock_reason == "No self-test this shift"
    assert bench.st.self_test(make_strip(seed=12), rng_seed=5)["passed"]  # a person unlocks it
    supervisor(bench, self_test_chat(say("Fine.")), make_strip(seed=12)).investigate("x")
    assert len((bench.st.dir / "selftests.jsonl").read_text().splitlines()) == 1 and not bench.st.locked


def test_agent_self_test_miss_locks_and_opens_quarantine(bench, monkeypatch):
    from d2i.audit import quarantine_list
    assert bench.st.self_test(make_strip(seed=12), rng_seed=5)["passed"] and not bench.st.locked
    monkeypatch.setattr(bench.st.judge, "pocket_threshold", 10.0)  # blind judge: every painted defect is missed
    monkeypatch.setattr(bench.st.judge, "border_threshold", 10.0)
    chat = self_test_chat(say("Self-test missed."))
    res = supervisor(bench, chat, make_strip(seed=12)).investigate("rejects fell")["steps"][1]["result"]
    assert res["passed"] is False and "empty_pocket" in res["missed"]
    rows = [json.loads(x) for x in (bench.st.dir / "selftests.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and rows[-1]["passed"] is False
    assert bench.st.locked and bench.st.lock_reason.startswith("Self-test missed: ")
    assert quarantine_list(bench.st.dir, bench.audit)["failed_at"] == rows[-1]["at"]


def test_agent_self_test_without_its_finder_says_so_and_records_nothing(bench):
    bench.st.layout_source = "sam"  # taught with SAM 3.1, and no SAM client to answer now
    chat = self_test_chat(say("Finder down."))
    out = supervisor(bench, chat, make_strip(seed=12)).investigate("x")
    assert out["steps"][1]["result"] == "Self-test could not run: the pocket finder (SAM 3.1) did not answer — try again"
    assert not (bench.st.dir / "selftests.jsonl").exists()


def test_agent_self_test_without_strip_records_nothing(bench):
    chat = self_test_chat(say("No strip."))
    out = supervisor(bench, chat, np.full((360, 640, 3), 128, np.uint8)).investigate("x")
    assert out["steps"][1]["result"] == "no good strip under the camera"
    assert not (bench.st.dir / "selftests.jsonl").exists()


def test_agent_self_test_waits_for_the_operator_to_confirm_a_good_strip(bench):
    """Found in testing: the agent ran the self-test on the defective strip in view; the failed run locked the station and
    opened quarantine. It runs only after the operator said yes, in this investigation, to a good strip in view."""
    st = bench.st
    assert st.self_test(make_strip(seed=12), rng_seed=5)["passed"]
    before = (st.dir / "selftests.jsonl").read_text(encoding="utf-8")
    chat = ScriptedChat(calls(("run_self_test", {})), calls(("ask_operator", GOOD_Q)), calls(("run_self_test", {})),
                        say("The operator has no good strip."))
    sup = supervisor(bench, chat, make_strip(seed=23, missing=(6,)), answer=lambda q: "No, it is the rejected one")
    steps = sup.investigate("self-test failed")["steps"]
    assert steps[0]["result"] == GOOD_STRIP_FIRST and steps[2]["result"] == GOOD_STRIP_FIRST
    assert (st.dir / "selftests.jsonl").read_text(encoding="utf-8") == before and not st.locked
    assert "ask_operator" in next(t["function"]["description"] for t in TOOLS if t["function"]["name"] == "run_self_test")
    steps = supervisor(bench, self_test_chat(say("Fine.")), make_strip(seed=12)).investigate("x")["steps"]
    assert steps[1]["result"]["passed"] is True  # confirmed: it runs


@pytest.mark.parametrize("question, answer, ok", [
    ("Is a known-good strip under the camera now?", "yes", True),
    ("Is a known-good strip under the camera now?", "Yes, it is.", True),
    ("Is a known good strip under the camera?", "ok placed", True),
    ("Is a good strip under the camera now?", "Done", True),
    ("Is a known-good strip under the camera now?", "y", True),
    ("Is the lighting good?", "yes", False),  # not a question about the strip
    ("Have you got a good strip?", "yes", False),  # not "under the camera"
    ("Is a known-good strip under the camera now?", "ok give me a minute", False),
    ("Is a known-good strip under the camera now?", "Okay I will fetch one", False),
    ("Is a known-good strip under the camera now?", "Yes, but it is the rejected one", False),
    ("Is a known-good strip under the camera now?", "no", False),
    ("Is a known-good strip under the camera now?", "not yet", False),
    ("Is a known-good strip under the camera now?", "wait", False),
    ("Is a known-good strip under the camera now?", "yes later", False),
    ("Is a known-good strip under the camera now?", "yes, the bad one", False),
    ("Is a known-good strip under the camera now?", "no answer", False),  # the screen's timeout
])
def test_self_test_gate_needs_a_plain_yes_to_a_good_strip_under_the_camera(bench, question, answer, ok):
    """Review: "Is the lighting good?" -> "yes", "ok give me a minute", "Okay I will fetch one" and "Yes, but it is
    the rejected one" all let the self-test run on whatever was in view."""
    sup = supervisor(bench, ScriptedChat(), make_strip(seed=12))
    sup._steps = [{"tool": "ask_operator", "args": {"question": question}, "result": answer}]
    assert sup._good_strip_confirmed() is ok
    assert "Is a known-good strip under the camera now?" in GOOD_STRIP_FIRST


def test_supervisor_sees_the_strip_the_last_inspection_found(bench, monkeypatch):
    """Found in testing: with a strip in view the offline finder answered "no strip" (camera_shift dx -640,
    compare_to_last_good an error). The tools use the layout of the station's most recent inspection; no paid call."""
    st = bench.st
    st.self_test(make_strip(seed=12), rng_seed=5)
    frame = make_strip(seed=23, missing=(6,), shift=(20, 0))
    v = st.inspect(frame, camera=True)
    assert v.status == "REJECT" and st.layout_for(frame) is v.layout and len(v.layout.pockets) == 10
    blind = lambda *a, **k: Layout((0, 0, 0, 0), [], "grid", False, "no strip")
    monkeypatch.setattr("d2i.supervisor.find_layout", blind)  # what the offline finder said on the close-up
    chat = ScriptedChat(calls(("camera_shift", {}), ("compare_to_last_good", {"pocket": 7})), say("Pocket 7 empty."))
    shift, cmp = (s["result"] for s in supervisor(bench, chat, frame).investigate("x")["steps"])
    assert shift == {"dx": 20.0, "dy": 0.0, "drift": False, "note": ""}, shift
    assert cmp["pocket"] == 7 and cmp["score"] > cmp["threshold"] and Path(cmp["evidence"]).exists(), cmp


def test_supervisor_uses_the_last_layout_only_for_the_view_it_was_found_in(bench):
    """Review: a tray crop (or an uploaded photo) left its layout in crop coordinates, and a moved or swapped strip
    left a stale one; camera_shift compared it with the full-frame box. Only a whole camera frame's layout is kept, and
    only while the camera still shows that view; otherwise the contour finder looks at the frame now."""
    st = bench.st
    st.self_test(make_strip(seed=12), rng_seed=5)
    frame = make_strip(seed=23, missing=(6,), shift=(20, 0))
    crop = frame[50:310, 60:600]
    st.inspect(crop)  # a tray crop or an upload: not the camera's view
    assert st.layout_for(frame) is None and st.layout_for(crop) is None
    shift = supervisor(bench, ScriptedChat(calls(("camera_shift", {})), say("x")), frame).investigate("x")["steps"]
    assert shift[0]["result"]["dx"] == 20.0, shift  # measured on the frame, not with the crop's layout
    v = st.inspect(frame, camera=True)
    assert st.layout_for(frame) is v.layout
    assert st.layout_for(make_strip(seed=24, missing=(6,), shift=(20, 0))) is v.layout  # only camera noise changed
    moved = make_strip(seed=23, missing=(6,), shift=(60, 0))
    for changed in (moved, np.full_like(frame, 40), crop):  # moved, strip taken away, another size
        assert st.layout_for(changed) is None
    shift = supervisor(bench, ScriptedChat(calls(("camera_shift", {})), say("x")), moved).investigate("x")["steps"]
    assert shift[0]["result"]["dx"] == 60.0 and shift[0]["result"]["drift"] is True, shift


def test_malformed_tool_call_holds_and_calls_qa(bench):
    for bad in (["junk"], [{"id": "c1", "function": {"name": ["image_health"], "arguments": "{}"}}]):
        out = supervisor(bench, ScriptedChat({"content": None, "tool_calls": bad}), make_strip(seed=12)).investigate("x")
        assert out["conclusion"] == MODEL_DOWN and out["fallback"] is True and bench.plc.coils[2] is True
        assert bench.audit.entries()[-1]["action"] == "agent_conclusion"
    out = supervisor(bench, ScriptedChat({"content": ""}), make_strip(seed=12)).investigate("x")
    assert out["conclusion"] == MODEL_DOWN and out["fallback"] is True


def test_qa_can_resume_without_model_request(bench):
    sup = supervisor(bench, ScriptedChat(say("Rejects look real.")), make_strip(seed=12))
    assert sup.investigate("rejects rose")["pending"] == [] and bench.plc.coils[2] is True
    resume = lambda *a: resume_line(bench.st, bench.plc, bench.audit, bench.users, *a)  # noqa: E731
    with pytest.raises(PermissionError):
        resume("op", "pw-op", "checked")
    with pytest.raises(ValueError):
        resume("qa1", "pw-qa", " ")
    assert resume("qa1", "pw-qa", "checked") == {"resumed": False, "message": "Station locked: No self-test this shift"}
    assert bench.plc.coils[2] is True
    assert bench.st.self_test(make_strip(seed=12), rng_seed=5)["passed"]  # a person's self-test records
    assert resume("qa1", "pw-qa", "strips re-checked by hand")["resumed"] is True
    assert bench.plc.coils[2] is False
    e = bench.audit.entries()[-1]
    assert (e["action"], e["user"], e["reason"]) == ("resume_line", "qa1", "strips re-checked by hand")


def test_hold_survives_restart(bench):
    supervisor(bench, ScriptedChat(say("Held.")), make_strip(seed=12)).investigate("rejects rose")
    held = json.loads((bench.st.dir / "hold.json").read_text(encoding="utf-8"))
    assert held["held"] is True and held["by"] == "supervisor" and held["at"]
    bench.plc = Plc(host="127.0.0.1", port=0)  # process restart: a fresh PLC starts with coil 2 off
    supervisor(bench, ScriptedChat(), make_strip(seed=12))
    assert bench.plc.coils[2] is True
    bench.st.self_test(make_strip(seed=12), rng_seed=5)
    resume_line(bench.st, bench.plc, bench.audit, bench.users, "qa1", "pw-qa", "checked")
    bench.plc = Plc(host="127.0.0.1", port=0)
    supervisor(bench, ScriptedChat(), make_strip(seed=12))
    assert bench.plc.coils[2] is False
    assert json.loads((bench.st.dir / "hold.json").read_text(encoding="utf-8"))["by"] == "qa1"


def test_supervisor_tool_lock_is_used(bench):
    class Rec:
        n = 0

        def __enter__(self):
            Rec.n += 1

        def __exit__(self, *a):
            return False
    chat = ScriptedChat(calls(("image_health", {}), ("reject_stats", {"window": 5})), say("Fine."))
    sup = Supervisor(bench.st, bench.plc, bench.audit, chat, lambda q: "no", baseline_from(GOOD), STRIP_BOX,
                     lambda: make_strip(seed=12), bench.good, tool_lock=Rec())
    sup.investigate("check")
    assert Rec.n == 3 and [s["tool"] for s in sup.steps] == ["image_health", "reject_stats"]  # 2 tools + its own hold


class FlagLock:
    """A tool_lock that records whether it is held."""
    held = False

    def __enter__(self):
        self.held = True

    def __exit__(self, *a):
        self.held = False


def test_the_agents_own_holds_are_written_under_the_station_lock(bench, monkeypatch):
    """Review: the hold at the start of an investigation and the too-many-calls hold were written outside the station
    lock, so one could land between the sign gate and the signature."""
    import d2i.supervisor as sv
    lock, seen, real = FlagLock(), [], sv.set_hold
    monkeypatch.setattr(sv, "set_hold", lambda *a, **k: (seen.append(lock.held), real(*a, **k))[1])
    chat = Endless(("no_such_tool", {}), ("no_such_tool", {}))  # two calls per step: stopped at 20
    sup = Supervisor(bench.st, bench.plc, bench.audit, chat, lambda q: "no", baseline_from(GOOD), STRIP_BOX,
                     lambda: make_strip(seed=12), bench.good, tool_lock=lock)
    assert sup.investigate("rejects rose")["conclusion"] == "investigation stopped: too many tool calls — call QA"
    assert seen == [True, True] and bench.plc.coils[2] is True
