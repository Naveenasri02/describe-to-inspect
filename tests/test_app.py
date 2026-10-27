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
from d2i.audit import Users
from d2i.checklist import approve
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
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    (tmp_path / "good").mkdir()
    for i in range(6):
        (tmp_path / "good" / f"{i}.jpg").write_bytes(jpg(make_strip(seed=i, shift=(i, 0))))
    c = TestClient(app_mod.create_app(tmp_path, None))
    assert c.post("/teach", json={}).json().get("error") is None
    up = lambda img: {"image": ("a.jpg", jpg(img), "image/jpeg")}
    assert c.post("/selftest", files=up(make_strip(seed=12))).json()["passed"] is True
    assert c.get("/status").json()["locked"] is False
    v = c.post("/inspect", files=up(make_strip(missing=(0,), seed=13))).json()
    assert v["status"] == "REJECT"
    assert "pocket 1" in " ".join(r["text"] for r in v["reasons"])
    s = c.get("/status").json()
    assert s["counts"]["REJECT"] == 1 and s["state"] == "REJECT"
    assert "Packaging-line QA bench" in c.get("/").text


class FakeClient:
    def spark_json(self, system, user, schema, name="result", **kw):
        return {"product": "vit C", "rows": 2, "per_row": 5, "item": "tablet",
                "reject_if": ["empty_pocket"], "unclear": []}


def test_describe_then_approve_flow(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None, client=FakeClient()))
    assert c.post("/approve", json={"by": "QA1"}).json()["error"]
    r = c.post("/describe", json={"text": "ten tablets"}).json()
    assert r["checklist"]["count"] == 10
    assert c.get("/status").json()["pending"]
    assert c.post("/approve", json={"by": "QA1"}).json()["checklist"]["version"] == 1
    assert c.get("/status").json()["checklist_version"] == 1


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


def bench(tmp_path, chat=None, client=None, plc=None):
    """Taught, self-tested bench in tmp_path/p with a QA user (qa1/pw-qa) and an operator (op/pw-op)."""
    p = tmp_path / "p"
    approve(CHECKLIST, "Priya", p / "checklists.jsonl")
    (p / "good").mkdir()
    for i in range(6):
        (p / "good" / f"{i}.jpg").write_bytes(jpg(make_strip(seed=i, shift=(i, 0))))
    users = Users(tmp_path / "users.json")
    users.add("qa1", "qa", "pw-qa")
    users.add("op", "operator", "pw-op")
    c = TestClient(app_mod.create_app(p, None, chat=chat, client=client, plc=plc))
    assert c.post("/teach", json={}).json().get("error") is None
    c.post("/selftest", files={"image": ("a.jpg", jpg(make_strip(seed=12)), "image/jpeg")})
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
    assert c.post("/challenge", json={"strip_label": "C-1", "ejected": True}).json()["user"] == "qa1"
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
    c.post("/teach", json={})  # re-taught: locked until a self-test passes
    assert plc.coils[1] is True
    c.post("/selftest", files=up(make_strip(seed=12)))
    assert plc.coils[1] is False
    c.app.state.station.clock = lambda: time.time() + 9 * 3600  # the shift runs out; only /status sees it
    c.get("/status")
    assert plc.coils[1] is True
