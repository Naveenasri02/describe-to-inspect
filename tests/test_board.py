"""Line board: /line JSON, /board page, and states.jsonl written once per state change."""
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_mod
import d2i.openitems as openitems_mod
from d2i.audit import AuditLog
from d2i.station import FINDER_DOWN, Verdict
from tests.conftest import make_strip
from tests.test_app import ScriptChat, _audit, _fake_judge, bench, call, login, up, wait_done
from tests.test_static import STATIC, _node_check

CRIT = "Critical: Rule 5 failed: pocket 1 has a different tablet"


def states(p):
    return [json.loads(x) for x in (p / "states.jsonl").read_text(encoding="utf-8").splitlines()]


def polls(c, n=3):
    for _ in range(n):
        c.get("/line")
        c.get("/status")


def test_line_shape_and_board_page(tmp_path):
    c, p = bench(tmp_path)
    page = c.get("/board")
    assert page.status_code == 200 and "<title>Line board</title>" in page.text
    j = c.get("/line").json()
    assert {"now", "state", "since", "reason", "by", "waiting_for", "also", "investigating", "product",
            "checklist_version", "batch", "release", "chip", "waiting", "due", "challenge", "challenge_set", "counts",
            "reconciliation", "reject_run", "rejects_by_rule", "day_start", "segments", "lost", "footer"} <= set(j)
    assert (j["state"], j["waiting_for"], j["investigating"]) == ("RUNNING", None, None)
    assert j["since"] <= j["now"] and j["batch"]["batch"] == "B1" and j["batch"]["open"] is True
    assert [r["done"] for r in j["release"]] == [True, True, True] and j["chip"] is None and j["waiting"] == []
    assert [d["name"] for d in j["due"]] == ["Leak test", "Print check", "Self-test expires"]
    assert j["due"][0]["in"] is None and 3500 < j["due"][1]["in"] <= 3600 and j["due"][2]["in"] > 7 * 3600
    assert j["counts"]["inspected"] == 0 and j["reconciliation"] == "open" and j["reject_run"] == {"n": 0, "of": 3}
    assert j["footer"] == {"audit_ok": True, "audit_entries": len(_audit(p)), "audit_broken_at": None,
                           "record": "draft", "signed_by": None, "signed_at": None}
    assert j["segments"][-1]["state"] == "RUNNING" and j["segments"][-1]["open"] is True


def test_board_is_read_only(tmp_path):
    routes = {r.path: r.methods for r in app_mod.create_app(tmp_path, None).routes if hasattr(r, "methods")}
    assert routes["/line"] == {"GET"} and routes["/board"] == {"GET"}


def test_states_written_once_per_change_on_hold_and_resume(tmp_path):
    c, p = bench(tmp_path)
    polls(c)
    n = len(states(p))
    assert states(p)[-1]["to"] == "RUNNING"
    login(c, "op", "pw-op")
    c.post("/hold", json={"reason": "foil looks wrong"})
    polls(c)
    rows = states(p)
    assert len(rows) == n + 1
    assert (rows[-1]["from"], rows[-1]["to"], rows[-1]["cause"], rows[-1]["by"]) == ("RUNNING", "HELD", "foil looks wrong", "op")
    j = c.get("/line").json()
    assert (j["state"], j["reason"], j["by"], j["waiting_for"]) == ("HELD", "foil looks wrong", "op", "QA")
    assert [(i["kind"], i["who"]) for i in j["waiting"]] == [("line_held", "QA")] and j["waiting"][0]["age"] >= 0
    login(c, "qa1", "pw-qa")
    assert c.post("/resume", json={"password": "pw-qa", "reason": "foil roll changed"}).json()["resumed"] is True
    polls(c)
    rows = states(p)
    assert len(rows) == n + 2
    assert (rows[-1]["from"], rows[-1]["to"], rows[-1]["cause"], rows[-1]["by"]) == \
        ("HELD", "RUNNING", "Resumed: foil roll changed", "qa1")
    held = [s for s in c.get("/line").json()["segments"] if s["state"] == "HELD"]
    assert held[-1]["closed_by"] == "qa1" and held[-1]["seconds"] == round(rows[-1]["time"] - rows[-2]["time"], 1)


