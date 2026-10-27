import cv2
import pytest
from fastapi.testclient import TestClient

import app as app_mod
from d2i.checklist import approve
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
    (tmp_path / "meta_spend.json").write_text("garbage")
    s = TestClient(app_mod.create_app(tmp_path, None)).get("/status")
    assert s.status_code == 200 and s.json()["counts"] == {"PASS": 1} and s.json()["spent_usd"] is None


def test_describe_multipart_without_audio_returns_error(tmp_path):
    c = TestClient(app_mod.create_app(tmp_path, None, client=FakeClient()))
    r = c.post("/describe", files={"other": ("a.txt", b"x")})
    assert r.status_code == 200 and "audio" in r.json()["error"]
