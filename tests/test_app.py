import hashlib
import json
import re
import threading
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import app as app_mod
import d2i.meta_api as meta_api
from datetime import datetime

from d2i.audit import AuditLog, Users
from d2i.batch import Batches
from d2i.checklist import CHECKS, approve
from d2i.meta_api import SamTrackPoint
from d2i.pockets import Layout
from d2i.record import _lines
from d2i.station import Verdict
from d2i.supervisor import set_hold
from d2i.plc import Plc
from tests.conftest import make_strip, pocket_box
from tests.test_station import CHECKLIST


def jpg(img):
    return cv2.imencode(".jpg", img)[1].tobytes()


def test_status_starts_locked(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None))
    s = c.get("/status").json()
    assert s["locked"] is True and s["spent_usd"] == 0 and s["checklist_version"] is None
    assert s["counts"] == {} and s["last_verdict"] is None
    assert c.get("/").status_code == 200 and c.get("/lamp").status_code == 200


def test_describe_without_key_returns_error_not_500(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("d2i.meta_api.KEY_FILE", tmp_path / "nokey.txt")
    c = TestClient(app_mod.create_app(tmp_path, None))
    r = c.post("/describe", json={"text": "ten tablets, two rows of five"})
    assert r.status_code == 200 and "nokey.txt" in r.json()["error"]


def test_inspect_without_checklist_says_no_checklist(tmp_path):
    Users(tmp_path / "users.json").add("op", "operator", "pw-op")
    c = TestClient(app_mod.create_app(tmp_path / "p", None))
    login(c, "op", "pw-op")
    r = c.post("/inspect", files={"image": ("a.jpg", jpg(make_strip()), "image/jpeg")})
    assert r.status_code == 200 and r.json()["status"] == "NO_CHECKLIST"


def test_inspect_upload_returns_verdict(tmp_path):
    p = tmp_path / "p"
    approve(CHECKLIST, "Priya", p / "checklists.jsonl")
    (p / "good").mkdir()
    for i in range(6):
        (p / "good" / f"{i}.jpg").write_bytes(jpg(make_strip(seed=i, shift=(i, 0))))
    Users(tmp_path / "users.json").add("qa1", "qa", "pw-qa")
    c = TestClient(app_mod.create_app(p, None))
    login(c, "qa1", "pw-qa")
    assert c.post("/teach", json={}).json().get("error") is None
    up = lambda img: {"image": ("a.jpg", jpg(img), "image/jpeg")}
    assert c.post("/selftest", files=up(make_strip(seed=12))).json()["passed"] is True
    assert c.get("/status").json()["locked"] is False
    open_batch(p)
    release_batch(p)
    v = c.post("/inspect", files=up(make_strip(missing=(0,), seed=13))).json()
    assert v["status"] == "REJECT"
    assert "pocket 1" in " ".join(r["text"] for r in v["reasons"])
    s = c.get("/status").json()
    assert s["counts"]["REJECT"] == 1 and s["state"] == "REJECT"
    assert "Packaging-line QA bench" in c.get("/").text


class FakeClient:
    has_key = False  # an offline bench: no Meta key, so teaching on the checklist grid is allowed

    def spark_json(self, system, user, schema, name="result", **kw):
        return {"product": "vit C", "rows": 2, "per_row": 5, "item": "tablet",
                "reject_if": ["empty_pocket"], "unclear": []}

    def sam_image(self, image, prompt):
        raise meta_api.MetaError("offline")  # the bench finds pockets with the checklist grid


def test_teach_offline_with_the_real_client_and_no_key(tmp_path, monkeypatch):
    """The app always has a Meta client; with no key it is an offline bench and teaches on the checklist grid."""
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("d2i.meta_api.KEY_FILE", tmp_path / "nokey.txt")
    c, p = bench(tmp_path, client=meta_api.MetaClient(meta_api.Budget()), batch=False)
    assert c.app.state.station.layout_source == "grid" and c.app.state.station.taught_at is not None


def test_describe_then_approve_flow(tmp_path):
    Users(tmp_path / "users.json").add("qa1", "qa", "pw-qa")
    c = TestClient(app_mod.create_app(tmp_path / "p", None, client=FakeClient()))
    login(c, "qa1", "pw-qa")
    assert c.post("/approve", json={}).json()["error"]
    r = c.post("/describe", json={"text": "ten tablets"}).json()
    assert r["checklist"]["count"] == 10
    assert c.get("/status").json()["pending"]
    cl = c.post("/approve", json={}).json()["checklist"]
    assert cl["version"] == 1 and cl["approved_by"] == "qa1"
    assert c.get("/status").json()["checklist_version"] == 1
    e = json.loads((tmp_path / "p" / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (e["action"], e["user"], e["details"]["approved_by"]) == ("checklist_approve", "qa1", "qa1")


def test_approve_and_teach_need_a_qa_login(tmp_path):
    c, p = bench(tmp_path, client=FakeClient())
    c.post("/describe", json={"text": "ten tablets"})
    for user in (None, ("op", "pw-op")):
        if user:
            login(c, *user)
        assert c.post("/approve", json={"by": "Anyone"}).json() == {"error": "Log in as a QA user first"}
        assert c.post("/teach", json={"capture": True}).json() == {"error": "Log in as a QA user first"}
    assert c.get("/status").json()["checklist_version"] == 1 and len(list((p / "good").glob("*.jpg"))) == 6
    login(c, "qa1", "pw-qa")
    assert c.post("/teach", json={}).json().get("error") is None
    e = json.loads((p / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (e["action"], e["user"], e["details"]["taught_by"]) == ("teach", "qa1", "qa1")


def test_status_survives_corrupt_log_line(tmp_path):
    (tmp_path / "log.jsonl").write_text('{"status": "PASS"}\nnot json\n', encoding="utf-8")
    meta_api.DEFAULT_SPEND.write_text("garbage")
    s = TestClient(app_mod.create_app(tmp_path, None)).get("/status")
    assert s.status_code == 200 and s.json()["counts"] == {"PASS": 1} and s.json()["spent_usd"] is None


def test_describe_multipart_without_audio_returns_error(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None, client=FakeClient()))
    r = c.post("/describe", files={"other": ("a.txt", b"x")})
    assert r.status_code == 200 and "audio" in r.json()["error"]


# --- login, quarantine, trace, Shift Supervisor ---
class ScriptChat:
    last_platform = "Baseten (open weights)"

    def __init__(self, *replies):
        self.replies = list(replies)

    def complete(self, messages, tools):
        return self.replies.pop(0)


def open_batch(p, no="B1"):
    """A clear line clearance and a started batch, written straight to the files (no SAM call)."""
    b = Batches(p)
    b.clearance({"clear": True, "found": []}, "qa1", time.time())
    return b.start(no, "01/2026", "01/2028", "45", "qa1", time.time())


def release_batch(p, no="B1"):
    """What releases an open batch: a passed self-test, a PRINT_OK print row and QA's specimen signature."""
    now = time.time()
    b = Batches(p)
    last = (b._read("selftests.jsonl") or [{"passed": True, "at": "x", "results": [], "checklist_version": 1, "taught_at": 0}])[-1]  # a copy keeps the station's lock state
    b._write("selftests.jsonl", dict(last, time=now))
    b._write("prints.jsonl", {"at": datetime.fromtimestamp(now).isoformat(timespec="seconds"), "batch": no, "time": now,
                              "read": {}, "status": "PRINT_OK", "reasons": [], "by": "op"})
    AuditLog(p / "audit.jsonl").append("specimen_signed", "qa1", {"batch": no, "print_at": "x"})


def bench(tmp_path, chat=None, client=None, plc=None, source=None, batch=True, release=True, as_user=("op", "pw-op")):
    """Taught, self-tested bench in tmp_path/p with a QA user (qa1/pw-qa) and an operator (op/pw-op)."""
    p = tmp_path / "p"
    approve(CHECKLIST, "Priya", p / "checklists.jsonl")
    (p / "good").mkdir()
    for i in range(6):
        (p / "good" / f"{i}.jpg").write_bytes(jpg(make_strip(seed=i, shift=(i, 0))))
    users = Users(tmp_path / "users.json")
    users.add("qa1", "qa", "pw-qa")
    users.add("op", "operator", "pw-op")
    c = TestClient(app_mod.create_app(p, source, chat=chat, client=client, plc=plc))
    login(c, "qa1", "pw-qa")  # QA teaches
    assert c.post("/teach", json={}).json().get("error") is None
    c.post("/selftest", files={"image": ("a.jpg", jpg(make_strip(seed=12)), "image/jpeg")})
    c.post("/logout")
    if as_user:  # /inspect, /tray and the voice inspect command need a login
        login(c, *as_user)
    if batch:
        open_batch(p)
        if release:
            release_batch(p)
    return c, p


def login(c, name, pw):
    return c.post("/login", json={"name": name, "password": pw}).json()


def test_release_requires_qa_login(tmp_path):
    c, p = bench(tmp_path)
    assert "Log in" in c.post("/release", json={"password": "pw-qa", "reason": "x"}).json()["error"]
    assert login(c, "op", "pw-op")["role"] == "operator"
    assert c.post("/release", json={"password": "pw-op", "reason": "x"}).json()["error"]
    assert login(c, "qa1", "wrong")["error"]
    assert login(c, "qa1", "pw-qa") == {"name": "qa1", "role": "qa"}
    assert c.post("/release", json={"password": "bad", "reason": "x"}).json()["error"]
    assert c.post("/release", json={"password": "pw-qa", "reason": "strips re-checked"}).json()["action"] == "release_quarantine"
    rid = c.get("/record").headers["X-Record-Id"]
    assert c.post("/sign", json={"password": "pw-qa", "record_id": rid, "meaning": "Approved"}).json()["signed_by"] == "qa1"


def test_trace_endpoint_lists_events(tmp_path):
    from d2i import trace
    trace.clear()
    c = TestClient(app_mod.create_app(tmp_path / "p", None))
    trace.record("DINOv2", "local", "pocket scoring", 12)
    rows = c.get("/trace").json()
    assert rows[-1]["model"] == "DINOv2" and rows[-1]["platform"] == "local"


def wait_done(c):
    for _ in range(100):
        s = c.get("/investigation").json()
        if not s["running"] and s["result"] is not None:
            return s
        time.sleep(0.05)
    raise AssertionError("investigation did not finish")


def call(name, args):
    return {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                                              "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_investigate_returns_pending_reteach(tmp_path):
    chat = ScriptChat(call("request_reteach", {"reason": "lamp moved"}), {"content": "Lamp moved."})
    c, p = bench(tmp_path, chat)
    assert c.post("/investigate", json={"trigger": "rejects rose"}).json()["id"]
    s = wait_done(c)
    assert s["result"]["conclusion"] == "Lamp moved." and [r["kind"] for r in s["pending"]] == ["reteach"]
    assert c.get("/status").json()["state"] == "HOLD"
    assert c.post("/approve-request", json={"id": s["pending"][0]["id"], "password": "pw-qa"}).json()["error"]  # not logged in
    login(c, "qa1", "pw-qa")
    assert c.post("/approve-request", json={"id": s["pending"][0]["id"], "password": "pw-qa"}).json()["approved"] is True


def test_investigate_needs_teaching(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path / "p", None))
    assert c.post("/investigate", json={}).json() == {"error": "Teach the station first"}


def test_resume_requires_qa_password(tmp_path):
    c, p = bench(tmp_path)
    (p / "hold.json").write_text('{"held": true}')
    assert c.get("/status").json()["held"] is True
    assert c.post("/resume", json={"password": "pw-qa", "reason": "ok"}).json()["error"]  # not logged in
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "wrong", "reason": "ok"}).json()["error"]
    assert c.get("/status").json()["held"] is True
    assert c.post("/resume", json={"password": "pw-qa", "reason": "lamp fixed"}).json()["resumed"] is True
    assert c.get("/status").json()["held"] is False


def test_status_shows_held(tmp_path):
    (tmp_path / "hold.json").write_text('{"held": true}')
    c = TestClient(app_mod.create_app(tmp_path, None))
    s = c.get("/status").json()
    assert s["held"] is True and s["state"] == "LOCKED" and s["quarantine_count"] == 0  # no checklist: locked too


def test_sign_signs_the_record_that_was_viewed(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    r = c.get("/record")
    rid, shown = r.headers["X-Record-Id"], re.search(r"SHA-256 of the record above: (\w{64})", r.text).group(1)
    with (p / "log.jsonl").open("a", encoding="utf-8") as f:  # the log changes after QA opened the record
        f.write(json.dumps({"status": "REJECT", "reasons": [], "time": time.time()}) + "\n")
    signed = c.post("/sign", json={"password": "pw-qa", "record_id": rid, "meaning": "Approved"}).json()
    assert signed["record_sha256"] == shown
    assert c.post("/sign", json={"password": "pw-qa", "record_id": "nope", "meaning": "Approved"}).json()["error"].startswith("Record expired")


class GateChat(ScriptChat):
    def __init__(self, *replies):
        super().__init__(*replies)
        self.gate = threading.Event()

    def complete(self, messages, tools):
        assert self.gate.wait(10)
        return super().complete(messages, tools)


def test_second_investigate_while_running_returns_error(tmp_path):
    chat = GateChat({"content": "Done."})
    c, p = bench(tmp_path, chat)
    assert c.post("/investigate", json={}).json()["id"]
    assert c.post("/investigate", json={}).json() == {"error": "an investigation is already running"}
    chat.gate.set()
    assert wait_done(c)["result"]["conclusion"] == "Done."


def test_answer_to_closed_question_is_rejected(tmp_path):
    chat = ScriptChat(call("ask_operator", {"question": "Did the lamp move?"}), {"content": "Noted."})
    c, p = bench(tmp_path, chat)
    assert c.post("/ask-operator-answer", json={"id": "x", "answer": "late"}).json()["error"] == "That question is closed"
    c.post("/investigate", json={})
    for _ in range(100):
        q = c.get("/investigation").json()["question"]
        if q:
            break
        time.sleep(0.05)
    assert q["text"] == "Did the lamp move?"
    assert c.post("/ask-operator-answer", json={"id": "wrong", "answer": "no"}).json()["error"] == "That question is closed"
    assert c.post("/ask-operator-answer", json={"id": q["id"], "answer": "yes"}).json() == {"ok": True}
    steps = wait_done(c)["steps"]
    assert steps[0]["result"] == "yes"
    assert c.post("/ask-operator-answer", json={"id": q["id"], "answer": "again"}).json()["error"] == "That question is closed"


def test_status_flags_unreadable_quarantine(tmp_path, monkeypatch):
    c = TestClient(app_mod.create_app(tmp_path, None))
    monkeypatch.setattr(app_mod, "quarantine_list", lambda *a, **k: 1 / 0)
    s = c.get("/status").json()
    assert s["quarantine_count"] is None and s["quarantine_error"] is True


def test_hold_endpoint_holds_line(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    assert c.post("/hold", json={}).json() == {"held": True}
    assert plc.coils[2] is True and c.get("/status").json()["held"] is True
    rows = [json.loads(l) for l in (p / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["action"] == "hold_line" and rows[-1]["reason"] == "operator voice command"


def up(img):
    return {"image": ("a.jpg", jpg(img), "image/jpeg")}


def test_held_line_turns_pass_into_hold(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    good = make_strip(seed=15, shift=(2, 1))
    assert c.post("/inspect", files=up(good)).json()["status"] == "PASS" and plc.regs[0] == 1
    c.post("/hold", json={})
    v = c.post("/inspect", files=up(good)).json()
    assert v["status"] == "HOLD" and [r["text"] for r in v["reasons"]] == ["Line held — QA must resume"]
    assert json.loads((p / "log.jsonl").read_text(encoding="utf-8").splitlines()[-1])["status"] == "HOLD"
    assert plc.regs[0] == 1 and plc.regs[2] == 1 and c.get("/status").json()["counts"] == {"PASS": 1, "HOLD": 1}
    assert c.post("/inspect", files=up(make_strip(missing=(0,), seed=13))).json()["status"] == "REJECT"


def test_inspect_error_is_logged_and_held(tmp_path, monkeypatch):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    monkeypatch.setattr(c.app.state.station, "_judge", lambda *a: 1 / 0)
    r = c.post("/inspect", files=up(make_strip(seed=15)))
    v = r.json()
    assert r.status_code == 200 and v["status"] == "HOLD"
    assert [x["text"] for x in v["reasons"]] == ["inspection error — check by hand"]
    row = json.loads((p / "log.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["status"] == "HOLD" and row["at"] and plc.regs[2] == 1 and plc.coils[2] is True


def test_app_spends_from_the_one_default_file(tmp_path, monkeypatch):
    meta_api.DEFAULT_SPEND.write_text('{"spent_usd": 1.25}')
    seen = []

    class Chat(ScriptChat):
        def __init__(self, budget):
            seen.append(budget)
            super().__init__({"content": "ok"})
    monkeypatch.setattr(app_mod, "ToolChat", Chat)
    c, p = bench(tmp_path)
    assert c.get("/status").json()["spent_usd"] == 1.25
    c.post("/investigate", json={})
    wait_done(c)
    assert seen[0].path == meta_api.DEFAULT_SPEND and seen[0].spent == 1.25


class VoiceClient:
    def __init__(self, text=None):
        self.text, self.kw = text, None

    def transcribe(self, wav, keywords=(), languages=None, mode="PUSH_TO_TALK"):
        if self.text is None:
            raise RuntimeError("voice is down")
        self.kw = (list(keywords), languages, mode)
        return self.text


def test_command_endpoint_returns_match(tmp_path):
    vc = VoiceClient("start the self test")
    c = TestClient(app_mod.create_app(tmp_path, None, client=vc))
    r = c.post("/command", files={"audio": ("a.wav", b"x")}, data={"languages": "Tamil"})
    assert r.json() == {"transcript": "start the self test", "command": "self_test"}
    assert vc.kw[1:] == (["Tamil"], "ENDPOINTING") and "blister" in vc.kw[0]


def test_command_endpoint_error_is_plain(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None, client=VoiceClient()))
    r = c.post("/command", files={"audio": ("a.wav", b"x")})
    assert r.status_code == 200 and r.json() == {"error": "voice is down"}
    assert "error" in c.post("/command", files={"other": ("a.txt", b"x")}).json()


def test_plc_lock_coil_follows_station_lock(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    assert plc.coils[1] is False
    login(c, "qa1", "pw-qa")
    c.post("/teach", json={})  # re-taught: locked until a self-test passes
    assert plc.coils[1] is True
    c.post("/selftest", files=up(make_strip(seed=12)))
    assert plc.coils[1] is False
    c.app.state.station.clock = lambda: time.time() + 9 * 3600  # the shift runs out; only /status sees it
    c.get("/status")
    assert plc.coils[1] is True


def test_banner_ready_then_no_strip_grey_then_pass(tmp_path):
    import numpy as np
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    assert c.get("/status").json()["state"] == "READY"
    assert c.post("/inspect", files=up(np.full((360, 640, 3), 128, np.uint8))).json()["status"] == "NO_STRIP"
    assert c.get("/status").json()["state"] == "NO STRIP"
    assert plc.regs == [0, 0, 0, 0] and plc.coils[0] is False  # no reject pulse, no pass counted
    c.post("/inspect", files=up(make_strip(seed=15, shift=(2, 1))))
    assert c.get("/status").json()["state"] == "PASS" and plc.regs[0] == 1


def test_hold_and_resume_work_without_a_supervisor(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    users = Users(tmp_path / "users.json")
    users.add("qa1", "qa", "pw-qa")
    c = TestClient(app_mod.create_app(tmp_path / "p", None, plc=plc))  # never taught: no Supervisor can be built
    assert c.post("/hold", json={"reason": "jam"}).json() == {"held": True}
    s = c.get("/status").json()
    assert plc.coils[2] is True and s["state"] == "LOCKED"  # never taught: locked as well as held
    assert s["text"].startswith("Line held by operator — QA must resume.")
    assert "Log in" in c.post("/resume", json={"password": "pw-qa", "reason": "ok"}).json()["error"]
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "jam cleared"}).json() == {
        "resumed": False, "message": "Station locked: No self-test this shift"}
    assert plc.coils[2] is True


def test_agent_runs_when_offline_finder_cannot_see_teaching_photos(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, ScriptChat({"content": "Checked."}), plc=plc)
    for f in (p / "good").glob("*.jpg"):  # close-ups taught with SAM: the strip touches the frame edge
        f.write_bytes(jpg(make_strip(seed=1)[70:290, 80:560]))
    assert c.post("/investigate", json={}).json().get("error") is None
    assert wait_done(c)["result"]["conclusion"] == "Checked."
    assert c.get("/status").json()["text"].startswith("Line held by the Shift Supervisor — QA must resume.")
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "checked"}).json()["resumed"] is True
    assert plc.coils[2] is False and c.get("/status").json()["held"] is False
    old = TestClient(app_mod.create_app(p, None, chat=ScriptChat({"content": "Old model."})))
    old.app.state.station.strip_box = None  # taught before the strip box was saved: the full-frame box
    assert old.post("/investigate", json={}).json().get("error") is None
    assert wait_done(old)["result"]["conclusion"] == "Old model."


class TrayClient(FakeClient):
    def sam_video(self, mp4, prompt):
        return [SamTrackPoint(1, 0, (70, 60, 570, 300), 0.9), SamTrackPoint(2, 0, (300, 100, 400, 200), 0.3)]


def test_tray_clip_logs_and_signals_every_verdict(tmp_path):
    clip = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    for _ in range(10):
        vw.write(make_strip(seed=12))
    vw.release()
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, client=TrayClient(), plc=plc, source=str(clip))
    try:
        log = lambda: _lines(p / "log.jsonl")  # noqa: E731
        n = len(log())
        vs = c.post("/tray").json()["verdicts"]
        assert [v["strip_id"] for v in vs] == [1, 2] and vs[0]["status"] in ("PASS", "REJECT", "HOLD")
        assert vs[1]["status"] == "HOLD" and "not seen clearly" in vs[1]["reasons"][0]["text"]
        assert [(r["strip_id"], r["status"]) for r in log()[n:]] == [(v["strip_id"], v["status"]) for v in vs]
        assert all(r["at"] and r["batch"] == "B1" for r in log()[n:]) and sum(plc.regs[:3]) == 2
        assert c.get("/status").json()["last_verdict"]["strip_id"] == 2
        c.post("/hold", json={})
        assert "PASS" not in [v["status"] for v in c.post("/tray").json()["verdicts"]]
    finally:
        c.app.state.source.stop.set()


def test_tray_needs_a_camera_or_video(tmp_path):
    photos = tmp_path / "photos"
    photos.mkdir()
    cv2.imwrite(str(photos / "a.jpg"), make_strip())
    approve(CHECKLIST, "Priya", tmp_path / "p" / "checklists.jsonl")
    for source in (None, str(photos), str(photos / "a.jpg")):
        Users(tmp_path / "users.json").add("op", "operator", "pw-op")
        c = TestClient(app_mod.create_app(tmp_path / "p", source, client=TrayClient()))
        login(c, "op", "pw-op")
        assert c.post("/tray").json() == {"error": "tray clip needs a camera or video source"}
        if c.app.state.source:
            c.app.state.source.stop.set()


def test_sign_pins_the_verdict_files(tmp_path):
    c, p = bench(tmp_path)
    c.post("/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    rid = c.get("/record").headers["X-Record-Id"]
    assert c.post("/sign", json={"password": "pw-qa", "record_id": rid, "meaning": "Approved"}).json()["signed_by"] == "qa1"
    e = json.loads((p / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    sha = lambda n: hashlib.sha256((p / n).read_bytes()).hexdigest()  # noqa: E731
    assert e["action"] == "sign"
    assert e["details"]["files_sha256"] == {n: sha(n) for n in ("log.jsonl", "selftests.jsonl", "states.jsonl")}


def test_self_test_before_teaching_says_teach_first(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "p" / "checklists.jsonl")
    Users(tmp_path / "users.json").add("qa1", "qa", "pw-qa")
    c = TestClient(app_mod.create_app(tmp_path / "p", None))
    login(c, "qa1", "pw-qa")
    assert c.post("/selftest", files=up(make_strip(seed=12))).json() == {"error": "Teach the station first"}


def test_selftest_needs_qa_and_challenge_needs_login(tmp_path):
    c, p = bench(tmp_path)
    before = (p / "selftests.jsonl").read_text(encoding="utf-8")
    for user in (None, ("op", "pw-op")):
        if user:
            login(c, *user)
        assert c.post("/selftest", files=up(make_strip(seed=12))).json() == {"error": "Log in as a QA user first"}
    assert (p / "selftests.jsonl").read_text(encoding="utf-8") == before
    assert not any(e["action"] == "selftest" and e["user"] != "qa1" for e in _audit(p))
    c.post("/logout")
    for url in ("/challenge/inspect", "/challenge/eject"):  # the verified run: tests/test_challenge.py
        assert c.post(url).json() == {"error": "Log in first"}
    assert not any(e["action"] == "physical_challenge" for e in _audit(p))


class ClearClient(FakeClient):
    def sam_image(self, image, prompt):
        if prompt in ("blister pack", "pill", "capsule") and getattr(self, "cleared", False):
            return []  # nothing left on the table
        raise meta_api.MetaError("offline")  # pockets: the checklist grid


def test_inspect_without_batch_refused_and_not_logged(tmp_path):
    clip = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    for _ in range(3):
        vw.write(make_strip(seed=12))
    vw.release()
    c, p = bench(tmp_path, client=TrayClient(), batch=False, source=str(clip))
    for url in ("/inspect", "/tray"):
        r = c.post(url, files=up(make_strip(seed=15))) if url == "/inspect" else c.post(url)
        assert r.status_code == 409 and r.json() == {"error": "Start a batch first (QA)"}
    c.app.state.source.stop.set()
    assert not (p / "log.jsonl").exists() and c.get("/status").json()["batch"] is None


def test_inspect_logs_batch(tmp_path):
    c, p = bench(tmp_path, client=ClearClient(), batch=False)
    login(c, "qa1", "pw-qa")
    c.app.state.station.client.cleared = True
    assert c.post("/clearance", files=up(make_strip(seed=1))).json() == {"clear": True, "found": []}
    form = {"batch": "lot 7", "mfg": "01/2026", "exp": "01/2028", "mrp": "Rs. 45", "product": "Paracetamol", "strength": "500 mg"}
    assert c.post("/batch/start", data=form).json()["batch"] == "LOT7"
    assert c.post("/batch/start", data=form).json() == {"error": "A batch is already open: LOT7"}
    assert c.get("/status").json()["batch"]["batch"] == "LOT7"
    release_batch(p, "LOT7")
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "PASS"
    assert _lines(p / "log.jsonl")[-1]["batch"] == "LOT7"
    assert c.post("/leak", data={"result": "pass", "note": "ok"}).json()["result"] == "pass"
    assert c.post("/batch/end", data=dict(END_OK, received="1", good_tray="1")).json()["event"] == "end"
    assert c.get("/status").json()["batch"] is None
    acts = [json.loads(x)["action"] for x in (p / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert acts[-5:] == ["line_clearance", "batch_start", "specimen_signed", "leak_test", "batch_end"]


def test_batch_endpoints_need_qa(tmp_path):
    c, p = bench(tmp_path, client=ClearClient(), batch=False)
    login(c, "op", "pw-op")
    for url, data in (("/clearance", {}), ("/batch/start", {"batch": "B1", "mfg": "01/2026", "exp": "01/2028", "mrp": "45"}),
                      ("/leak", {"result": "pass"}), ("/batch/end", {})):
        assert c.post(url, data=data).json() == {"error": "Log in as a QA user first"}
    assert not (p / "batches.jsonl").exists() and not (p / "clearances.jsonl").exists()


class PrintClient(FakeClient):
    def __init__(self, read=None, boom=None):
        self.read, self.boom = read, boom

    def spark_json(self, system, user, schema, name="result", **kw):
        if self.boom:
            raise self.boom
        return self.read


def _print_bench(tmp_path, **kw):
    c, p = bench(tmp_path, client=PrintClient(**kw), release=False)
    login(c, "op", "pw-op")
    return c, p


def test_print_without_batch_refused(tmp_path):
    c, p = bench(tmp_path, client=PrintClient(), batch=False)
    login(c, "op", "pw-op")
    r = c.post("/print", files={"image": ("a.jpg", jpg(make_strip(seed=1)), "image/jpeg")})
    assert r.status_code == 409 and r.json()["error"] == "Start a batch first (QA)"
    assert not (p / "prints.jsonl").exists()


def test_print_logged(tmp_path):
    read = {"batch": "B1", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = _print_bench(tmp_path, read=read)
    r = c.post("/print", files={"image": ("a.jpg", jpg(make_strip(seed=1)), "image/jpeg")})
    assert r.json()["status"] == "PRINT_OK"
    row = _lines(p / "prints.jsonl")[0]
    assert row["batch"] == "B1" and row["by"] == "op" and row["status"] == "PRINT_OK" and row["read"] == read


def test_print_needs_login(tmp_path):
    c, p = bench(tmp_path, client=PrintClient(), release=False)
    r = c.post("/print", files={"image": ("a.jpg", jpg(make_strip(seed=1)), "image/jpeg")})
    assert "error" in r.json() and not (p / "prints.jsonl").exists()


def test_print_model_failure_is_plain_error_and_not_logged(tmp_path):
    c, p = _print_bench(tmp_path, boom=meta_api.MetaError("down"))
    r = c.post("/print", files={"image": ("a.jpg", jpg(make_strip(seed=1)), "image/jpeg")})
    assert r.status_code == 200 and "down" in r.json()["error"] and not (p / "prints.jsonl").exists()


# --- severity classes and the drift rule ---
def _fake_judge(c, *checks):
    """The station returns a REJECT carrying these checks (no model is asked)."""
    st = c.app.state.station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))  # every check is on, so every kind is a named rule
    st._judge = lambda image, sid, name: Verdict("REJECT", [st._reason(k, 0) for k in checks], strip_id=sid, image_name=name)


def _audit(p):
    return [json.loads(l) for l in (p / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_critical_reject_holds_line(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    _fake_judge(c, "wrong_tablet")
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "REJECT"
    assert c.get("/status").json()["held"] is True and plc.coils[2] is True
    e = _audit(p)[-1]
    assert e["action"] == "hold_line" and e["user"] == "station" and e["reason"].startswith("Critical: Rule 5 failed")


def test_three_rejects_in_a_row_hold_line(tmp_path):
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    _fake_judge(c, "empty_pocket")
    for _ in range(2):
        c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is False
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is True and plc.coils[2] is True
    e = _audit(p)[-1]
    assert (e["user"], e["reason"]) == ("station", "3 rejects in a row — check the machine")


def test_major_reject_does_not_hold_line(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "broken_tablet", "tablet_spot")
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is False and not any(e["action"] == "hold_line" for e in _audit(p))


def test_pass_resets_the_reject_run(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    for _ in range(2):
        c.post("/inspect", files=up(make_strip(seed=15)))
    del c.app.state.station._judge  # back to the real judge: a good strip passes
    assert c.post("/inspect", files=up(make_strip(seed=15, shift=(2, 1)))).json()["status"] == "PASS"
    _fake_judge(c, "empty_pocket")
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is False


WA7 = {"batch": "26F12061S", "mfg": "JUL.2026", "exp": "DEC.2027", "mrp": "₹700.00/10 Tablets (₹70.00/1N)", "readable": True}
WA7_DIFF = "Batch no. on strip is 26F12061S, batch record says 26F1206IS"


def _eye_bench(tmp_path, read=WA7):
    """The real wa7 foil: B.No 26F1206IS, read by Spark as 26F12061S (3 of 3 reads). QA is logged in."""
    c, p = bench(tmp_path, client=PrintClient(read=read), batch=False)
    b = Batches(p)
    b.clearance({"clear": True, "found": []}, "qa1", time.time())
    b.start("26F1206IS", "07/2026", "12/2027", "700", "qa1", time.time())
    login(c, "qa1", "pw-qa")
    return c, p


def _eye(c, result, password="pw-qa", note="B.No 26F1206IS on the strip, checked by eye"):
    return c.post("/print/eye", json={"result": result, "password": password, "note": note}).json()


def test_print_difference_holds_the_strip_for_a_check_by_eye_not_the_line(tmp_path):
    """Spark is a generative model: per draft EU Annex 22 it only flags. A read that differs from the batch record
    holds the print check for QA's check by eye; it no longer holds the line on the model's word."""
    c, p = _eye_bench(tmp_path)
    r = c.post("/print", files=up(make_strip(seed=1))).json()
    assert r == {"status": "HOLD", "reasons": [WA7_DIFF], "by_eye": True}
    s = c.get("/status").json()
    assert s["held"] is False and not _holds(p) and s["print_by_eye"] == [WA7_DIFF]
    assert "Start-up print check" in s["blockers"] and _lines(p / "prints.jsonl")[-1]["by_eye"] is True


def test_qa_print_matches_is_print_ok_audited_and_releases(tmp_path):
    c, p = _eye_bench(tmp_path)
    c.post("/print", files=up(make_strip(seed=1)))
    assert "error" in _eye(c, "matches", password="wrong")
    assert _eye(c, "matches", note=" ")["error"] == "Write a note: what you checked on the strip"
    row = _eye(c, "matches")
    assert row["status"] == "PRINT_OK" and row["by"] == "qa1" and row["checked_by_eye"] == "matches"
    assert row["read"] == WA7 and row["reasons"] == ["Print matches (checked by eye): B.No 26F1206IS on the strip, checked by eye"]
    s = c.get("/status").json()
    assert "Start-up print check" not in s["blockers"] and s["print_by_eye"] is None and s["held"] is False
    e = _audit(p)[-1]
    assert (e["action"], e["user"], e["details"]["result"], e["details"]["reasons"]) == ("print_checked_by_eye", "qa1", "matches", [WA7_DIFF])
    assert _eye(c, "matches")["error"] == "No print check waits for a check by eye"  # one decision per read
    assert c.post("/specimen/sign", json={"password": "pw-qa"}).json()["action"] == "specimen_signed"


def test_qa_wrong_print_is_critical_and_holds_the_line(tmp_path):
    c, p = _eye_bench(tmp_path)
    c.post("/print", files=up(make_strip(seed=1)))
    assert _eye(c, "wrong", note="")["status"] == "WRONG_PRINT"
    s = c.get("/status").json()
    assert s["held"] is True and s["print_by_eye"] is None and "Start-up print check" in s["blockers"]
    h = _holds(p)[-1]
    assert h["user"] == "qa1" and h["reason"] == "Critical: wrong print — " + WA7_DIFF


def test_check_by_eye_is_qa_only_and_needs_a_flagged_read(tmp_path):
    c, p = _eye_bench(tmp_path, read=dict(WA7, batch=""))  # unreadable field: check by hand, nothing to decide
    r = c.post("/print", files=up(make_strip(seed=1))).json()
    assert r["status"] == "HOLD" and r["by_eye"] is False and c.get("/status").json()["print_by_eye"] is None
    assert _eye(c, "matches")["error"] == "No print check waits for a check by eye"
    login(c, "op", "pw-op")
    assert "QA" in _eye(c, "matches", password="pw-op")["error"]
    assert not [e for e in _audit(p) if e["action"] == "print_checked_by_eye"]


def test_unreadable_print_does_not_hold_line(tmp_path):
    read = {"batch": "", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = _print_bench(tmp_path, read=read)
    assert c.post("/print", files=up(make_strip(seed=1))).json()["status"] == "HOLD"
    assert c.get("/status").json()["held"] is False


def test_tray_locked_makes_no_paid_call(tmp_path):
    clip = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    for _ in range(3):
        vw.write(make_strip(seed=12))
    vw.release()

    class Counting(TrayClient):
        calls = 0

        def sam_video(self, mp4, prompt):
            Counting.calls += 1
            return super().sam_video(mp4, prompt)

    approve(CHECKLIST, "Priya", tmp_path / "p" / "checklists.jsonl")
    open_batch(tmp_path / "p")
    release_batch(tmp_path / "p")
    Users(tmp_path / "users.json").add("op", "operator", "pw-op")
    c = TestClient(app_mod.create_app(tmp_path / "p", str(clip), client=Counting()))  # never taught: LOCKED
    login(c, "op", "pw-op")
    try:
        v = c.post("/tray").json()["verdicts"]
        assert [x["status"] for x in v] == ["LOCKED"] and Counting.calls == 0
        assert not (tmp_path / "p" / "tray.mp4").exists()
    finally:
        c.app.state.source.stop.set()


def _holds(p):
    return [e for e in _audit(p) if e["action"] == "hold_line"]


def test_no_second_hold_while_held(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "wrong_tablet")
    c.post("/inspect", files=up(make_strip(seed=15)))
    first = json.loads((p / "hold.json").read_text(encoding="utf-8"))
    for _ in range(4):  # more criticals and a drift run, all while held
        c.post("/inspect", files=up(make_strip(seed=15)))
    assert len(_holds(p)) == 1 and json.loads((p / "hold.json").read_text(encoding="utf-8")) == first


def test_drift_restarts_after_qa_resume(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    for _ in range(3):
        c.post("/inspect", files=up(make_strip(seed=15)))
    assert len(_holds(p)) == 1
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "checked"}).json()["resumed"] is True
    time.sleep(1.1)  # verdict and audit times have one-second resolution
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is False and len(_holds(p)) == 1
    for _ in range(2):
        c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is True and len(_holds(p)) == 2


def test_failing_hold_still_records_the_wrong_print(tmp_path, monkeypatch):
    c, p = _eye_bench(tmp_path)
    c.post("/print", files=up(make_strip(seed=1)))
    monkeypatch.setattr(app_mod, "hold_line", lambda *a: 1 / 0)
    assert "error" in _eye(c, "wrong")
    assert _lines(p / "prints.jsonl")[-1]["status"] == "WRONG_PRINT"


def test_wrong_print_while_held_adds_no_second_hold(tmp_path):
    c, p = _eye_bench(tmp_path)
    c.post("/hold", json={})
    first = (p / "hold.json").read_text(encoding="utf-8")
    n = len(_holds(p))
    assert c.post("/print", files=up(make_strip(seed=1))).json()["status"] == "HOLD"
    assert _eye(c, "wrong")["status"] == "WRONG_PRINT"
    assert len(_lines(p / "prints.jsonl")) == 2 and len(_holds(p)) == n
    assert (p / "hold.json").read_text(encoding="utf-8") == first


# --- Task 28: release gate ---
def _print_ok(p, no="B1"):
    Batches(p)._write("prints.jsonl", {"at": datetime.now().isoformat(timespec="seconds"), "time": time.time(), "batch": no, "read": {},
                                       "status": "PRINT_OK", "reasons": [], "by": "op"})


def test_inspect_refused_until_released(tmp_path):
    clip = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    for _ in range(3):
        vw.write(make_strip(seed=12))
    vw.release()
    c, p = bench(tmp_path, client=TrayClient(), release=False, source=str(clip))
    want = ["Self-test for this batch", "Start-up print check", "QA signature on the start-up specimen"]
    s = c.get("/status").json()
    assert s["blockers"] == want and s["state"] == "NOT RELEASED" and "Batch not released" in s["text"]
    for url in ("/inspect", "/tray"):
        r = c.post(url, files=up(make_strip(seed=15))) if url == "/inspect" else c.post(url)
        assert r.status_code == 409 and r.json() == {"error": "Batch not released: " + ", ".join(want)}
    assert not (p / "log.jsonl").exists()
    release_batch(p)
    assert c.get("/status").json()["blockers"] == []
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "PASS"
    c.app.state.source.stop.set()


def test_specimen_sign_needs_qa_password_and_print_ok(tmp_path):
    c, p = bench(tmp_path, release=False)
    assert c.post("/specimen/sign", json={"password": "pw-qa"}).json() == {"error": "Log in as a QA user first"}
    login(c, "qa1", "pw-qa")
    assert "PRINT_OK" in c.post("/specimen/sign", json={"password": "pw-qa"}).json()["error"]
    _print_ok(p)
    assert "error" in c.post("/specimen/sign", json={"password": "wrong"}).json()
    assert "QA signature on the start-up specimen" in c.get("/status").json()["blockers"]
    assert "error" not in c.post("/specimen/sign", json={"password": "pw-qa"}).json()
    e = _audit(p)[-1]
    assert e["action"] == "specimen_signed" and e["user"] == "qa1" and e["details"]["batch"] == "B1" and e["details"]["print_at"]
    assert "QA signature on the start-up specimen" not in c.get("/status").json()["blockers"]


def test_foil_change_makes_checks_due(tmp_path):
    c, p = bench(tmp_path, as_user=None)
    b = Batches(p)
    b.leak("pass", "qa1", "", time.time())
    _print_ok(p)
    assert c.get("/status").json()["due"] == []
    assert c.post("/foil-change").json() == {"error": "Log in first"}
    login(c, "op", "pw-op")
    assert c.post("/foil-change").json()["event"] == "foil_change"
    time.sleep(1.1)
    assert c.get("/status").json()["due"] == ["Print check due"]  # a foil change restarts the print clock only
    assert _audit(p)[-1]["action"] == "foil_change"


END_OK = {"received": "0", "good_tray": "0", "reject_box": "0", "samples": "0", "unused": "0", "foil_issued": "0",
          "foil_used": "0", "foil_returned": "0", "foil_destroyed": "0", "cleared": "true"}


def test_batch_end_stores_reconciliation(tmp_path):
    c, p = bench(tmp_path)
    c.post("/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    assert c.post("/leak", data={"result": "pass", "samples": "5"}).json()["samples"] == 5
    assert c.get("/status").json()["reconciliation"] == "open"
    end = c.post("/batch/end", data=dict(END_OK, received="1", good_tray="1")).json()
    t = end["totals"]
    assert (t["inspected"], t["passed"], t["status"], t["limits"]) == (1, 1, "within", {"strips": 0, "foil": 0.5})
    assert Batches(p).rows[-1]["totals"] == t
    assert c.get("/status").json()["reconciliation"] == "within"


def test_batch_end_outside_limit_needs_password_and_reason(tmp_path):
    c, p = bench(tmp_path)
    c.post("/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    bad = dict(END_OK, received="1", good_tray="0")  # the log says 1 pass, the tray holds none
    r = c.post("/batch/end", data=bad).json()
    assert "Good tray" in r["error"] and "investigation reason" in r["error"] and Batches(p).open
    assert "error" in c.post("/batch/end", data=dict(bad, password="wrong", reason="miscount")).json()
    assert "error" in c.post("/batch/end", data=dict(bad, password="pw-qa")).json() and Batches(p).open
    end = c.post("/batch/end", data=dict(bad, password="pw-qa", reason="strip found under the tray")).json()
    assert end["totals"]["status"] == "outside" and end["totals"]["investigated"] == {"by": "qa1", "reason": "strip found under the tray"}
    assert _audit(p)[-1]["action"] == "batch_end" and _audit(p)[-1]["details"]["totals"]["investigated"]["by"] == "qa1"
    assert c.get("/status").json()["reconciliation"] == "outside"


def test_batch_end_needs_the_clearance_tick_and_a_limit_applies(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    assert "clearance" in c.post("/batch/end", data=dict(END_OK, cleared="")).json()["error"]
    wide = dict(END_OK, foil_issued="0.4", strips_limit="0", foil_limit="0.5")  # 0.4 m unaccounted, limit 0.5
    assert c.post("/batch/end", data=wide).json()["totals"]["status"] == "within"


def test_batch_end_refuses_bad_numbers_and_blank_required_fields(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    for bad in ({"received": "nan"}, {"unused": "inf"}, {"samples": "-inf"}, {"foil_used": "nan"}, {"foil_issued": "inf"},
                {"strips_limit": "nan"}, {"foil_limit": "inf"}, {"foil_limit": "-inf"}, {"received": "2.5"}, {"unused": "0.5"}):
        r = c.post("/batch/end", data=dict(END_OK, **bad)).json()
        assert "error" in r and Batches(p).open, bad
    assert "numbers" in c.post("/batch/end", data=dict(END_OK, received="nan")).json()["error"]
    assert "whole" in c.post("/batch/end", data=dict(END_OK, received="2.5")).json()["error"]
    for k in ("received", "good_tray", "reject_box", "foil_issued"):
        r = c.post("/batch/end", data=dict(END_OK, **{k: ""})).json()
        assert "error" in r and "must be entered" in r["error"] and Batches(p).open, k
    assert c.post("/batch/end", data=dict(END_OK, unused="", foil_used="")).json()["event"] == "end"


def test_selftest_not_run_is_not_a_failed_selftest_row(tmp_path, monkeypatch):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    note = "Self-test could not run: the pocket finder did not answer"
    monkeypatch.setattr(c.app.state.station, "self_test", lambda im: {"passed": False, "not_run": note})
    c.post("/selftest", files=up(make_strip(seed=12)))
    last = _audit(p)[-1]
    assert last["action"] == "selftest_not_run" and last["details"] == {"not_run": note}
    assert not any(e["action"] == "selftest" and e["details"].get("passed") is False for e in _audit(p))

def test_failed_leak_test_stays_due_and_holds_the_line(tmp_path):
    c, p = bench(tmp_path)
    _print_ok(p)
    login(c, "qa1", "pw-qa")
    assert c.post("/leak", data={"result": "fail", "note": "bubbles"}).json()["result"] == "fail"
    s = c.get("/status").json()
    assert s["due"] == ["Leak test due"] and s["held"] is True
    assert [(e["user"], e["reason"]) for e in _holds(p)] == [("station", "Leak test failed — check the sealing")]
    c.post("/leak", data={"result": "fail"})  # already held: no second hold
    assert len(_holds(p)) == 1
    assert c.post("/leak", data={"result": "pass"}).json()["result"] == "pass"
    assert c.get("/status").json()["due"] == []


def test_leak_refuses_negative_samples(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    assert c.post("/leak", data={"result": "pass", "samples": "-2"}).json() == {"error": "Samples must be 0 or more"}
    assert not any(r["event"] == "leak" for r in Batches(p).rows)


def test_status_gives_the_screens_their_data(tmp_path):
    c, p = bench(tmp_path, plc=Plc(host="127.0.0.1", port=0))
    s = c.get("/status").json()
    assert s["taught"] is True and s["good_photos"] == 6 and s["checklist"]["approved_by"] == "Priya"
    assert s["selftest"]["at"] and s["machine_link"] is False and s["hold_reason"] == ""
    assert s["totals"] == {"inspected": 0, "passed": 0, "rejected": 0, "held": 0, "no_strip": 0, "status": "open"}
    c.post("/inspect", files=up(make_strip(seed=15)))
    c.post("/hold", json={"reason": "foil looks wrong"})
    s = c.get("/status").json()
    assert s["totals"]["inspected"] == 1 and s["hold_reason"] == "foil looks wrong"
    set_hold(p, Plc(host="127.0.0.1", port=0), True, "supervisor")  # the agent's hold: no stale operator reason
    assert c.get("/status").json()["hold_reason"] == ""
    assert c.get("/good/0").headers["content-type"] == "image/jpeg" and c.get("/good/6").status_code == 404
    assert c.get("/static/station.css").status_code == 200
    assert c.get("/static/fonts/IBMPlexSans-Regular.woff2").headers["content-type"] == "font/woff2"
    assert TestClient(app_mod.create_app(tmp_path / "q", None)).get("/status").json()["machine_link"] is None


def test_status_shows_the_pending_sentence(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None, client=FakeClient()))
    c.post("/describe", json={"text": "ten tablets"})
    s = c.get("/status").json()
    assert s["pending_sentence"] == "ten tablets" and s["taught"] is False and s["totals"] is None


def test_reject_saves_photo_and_logs_name(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "REJECT"
    row = _lines(p / "log.jsonl")[-1]
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9]+\.jpg", row["photo"])
    assert cv2.imread(str(p / "rejects" / row["photo"])) is not None


def test_reject_photo_draws_the_layout_the_strip_was_judged_on(tmp_path):
    """Rehearsal: the reject photo ran the offline finder again, which saw no strip on the close-up, so the photo had
    no outline and no red box. It now draws the verdict's own layout (no second search)."""
    c, p = bench(tmp_path)
    st = c.app.state.station

    def judged(image, sid, name):
        v = Verdict("REJECT", [st._reason("empty_pocket", 6)], strip_id=sid, image_name=name)
        v.layout = Layout((80, 70, 560, 290), [pocket_box(i) for i in range(10)], "sam", True)
        return v
    st._judge = judged
    flat = np.full((360, 640, 3), 128, np.uint8)  # the offline finder sees no strip in this frame
    assert c.post("/inspect", files=up(flat)).json()["status"] == "REJECT"
    photo = cv2.imread(str(p / "rejects" / _lines(p / "log.jsonl")[-1]["photo"])).astype(int)
    near = lambda px, bgr: (np.abs(px - bgr).max(axis=-1) < 50).mean()
    x0, y0, x1, y1 = pocket_box(6)
    assert near(photo[y0 + 8:y1 - 8, x0], app_mod.RED) > 0.9  # pocket 7: a red box
    assert near(photo[y0 + 8:y1 - 8, pocket_box(5)[0]], app_mod.GREEN) > 0.9  # the others green
    assert near(photo[80:280, 80], app_mod.BLUE) > 0.9  # the strip outline


def test_status_says_what_a_running_inspection_is_doing(tmp_path):
    """The screen polls /status during a long action and shows the station's step with the seconds."""
    c, p = bench(tmp_path)
    st = c.app.state.station
    started, go, judge = threading.Event(), threading.Event(), st._judge

    def slow(image, sid, name):
        started.set()
        go.wait(10)
        return judge(image, sid, name)
    st._judge = slow
    t = threading.Thread(target=lambda: c.post("/inspect", files=up(make_strip(seed=15))))
    t.start()
    try:
        assert started.wait(10) and c.get("/status").json()["step"] == "Finding the strip"  # taught offline: no SAM
    finally:
        go.set()
        t.join(30)
    assert c.get("/status").json()["step"] == ""


def test_only_a_camera_inspection_leaves_its_layout_for_the_supervisor(tmp_path):
    """An uploaded photo is not the camera's view: its layout must not be read against camera frames."""
    folder = tmp_path / "cam"
    folder.mkdir()
    frame = make_strip(seed=15)
    cv2.imwrite(str(folder / "a.png"), frame)
    c, p = bench(tmp_path, source=str(folder))
    st, src = c.app.state.station, c.app.state.source
    try:
        for _ in range(100):
            if src.get() is not None:
                break
            time.sleep(0.05)
        c.post("/inspect", files=up(frame))
        assert st.layout_for(src.get()) is None
        c.post("/inspect")  # the camera frame
        assert st.layout_for(src.get()) is not None and st.layout_for(src.get()).ok
    finally:
        src.stop.set()


def test_pass_saves_no_photo(tmp_path):
    c, p = bench(tmp_path)
    c.app.state.station._judge = lambda image, sid, name: Verdict("PASS", strip_id=sid, image_name=name)
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert "photo" not in _lines(p / "log.jsonl")[-1] and not (p / "rejects").exists()


def test_held_line_pass_saves_no_photo(tmp_path):
    c, p = bench(tmp_path)
    c.app.state.station._judge = lambda image, sid, name: Verdict("PASS", strip_id=sid, image_name=name)
    c.post("/hold", json={})
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "HOLD"
    row = _lines(p / "log.jsonl")[-1]
    assert row["reasons"][0]["check"] == "line_held" and "photo" not in row and not (p / "rejects").exists()


def _tray_bench(tmp_path, monkeypatch, kinds):
    """/tray returns one logged verdict per kind (None = PASS); no SAM video call, no recording."""
    clip = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (640, 360))
    vw.write(make_strip(seed=12))
    vw.release()
    c, p = bench(tmp_path, source=str(clip))
    _fake_judge(c, "empty_pocket")

    def clip_verdicts(path, station, client, held=False, batch=None, user=None):
        return [station.log(Verdict("REJECT", [station._reason(k, 0)], strip_id=i) if k else Verdict("PASS", strip_id=i),
                            held, batch, user=user) for i, k in enumerate(kinds, 1)]
    monkeypatch.setattr(app_mod, "record_clip", lambda *a: clip)
    monkeypatch.setattr(app_mod, "inspect_clip", clip_verdicts)
    return c, p


@pytest.mark.parametrize("kinds, reason", [
    (["empty_pocket"] * 3 + [None], "3 rejects in a row — check the machine"),  # a pass after the run must not hide it
    ([None, "wrong_tablet", None], "Critical: Rule 5 failed: pocket 1 has a different tablet")])
def test_tray_clip_holds_on_drift_or_critical(tmp_path, monkeypatch, kinds, reason):
    c, p = _tray_bench(tmp_path, monkeypatch, kinds)
    try:
        assert [v["status"] for v in c.post("/tray").json()["verdicts"]] == ["REJECT" if k else "PASS" for k in kinds]
        assert c.get("/status").json()["held"] is True
        assert [(e["user"], e["reason"]) for e in _holds(p)] == [("station", reason)]
    finally:
        c.app.state.source.stop.set()


def test_corrupt_log_line_never_errors_inspect(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    with (p / "log.jsonl").open("a", encoding="utf-8") as f:
        f.write("not json\n")
    for _ in range(3):
        assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "REJECT"
    assert c.get("/status").json()["held"] is True


def test_rejects_route_refuses_bad_names(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    c.post("/inspect", files=up(make_strip(seed=15)))
    name = _lines(p / "log.jsonl")[-1]["photo"]
    c.post("/logout")
    assert c.get("/rejects/" + name).status_code == 401  # nobody logged in
    login(c, "op", "pw-op")
    assert c.get("/rejects/" + name).status_code == 200
    for bad in ("../x.jpg", "a.jpg", "..%2Fx.jpg", "20260101-000000-1.png"):
        assert c.get("/rejects/" + bad).status_code == 404
    assert c.get("/rejects/20260101-000000-9.jpg").status_code == 404  # right shape, no such file


def test_inspection_error_hold_gets_a_photo(tmp_path, monkeypatch):
    c, p = bench(tmp_path)
    monkeypatch.setattr(c.app.state.station, "_judge", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "HOLD"
    row = _lines(p / "log.jsonl")[-1]
    assert (p / "rejects" / row["photo"]).is_file()


def test_failed_imwrite_leaves_no_photo_name(tmp_path, monkeypatch):
    c, p = bench(tmp_path)
    _fake_judge(c, "empty_pocket")
    monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: False)
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "REJECT"
    assert "photo" not in _lines(p / "log.jsonl")[-1]


def test_batch_start_needs_product_and_strength_and_audits_them(tmp_path):
    c, p = bench(tmp_path, client=ClearClient(), batch=False)
    login(c, "qa1", "pw-qa")
    c.app.state.station.client.cleared = True
    c.post("/clearance", files=up(make_strip(seed=1)))
    base = {"batch": "L1", "mfg": "01/2026", "exp": "01/2028", "mrp": "45"}
    assert "Product name" in c.post("/batch/start", data=base).json()["error"]
    assert "Strength" in c.post("/batch/start", data=dict(base, product="Para", strength="x")).json()["error"]
    row = c.post("/batch/start", data=dict(base, product="Para", strength="500 mg")).json()
    assert (row["product"], row["strength"]) == ("Para", "500 mg")
    assert '<input id="bst" maxlength="40" placeholder="Strength (blank if not printed)"' in c.get("/").text
    last = [json.loads(x) for x in (p / "audit.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    assert last["action"] == "batch_start" and last["details"]["strength"] == "500 mg"


class SaysClient(FakeClient):
    def __init__(self, text):
        self.text = text

    def transcribe(self, wav, keywords=(), languages=None, mode="PUSH_TO_TALK"):
        return self.text


def test_inspect_tray_and_voice_inspect_need_login_and_verdict_carries_user(tmp_path):
    c, p = bench(tmp_path, client=SaysClient("inspect the strip"), as_user=None)
    assert c.post("/inspect", files=up(make_strip(seed=15))).json() == {"error": "Log in first"}
    assert c.post("/tray").json() == {"error": "Log in first"}
    assert c.post("/command", files={"audio": ("a.wav", b"x")}).json() == {"error": "Log in first"}
    assert not (p / "log.jsonl").exists()
    login(c, "op", "pw-op")
    assert c.post("/command", files={"audio": ("a.wav", b"x")}).json()["command"] == "inspect"
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "PASS"
    assert _lines(p / "log.jsonl")[-1]["user"] == "op"
    login(c, "qa1", "pw-qa")
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert _lines(p / "log.jsonl")[-1]["user"] == "qa1"


def test_voice_hold_command_works_without_login(tmp_path):
    c, _ = bench(tmp_path, client=SaysClient("stop the line"), as_user=None)
    assert c.post("/command", files={"audio": ("a.wav", b"x")}).json()["command"] == "hold"


def test_sign_needs_a_meaning_and_reopened_record_shows_signed(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    rid = c.get("/record").headers["X-Record-Id"]
    assert "Reviewed or Approved" in c.post("/sign", json={"password": "pw-qa", "record_id": rid}).json()["error"]
    assert "DRAFT — needs QA signature" in c.get("/record").text
    out = c.post("/sign", json={"password": "pw-qa", "record_id": rid, "meaning": "Reviewed"}).json()
    assert out["signed_by"] == "qa1" and out["meaning"] == "Reviewed"
    html = c.get("/record").text
    assert "DRAFT" not in html and "Signed by qa1 — Reviewed — " in html and "Signature manifest" in html


def test_record_config_block_has_app_version_and_camera_source(tmp_path):
    c, p = bench(tmp_path, source=None)
    cfg = c.get("/record").text.split("<h2>Configuration</h2>")[1].split("<h2>")[0]
    for s in ("App version", "Camera source", "Pocket threshold", "Border threshold"):
        assert s in cfg


# --- Task 35 (b): no signature while anything waits for a person; QA review notes for missed hourly checks ---
def _sign(c):
    r = c.get("/record")
    assert "X-Record-Id" in r.headers, r.text[:300]
    return c.post("/sign", json={"record_id": r.headers["X-Record-Id"], "password": "pw-qa", "meaning": "Approved"}).json()


def _backdate_batch(p, seconds):
    """The open batch started `seconds` earlier, so its hourly windows went by with no check."""
    rows = [json.loads(x) for x in (p / "batches.jsonl").read_text(encoding="utf-8").splitlines()]
    for r in rows:
        if r["event"] == "start":
            r["time"] -= seconds
            r["at"] = datetime.fromtimestamp(r["time"]).isoformat(timespec="seconds")
    (p / "batches.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_sign_refused_while_anything_waits_for_a_person(tmp_path):
    chat = ScriptChat(call("request_resume", {"reason": "lamp moved back"}), {"content": "Lamp moved back."})
    c, p = bench(tmp_path, chat)
    login(c, "qa1", "pw-qa")

    def refused(text):
        out = _sign(c)
        assert out["error"].startswith("Sign refused — still waiting: ") and text in out["error"], out

    c.post("/investigate", json={"trigger": "glare"})
    req = wait_done(c)["pending"][0]  # the agent holds the line and asks QA to resume it
    refused("Shift Supervisor asks QA to approve: resume — lamp moved back")
    refused("Line held by the Shift Supervisor")
    assert c.post("/approve-request", json={"id": req["id"], "password": "pw-qa"}).json()["approved"] is True
    c.post("/hold", json={"reason": "foil jam"})
    refused("Line held by qa1 — foil jam")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "jam cleared"}).json()["resumed"] is True
    rc = {"window_id": "w1", "open": True, "since": time.time(), "from_strip": 1, "to_strip": 2, "total": 2, "done": 0}
    (p / "recheck.json").write_text(json.dumps(rc), encoding="utf-8")
    refused("Re-check 0 of 2")
    (p / "recheck.json").write_text(json.dumps(dict(rc, open=False)), encoding="utf-8")
    b, now = Batches(p), time.time()
    b._write("challenge_runs.jsonl", {"time": now, "at": "x", "by": "op", "batch": "B1", "passed": False, "results": []})
    refused("Physical challenge failed")
    b._write("challenge_runs.jsonl", {"time": now + 1, "at": "x", "by": "op", "batch": "B1", "passed": True, "results": []})
    _backdate_batch(p, 4 * 3600 + 100)  # 4 hours with no checks: 8 missed windows
    out = _sign(c)["error"]  # at most 5 items listed, then how many more
    assert out.count("Leak test missed") + out.count("Print check missed") == 5 and out.endswith("; and 3 more")
    for m in c.get("/missed").json()["missed"]:
        assert c.post("/review-missed", json={"window": m["start"], "kind": m["kind"], "password": "pw-qa",
                                              "note": "done on paper"}).json()["window"] == m["window"]
    time.sleep(1.1)  # verdict times are whole seconds: the strip lands after the last passed self-test
    t = float(int(time.time()))  # a strip, then a failed self-test: quarantine opens (not after the QA release below)
    b._write("log.jsonl", {"status": "PASS", "reasons": [], "pocket_scores": [], "skipped": [], "strip_id": None,
                           "image_name": None, "at": datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                           "checklist_version": 1, "batch": "B1", "user": "op"})
    b._write("selftests.jsonl", dict(b._read("selftests.jsonl")[-1], passed=False, failed=["x"], time=t,
                                     at=datetime.fromtimestamp(t).isoformat(timespec="seconds")))
    refused("Quarantine: 1 strips")
    assert c.post("/release", json={"password": "pw-qa", "reason": "strips re-checked"}).json()["action"] == "release_quarantine"
    assert not any(e["action"] == "sign" for e in _audit(p))  # nothing was signed while anything waited
    out = _sign(c)
    assert out.get("signed_by") == "qa1", out  # clear: the open batch's record signs


def test_review_note_closes_a_missed_check(tmp_path):
    from d2i.openitems import window_id
    c, p = bench(tmp_path)
    _backdate_batch(p, 3700)  # one whole hour went by: the leak test and the print check were missed
    login(c, "qa1", "pw-qa")
    missed = c.get("/missed").json()["missed"]
    assert [m["kind"] for m in missed] == ["leak", "print"] and missed[0]["window"] == window_id("leak", missed[0]["start"])
    m, note = missed[0], "tested 10:05, entered late"
    body = {"window": m["start"], "kind": m["kind"], "note": note}
    assert c.post("/review-missed", json=dict(body, password="bad")).json()["error"] == "QA login required"
    assert "note" in c.post("/review-missed", json=dict(body, password="pw-qa", note=" ")).json()["error"]
    assert "error" in c.post("/review-missed", json=dict(body, password="pw-qa", window=m["start"] + 600)).json()
    login(c, "op", "pw-op")
    assert c.post("/review-missed", json=dict(body, password="pw-op")).json() == {"error": "Log in as a QA user first"}
    assert not (p / "reviews.jsonl").exists()
    login(c, "qa1", "pw-qa")
    row = c.post("/review-missed", json=dict(body, password="pw-qa")).json()
    assert row == {"time": row["time"], "at": row["at"], "kind": "missed_check", "window": m["window"], "by": "qa1",
                   "note": note, "batch": "B1"}
    assert [json.loads(x) for x in (p / "reviews.jsonl").read_text(encoding="utf-8").splitlines()] == [row]
    e = _audit(p)[-1]
    assert (e["action"], e["user"], e["reason"], e["details"]["window"]) == ("review_missed", "qa1", note, m["window"])
    assert [x["kind"] for x in c.get("/missed").json()["missed"]] == ["print"]  # the leak window is closed
    waiting = [i["text"] for i in c.get("/line").json()["waiting"] if i["kind"] == "missed_check"]
    assert len(waiting) == 1 and waiting[0].startswith("Print check missed")
    assert "error" in c.post("/review-missed", json=dict(body, password="pw-qa")).json()  # one note per window
    p2 = missed[1]  # the GET's string id is taken as well as the start number; line separators never split a row
    row = c.post("/review-missed", json={"window": p2["window"], "kind": "print", "password": "pw-qa",
                                         "note": " checked on paper by\u0085qa\r\nlater\x0b "}).json()
    assert row["window"] == p2["window"] and row["note"] == "checked on paper by qa later"
    assert len((p / "reviews.jsonl").read_text(encoding="utf-8").splitlines()) == 2 and c.get("/missed").json()["missed"] == []


# --- Task 35 (b,d) fix round 1 ---
def _sign_batch(c, batch, password="pw-qa"):
    r = c.get("/record", params={"batch": batch})
    assert "X-Record-Id" in r.headers, r.text[:300]
    return c.post("/sign", json={"record_id": r.headers["X-Record-Id"], "password": password, "meaning": "Approved"}).json()


def test_a_hold_between_the_gate_and_the_signature_cannot_get_in(tmp_path, monkeypatch):
    """Review: the gate read "nothing waits" and the signature was written later, outside the station lock that every
    hold is written under, so a hold could land in between and the record was signed on a held line."""
    real, armed, threads = app_mod.open_items, threading.Event(), []

    def slow(*a, **k):
        out = real(*a, **k)
        if armed.is_set():  # the gate has just read the open items
            armed.clear()
            threads.append(threading.Thread(target=lambda: TestClient(c.app).post("/hold", json={"reason": "foil jam"})))
            threads[0].start()
            time.sleep(0.8)  # long enough for the hold to land if nothing stops it
        return out

    monkeypatch.setattr(app_mod, "open_items", slow)
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    rid = c.get("/record").headers["X-Record-Id"]
    armed.set()
    out = c.post("/sign", json={"record_id": rid, "password": "pw-qa", "meaning": "Approved"}).json()
    threads[0].join()
    acts = [e["action"] for e in _audit(p)]
    assert out.get("signed_by") == "qa1" and acts.index("sign") < acts.index("hold_line")  # the hold waited


def test_an_older_batch_signs_only_after_its_own_missed_checks_have_notes(tmp_path):
    """Review probe: B1 missed its hourly checks, then B2 started. B1's misses stopped counting anywhere, so B1 signed
    with no notes, and no note could be written for them. Now B1's record waits for B1's notes, and QA can write them."""
    c, p = bench(tmp_path)
    _backdate_batch(p, 3700)  # B1: one whole hour with no leak test and no print check
    now = time.time() - 5  # B1 ended a moment ago; B2's line clearance comes after it
    Batches(p)._write("batches.jsonl", {"event": "end", "batch": "B1", "by": "qa1", "time": now,
                                        "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                                        "totals": {"status": "within", "lines": []}})
    open_batch(p, "B2")
    release_batch(p, "B2")
    login(c, "qa1", "pw-qa")
    assert c.get("/missed").json()["missed"] == []  # the last started batch (B2) has missed nothing yet
    b1 = c.get("/missed", params={"batch": "B1"}).json()["missed"]
    assert [(m["kind"], m["batch"]) for m in b1] == [("leak", "B1"), ("print", "B1")]
    assert _sign_batch(c, "B1", password="wrong")["error"] == "QA login required"  # the password first, then the list
    out = _sign_batch(c, "B1")["error"]
    assert out.startswith("Sign refused — still waiting: Batch B1: Leak test missed") and "Batch B1: Print check missed" in out
    m = b1[0]
    assert "error" in c.post("/review-missed", json={"window": m["window"], "kind": m["kind"], "password": "pw-qa",
                                                     "note": "x"}).json()  # B1's window is not in B2
    for m in b1:
        row = c.post("/review-missed", json={"batch": "B1", "window": m["window"], "kind": m["kind"], "password": "pw-qa",
                                             "note": "tested on paper"}).json()
        assert (row["window"], row["batch"], row["by"]) == (m["window"], "B1", "qa1")
    assert c.get("/missed", params={"batch": "B1"}).json()["missed"] == []
    assert _sign_batch(c, "B1")["signed_by"] == "qa1"
    checks = c.get("/record", params={"batch": "B1"}).text.split("<h2>In-process checks</h2>")[1].split("<h2>")[0]
    assert "tested on paper" in checks  # in B1's own section, not only in the audit trail


def test_sign_refused_while_the_reconciliation_is_outside_without_a_reason(tmp_path):
    c, p = bench(tmp_path)
    now = time.time()
    end = {"event": "end", "batch": "B1", "by": "qa1", "time": now, "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
           "totals": {"status": "outside", "lines": []}}
    Batches(p)._write("batches.jsonl", end)
    login(c, "qa1", "pw-qa")
    assert _sign(c)["error"] == "Sign refused — still waiting: Reconciliation outside limit — batch B1, no investigation reason"
    rows = (p / "batches.jsonl").read_text(encoding="utf-8").splitlines()[:-1]  # the same end row, with QA's reason
    end["totals"]["investigated"] = {"by": "qa1", "reason": "recounted the reject box"}
    (p / "batches.jsonl").write_text("".join(x + "\n" for x in rows + [json.dumps(end)]), encoding="utf-8")
    assert _sign(c)["signed_by"] == "qa1"


# --- Final fix 2 ---
@pytest.mark.parametrize("ch", [" ", " ", "\x85"])
def test_line_separators_in_typed_text_never_split_a_record_row(tmp_path, ch):
    """Review: json.dumps(ensure_ascii=False) wrote U+2028 / U+2029 / U+0085 raw and every reader splits on
    str.splitlines(), so one pasted character broke the audit chain, batches.jsonl or checklists.jsonl for good."""
    c, p = bench(tmp_path, client=FakeClient())
    c.post("/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    assert c.post("/hold", json={"reason": f"foil{ch}jam"}).json() == {"held": True}
    assert c.post("/resume", json={"password": "pw-qa", "reason": f"jam{ch}cleared"}).json()["resumed"] is True
    assert c.post("/leak", data={"result": "pass", "note": f"no{ch}bubbles"}).json()["note"] == f"no{ch}bubbles"
    end = c.post("/batch/end", data=dict(END_OK, received="1", good_tray="0", password="pw-qa",
                                          reason=f"strip under{ch}the tray")).json()
    assert end["totals"]["investigated"]["reason"] == f"strip under{ch}the tray" and Batches(p).open is None
    assert c.post("/describe", json={"text": f"ten tablets{ch}two rows of five"}).json()["checklist"]
    assert c.post("/approve").json()["checklist"]["sentence"] == f"ten tablets{ch}two rows of five"
    a = AuditLog(p / "audit.jsonl")
    assert a.verify() == (True, None)
    assert [e["reason"] for e in a.entries() if e["action"] in ("hold_line", "resume_line")][-2:] == [
        f"foil{ch}jam", f"jam{ch}cleared"]
    assert Batches(p).rows[-1]["totals"]["investigated"]["reason"] == f"strip under{ch}the tray"
    for f in p.glob("*.jsonl"):
        assert ch not in f.read_text(encoding="utf-8"), f.name
    again = TestClient(app_mod.create_app(p, None))  # Station.__init__ reads checklists.jsonl
    assert again.get("/status").json()["checklist"]["sentence"] == f"ten tablets{ch}two rows of five"


def test_resume_writes_its_audit_row_before_the_hold_is_lifted(tmp_path):
    """Review: resume_line cleared hold.json first, so a damaged audit log left the line running with no resume row."""
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    c.post("/hold", json={"reason": "foil jam"})
    with (p / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert "damaged" in c.post("/resume", json={"password": "pw-qa", "reason": "jam cleared"}).json()["error"]
    assert c.get("/status").json()["held"] is True


def test_an_older_batch_record_shows_that_batch_and_refuses_unknown_numbers(tmp_path):
    """Review probe: /record?batch=B1 while B2 ran showed B2's Batch, Release and Reconciliation sections under a B1
    header; a lowercase "b1" or an unknown number skipped the older batch's own sign gate."""
    c, p = bench(tmp_path)
    _backdate_batch(p, 3700)  # B1: one whole hour with no leak test and no print check
    now = time.time() - 5
    Batches(p)._write("batches.jsonl", {"event": "end", "batch": "B1", "by": "qa1", "time": now,
                                        "at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                                        "totals": {"status": "within", "lines": []}})
    open_batch(p, "B2")  # open, not released
    login(c, "qa1", "pw-qa")
    r = c.get("/record", params={"batch": " b1 "})
    batch = r.text.split("<h2>Batch</h2>")[1].split("<h2>")[0]
    release = r.text.split("<h2>Release</h2>")[1].split("<h2>")[0]
    assert "<dd>B1</dd>" in batch and "B2" not in batch and "Not done" not in release
    assert "Not done" in c.get("/record").text.split("<h2>Release</h2>")[1].split("<h2>")[0]  # B2's own
    out = _sign_batch(c, "b1")["error"]
    assert out.startswith("Sign refused — still waiting: Batch B1: Leak test missed")
    r = c.get("/record", params={"batch": "NOSUCH"})
    assert "X-Record-Id" not in r.headers and "No batch NOSUCH was started" in r.text
    assert not any(e["action"] == "sign" for e in _audit(p))


def test_qa_declines_a_supervisor_request(tmp_path):
    """Review: QA could only approve a Shift Supervisor request; one QA did not want blocked signing until a restart."""
    chat = ScriptChat(call("request_resume", {"reason": "lamp moved back"}), {"content": "Lamp moved back."})
    c, p = bench(tmp_path, chat)
    c.post("/investigate", json={"trigger": "glare"})
    req = wait_done(c)["pending"][0]
    body = {"id": req["id"], "password": "pw-qa", "reason": "lamp not checked yet"}
    assert c.post("/decline-request", json=body).json() == {"error": "Log in as a QA user first"}  # operator
    login(c, "qa1", "pw-qa")
    assert c.post("/decline-request", json=dict(body, password="wrong")).json() == {"error": "QA login required"}
    assert "reason" in c.post("/decline-request", json=dict(body, reason=" ")).json()["error"]
    assert c.get("/investigation").json()["pending"] == [req]
    out = c.post("/decline-request", json=body).json()
    assert (out["id"], out["kind"], out["declined_by"]) == (req["id"], "resume", "qa1")
    e = _audit(p)[-1]
    assert (e["action"], e["user"], e["reason"], e["details"]["request"]) == (
        "agent_decline", "qa1", "lamp not checked yet", "Agent request: lamp moved back")
    assert c.get("/investigation").json()["pending"] == [] and "error" in c.post("/decline-request", json=body).json()
    assert not any(i["kind"] == "request" for i in c.get("/line").json()["waiting"])
    assert c.get("/status").json()["held"] is True  # declining a resume request does not resume the line
    assert c.post("/resume", json={"password": "pw-qa", "reason": "lamp checked"}).json()["resumed"] is True
    assert _sign(c).get("signed_by") == "qa1"


def test_supervisor_requests_survive_a_teach_and_a_restart(tmp_path):
    """Review: requests lived in memory, so a teach or a restart dropped them from the sign gate with no QA decision."""
    chat = ScriptChat(call("request_reteach", {"reason": "lamp moved"}), {"content": "Lamp moved."})
    c, p = bench(tmp_path, chat)
    c.post("/investigate", json={"trigger": "rejects rose"})
    req = wait_done(c)["pending"][0]
    login(c, "qa1", "pw-qa")
    assert c.post("/teach", json={}).json().get("error") is None
    assert c.get("/investigation").json()["pending"] == [req]
    again = TestClient(app_mod.create_app(p, None, chat=ScriptChat()))
    login(again, "qa1", "pw-qa")
    assert again.get("/investigation").json()["pending"] == [req]
    assert any(i["kind"] == "request" for i in again.get("/line").json()["waiting"])
    assert "Shift Supervisor asks QA to approve: reteach" in _sign(again)["error"]
    assert again.post("/approve-request", json={"id": req["id"], "password": "pw-qa"}).json()["approved"] is True
    assert again.get("/investigation").json()["pending"] == []


def test_drift_restarts_after_an_approved_agent_resume(tmp_path):
    """Review: only "Resume line" restarted the 3-rejects count, so after an approved agent resume the very next reject
    held the line again and the board showed "4 of 3"."""
    chat = ScriptChat(call("request_resume", {"reason": "machine checked"}), {"content": "Machine checked."})
    c, p = bench(tmp_path, chat)
    _fake_judge(c, "empty_pocket")
    for _ in range(3):
        c.post("/inspect", files=up(make_strip(seed=15)))
    assert len(_holds(p)) == 1
    c.post("/investigate", json={"trigger": "3 rejects in a row"})
    req = wait_done(c)["pending"][0]
    login(c, "qa1", "pw-qa")
    assert c.post("/approve-request", json={"id": req["id"], "password": "pw-qa"}).json()["approved"] is True
    time.sleep(1.1)  # verdict and audit times have one-second resolution
    c.post("/inspect", files=up(make_strip(seed=15)))
    assert c.get("/status").json()["held"] is False and len(_holds(p)) == 1
    assert c.get("/line").json()["reject_run"] == {"n": 1, "of": 3}


def test_end_batch_and_an_inspection_never_overlap(tmp_path, monkeypatch):
    """Review: End batch did not take the station lock and /inspect read the open batch before taking it, so a strip
    inspected during End batch landed in the ended batch, after its totals were written."""
    c, p = bench(tmp_path)
    qa_c = TestClient(c.app)
    login(qa_c, "qa1", "pw-qa")
    st, entered, gate, out = c.app.state.station, threading.Event(), threading.Event(), {}
    real = st._judge

    def slow(*a, **k):  # an inspection in flight
        entered.set()
        assert gate.wait(10)
        return real(*a, **k)

    st._judge = slow
    t = threading.Thread(target=lambda: out.update(v=c.post("/inspect", files=up(make_strip(seed=15))).json()))
    t.start()
    assert entered.wait(10)
    e = threading.Thread(target=lambda: out.update(end=qa_c.post("/batch/end", data=dict(END_OK, received="1", good_tray="1")).json()))
    e.start()
    time.sleep(0.5)
    gate.set()
    t.join()
    e.join()
    assert out["v"]["status"] == "PASS" and out["end"]["totals"]["inspected"] == 1, out  # End batch waited for it
    open_batch(p, "B2")
    release_batch(p, "B2")
    real_reconcile, armed = app_mod.reconcile, threading.Event()

    def slow_reconcile(*a, **k):  # an End batch in flight
        if len(a) > 2 and armed.is_set():
            armed.clear()
            entered.set()
            assert gate.wait(10)
        return real_reconcile(*a, **k)

    monkeypatch.setattr(app_mod, "reconcile", slow_reconcile)
    st._judge, entered, gate = real, threading.Event(), threading.Event()
    armed.set()
    e = threading.Thread(target=lambda: out.update(end=qa_c.post("/batch/end", data=END_OK).json()))
    e.start()
    assert entered.wait(10)
    t = threading.Thread(target=lambda: out.update(r=c.post("/inspect", files=up(make_strip(seed=15)))))
    t.start()
    time.sleep(0.5)
    gate.set()
    e.join()
    t.join()
    assert out["end"]["event"] == "end" and out["r"].status_code == 409 and out["r"].json() == {"error": "Start a batch first (QA)"}
    assert not [r for r in _lines(p / "log.jsonl") if r.get("batch") == "B2"]


def test_signing_an_older_batch_leaves_the_last_batch_unsigned(tmp_path):
    """Review: any signature after the last batch's end cleared its "Record unsigned", and the board footer named the
    last signer of any batch: signing B1's record turned B2's board to "signed"."""
    c, p = bench(tmp_path)
    t = time.time() - 20
    Batches(p)._write("batches.jsonl", {"event": "end", "batch": "B1", "by": "qa1", "time": t,
                                        "at": datetime.fromtimestamp(t).isoformat(timespec="seconds"),
                                        "totals": {"status": "within", "lines": []}})
    open_batch(p, "B2")
    login(c, "qa1", "pw-qa")
    assert c.post("/batch/end", data=END_OK).json()["event"] == "end"
    assert _sign_batch(c, "B1")["signed_by"] == "qa1"
    j = c.get("/line").json()
    assert [i["text"] for i in j["waiting"]][0].startswith("Record unsigned — batch B2")
    assert (j["state"], j["footer"]["record"], j["footer"]["signed_by"]) == ("COMPLETE", "draft", None)
    assert _sign_batch(c, "B2")["signed_by"] == "qa1"
    j = c.get("/line").json()
    assert (j["state"], j["footer"]["record"], j["footer"]["signed_by"]) == ("IDLE", "signed", "qa1")


def test_a_rechecked_strip_moves_no_plc_counter(tmp_path):
    """Review: re-checked strips went through publish, so the machine's PASS / REJECT counters counted them twice."""
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "PASS"
    _fake_judge(c, "wrong_tablet")
    c.post("/inspect", files=up(make_strip(seed=15)))  # critical: held, the passed strip goes into a re-check
    assert c.get("/recheck").json()["open"] is True
    before = list(plc.regs)
    assert c.post("/inspect", files=up(make_strip(seed=15))).json()["status"] == "REJECT"  # the re-checked strip
    assert _lines(p / "log.jsonl")[-1]["recheck"] is True
    assert plc.regs[:3] == before[:3] and plc.coils[0] is True  # no counter moved; the reject output still pulses


def test_batch_end_refuses_numbers_python_reads_but_people_do_not_type(tmp_path):
    """Review: float() reads "1_0" as 10 (and "1e1" as 10); typed counts must be plain numbers."""
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    for bad in ({"received": "1_0"}, {"good_tray": "1e1"}, {"foil_issued": "0_5"}, {"strips_limit": "1_0"}, {"samples": " 1 0 "}):
        assert c.post("/batch/end", data=dict(END_OK, **bad)).json() == {"error": "Counts and limits must be numbers"}, bad
    assert c.post("/batch/end", data=dict(END_OK, received="-1")).json() == {"error": "Counts and limits cannot be negative"}
    assert Batches(p).open and c.post("/batch/end", data=dict(END_OK, foil_issued=".4", foil_used="0.4")).json()["event"] == "end"


def test_held_and_locked_shows_locked_on_the_lamp_as_stopped_on_the_board(tmp_path):
    """Review: held and locked at once, the lamp showed amber HOLD while the board showed red STOPPED (ruling 22)."""
    c, p = bench(tmp_path)
    c.post("/hold", json={"reason": "foil jam"})
    assert c.get("/status").json()["state"] == "HOLD"
    b = Batches(p)
    b._write("selftests.jsonl", dict(b._read("selftests.jsonl")[-1], passed=False, failed=["x"], time=time.time()))
    s = c.get("/status").json()
    assert (s["state"], s["held"], s["locked"]) == ("LOCKED", True, True)
    assert s["text"].startswith("Line held by op — QA must resume.") and "Self-test missed: x" in s["text"]
    assert c.get("/line").json()["state"] == "STOPPED"


def test_hold_line_reaches_the_machine_while_a_long_action_runs(tmp_path):
    """Review: "Hold line" waited for the station lock, so during a tray clip, a teach or a self-test the hold reached
    the PLC only when that action ended. The coil is set at once; hold.json and the audit row follow."""
    plc = Plc(host="127.0.0.1", port=0)
    c, p = bench(tmp_path, plc=plc)
    lock, out = c.app.state.station_lock, {}
    lock.acquire()  # a long station action runs
    try:
        t = threading.Thread(target=lambda: out.update(r=c.post("/hold", json={"reason": "foil jam"}).json()))
        t.start()
        for _ in range(100):
            if plc.coils[2]:
                break
            time.sleep(0.02)
        assert plc.coils[2] is True and not _holds(p) and t.is_alive()
    finally:
        lock.release()
    t.join()
    assert out["r"] == {"held": True} and [e["reason"] for e in _holds(p)] == ["foil jam"]
    assert c.get("/status").json()["held"] is True


def test_board_strip_number_does_not_count_rechecked_strips(tmp_path):
    """Integration 2 note: the board chip's strip number counted re-check rows; the Run screen does not."""
    c, p = bench(tmp_path)
    c.post("/inspect", files=up(make_strip(seed=15)))  # strip 1 passes
    _fake_judge(c, "wrong_tablet")
    c.post("/inspect", files=up(make_strip(seed=15)))  # strip 2: critical, strip 1 goes into a re-check
    assert c.get("/line").json()["chip"]["strip"] == 2
    c.post("/inspect", files=up(make_strip(seed=15)))  # the re-check of strip 1
    assert _lines(p / "log.jsonl")[-1]["recheck"] is True and c.get("/line").json()["chip"]["strip"] == 2
