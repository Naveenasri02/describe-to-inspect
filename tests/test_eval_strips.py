from pathlib import Path

import pytest

from tools.eval_strips import SelfTestFailed, TooFewPhotos, evaluate, split_good, summary_lines


class FakeStation:
    def __init__(self, reject=(), hold=(), no_strip=(), test_ok=True):
        self.reject, self.hold, self.no_strip, self.test_ok = set(reject), set(hold), set(no_strip), test_ok
        self.taught, self.inspected = None, []

    def teach(self, images, crosscheck=False):
        self.taught = list(images)

    def self_test(self, good_image, rng_seed=None, record=True):
        return {"passed": self.test_ok, "failed": []}

    def inspect(self, image, strip_id=None, image_name=None, user=None):
        self.inspected.append(image_name)
        self.users = getattr(self, "users", []) + [user]
        n = image_name

        class V:
            status = "REJECT" if n in self.reject else "HOLD" if n in self.hold else "NO_STRIP" if n in self.no_strip else "PASS"
            reasons = [{"text": "r"}] if image_name in self.reject else []
        return V()


def paths(prefix, n):
    return [Path(f"{prefix}{i:02d}.jpg") for i in range(n)]


def test_split_holds_out_last_three_sorted():
    good = paths("g", 10)
    teach, hold = split_good(reversed(good))
    assert hold == good[-3:] and teach == good[:-3]


def test_split_refuses_too_few_good():
    with pytest.raises(TooFewPhotos):
        split_good(paths("g", 7))


def test_counts_and_lines():
    good, bad = paths("g", 9), paths("b", 4)
    st = FakeStation(reject={"g08.jpg", "b00.jpg", "b01.jpg", "b02.jpg"})
    res = evaluate(st, good, bad, load=lambda p: p.name)
    assert (res["caught"], res["bad"], res["false_rejects"], res["held_out"]) == (3, 4, 1, 3)
    assert len(st.taught) == 6
    lines = summary_lines(res)
    assert "caught 3 of 4 bad (0 held for a person)" in lines
    assert "false rejects 1 of 3 good (0 held for a person)" in lines


def test_evaluate_refuses_no_bad():
    with pytest.raises(TooFewPhotos):
        evaluate(FakeStation(), paths("g", 9), [], load=lambda p: p.name)


def test_failed_self_test_aborts():
    st = FakeStation(test_ok=False)
    with pytest.raises(SelfTestFailed):
        evaluate(st, paths("g", 9), paths("b", 2), load=lambda p: p.name)
    assert st.inspected == []


def test_no_strip_is_not_judged_and_hold_counts():
    st = FakeStation(hold={"b00.jpg"}, no_strip={"b01.jpg", "g08.jpg"})
    res = evaluate(st, paths("g", 9), paths("b", 3), load=lambda p: p.name)
    assert (res["caught"], res["held"], res["false_rejects"], res["not_judged"]) == (1, 1, 0, 2)
    lines = summary_lines(res)
    assert "caught 1 of 3 bad (1 held for a person)" in lines and "not judged: 2" in lines


def test_smoke_video_call_reaches_client(tmp_path):
    import numpy as np
    from tools.smoke_live import run_calls

    class C:
        seen = []

        def spark_json(self, *a, **k): return {"reply": "ok"}
        def sam_image(self, img, prompt): return []
        def sam_video(self, mp4, prompt): C.seen.append(mp4); return []
        def transcribe(self, wav): return []

    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    res = run_calls(C(), np.zeros((2, 2, 3), np.uint8), clip, b"w")
    assert C.seen == [b"x"] and all(r[1].startswith("OK") for r in res)


def test_eval_verdicts_carry_user_eval():
    st = FakeStation()
    evaluate(st, paths("g", 8), paths("b", 3), load=lambda p: object())
    assert st.users and set(st.users) == {"eval"}
