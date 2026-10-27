import json
import shutil

import numpy as np
import pytest

from d2i.checklist import approve
from d2i.judge import Judge
from d2i.meta_api import SamObject
from d2i.pockets import _strip_by_contour
from d2i.station import Station, Verdict
from tests.conftest import STRIP_BOX, make_strip, pocket_box

T0 = 1_800_000_000.0
CHECKLIST = {"product": "vitamin C strip", "rows": 2, "per_row": 5, "count": 10, "item": "tablet",
             "reject_if": ["empty_pocket", "broken_tablet", "foil_damage"], "unclear": [],
             "sentence": "Ten tablets in two rows of five.",
             "rules": ["Rule 1: exactly 10 tablets in 2 rows of 5", "Rule 2: no pocket may be empty",
                       "Rule 3: no tablet may be broken or chipped", "Rule 4: foil must not be torn, cut or missing"],
             "version": None, "approved_by": None, "approved_at": None}


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture(scope="module")
def taught(tmp_path_factory):
    base = tmp_path_factory.mktemp("product")
    approve(CHECKLIST, "Priya", base / "checklists.jsonl")
    judge = Judge()
    st = Station(base, judge=judge, clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    return base, judge


@pytest.fixture
def station(taught, tmp_path):
    base, judge = taught
    shutil.copytree(base, tmp_path / "p")
    clock = Clock()
    return Station(tmp_path / "p", judge=judge, clock=clock), clock


def test_new_station_is_locked(station):
    st, _ = station
    assert st.locked and "self-test" in st.lock_reason
    assert st.inspect(make_strip(seed=11)).status == "LOCKED"


def test_self_test_passes_and_unlocks(station):
    st, _ = station
    result = st.self_test(make_strip(seed=12), rng_seed=5)
    assert result["passed"] is True, result
    assert {r["kind"] for r in result["results"]} == set(CHECKLIST["reject_if"])
    assert all(r["caught"] and r["named_right"] for r in result["results"])
    assert st.locked is False
    line = json.loads((st.dir / "selftests.jsonl").read_text().splitlines()[-1])
    assert line["passed"] is True and line["checklist_version"] == 1


def test_reject_names_rule_and_pocket(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    v = st.inspect(make_strip(seed=13, missing=(6,)), strip_id=4)
    assert isinstance(v, Verdict) and v.status == "REJECT"
    assert {"rule": 2, "check": "empty_pocket", "pocket": 7, "text": "Rule 2 failed: pocket 7 is empty"} in v.reasons
    logged = json.loads((st.dir / "log.jsonl").read_text().splitlines()[-1])
    assert logged["status"] == "REJECT" and logged["strip_id"] == 4 and logged["checklist_version"] == 1


def test_cracked_tablet_named_rule_3(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    v = st.inspect(make_strip(seed=14, cracked=(2,)))
    assert v.status == "REJECT" and any(r["rule"] == 3 and r["pocket"] == 3 for r in v.reasons), v.reasons


def test_good_strip_passes(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    v = st.inspect(make_strip(seed=15, shift=(2, 1)))
    assert v.status == "PASS" and v.reasons == []


def test_blank_frame_no_strip(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    assert st.inspect(np.full((360, 640, 3), 128, np.uint8)).status == "NO_STRIP"


class ElevenPills:
    def sam_image(self, image, prompt):
        boxes = [STRIP_BOX] if prompt == "blister strip" else [pocket_box(i) for i in [*range(10), 0]]
        return [SamObject(i, b, 0.9, None) for i, b in enumerate(boxes)]


def test_count_mismatch_holds(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.client = ElevenPills()
    v = st.inspect(make_strip(seed=16))
    assert v.status == "HOLD" and v.reasons[0]["rule"] == 1 and "11 pills" in v.reasons[0]["text"]


def test_unlisted_defect_is_held_not_passed(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.checklist = dict(st.checklist, reject_if=["foil_damage"])
    v = st.inspect(make_strip(seed=17, missing=(1,)))
    assert v.status == "HOLD" and v.reasons[0]["pocket"] == 2


def test_broken_judge_fails_self_test_and_locks(station, monkeypatch):
    st, _ = station
    monkeypatch.setattr(st.judge, "pocket_threshold", 10.0)
    monkeypatch.setattr(st.judge, "border_threshold", 10.0)
    result = st.self_test(make_strip(seed=12), rng_seed=5)
    assert result["passed"] is False and st.locked is True
    assert st.inspect(make_strip(seed=18, missing=(0,))).status == "LOCKED"


def test_shift_expiry_locks(station):
    st, clock = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    assert st.locked is False
    clock.t += 9 * 3600
    assert st.locked is True and "8" in st.lock_reason


class ContourSam:
    """Fake SAM: strip from the contour finder, a pill in every pocket except `skip`."""

    def __init__(self, skip=()):
        self.skip = skip

    def sam_image(self, image, prompt):
        strip = _strip_by_contour(image)
        if prompt == "blister strip":
            return [SamObject(0, strip, 0.95, None)]
        return [SamObject(i, pocket_box(i, strip=strip), 0.9, None) for i in range(10) if i not in self.skip]


def test_station_inspect_uses_saved_template(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ContourSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert len(st.template) == 10 and len(Station(tmp_path, clock=lambda: T0).template) == 10
    assert st.self_test(make_strip(seed=12), rng_seed=5)["passed"]
    st.client = ContourSam(skip=(6,))
    v = st.inspect(make_strip(seed=13, missing=(6,)))
    assert v.status == "REJECT"
    assert any(r["text"] == "Rule 2 failed: pocket 7 is empty" for r in v.reasons), v.reasons
