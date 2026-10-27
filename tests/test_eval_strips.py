from pathlib import Path

import pytest

from tools.eval_strips import TooFewPhotos, evaluate, split_good, summary_lines


class FakeStation:
    def __init__(self, reject=()):
        self.reject, self.taught = set(reject), None

    def teach(self, images, crosscheck=False):
        self.taught = list(images)

    def self_test(self, good_image, rng_seed=None, record=True):
        return {"passed": True, "failed": []}

    def inspect(self, image, strip_id=None, image_name=None):
        class V:
            status = "REJECT" if image_name in self.reject else "PASS"
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
    assert "caught 3 of 4 bad" in lines and "false rejects 1 of 3 good" in lines


def test_evaluate_refuses_no_bad():
    with pytest.raises(TooFewPhotos):
        evaluate(FakeStation(), paths("g", 9), [], load=lambda p: p.name)
