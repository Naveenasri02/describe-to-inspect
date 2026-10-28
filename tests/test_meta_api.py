import io
import json
import wave

import cv2
import numpy as np
import pytest

from d2i.meta_api import (Budget, BudgetExceeded, MetaClient, MetaError, SamObject)
from tests.conftest import FakeHTTP, responses_body, sam_text


def budget(tmp_path, limit=50.0):
    return Budget(tmp_path / "spend.json", limit=limit)


def chat_body(content, prompt_tokens=100, completion_tokens=10):
    return {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}}


def wav_bytes(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_budget_refuses_past_limit(tmp_path):
    b = budget(tmp_path, limit=1.0)
    b.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.6)
    assert Budget(tmp_path / "spend.json", limit=1.0).spent == pytest.approx(0.6)


def test_two_budgets_on_one_file_share_the_total(tmp_path):
    a, b = budget(tmp_path, limit=1.0), budget(tmp_path, limit=1.0)
    a.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.6)
    b.reserve(0.3)
    a.settle(0.6, 0.1)
    assert a.spent == b.spent == pytest.approx(0.4)


def test_corrupt_spend_file_refuses_paid_calls(tmp_path):
    import d2i.meta_api as m
    assert m.DEFAULT_SPEND.name == "meta_spend.json" and Budget().path == m.DEFAULT_SPEND
    m.DEFAULT_SPEND.write_text("garbage")
    b = Budget()  # building it does not crash
    with pytest.raises(BudgetExceeded, match="^spend file unreadable — check meta_spend.json$"):
        b.reserve(0.01)
    assert m.DEFAULT_SPEND.read_text() == "garbage"  # never reset to $0