def test_critical_reject_logs_held_with_rule_and_strip(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "wrong_tablet")
    c.post("/inspect", files=up(make_strip(seed=15)))
    r = states(p)[-1]  # written by the inspect request itself, before any poll
    assert (r["to"], r["by"], r["rule"], r["strip"]) == ("HELD", "station", 5, 1) and r["cause"].startswith(CRIT[:24])
    j = c.get("/line").json()
    assert j["state"] == "HELD" and (j["chip"]["status"], j["chip"]["rule"]) == ("REJECT", 5)
    assert j["waiting"][0]["kind"] == "line_held" and j["waiting"][0]["evidence"].startswith("audit #")


def test_reject_is_a_chip_beside_running_never_a_state(tmp_path):
    c, p = bench(tmp_path)
    _fake_judge(c, "broken_tablet")  # major: rejected, the line keeps running
    c.post("/inspect", files=up(make_strip(seed=15)))
    j = c.get("/line").json()
    assert j["state"] == "RUNNING" and (j["chip"]["status"], j["chip"]["rule"], j["chip"]["strip"]) == ("REJECT", 3, 1)
    assert j["counts"]["rejected"] == 1 and j["reject_run"] == {"n": 1, "of": 3}
    assert j["rejects_by_rule"] == [{"rule": 3, "text": "no tablet may be broken or chipped", "batch": 1, "hour": 1}]
    assert "REJECT" not in {r["to"] for r in states(p)}


def test_finder_down_suspends_until_it_answers(tmp_path):
    c, p = bench(tmp_path)
    polls(c, 1)  # the board saw the line running
    st = c.app.state.station
    st._judge = lambda image, sid, name: Verdict("HOLD", [dict(FINDER_DOWN)], strip_id=sid, image_name=name)
    c.post("/inspect", files=up(make_strip(seed=15)))
    j = c.get("/line").json()
    assert j["state"] == "SUSPENDED" and j["chip"]["status"] == "HOLD" and j["waiting_for"] is None
    del st._judge  # SAM answers again: the real judge passes a good strip
    assert c.post("/inspect", files=up(make_strip(seed=15, shift=(2, 1)))).json()["status"] == "PASS"
    assert c.get("/line").json()["state"] == "RUNNING"
    assert [r["to"] for r in states(p)][-3:] == ["RUNNING", "SUSPENDED", "RUNNING"]
    assert states(p)[-1]["cause"] == "Pocket finder answered"


def test_end_batch_is_complete_until_the_record_is_signed(tmp_path):
    c, p = bench(tmp_path)
    login(c, "qa1", "pw-qa")
    assert c.post("/batch/end", data={"cleared": "true"}).json()["totals"]["status"] == "within"
    j = c.get("/line").json()
    assert (j["state"], j["footer"]["record"], j["batch"]["open"]) == ("COMPLETE", "draft", False)
    assert [i["kind"] for i in j["waiting"]] == ["record_unsigned"] and j["reconciliation"] == "within"
    rid = c.get("/record").headers["X-Record-Id"]
    assert c.post("/sign", json={"record_id": rid, "password": "pw-qa"}).json()["signed_by"] == "qa1"
    j = c.get("/line").json()
    assert (j["state"], j["waiting"], j["footer"]["record"], j["footer"]["signed_by"]) == ("IDLE", [], "signed", "qa1")
    assert [r["to"] for r in states(p)][-2:] == ["COMPLETE", "IDLE"]


