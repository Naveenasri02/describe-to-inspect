import json

from d2i import trace


def test_trace_caps_and_writes_file(tmp_path):
    trace.clear()
    trace.set_path(tmp_path / "t.jsonl")
    for i in range(205):
        trace.record("m", "local", "j", i)
    trace.set_path(None)
    assert len(trace.recent(500)) == 200 and trace.recent(1)[0]["ms"] == 204
    assert json.loads((tmp_path / "t.jsonl").read_text().splitlines()[0])["model"] == "m"


def test_trace_survives_bad_path(tmp_path):
    trace.set_path(tmp_path / "no" / "such" / "t.jsonl")
    trace.record("m", "local", "j", 1)
    trace.set_path(None)
