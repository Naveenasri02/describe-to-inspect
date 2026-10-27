import json
import shutil

import numpy as np
import pytest

from d2i.checklist import CHECKS, approve
from d2i.judge import Judge
from d2i.meta_api import SamObject
from d2i.defects import paint
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
        boxes = [STRIP_BOX] if prompt == "blister strip" else [pocket_box(i) for i in range(10)] + [(267, 130, 277, 140)]
        return [SamObject(i, b, 0.9, None) for i, b in enumerate(boxes)]


def test_count_mismatch_holds(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.client = ElevenPills()
    v = st.inspect(make_strip(seed=16))
    assert v.status == "HOLD" and v.reasons[0]["rule"] == 1 and "outside the pocket pattern" in v.reasons[0]["text"]


def test_unlisted_defect_is_held_not_passed(station):
    st, _ = station
    st.self_test(make_strip(seed=12), rng_seed=5)
    st.checklist = dict(st.checklist, reject_if=["foil_damage"])
    v = st.inspect(make_strip(seed=14, cracked=(2,)))
    assert v.status == "HOLD" and v.reasons[0] == {"rule": None, "check": "unlisted_anomaly", "pocket": 3,
                                                   "text": "Pocket 3 looks unusual — check by hand"}


def test_empty_pocket_always_breaks_rule_1(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=["broken_tablet"])
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert [x["kind"] for x in r["results"]] == ["broken_tablet", "empty_pocket"] and r["passed"], r
    v = st.inspect(make_strip(seed=17, missing=(1,)))
    assert v.status == "REJECT"
    assert {"rule": 1, "check": "empty_pocket", "pocket": 2, "text": "Rule 1 failed: pocket 2 is empty"} in v.reasons


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


def test_strip_cut_skipped_without_profile(station):
    st, _ = station
    st.checklist = dict(st.checklist, reject_if=list(CHECKS))
    r = st.self_test(make_strip(seed=12), rng_seed=5)
    assert "strip_cut" in r["skipped"] and st.inspect(make_strip(seed=13)).skipped.count("strip size") == 1