def test_line_gives_the_held_segment_and_lost_time(tmp_path):
    c, p = bench(tmp_path)
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    t = max(time.time(), midnight + 3600)  # a fixed "now" at least an hour into today
    rows = [{"time": t - 900, "to": "RUNNING", "cause": "Batch released", "by": None},
            {"time": t - 600, "to": "HELD", "cause": CRIT, "by": "station", "rule": 5, "strip": 7},
            {"time": t - 240, "to": "RUNNING", "cause": "Resumed: lamp moved back", "by": "qa1"}]
    (p / "states.jsonl").write_text("".join(json.dumps(dict(r, at="x", rule=r.get("rule"), strip=r.get("strip"))) + "\n"
                                            for r in rows), encoding="utf-8")
    j = TestClient(app_mod.create_app(p, None, clock=lambda: t)).get("/line").json()
    held = [s for s in j["segments"] if s["state"] == "HELD"]
    assert len(states(p)) == 3 and j["state"] == "RUNNING" and j["since"] == t - 240
    assert [(s["seconds"], s["closed_by"], s["close_cause"]) for s in held] == [(360.0, "qa1", "Resumed: lamp moved back")]
    assert j["lost"] == [{"state": "HELD", "cause": CRIT, "seconds": 360.0}] and j["day_start"] == midnight


def test_board_page_static_checks(tmp_path):
    html = (STATIC / "line.html").read_text(encoding="utf-8")
    low = html.lower()
    assert "http://" not in html and "https://" not in html and "<script src" not in low and "<canvas" not in low
    assert "dashboard" not in low and not re.search(r"\b(oee|aql|accuracy|spend|usd)\b", low)
    assert "Built with Muse Spark Open" in html and 'href="/static/station.css"' in html
    assert html.count('id="pareto"') == 1 and html.count('id="timeline"') == 1
    assert 'fetch("/line"' in html and "setInterval(tick,1000)" in html and "POST" not in html
    for i, code in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
        _node_check(code, tmp_path / f"line.{i}.js")
    assert 'href="/board"' in (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/board" in (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_failed_self_test_stops_the_line_and_no_self_test_counts(tmp_path):
    c, p = bench(tmp_path)
    with (p / "selftests.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"passed": False, "failed": ["empty_pocket"], "results": [], "time": time.time(),
                            "at": datetime.now().isoformat(timespec="seconds"), "checklist_version": 1}) + "\n")
    j = c.get("/line").json()
    assert (j["state"], j["reason"], j["waiting_for"]) == ("STOPPED", "Self-test failed", "QA")
    assert j["due"][-1] == {"name": "Self-test expires", "in": None}


# --- concurrency: a row is never older than the facts behind the row before it; a resume always carries the QA name ---
def continuous(rows):
    assert [r["time"] for r in rows] == sorted(r["time"] for r in rows)
    assert all(a["to"] == b["from"] for a, b in zip(rows, rows[1:]))  # no phantom row: each one continues the last


def held_by_op_then_qa_logged_in(c):
    c.get("/line")
    login(c, "op", "pw-op")
    c.post("/hold", json={"reason": "foil looks wrong"})
    login(c, "qa1", "pw-qa")


def test_a_slow_poll_never_appends_a_stale_hold_after_the_resume(tmp_path, monkeypatch):
    real, armed = openitems_mod.open_items, threading.Event()

    def slow(*a, **k):
        if armed.is_set():
            armed.clear()
            time.sleep(0.8)  # a slow poll (big audit log) that read hold.json before the resume
        return real(*a, **k)

    monkeypatch.setattr(openitems_mod, "open_items", slow)
    c, p = bench(tmp_path)
    held_by_op_then_qa_logged_in(c)
    armed.set()
    t = threading.Thread(target=lambda: TestClient(c.app).get("/line"))
    t.start()
    time.sleep(0.3)
    assert c.post("/resume", json={"password": "pw-qa", "reason": "foil roll changed"}).json()["resumed"] is True
    t.join()
    polls(c)
    rows = states(p)
    continuous(rows)
    assert [(r["to"], r["by"]) for r in rows[-2:]] == [("HELD", "op"), ("RUNNING", "qa1")]


