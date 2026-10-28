import hashlib
import json
import re
import threading
import time

import cv2
import pytest
from fastapi.testclient import TestClient

import app as app_mod
import d2i.meta_api as meta_api
from datetime import datetime

from d2i.audit import AuditLog, Users
from d2i.batch import Batches
from d2i.checklist import CHECKS, approve
from d2i.meta_api import SamTrackPoint
from d2i.record import _lines
from d2i.station import Verdict
from d2i.supervisor import set_hold
from d2i.plc import Plc
from tests.conftest import make_strip
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
    c = TestClient(app_mod.create_app(tmp_path, None))
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


def bench(tmp_path, chat=None, client=None, plc=None, source=None, batch=True, release=True):
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
    assert c.post("/sign", json={"password": "pw-qa", "record_id": rid}).json()["signed_by"] == "qa1"


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
    assert s["held"] is True and s["state"] == "HOLD" and s["quarantine_count"] == 0


def test_sign_signs_the_record_that_was_viewed(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    r = c.get("/record")
    rid, shown = r.headers["X-Record-Id"], re.search(r"SHA-256 of the record above: (\w{64})", r.text).group(1)
    with (p / "log.jsonl").open("a", encoding="utf-8") as f:  # the log changes after QA opened the record
        f.write(json.dumps({"status": "REJECT", "reasons": [], "time": time.time()}) + "\n")
    signed = c.post("/sign", json={"password": "pw-qa", "record_id": rid}).json()
    assert signed["record_sha256"] == shown
    assert c.post("/sign", json={"password": "pw-qa", "record_id": "nope"}).json()["error"].startswith("Record expired")


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
    assert plc.coils[2] is True and s["state"] == "HOLD"
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
        c = TestClient(app_mod.create_app(tmp_path / "p", source, client=TrayClient()))
        assert c.post("/tray").json() == {"error": "tray clip needs a camera or video source"}
        if c.app.state.source:
            c.app.state.source.stop.set()


def test_sign_pins_the_verdict_files(tmp_path):
    c, p = bench(tmp_path)
    c.post("/inspect", files=up(make_strip(seed=15)))
    login(c, "qa1", "pw-qa")
    rid = c.get("/record").headers["X-Record-Id"]
    assert c.post("/sign", json={"password": "pw-qa", "record_id": rid}).json()["signed_by"] == "qa1"
    e = json.loads((p / "audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    sha = lambda n: hashlib.sha256((p / n).read_bytes()).hexdigest()  # noqa: E731
    assert e["action"] == "sign"
    assert e["details"]["files_sha256"] == {"log.jsonl": sha("log.jsonl"), "selftests.jsonl": sha("selftests.jsonl")}


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
    form = {"batch": "lot 7", "mfg": "01/2026", "exp": "01/2028", "mrp": "Rs. 45"}
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


def test_wrong_print_holds_line(tmp_path):
    read = {"batch": "B9", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = _print_bench(tmp_path, read=read)
    assert c.post("/print", files=up(make_strip(seed=1))).json()["status"] == "HOLD"
    assert c.get("/status").json()["held"] is True
    e = _audit(p)[-1]
    assert e["user"] == "station" and e["reason"].startswith("Critical: wrong print — Batch no. on strip is B9")


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
    c = TestClient(app_mod.create_app(tmp_path / "p", str(clip), client=Counting()))  # never taught: LOCKED
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


def test_failing_hold_still_records_the_print(tmp_path, monkeypatch):
    read = {"batch": "B9", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = _print_bench(tmp_path, read=read)
    monkeypatch.setattr(app_mod, "hold_line", lambda *a: 1 / 0)
    assert "error" in c.post("/print", files=up(make_strip(seed=1))).json()
    assert _lines(p / "prints.jsonl")[0]["status"] == "HOLD"


def test_wrong_print_while_held_adds_no_second_hold(tmp_path):
    read = {"batch": "B9", "mfg": "01/2026", "exp": "01/2028", "mrp": "45", "readable": True}
    c, p = _print_bench(tmp_path, read=read)
    c.post("/hold", json={})
    first = (p / "hold.json").read_text(encoding="utf-8")
    n = len(_holds(p))
    assert c.post("/print", files=up(make_strip(seed=1))).json()["status"] == "HOLD"
    assert len(_lines(p / "prints.jsonl")) == 1 and len(_holds(p)) == n
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
    c, p = bench(tmp_path)
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

    def clip_verdicts(path, station, client, held=False, batch=None):
        return [station.log(Verdict("REJECT", [station._reason(k, 0)], strip_id=i) if k else Verdict("PASS", strip_id=i),
                            held, batch) for i, k in enumerate(kinds, 1)]
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
