"""Line board: /line JSON, /board page, and states.jsonl written once per state change."""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_mod
from d2i.station import FINDER_DOWN, Verdict
from tests.conftest import make_strip
from tests.test_app import _audit, _fake_judge, bench, login, up
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