def test_a_poll_during_a_resume_still_logs_the_qa_name(tmp_path, monkeypatch):
    real = AuditLog.append

    def slow(self, action, *a, **k):
        if action == "resume_line":
            time.sleep(0.6)  # hold.json is already written; the audit row is not yet
        return real(self, action, *a, **k)

    monkeypatch.setattr(AuditLog, "append", slow)
    c, p = bench(tmp_path)
    held_by_op_then_qa_logged_in(c)
    t = threading.Thread(target=lambda: (time.sleep(0.3), TestClient(c.app).get("/line")))
    t.start()
    assert c.post("/resume", json={"password": "pw-qa", "reason": "foil roll changed"}).json()["resumed"] is True
    t.join()
    polls(c)
    rows = states(p)
    continuous(rows)
    assert (rows[-1]["from"], rows[-1]["to"], rows[-1]["cause"], rows[-1]["by"]) == \
        ("HELD", "RUNNING", "Resumed: foil roll changed", "qa1")


def test_line_while_the_station_is_busy_serves_the_last_state_and_records_nothing(tmp_path):
    c, p = bench(tmp_path)
    assert c.get("/line").json()["state"] == "RUNNING"
    n = len(states(p))
    lock = c.app.state.station_lock
    lock.acquire()  # a slow station operation (tray clip, teach) holds it
    try:
        (p / "hold.json").write_text(json.dumps({"held": True, "at": datetime.now().astimezone().isoformat(), "by": "op"}),
                                     encoding="utf-8")
        j = c.get("/line").json()
        assert j["state"] == "RUNNING" and len(states(p)) == n
    finally:
        lock.release()
    assert c.get("/line").json()["state"] == "HELD" and len(states(p)) == n + 1


def test_status_polls_do_not_work_out_the_line_state(tmp_path):
    c, p = bench(tmp_path)
    c.get("/line")
    n = len(states(p))
    (p / "hold.json").write_text(json.dumps({"held": True, "at": datetime.now().astimezone().isoformat(), "by": "op"}),
                                 encoding="utf-8")
    c.get("/status")
    assert len(states(p)) == n
    c.get("/line")
    assert len(states(p)) == n + 1


def test_agent_approved_resume_names_the_qa_and_quotes_the_agent(tmp_path):
    chat = ScriptChat(call("request_resume", {"reason": "lamp moved back"}), {"content": "Lamp moved back."})
    c, p = bench(tmp_path, chat)
    c.get("/line")
    assert c.post("/investigate", json={"trigger": "rejects rose"}).json()["id"]
    s = wait_done(c)
    c.get("/line")
    assert (states(p)[-1]["to"], states(p)[-1]["by"]) == ("HELD", "supervisor")
    login(c, "qa1", "pw-qa")
    assert c.post("/approve-request", json={"id": s["pending"][0]["id"], "password": "pw-qa"}).json()["approved"] is True
    r = states(p)[-1]
    assert (r["from"], r["to"], r["by"], r["rule"]) == ("HELD", "RUNNING", "qa1", None)
    assert r["cause"] == 'Resumed by qa1 (approved agent request): the agent wrote "lamp moved back"'


def test_rule_only_from_the_station_never_from_typed_text(tmp_path):
    c, p = bench(tmp_path)
    c.get("/line")
    login(c, "op", "pw-op")
    c.post("/hold", json={"reason": "Rule 5 looks wrong to me"})
    r = states(p)[-1]
    assert (r["to"], r["cause"], r["by"], r["rule"]) == ("HELD", "Rule 5 looks wrong to me", "op", None)


def test_unreadable_records_show_on_the_board_and_never_blank_it(tmp_path):
    c, p = bench(tmp_path)
    for name in ("prints.jsonl", "challenge_runs.jsonl"):
        (p / name).write_text("not json\n", encoding="utf-8")
    j = c.get("/line").json()
    assert "error" not in j and j["state"] == "STARTING" and "Cannot read" in j["reason"]
    assert j["due"][0] == {"name": "Hourly checks", "in": None, "text": "cannot read the records"}
    assert any(i["text"].startswith("Cannot read") for i in j["waiting"]) and j["challenge"] is None
    (p / "selftests.jsonl").write_text("not json\n", encoding="utf-8")
    j = c.get("/line").json()
    assert "error" not in j and j["state"] == "STOPPED" and j["reason"].startswith("Cannot read self-test log")


