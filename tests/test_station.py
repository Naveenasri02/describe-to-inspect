import json
import os
import shutil
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import torch

from d2i import challenge
from d2i.checklist import CHECKS, approve
from d2i.judge import Judge
from d2i.meta_api import MetaError, SamObject
from d2i.defects import paint
from d2i.measure import pocket_check, teach_profile
from d2i.pockets import Layout, _strip_by_contour
from d2i.station import EMPTY_MARGIN, Station, Verdict, nearest, painted_prototypes, similarity
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
    assert {"rule": 2, "check": "empty_pocket", "pocket": 7, "text": "Rule 2 failed: pocket 7 is empty",
            "severity": "major"} in v.reasons
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
        boxes = [STRIP_BOX] if prompt == "blister strip" else [pocket_box(i) for i in range(10)] + [(267, 130, 277, 140)]
        return [SamObject(i, b, 0.9, None) for i, b in enumerate(boxes)]


def test_count_mismatch_holds(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.client, st.layout_source = ElevenPills(), "sam"  # SAM 3.1 pockets are counted on a product taught with SAM
    v = st.inspect(make_strip(seed=16))
    assert v.status == "HOLD" and v.reasons[0]["rule"] == 1 and "outside the pocket pattern" in v.reasons[0]["text"]


def test_unlisted_defect_is_held_not_passed(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.checklist = dict(st.checklist, reject_if=["foil_damage"])
    v = st.inspect(make_strip(seed=14, cracked=(2,)))
    assert v.status == "HOLD" and v.reasons[0] == {"rule": None, "check": "unlisted_anomaly", "pocket": 3,
                                                   "severity": "major", "text": "Pocket 3 looks unusual — check by hand"}


def test_empty_pocket_always_breaks_rule_1(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=["broken_tablet"])
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert [x["kind"] for x in r["results"]] == ["broken_tablet", "empty_pocket"] and r["passed"], r
    v = st.inspect(make_strip(seed=17, missing=(1,)))
    assert v.status == "REJECT"
    assert {"rule": 1, "check": "empty_pocket", "pocket": 2, "text": "Rule 1 failed: pocket 2 is empty", "severity": "major"} in v.reasons


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

    bare_filter = True
    has_key = True  # a key is set: teach needs SAM to answer

    def __init__(self, skip=()):
        self.skip = skip

    def sam_image(self, image, prompt):
        strip = _strip_by_contour(image)
        if prompt == "blister strip":
            return [SamObject(0, strip, 0.95, None)]
        boxes = [pocket_box(i, strip=strip) for i in range(10) if i not in self.skip]
        # a real SAM finds no pill where the pocket shows bare foil
        return [SamObject(i, b, 0.9, None) for i, b in enumerate(boxes)
                if not self.bare_filter or image[(b[1] + b[3]) // 2 - 4:(b[1] + b[3]) // 2 + 4, (b[0] + b[2]) // 2 - 4:(b[0] + b[2]) // 2 + 4].mean() > 200]


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


class CountClient(ContourSam):
    def __init__(self, spark, skip=()):
        super().__init__(skip)
        self.spark = spark

    def spark_count(self, image, item):
        return self.spark


def test_crosscheck_disagreement_blocks_teach(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=CountClient(9), clock=lambda: T0)
    assert st.crosscheck(make_strip()) == {"sam": 10, "spark": 9, "agree": False}
    with pytest.raises(ValueError, match="photo 1: SAM 3.1 found 10 pockets, Muse Spark counted 9"):
        st.teach([make_strip(seed=i) for i in range(6)], crosscheck=True)
    assert st.taught_at is None
    st.client = CountClient(10)
    st.teach([make_strip(seed=i) for i in range(6)], crosscheck=True)
    assert st.taught_at == T0


def test_stale_template_after_checklist_change_locks(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    Station(tmp_path, client=ContourSam(), clock=lambda: T0).teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    approve(dict(CHECKLIST, per_row=4, count=8), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ContourSam(), clock=lambda: T0)
    assert st.lock_reason == "Checklist changed — teach the station again"
    assert st.inspect(make_strip()).status == "LOCKED"


def test_teach_saves_the_sam_strip_box(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ContourSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])  # shifts 0..5 px: median x is +2.5
    want = (STRIP_BOX[0] + 2.5, STRIP_BOX[1], STRIP_BOX[2] + 2.5, STRIP_BOX[3])
    assert all(abs(g - w) <= 2 for g, w in zip(st.strip_box, want)), st.strip_box
    assert Station(tmp_path, clock=lambda: T0).strip_box == st.strip_box


class TightSam(ContourSam):
    """Pills drawn as tight boxes around the discs; `edit` maps pocket index -> replacement list of pill boxes."""

    def __init__(self, edit=None):
        super().__init__()
        self.edit = edit or {}

    def sam_image(self, image, prompt):
        objs = super().sam_image(image, prompt)
        if prompt == "blister strip":
            return objs
        out = []
        for i, o in enumerate(objs):
            x0, y0, x1, y1 = o.box
            cx, cy, r = (x0 + x1) // 2, (y0 + y1) // 2, int((x1 - x0) / 0.88 * 0.32)
            tight = (cx - r, cy - r, cx + r, cy + r)
            out += [SamObject(i, b, 0.9, None) for b in self.edit.get(i, [tight])]
        return out


def test_double_wrong_spot_named_rules(tmp_path):
    cl = dict(CHECKLIST, reject_if=list(CHECKS))
    approve(cl, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=TightSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert st.profile is not None and Station(tmp_path, clock=lambda: T0).profile == st.profile
    strip = make_strip(seed=21)
    box = TightSam().sam_image(strip, "pill")[2].box
    cx, cy, r = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2, (box[2] - box[0]) // 2
    assert st._judge(strip, None, None).status == "PASS"
    cases = {
        "double_tablet": (strip, [box, (cx + 2, cy - 5, cx + 2 + r, cy - 5 + r)]),
        "wrong_tablet": (strip, [(cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2)]),
        "tablet_spot": (paint(strip, pocket_box(2, strip=_strip_by_contour(strip)), "tablet_spot", _strip_by_contour(strip),
                              np.random.default_rng(3)), [box]),
    }
    for kind, (image, boxes) in cases.items():
        st.client = TightSam({2: boxes})
        v = st._judge(image, None, None)
        want = {"rule": CHECKS.index(kind) + 2, "check": kind, "pocket": 3}
        assert v.status == "REJECT" and any(want.items() <= x.items() for x in v.reasons), (kind, v.reasons)
        assert v.skipped == []


def test_grid_fallback_marks_measurements_skipped(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    assert st.self_test(make_strip(seed=12), rng_seed=5)["passed"]
    v = st.inspect(make_strip(seed=13))
    assert v.status == "PASS" and v.skipped == ["tablet measurements", "strip size"]
    assert json.loads((st.dir / "log.jsonl").read_text().splitlines()[-1])["skipped"] == ["tablet measurements", "strip size"]


class ImageSam(ContourSam):
    """Fake SAM that looks at the pixels: one box around the bright blob in each pocket window, split in two
    when the blob is clearly wider than tall (what a real SAM does with two touching tablets)."""

    bare_filter = False

    def sam_image(self, image, prompt):
        objs = super().sam_image(image, prompt)
        if prompt == "blister strip":
            return objs
        out = []
        for o in objs:
            x0, y0, x1, y1 = o.box
            ex, ey = int((x1 - x0) * 0.1), int((y1 - y0) * 0.1)
            ox, oy = x0 - ex, y0 - ey
            ys, xs = np.nonzero(image[oy:y1 + ey, ox:x1 + ex].min(axis=2) > 200)
            if len(xs) == 0:
                continue
            bx0, bx1, by0, by1 = xs.min() + ox, xs.max() + ox, ys.min() + oy, ys.max() + oy
            if (bx1 - bx0) > 1.1 * (by1 - by0):
                mid = (bx0 + bx1) // 2
                boxes = [(bx0, by0, mid, by1), (mid, by0, bx1, by1)]
            else:
                boxes = [(bx0, by0, bx1, by1)]
            for b in boxes:  # a SAM box hugs the pocket, a little wider than the tablet
                cx, cy, w, h = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, (b[2] - b[0]) * 1.3, (b[3] - b[1]) * 1.3
                out.append(SamObject(len(out), (int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)), 0.9, None))
        return out


NEW3 = ["double_tablet", "wrong_tablet", "tablet_spot"]


def test_selftest_catches_and_names_new_kinds(tmp_path):
    cl = dict(CHECKLIST, reject_if=NEW3)
    approve(cl, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert st.profile is not None
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert r["skipped"] == [] and r["passed"], r
    got = {x["kind"]: x for x in r["results"]}
    assert set(NEW3) <= set(got) and all(got[k]["caught"] and got[k]["named_right"] for k in NEW3), r


def test_selftest_lists_skipped_kinds_without_profile(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=NEW3)
    assert st.profile is None
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert r["skipped"] == NEW3 and r["passed"], r
    assert [x["kind"] for x in r["results"]] == ["empty_pocket"]
    assert json.loads((st.dir / "selftests.jsonl").read_text().splitlines()[-1])["skipped"] == NEW3


def test_station_strip_cut_named(tmp_path):
    cl = dict(CHECKLIST, reject_if=["strip_cut"])
    approve(cl, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert st.strip_profile is not None and Station(tmp_path, clock=lambda: T0).strip_profile == st.strip_profile
    good = make_strip(seed=21)
    v = st._judge(good, None, None)
    assert v.status == "PASS" and "strip size" not in v.skipped, v
    cut = paint(good, pocket_box(0), "strip_cut", STRIP_BOX, np.random.default_rng(3))
    v = st._judge(cut, None, None)
    want = {"rule": 2, "check": "strip_cut"}
    assert v.status == "REJECT" and any(want.items() <= x.items() for x in v.reasons), v.reasons
    assert st.self_test(make_strip(seed=12), rng_seed=5)["skipped"] == []
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert [x["kind"] for x in r["results"]].count("strip_cut") == 1 and r["passed"], r


def test_full_checks_strip_cut_and_empty_pocket(tmp_path):
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    good = make_strip(seed=21)
    assert st._judge(good, None, None).status == "PASS"
    v = st._judge(paint(good, pocket_box(0), "strip_cut", STRIP_BOX, np.random.default_rng(3)), None, None)
    assert v.status == "REJECT" and [x["check"] for x in v.reasons] == ["strip_cut"], v.reasons
    assert v.skipped == ["pocket checks (strip cut)"], v.skipped
    v = st._judge(make_strip(seed=22, missing=(0, 5)), None, None)  # a whole end column empty: pill bounds shrink
    assert v.status == "REJECT" and any(x["check"] == "empty_pocket" for x in v.reasons), v.reasons
    assert not any(x["check"] == "strip_cut" for x in v.reasons), v.reasons


def test_full_checks_selftest_catches_and_names_every_kind(tmp_path):
    """Every check on, SAM pill boxes: each painted kind is caught and named right (a crack is not a spot or a
    different tablet), over several seeds."""
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    for seed in range(6):
        r = st.self_test(make_strip(seed=12), rng_seed=seed, record=False)
        assert r["skipped"] == [] and {x["kind"] for x in r["results"]} == set(CHECKS), (seed, r)
        assert r["passed"], (seed, r)


def test_strip_cut_skipped_without_profile(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert "strip_cut" in r["skipped"] and st.inspect(make_strip(seed=13)).skipped.count("strip size") == 1


class FailingSam(ImageSam):
    """ImageSam that raises MetaError, as when SAM 3.1 does not answer after the retry: on every image ("all") or on
    the images fail(image) picks (calls run together, so their order is not fixed). Counts every call; a layout
    asks all five prompts at once."""

    def __init__(self, fail=()):
        super().__init__()
        self.fail, self.calls, self._lock = fail, 0, threading.Lock()

    def sam_image(self, image, prompt):
        with self._lock:
            self.calls += 1
        if self.fail == "all" or (callable(self.fail) and self.fail(image)):
            raise MetaError("Meta API 503: busy")
        return super().sam_image(image, prompt)


def marked(image):
    """A painted crack or torn notch: dark marks inside the strip (a broken tablet or foil damage)."""
    return bool((image[80:280, 90:550].min(axis=2) < 100).any())


FINDER = {"rule": 1, "check": "finder", "pocket": None,
          "text": "Pocket finder (SAM 3.1) did not answer — check this strip by hand", "severity": "major"}
NOT_RUN = "Self-test could not run: the pocket finder (SAM 3.1) did not answer — try again"


@pytest.fixture(scope="module")
def sam_taught(tmp_path_factory):
    base = tmp_path_factory.mktemp("sam_product")
    approve(CHECKLIST, "Priya", base / "checklists.jsonl")
    st = Station(base, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert st.self_test(make_strip(seed=12), rng_seed=5)["passed"]
    return base, st.judge


@pytest.fixture
def sam_station(sam_taught, tmp_path):
    base, judge = sam_taught
    shutil.copytree(base, tmp_path / "p")
    return Station(tmp_path / "p", client=ImageSam(), judge=judge, clock=lambda: T0)


def test_turned_and_moved_strip_passes_and_an_empty_pocket_is_still_caught(sam_station):
    st = sam_station  # taught on straight strips (ImageSam pills), self-test passed
    for turn in (2.5, -3.0):  # placed by hand: turned and moved
        v = st.inspect(make_strip(seed=15, shift=(10, 6), turn=turn))
        assert v.status == "PASS", (turn, v.reasons, [round(s, 3) for s in v.pocket_scores])
        v = st.inspect(make_strip(seed=13, shift=(10, 6), turn=turn, missing=(6,)))
        assert v.status == "REJECT" and any(r["check"] == "empty_pocket" and r["pocket"] == 7 for r in v.reasons),             (turn, v.reasons)


class DomeSam(ImageSam):
    """Like SAM 3.1 in the rehearsal: the clear dome of an empty pocket is outlined as a pill too."""

    def sam_image(self, image, prompt):
        objs = super().sam_image(image, prompt)
        if prompt == "blister strip":
            return objs
        strip = _strip_by_contour(image)
        for i in range(10):
            x0, y0, x1, y1 = pocket_box(i, strip=strip)
            if not any(x0 <= (o.box[0] + o.box[2]) / 2 <= x1 and y0 <= (o.box[1] + o.box[3]) / 2 <= y1 for o in objs):
                cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
                objs.append(SamObject(len(objs), (cx - 40, cy - 40, cx + 40, cy + 40), 0.9, None))
        return objs


def test_empty_pocket_outlined_by_sam_is_named_empty(tmp_path):
    """Rehearsal: SAM outlined the painted empty pocket, so it was measured as a different tablet and every self-test
    said "empty_pocket: caught, named wrong". A real empty pocket (bare foil under the dome) is named empty too."""
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=DomeSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    r = st.self_test(make_strip(seed=12), rng_seed=5, record=False)
    got = {x["kind"]: x for x in r["results"]}
    assert got["empty_pocket"]["caught"] and got["empty_pocket"]["named_right"] and r["passed"], r
    v = st._judge(make_strip(seed=13, missing=(6,)), None, None)
    assert v.status == "REJECT" and v.reasons[0]["text"] == "Rule 2 failed: pocket 7 is empty", v.reasons


def _dome(seed, k):
    """A strip whose pocket k is a real empty dome: grey (reflections, the table through the plastic), not foil."""
    img = make_strip(seed=seed, missing=(k,))
    x0, y0, x1, y1 = pocket_box(k, strip=_strip_by_contour(img))
    cv2.circle(img, ((x0 + x1) // 2, (y0 + y1) // 2), (x1 - x0) * 32 // 88, (120, 120, 120), -1)
    return img


def test_real_empty_dome_is_named_empty_once_an_empty_challenge_strip_is_registered(tmp_path):
    """Real photos: SAM outlined 87/88 empty pockets; their colour (reflections, the table through the dome) is past the
    foil tolerance, and the judge named them broken (nearest painted pocket), so the colour test called them a different
    tablet (critical, line held). QA's registered empty challenge strip teaches the judge a real empty dome: later
    domes, in any pocket, are named empty."""
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=DomeSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    later = _dome(15, 8)
    assert [r["check"] for r in st._judge(later, None, None).reasons] == ["wrong_tablet"]  # painted prototypes only
    photo = _dome(13, 6)
    challenge.register(tmp_path, st.checklist, "C1", "empty_pocket", "", photo, "qa1", "now",
                       learn=lambda: st.failing_pocket(photo))
    assert (tmp_path / "challenge" / "C1.npy").is_file()
    v = st._judge(later, None, None)
    assert v.status == "REJECT" and [r["check"] for r in v.reasons] == ["empty_pocket"] and v.reasons[0]["pocket"] == 9, \
        v.reasons
    assert st.self_test(make_strip(seed=12), rng_seed=5, record=False)["passed"]  # painted defects still named right
    # review 3: a coloured tablet reads nearer its foil ring than the white taught one, and the judge leaned "empty" by
    # 0.02-0.10 only (real domes by 0.17+): 5 of 6 were named empty (major), not a different tablet (critical)
    for name, bgr in ODD_COLOURS.items():
        img = make_strip(seed=21)
        x0, y0, x1, y1 = pocket_box(3, strip=_strip_by_contour(img))
        cv2.circle(img, ((x0 + x1) // 2, (y0 + y1) // 2), 30, bgr, -1)
        assert [(r["check"], r["pocket"]) for r in st._judge(img, None, None).reasons] == [("wrong_tablet", 4)], name


ODD_COLOURS = {"tan": (140, 165, 190), "peach": (150, 180, 235), "orange": (40, 140, 240), "brown": (60, 90, 130),
               "pink": (190, 170, 240), "yellow": (80, 220, 235)}  # BGR; a light grey one reads as the foil itself


def test_challenge_strip_prototypes_follow_their_kind_and_a_new_photo(tmp_path):
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=DomeSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert st.failing_pocket(make_strip(seed=15)) is None  # a good strip: no pocket fails, nothing is learned
    cracked = make_strip(seed=16, cracked=(2,))
    challenge.register(tmp_path, st.checklist, "C2", "broken_tablet", "", cracked, "qa1", "now",
                       learn=lambda: st.failing_pocket(cracked))
    assert [len(p.reshape(-1, p.shape[-1])) for p in st._prototypes().values()] == [1, 2]  # empty, broken (+C2)
    good = make_strip(seed=15)
    challenge.register(tmp_path, st.checklist, "C2", "broken_tablet", "", good, "qa1", "now",
                       learn=lambda: st.failing_pocket(good))  # the same label, a photo with no failing pocket
    assert not (tmp_path / "challenge" / "C2.npy").exists() and "prototype" not in challenge.load_set(tmp_path)["strips"][0]
    assert [len(p.reshape(-1, p.shape[-1])) for p in st._prototypes().values()] == [1, 1]


PHOTOS = Path(os.environ.get("D2I_PHOTOS", Path(__file__).resolve().parents[1] / "photos"))  # git-ignored
WA5 = PHOTOS / "real_wa" / "wa5.jpg"


@pytest.mark.skipif(not WA5.exists(), reason="the real photos are not in the repo (set D2I_PHOTOS)")
def test_real_photo_empty_domes_named_empty_after_one_is_registered():
    """wa5.jpg: three white tablets, three empty domes, silver foil. The painted prototypes (taught on its three
    tablets, as teach paints them) named every real dome broken (29 Oct, flat foil and dome paintings alike); one dome
    as a registered challenge strip names the other two empty, and a painted broken tablet stays broken."""
    img = cv2.imread(str(WA5))
    strip = (195, 390, 575, 905)
    tablets = [(247, 460, 340, 550), (400, 450, 485, 545), (262, 612, 356, 722)]
    domes = [(400, 600, 525, 720), (255, 760, 380, 885), (415, 755, 540, 880)]  # as SAM outlines a dome
    judge = Judge()
    protos = painted_prototypes(judge, [img], [Layout(strip, tablets, "sam", True)])
    emb = judge.pocket_embeddings(img, Layout(strip, domes, "sam", True)).cpu()
    learned = dict(protos, empty_pocket=torch.vstack([protos["empty_pocket"], emb[0]]))
    assert [nearest(e, learned) for e in emb[1:]] == ["empty_pocket"] * 2
    broken = paint(img, tablets[0], "broken_tablet", strip, np.random.default_rng(3))
    assert nearest(judge.pocket_embeddings(broken, Layout(strip, tablets, "sam", True))[0].cpu(), learned) == "broken_tablet"
    margin = lambda e: (lambda s: s["empty_pocket"] - s["broken_tablet"])(similarity(e, learned))
    assert all(margin(e) >= EMPTY_MARGIN for e in emb[1:])  # the judge's "empty" outweighs the colour test here
    for gain in ((0.75, 0.85, 1.0), (0.3, 0.65, 1.0), (0.4, 0.95, 1.0), (0.85, 0.75, 1.0), (0.35, 0.45, 0.6)):
        tinted = img.copy()  # the real white tablet 3 tinted peach, orange, yellow, pink, brown: a different tablet
        x0, y0, x1, y1 = tablets[2]
        crop = tinted[y0:y1, x0:x1].astype(np.float32)
        crop[crop.mean(2) > 150] *= np.array(gain, np.float32)
        tinted[y0:y1, x0:x1] = np.clip(crop, 0, 255).astype(np.uint8)
        assert margin(judge.pocket_embeddings(tinted, Layout(strip, tablets, "sam", True)).cpu()[2]) < EMPTY_MARGIN, gain


@pytest.mark.skipif(not WA5.exists(), reason="the real photos are not in the repo (set D2I_PHOTOS)")
def test_real_photo_painted_empty_pocket_reads_empty_when_sam_outlines_it():
    """wa5.jpg, each tablet painted empty (4 seeds), measured as when SAM outlines the painted pocket: the self-test's
    empty pocket must read empty, never a tablet with a spot (the dome painting read tablet 2 as tablet_spot 4 of 4)."""
    img = cv2.imread(str(WA5))
    strip, tablets = (195, 390, 575, 905), [(247, 460, 340, 550), (400, 450, 485, 545), (262, 612, 356, 722)]
    lay = Layout(strip, tablets, "sam", True, "", tablets)
    prof = teach_profile([img, img], [lay, lay])
    got = [pocket_check(paint(img, b, "empty_pocket", strip, np.random.default_rng(seed)), b, [b], prof)
           for b in tablets for seed in range(4)]
    assert got == ["empty_pocket"] * 12, got


def test_teach_refuses_a_photo_with_an_outlined_empty_pocket(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=DomeSam(), clock=lambda: T0)
    photos = [make_strip(seed=i, shift=(i, 0)) for i in range(6)]
    photos[2] = make_strip(seed=2, shift=(2, 0), missing=(4,))  # 10 boxes: 9 tablets and the empty dome
    with pytest.raises(ValueError, match="photo 3: pocket 5 looks empty"):
        st.teach(photos)


class CutSam(ImageSam):
    """Rehearsal p17_strip_cut: SAM's strip box ended where the cut end began, the taught pockets no longer fitted
    the tablets and one of them read as outside the pocket pattern."""

    def sam_image(self, image, prompt):
        objs = super().sam_image(image, prompt)
        if prompt == "blister strip":
            x0, y0, x1, y1 = objs[0].box
            return [SamObject(0, (x0, y0, x0 + int(0.8 * (x1 - x0)), y1), 0.95, None)]
        return objs + [SamObject(99, (214, 170, 234, 190), 0.9, None)]  # between the rows, off the pattern


def test_cut_strip_is_named_cut_when_its_tablets_no_longer_fit_the_pattern(tmp_path):
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    st.client = CutSam()
    v = st._judge(make_strip(seed=21), None, None)
    assert v.status == "REJECT" and [r["check"] for r in v.reasons] == ["strip_cut"], v.reasons


def test_border_judged_on_the_same_patches_at_teach_and_inspect(sam_station):
    """Review: teach scored straight photos on every border patch, inspect left the outer ring out for turned strips.
    The outer ring is left out at both. A 2 px band of table along the strip's edge (what a strip box 1 px too big
    shows) scored 0.19 on the whole grid against a 0.08 threshold; now 0.06. A torn strip is still caught (0.28 ->
    0.21)."""
    st = sam_station
    good = make_strip(seed=15, shift=(10, 6))
    x0, y0, x1, y1 = st._layout(good).strip_box
    band = good.copy()
    band[y0:y0 + 2, x0 + 14:x1 - 14] = 40
    band[y0 + 14:y1 - 14, x0:x0 + 2] = 40
    assert st.inspect(band).status == "PASS"
    lay = st._layout(good)
    torn = paint(good, lay.pockets[4], "foil_damage", lay.strip_box, np.random.default_rng(1))
    v = st.inspect(torn)
    assert v.status == "REJECT" and any(r["check"] == "foil_damage" for r in v.reasons), v.reasons


def test_teach_measures_each_photo_turn_against_the_taught_pockets(tmp_path):
    """Real straight teach photos read -0.53 to -0.59 deg against the uniform checklist grid (real pockets do not sit
    on it), so their border was straightened at teach and not at inspect. Each photo's turn is measured against the
    taught pockets, as inspect does."""
    class OffGrid(ImageSam):  # the right column sits 8 px lower than a uniform grid would put it
        def sam_image(self, image, prompt):
            objs = super().sam_image(image, prompt)
            return objs if prompt == "blister strip" else [
                SamObject(o.id, (o.box[0], o.box[1] + 8, o.box[2], o.box[3] + 8) if o.box[0] > 400 else o.box, 0.9, None)
                for o in objs]
    from d2i.pockets import find_layout
    photo = make_strip(seed=1)
    assert find_layout(photo, CHECKLIST, OffGrid()).angle == 0.0  # no taught pattern yet: no turn
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=OffGrid(), clock=lambda: T0)
    seen = []
    teach = st.judge.teach
    st.judge.teach = lambda images, layouts: (seen.extend(l.angle for l in layouts), teach(images, layouts))[1]
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    assert all(abs(a) < 1e-3 for a in seen), seen


def test_tablet_size_does_not_change_when_the_strip_is_turned(tmp_path):
    """Review: on a turned strip the taught pockets were stretched over the upright box around it, so tablets read
    about 6% small at 3 deg (half the 12% tolerance) and a tablet 15% larger read only 8% larger and passed."""
    from d2i.measure import features
    approve(dict(CHECKLIST, reject_if=list(CHECKS)), "Priya", tmp_path / "checklists.jsonl")
    st = Station(tmp_path, client=ImageSam(), clock=lambda: T0)
    st.teach([make_strip(seed=i, shift=(i, 0)) for i in range(6)])
    sizes = []
    for turn in (0.0, 3.0):
        good = make_strip(seed=15, shift=(10, 6), turn=turn)
        lay = st._layout(good)
        sizes.append(features(good, lay.slots[0], lay.in_pocket[0][0])["size"])
        assert st._judge(good, None, None).status == "PASS", turn
        v = st._judge(make_strip(seed=15, shift=(10, 6), turn=turn, big=(3,)), None, None)
        assert any(r["check"] == "wrong_tablet" and r["pocket"] == 4 for r in v.reasons), (turn, v.reasons)
    assert abs(sizes[1] - sizes[0]) < 0.02, sizes


def test_teach_records_the_layout_source(sam_station, taught):
    st = sam_station
    assert st.layout_source == "sam" and torch.load(st.dir / "model.pt")["layout_source"] == "sam"
    assert Station(taught[0], judge=taught[1]).layout_source == "grid"  # taught with no client: the checklist grid
    state = torch.load(st.dir / "model.pt")
    del state["layout_source"]  # taught before the source was saved: every such product was taught with SAM
    torch.save(state, st.dir / "model.pt")
    assert Station(st.dir, judge=st.judge, clock=lambda: T0).layout_source == "sam"


def test_teach_refuses_a_photo_the_pocket_finder_missed(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    photos = [make_strip(seed=i, shift=(i, 0)) for i in range(6)]
    st = Station(tmp_path, client=FailingSam(fail=lambda im: im is photos[2]), clock=lambda: T0)
    with pytest.raises(ValueError, match=r"^photo 3: the pocket finder \(SAM 3.1\) did not answer — "
                                         r"save that photo again$"):
        st.teach(photos)
    assert st.taught_at is None and not (tmp_path / "model.pt").exists()


def test_teach_names_every_photo_the_pocket_finder_missed(tmp_path):
    """Review: with SAM down on most photos the message named only the first grid photo, as if one photo were odd."""
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    photos = [make_strip(seed=i, shift=(i, 0)) for i in range(6)]
    st = Station(tmp_path, client=FailingSam(fail=lambda im: im is not photos[0]), clock=lambda: T0)
    with pytest.raises(ValueError, match=r"^photos 2, 3, 4, 5, 6: the pocket finder \(SAM 3.1\) did not answer — "
                                         r"save those photos again$"):
        st.teach(photos)


def test_teach_refuses_when_sam_answers_no_photo(tmp_path):
    approve(CHECKLIST, "Priya", tmp_path / "checklists.jsonl")
    photos = [make_strip(seed=i, shift=(i, 0)) for i in range(6)]
    st = Station(tmp_path, client=FailingSam(fail="all"), clock=lambda: T0)  # a key is set, SAM never answers
    with pytest.raises(ValueError, match=r"^the pocket finder \(SAM 3.1\) did not answer — check the network and "
                                         r"save the photos again$"):
        st.teach(photos)
    assert st.taught_at is None and not (tmp_path / "model.pt").exists()
    st.client.has_key = False  # no key at all: an offline product, taught on the checklist grid
    st.teach(photos)
    assert st.layout_source == "grid" and st.client.calls == 48  # four failed calls per photo ("pill pack" waits), per teach


def test_inspect_holds_when_the_taught_finder_does_not_answer(sam_station):
    st = sam_station
    assert not st.locked and st.inspect(make_strip(seed=15)).status == "PASS"
    for client in (FailingSam(fail="all"), None):
        st.client = client
        v = st.inspect(make_strip(seed=13, missing=(6,)))  # a real empty pocket: still never judged on the grid
        assert v.status == "HOLD" and v.reasons == [FINDER] and v.pocket_scores == [], v
    assert json.loads((st.dir / "log.jsonl").read_text(encoding="utf-8").splitlines()[-1])["reasons"] == [FINDER]


def test_grid_taught_product_stays_on_the_grid(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.client = FailingSam(fail="all")
    assert st.inspect(make_strip(seed=15)).status == "PASS"
    assert st.inspect(make_strip(seed=13, missing=(6,))).status == "REJECT"
    assert st.client.calls == 0  # never judged with a different pocket finder than the one taught


def test_self_test_does_not_run_without_its_finder(sam_station):
    st = sam_station
    before = (st.dir / "selftests.jsonl").read_text(encoding="utf-8")
    for fail, done in (("all", 0), (marked, 1)):  # good strip's layout fails; the 2nd and 3rd painted strips' fail
        for record in (True, "failures"):
            st.client = FailingSam(fail=fail)
            r = st.self_test(make_strip(seed=12), rng_seed=5, record=record)
            assert r["passed"] is False and r["not_run"] == NOT_RUN and r["good_passed"] is False, r
            assert r["failed"] == ["pocket finder did not answer"] and len(r["results"]) == done, r
            assert {"strip_found", "skipped"} <= r.keys()
    assert (st.dir / "selftests.jsonl").read_text(encoding="utf-8") == before and not st.locked


def test_self_test_judges_the_painted_strips_together_with_the_same_results(sam_station, monkeypatch):
    """Rehearsal: a self-test took about 4 minutes, one painted strip after another. They are judged together now
    (bounded pool); the results are the same, in the same order."""
    import d2i.station as station_mod
    st = sam_station
    monkeypatch.setattr(station_mod, "SELF_TEST_WORKERS", 1)
    one_by_one = st.self_test(make_strip(seed=12), rng_seed=5, record=False)
    monkeypatch.undo()
    judge, lock, now, most = st._judge, threading.Lock(), [0], [0]

    def watched(image, sid, name):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        try:
            time.sleep(0.2)
            return judge(image, sid, name)
        finally:
            with lock:
                now[0] -= 1
    monkeypatch.setattr(st, "_judge", watched)
    together = st.self_test(make_strip(seed=12), rng_seed=5, record=False)
    assert together == one_by_one and one_by_one["passed"] and most[0] >= 2, (most, together)


class SlowSam(ImageSam):
    def sam_image(self, image, prompt):
        time.sleep(1.5)
        return super().sam_image(image, prompt)


def test_trace_times_dinov2_alone(sam_station):
    """Rehearsal: the "dinov2-small judge" row said 22-26 s, because it timed the SAM 3.1 calls too."""
    from d2i import trace
    st = sam_station
    st.client = SlowSam()
    trace.clear()
    t0 = time.perf_counter()
    assert st.inspect(make_strip(seed=15)).status == "PASS"
    assert time.perf_counter() - t0 >= 1.5
    (row,) = [e for e in trace.recent() if e["model"] == "dinov2-small"]
    assert row["job"] == "judge" and row["ms"] < 1500, row


def test_reason_carries_severity(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    assert st._reason("wrong_tablet", 0)["severity"] == "critical"
    assert st._reason("tablet_spot", 1)["severity"] == "minor"
    assert st._reason("foil_damage")["severity"] == "major"
    assert st._unlisted(2)["severity"] == "major"