def test_concurrent_reserves_lose_no_spend(tmp_path):
    import threading
    budgets = [budget(tmp_path) for _ in range(4)]

    def spend(b):
        for _ in range(50):
            b.reserve(0.01)
    threads = [threading.Thread(target=spend, args=(b,)) for b in budgets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert budgets[0].spent == pytest.approx(2.0)


def test_spark_json_sends_schema_and_settles(tmp_path):
    http = FakeHTTP([chat_body('{"a": 1}')])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    assert client.spark_json("sys", "user", schema) == {"a": 1}
    call = http.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["json"]["response_format"]["type"] == "json_schema"
    assert call["json"]["response_format"]["json_schema"]["strict"] is True
    assert client.budget.spent == pytest.approx(100 * 1.25e-6 + 10 * 4.25e-6)


def test_spark_json_defaults_low_effort_and_room(tmp_path):
    http = FakeHTTP([chat_body('{"a": 1}')])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    client.spark_json("sys", "user", {"type": "object", "properties": {}, "required": []})
    assert http.calls[0]["json"]["reasoning_effort"] == "low" and http.calls[0]["json"]["max_tokens"] == 4000


def test_spark_json_missing_required_key_raises(tmp_path):
    http = FakeHTTP([chat_body('{"b": 1}')])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    with pytest.raises(MetaError, match="a"):
        client.spark_json("sys", "user", schema)


def test_meta_error_keeps_reservation(tmp_path):
    http = FakeHTTP([(403, {"error": "forbidden"})])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    with pytest.raises(MetaError, match="403"):
        client.spark_json("sys", "user", {"type": "object", "properties": {}, "required": []})
    assert client.budget.spent > 0


def test_network_errors_become_meta_error(tmp_path):
    import requests
    schema = {"type": "object", "properties": {}, "required": []}
    for items in ([requests.ConnectionError("no route")] * 2, [requests.Timeout("read timed out")] * 2,
                  [(200, "<html>busy</html>")]):
        client = MetaClient(budget(tmp_path), key="k", http=FakeHTTP(items))
        with pytest.raises(MetaError, match="^Meta API unreachable: "):
            client.spark_json("sys", "user", schema)


def test_busy_api_is_tried_once_more_after_1_s(tmp_path, monkeypatch):
    waits = []
    monkeypatch.setattr("d2i.meta_api.RETRY_WAIT", 1.0)
    monkeypatch.setattr("d2i.meta_api.time.sleep", waits.append)
    for first in ((503, "busy"), (429, "slow down"), (500, "boom")):
        http = FakeHTTP([first, responses_body(sam_text([(0, (10, 20, 30, 40), 0.9)]))])
        client = MetaClient(budget(tmp_path), key="k", http=http)
        assert len(client.sam_image(np.zeros((360, 640, 3), np.uint8), "pill")) == 1
        assert len(http.calls) == 2
    assert waits == [1.0] * 3


def test_each_attempt_reserves_and_a_failed_one_keeps_it(tmp_path):
    http = FakeHTTP([(503, "busy"), responses_body("")])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    client.sam_image(np.zeros((8, 8, 3), np.uint8), "pill")
    assert client.budget.spent == pytest.approx(2 * 0.0025)


def test_other_4xx_is_not_retried(tmp_path):
    http = FakeHTTP([(400, "bad request"), responses_body("")])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    with pytest.raises(MetaError, match="400"):
        client.sam_image(np.zeros((8, 8, 3), np.uint8), "pill")
    assert len(http.calls) == 1


def test_timeout_twice_gives_up_after_two_posts(tmp_path):
    import requests
    http = FakeHTTP([requests.Timeout("read timed out")] * 2 + [responses_body("")])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    with pytest.raises(MetaError, match="^Meta API unreachable: "):
        client.sam_image(np.zeros((8, 8, 3), np.uint8), "pill")
    assert len(http.calls) == 2


def test_busy_twice_gives_up_after_two_posts(tmp_path):
    http = FakeHTTP([(503, "busy"), (502, "bad gateway"), responses_body("")])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    with pytest.raises(MetaError, match="502"):
        client.sam_image(np.zeros((8, 8, 3), np.uint8), "pill")
    assert len(http.calls) == 2


def test_spend_stop_is_never_retried(tmp_path):
    http = FakeHTTP([responses_body("")])
    client = MetaClient(budget(tmp_path, limit=0.001), key="k", http=http)
    with pytest.raises(BudgetExceeded):
        client.sam_image(np.zeros((8, 8, 3), np.uint8), "pill")
    assert http.calls == []


def test_image_calls_time_out_after_30_s(tmp_path):
    img = np.zeros((8, 8, 3), np.uint8)
    http = FakeHTTP([responses_body(""), chat_body(json.dumps({"count": 1, "points": [[1, 2]]})), chat_body('{}')])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    client.sam_image(img, "pill")
    client.spark_count(img, "tablet")
    client.spark_json("s", "u", {"type": "object", "properties": {}, "required": []})
    assert [c["timeout"] for c in http.calls] == [30, 30, 180]


def test_no_key_raises_with_key_file_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("d2i.meta_api.KEY_FILE", tmp_path / "missing.txt")
    client = MetaClient(budget(tmp_path), http=FakeHTTP([]))
    with pytest.raises(MetaError, match="missing.txt"):
        client.spark_json("sys", "user", {"type": "object", "properties": {}, "required": []})


def test_has_key_from_argument_env_or_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setattr("d2i.meta_api.KEY_FILE", tmp_path / "key.txt")
    assert MetaClient(budget(tmp_path), key="k").has_key and not MetaClient(budget(tmp_path)).has_key
    (tmp_path / "key.txt").write_text("k\n")
    assert MetaClient(budget(tmp_path)).has_key
    (tmp_path / "key.txt").unlink()
    monkeypatch.setenv("MODEL_API_KEY", "k")
    assert MetaClient(budget(tmp_path)).has_key


def test_sam_image_parses_boxes(tmp_path):
    http = FakeHTTP([responses_body(sam_text([(0, (10, 20, 30, 40), 0.9)]))])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    objs = client.sam_image(np.zeros((360, 640, 3), np.uint8), "blister pocket")
    assert len(objs) == 1
    o = objs[0]
    assert isinstance(o, SamObject) and o.id == 0 and o.box == (10, 20, 30, 40)
    assert o.score == pytest.approx(0.9)
    assert o.mask.shape == (360, 640) and o.mask[25, 15] and not o.mask[5, 5]
    body = http.calls[0]["json"]
    assert http.calls[0]["url"].endswith("/responses") and body["model"] == "sam-3.1"
    assert client.budget.spent == pytest.approx(0.0025)


def test_sam_boxes_scaled_to_image_size(tmp_path):
    http = FakeHTTP([responses_body(sam_text([(3, (20, 40, 60, 80), 0.8)], size=(1280, 720)))])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    (o,) = client.sam_image(np.zeros((360, 640, 3), np.uint8), "blister strip")
    assert o.box == (10, 20, 30, 40) and o.mask.shape == (360, 640)


def test_sam_video_parses_frames(tmp_path):
    path = tmp_path / "clip.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (64, 48))
    for _ in range(3):
        vw.write(np.zeros((48, 64, 3), np.uint8))
    vw.release()
    text = sam_text([(1, (2, 2, 10, 10), 0.7)], frame=0, size=(64, 48)) + "\n" + \
        sam_text([(1, (4, 2, 12, 10), 0.8)], frame=2, size=(64, 48))
    http = FakeHTTP([responses_body(text)])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    pts = client.sam_video(path.read_bytes(), "blister strip")
    assert [(p.id, p.frame, p.box) for p in pts] == [(1, 0, (2, 2, 10, 10)), (1, 2, (4, 2, 12, 10))]
    assert client.budget.spent == pytest.approx(50 * 0.0002)  # billed at the 50-frame minimum


def test_transcribe_multipart(tmp_path):
    http = FakeHTTP([{"transcript": "பத்து மாத்திரைகள்"}, {"transcript": "ten tablets"}])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    assert client.transcribe(wav_bytes(), languages=["Tamil", "English"]) == "பத்து மாத்திரைகள்"
    req = json.loads(http.calls[0]["files"]["request"][1])
    assert req["audioEncoding"] == "WAV" and req["languageBias"] == ["Tamil", "English"]
    assert http.calls[0]["url"].endswith("/asr/transcribe")
    client.transcribe(wav_bytes())
    assert "languageBias" not in json.loads(http.calls[1]["files"]["request"][1])


def test_transcribe_sends_mode(tmp_path):
    http = FakeHTTP([{"transcript": "a"}, {"transcript": "b"}])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    client.transcribe(wav_bytes(), mode="ENDPOINTING")
    assert json.loads(http.calls[0]["files"]["request"][1])["mode"] == "ENDPOINTING"
    client.transcribe(wav_bytes())
    assert json.loads(http.calls[1]["files"]["request"][1])["mode"] == "PUSH_TO_TALK"


def test_trace_records_meta_calls(tmp_path):
    from d2i import trace
    trace.clear()
    http = FakeHTTP([chat_body(json.dumps({"x": 1}))])
    MetaClient(budget(tmp_path), key="k", http=http).spark_json("s", "u", {"type": "object", "required": ["x"]})
    ev = trace.recent()[-1]
    assert ev["model"] == "muse-spark-1.3" and ev["platform"] == "Meta API" and ev["job"] == "spark_json"
    assert ev["ok"] and ev["usd"] > 0
    with pytest.raises(MetaError):
        MetaClient(budget(tmp_path), key="k", http=FakeHTTP([(500, "boom")] * 2)).sam_image(
            np.zeros((8, 8, 3), np.uint8), "x")
    ev = trace.recent()[-1]
    assert ev["model"] == "sam-3.1" and not ev["ok"] and ev["usd"] == 0


def test_spark_count_parses(tmp_path):
    http = FakeHTTP([chat_body(json.dumps({"count": 10, "points": [[1, 2]] * 10}))])
    n = MetaClient(budget(tmp_path), key="k", http=http).spark_count(np.zeros((8, 8, 3), np.uint8), "tablet")
    assert n == 10
    content = http.calls[0]["json"]["messages"][-1]["content"]
    assert content[0]["text"] == "Point at every tablet in this blister strip and count them."
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_chat_tools_sends_tools_and_returns_message(tmp_path):
    from d2i import trace
    trace.clear()
    msg = {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": "image_health", "arguments": "{}"}}]}
    http = FakeHTTP([{"choices": [{"message": msg}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
                     {"choices": [{"message": {"content": None}}]}])
    client = MetaClient(budget(tmp_path), key="k", http=http)
    tools = [{"type": "function", "function": {"name": "image_health", "parameters": {"type": "object"}}}]
    assert client.chat_tools([{"role": "user", "content": "go"}], tools) == msg
    body = http.calls[0]["json"]
    assert body["model"] == "muse-spark-1.3" and body["tools"] == tools and body["reasoning_effort"] == "low"
    assert body["max_tokens"] == 4000 and "tool_choice" not in body
    assert client.budget.spent == pytest.approx(100 * 1.25e-6 + 10 * 4.25e-6)
    ev = trace.recent()[-1]
    assert ev["platform"] == "Meta API" and ev["model"] == "muse-spark-1.3" and ev["job"] == "chat_tools"
    with pytest.raises(MetaError, match="no answer"):
        client.chat_tools([{"role": "user", "content": "go"}], tools)