# --- the page never freezes on a stale state ---
HARNESS = r"""
let now=1e6,seq=0;const timers=[];Date.now=()=>now;
globalThis.setInterval=(f,ms)=>{timers.push({f,ms,at:now+ms,rep:true,id:++seq});return seq};
globalThis.setTimeout=(f,ms)=>{timers.push({f,ms,at:now+ms,rep:false,id:++seq});return seq};
globalThis.clearTimeout=id=>{const i=timers.findIndex(t=>t.id===id);if(i>=0)timers.splice(i,1)};
function el(){const s=new Set();return{style:{},children:[],textContent:"",hidden:false,className:"",title:"",clientWidth:1000,
  previousElementSibling:{textContent:""},classList:{add:c=>s.add(c),remove:c=>s.delete(c),contains:c=>s.has(c),
  toggle:(c,on)=>{(on===undefined?!s.has(c):on)?s.add(c):s.delete(c)}},
  append(...k){this.children.push(...k)},prepend(...k){this.children.unshift(...k)},replaceChildren(...k){this.children=k}}}
const els={};globalThis.document={getElementById:id=>els[id]||(els[id]=el()),createElement:()=>el(),body:el()};
const IGNORE_ABORT=__IGNORE__;let calls=0;const pending=[];
globalThis.fetch=(url,opt)=>{calls++;return new Promise((res,rej)=>{pending.push(res);
  if(!IGNORE_ABORT)opt.signal.addEventListener("abort",()=>rej(new Error("aborted")))})};
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r))};
async function advance(ms){const end=now+ms;for(;;){timers.sort((a,b)=>a.at-b.at);const t=timers[0];if(!t||t.at>end)break;
  now=t.at;if(t.rep)t.at+=t.ms;else timers.shift();t.f();await flush()}now=end;await flush()}
const isStale=()=>document.body.classList.contains("stale");
const GOOD={now:1000,state:"RUNNING",since:900,reason:"",by:null,waiting_for:null,also:[],investigating:null,product:"x",count:10,
  checklist_version:1,batch:{batch:"B1",open:true},release:[{item:"Self-test for this batch",done:true}],chip:null,waiting:[],
  due:[{name:"Leak test",in:100},{name:"Hourly checks",in:null,text:"cannot read the records"}],challenge:null,challenge_set:false,
  counts:{inspected:1,passed:1,rejected:0,held:0,no_strip:0},reconciliation:"open",reject_run:{n:0,of:3},rejects_by_rule:[],
  day_start:0,segments:[{state:"RUNNING",start:900,end:1000,seconds:100,cause:"x",by:null,open:true,closed_by:null,close_cause:null}],
  lost:[],footer:{audit_ok:true,audit_entries:3,audit_broken_at:null,record:"draft",signed_by:null,signed_at:null}};
__SCRIPT__
(async()=>{const o={};await flush();o.loaded_calls=calls;o.loaded_stale=isStale();
  await advance(1000);o.calls_in_flight=calls;
  pending.shift()({json:async()=>GOOD});await flush();o.green=!isStale();
  await advance(2400);o.still_green=!isStale();
  await advance(1700);o.lost=isStale();o.text=els.stale.textContent;o.calls=calls;
  console.log(JSON.stringify(o))})();
"""


@pytest.mark.parametrize("ignore_abort", ["false", "true"])
def test_board_goes_grey_when_the_station_stops_answering(tmp_path, ignore_abort):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", (STATIC / "line.html").read_text(encoding="utf-8"), re.S)[0]
    f = tmp_path / "board.js"
    f.write_text(HARNESS.replace("__IGNORE__", ignore_abort).replace("__SCRIPT__", script), encoding="utf-8")
    r = subprocess.run([node, str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert (o["loaded_calls"], o["loaded_stale"], o["calls_in_flight"]) == (1, False, 1)  # never two fetches at once
    assert o["green"] and o["still_green"]  # an answer 2.4 s ago is still live
    assert o["lost"] and o["text"] == "Line board lost contact with the station — check the station screen"
