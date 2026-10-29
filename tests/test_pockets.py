import numpy as np
import pytest

from d2i.meta_api import MetaError, SamObject
from d2i.pockets import POCKET_PROMPTS, STRIP_PROMPTS, Layout, find_layout, grid, pill_prompts, template_from
from tests.conftest import STRIP_BOX, make_strip, pocket_box

ASKED = sorted(STRIP_PROMPTS + POCKET_PROMPTS)  # every prompt; the first two strip prompts and the pill prompts at once
FIRST = sorted(STRIP_PROMPTS[:2] + POCKET_PROMPTS)  # "pill pack", then "blister": asked only while no strip is found

CL = {"rows": 2, "per_row": 5, "count": 10, "item": "tablet"}


class FakeSam:
    def __init__(self, strips, pockets):
        self.answers = {"blister strip": strips, "pill": pockets}
        self.more = {}  # extra strip prompts, e.g. "blister pack"
        self.prompts = []

    def sam_image(self, image, prompt):
        self.prompts.append(prompt)
        return [SamObject(i, box, score, None) for i, (box, score) in enumerate(
            self.more.get(prompt) or self.answers.get(prompt, []))]


class BrokenSam:
    def sam_image(self, image, prompt):
        raise MetaError("Meta API 503: busy")


def test_grid_count_and_order():
    boxes = grid((0, 0, 500, 200), 2, 5)
    assert len(boxes) == 10
    assert boxes[0][0] < 100 and boxes[0][1] < 100       # first is top-left
    assert boxes[5][0] < 100 and boxes[5][1] >= 100      # sixth starts the bottom row
    assert boxes[1][0] > boxes[0][0]


def test_fallback_finds_strip_on_synthetic():
    img = make_strip()
    lay = find_layout(img, CL)
    assert isinstance(lay, Layout) and lay.source == "grid" and lay.ok and len(lay.pockets) == 10
    x0, y0, x1, y1 = lay.strip_box
    assert 0 <= x0 < x1 <= 640 and 0 <= y0 < y1 <= 360
    assert (x1 - x0) * (y1 - y0) >= 0.3 * 640 * 360
    assert abs(x0 - STRIP_BOX[0]) <= 3 and abs(y1 - STRIP_BOX[3]) <= 3


def test_sam_layout_sorted_row_major():
    shuffled = [7, 2, 9, 0, 5, 1, 8, 3, 6, 4]
    sam = FakeSam([((STRIP_BOX), 0.95)], [(pocket_box(i), 0.9) for i in shuffled])
    lay = find_layout(make_strip(), CL, sam)
    assert lay.source == "sam" and lay.ok and lay.strip_box == STRIP_BOX
    assert lay.pockets == [pocket_box(i) for i in range(10)]


def test_sam_count_mismatch_not_ok():
    sam = FakeSam([(STRIP_BOX, 0.95)], [(pocket_box(i), 0.9) for i in range(9)])
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok is False and "9" in lay.note and lay.source == "sam"


def test_sam_drops_low_score_and_outside_pockets():
    pockets = [(pocket_box(i), 0.9) for i in range(10)] + [((5, 5, 40, 40), 0.9), (pocket_box(3), 0.3)]
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], pockets))
    assert lay.ok and len(lay.pockets) == 10


def test_empty_frame_not_ok():
    lay = find_layout(np.full((360, 640, 3), 128, np.uint8), CL)
    assert lay.ok is False and lay.note == "no strip"


def test_meta_error_falls_back_to_grid():
    lay = find_layout(make_strip(), CL, BrokenSam())
    assert lay.source == "grid" and lay.ok


def test_network_error_falls_back_to_grid(tmp_path):
    import requests
    from d2i.meta_api import Budget, MetaClient
    from tests.conftest import FakeHTTP
    client = MetaClient(Budget(tmp_path / "s.json"), key="k",
                        http=FakeHTTP([requests.ConnectionError("no route to api.meta.ai")] * 8))  # 4 calls at once, 1 retry
    lay = find_layout(make_strip(), CL, client)
    assert lay.source == "grid" and lay.ok


def _pills(idx, strip=STRIP_BOX):
    return [(pocket_box(i, strip=strip), 0.9) for i in idx]


def test_teach_layout_uses_pills():
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills([7, 2, 9, 0, 5, 1, 8, 3, 6, 4])))
    assert lay.ok and lay.pockets == [pocket_box(i) for i in range(10)]


def test_teach_layout_wrong_pill_count_not_ok():
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills(range(9))))
    assert lay.ok is False and "9" in lay.note


def _template():
    return template_from([find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills(range(10))))])


def test_template_maps_pockets_onto_new_strip():
    moved = (110, 70, 590, 290)
    lay = find_layout(make_strip(shift=(30, 0)), CL, FakeSam([(moved, 0.95)], _pills(range(10), moved)), _template())
    assert lay.ok and len(lay.pockets) == 10
    for got, want in zip(lay.pockets, [pocket_box(i) for i in range(10)]):
        assert all(abs(g - (w + (30 if k % 2 == 0 else 0))) <= 2 for k, (g, w) in enumerate(zip(got, want)))


def test_empty_pocket_with_template_stays_ok():
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills([i for i in range(10) if i != 6])), _template())
    assert lay.ok and len(lay.pockets) == 10


def test_extra_object_outside_pattern_holds():
    stray = ((267, 130, 277, 140), 0.9)  # between pocket columns
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills(range(9)) + [stray]), _template())
    assert lay.ok is False and lay.note == "object outside the pocket pattern"


def test_second_strip_prompt_used_when_first_empty():
    sam = FakeSam([], _pills(range(10)))
    sam.more["blister pack"] = [(STRIP_BOX, 0.9)]
    sam.more["pill pack"] = [((0, 0, 640, 360), 0.9)]  # a later prompt: not asked
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and lay.strip_box == STRIP_BOX and lay.source == "sam"
    assert sorted(sam.prompts) == FIRST


def test_blister_is_the_last_strip_fallback():
    """Real photos (29 Oct): "blister" found strips the chain missed (wa0, wa3, wa7, 2626, 2640), but alone it missed
    4/25 tablet-side photos, so it is asked last, and only when "pill pack" found nothing either."""
    assert STRIP_PROMPTS[-1] == "blister" and STRIP_PROMPTS[:3] == ("blister strip", "blister pack", "pill pack")
    sam = FakeSam([], _pills(range(10)))
    sam.more["blister"] = [(STRIP_BOX, 0.6)]
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and lay.strip_box == STRIP_BOX and lay.source == "sam"
    assert sam.prompts[-2:] == ["pill pack", "blister"]
    sam = FakeSam([], _pills(range(10)))
    sam.more["pill pack"] = [(STRIP_BOX, 0.9)]
    sam.more["blister"] = [((0, 0, 640, 360), 0.9)]  # not asked: "pill pack" found the strip
    lay = find_layout(make_strip(), CL, sam)
    assert lay.strip_box == STRIP_BOX and "blister" not in sam.prompts


def test_pills_bound_the_strip_when_no_strip_prompt_works():
    sam = FakeSam([], _pills(range(10)))
    lay = find_layout(make_strip(), CL, sam)
    x0, y0, x1, y1 = (min(pocket_box(i)[0] for i in range(10)), min(pocket_box(i)[1] for i in range(10)),
                      max(pocket_box(i)[2] for i in range(10)), max(pocket_box(i)[3] for i in range(10)))
    px, py = int((x1 - x0) * 0.08), int((y1 - y0) * 0.08)
    assert lay.strip_box == (x0 - px, y0 - py, x1 + px, y1 + py)
    assert lay.source == "sam-pills" and lay.ok and len(lay.pockets) == 10
    assert sorted(sam.prompts) == ASKED


def test_no_strip_and_no_pills_is_no_strip():
    lay = find_layout(make_strip(), CL, FakeSam([], []))
    assert lay.ok is False and lay.note == "no strip" and lay.source == "sam"


def test_first_pill_prompt_that_finds_pills_is_used():
    for strips in ([(STRIP_BOX, 0.95)], []):
        sam = FakeSam(strips, _pills(range(10)))
        sam.more["white pill"] = _pills(range(9))
        lay = find_layout(make_strip(), CL, sam)
        assert lay.ok and len(lay.pills) == 10 and sorted(sam.prompts) == (FIRST if strips else ASKED)


def test_white_pill_used_when_pill_finds_nothing():
    sam = FakeSam([(STRIP_BOX, 0.95)], [])
    sam.more["white pill"] = _pills(range(10))
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and len(lay.pockets) == 10


def test_layout_asks_every_prompt_at_once_and_each_call_is_paid_and_traced(tmp_path):
    """Rehearsal: strip prompts then pill prompts one after another, 7.3 s each, about 30 s per inspect. They go out
    together now (none gets past the barrier until all four are in flight; "pill pack" is not needed here); the answers
    are taken in the same order as before; every call reserves and settles its price; each trace row shows its own."""
    import threading
    from d2i import trace
    from d2i.meta_api import SAM_IMAGE, Budget, MetaClient
    from tests.conftest import FakeResponse, responses_body, sam_text

    answers = {"blister strip": [], "blister pack": [(0, STRIP_BOX, 0.95)], "pill pack": [(0, (0, 0, 640, 360), 0.9)],
               "pill": [(i, pocket_box(i), 0.9) for i in range(10)], "white pill": [(0, pocket_box(0), 0.9)]}

    class PromptHTTP:
        def __init__(self):
            self.barrier, self.prompts = threading.Barrier(4, timeout=3), []

        def post(self, url, headers=None, json=None, timeout=None, **kw):
            prompt = json["input"][0]["content"][0]["text"]
            self.prompts.append(prompt)
            self.barrier.wait()  # breaks unless all four calls are in flight together
            return FakeResponse(200, responses_body(sam_text(answers[prompt])))

    trace.clear()
    http, budget = PromptHTTP(), Budget(tmp_path / "spend.json")
    lay = find_layout(make_strip(), CL, MetaClient(budget, key="k", http=http))
    assert lay.ok and lay.strip_box == STRIP_BOX and lay.pockets == [pocket_box(i) for i in range(10)]
    assert sorted(http.prompts) == FIRST and budget.spent == pytest.approx(4 * SAM_IMAGE)
    rows = [e for e in trace.recent() if e["model"] == "sam-3.1"]
    assert len(rows) == 4 and all(e["usd"] == pytest.approx(SAM_IMAGE) for e in rows), rows


def _down(prompt):
    class OneDown(FakeSam):
        def sam_image(self, image, p):
            if p == prompt:
                raise MetaError("Meta API 503: busy")
            return super().sam_image(image, p)
    return OneDown


def test_a_failed_prompt_whose_answer_is_not_needed_does_not_matter():
    lay = find_layout(make_strip(), CL, _down("blister pack")([(STRIP_BOX, 0.95)], _pills(range(10))))
    assert lay.source == "sam" and lay.ok  # "blister strip" answered first: "blister pack" was not needed


def test_a_failed_pill_prompt_is_the_finder_down():
    """Every pill prompt's answer is used now: without one, a tablet only it outlines would read as an empty pocket."""
    lay = find_layout(make_strip(), CL, _down("white pill")([(STRIP_BOX, 0.95)], _pills(range(10))))
    assert lay.source == "grid"


def test_every_pill_prompt_is_used_and_the_same_tablet_counts_once():
    """Real photo wa5: "pill" found none of 3 white tablets, "white pill" all 3; a coloured odd tablet among white ones
    is outlined by "pill" only. The answers are merged: a box on a tablet already outlined (IoU >= 0.5) is dropped."""
    sam = FakeSam([(STRIP_BOX, 0.95)], _pills([4]))  # "pill": the odd (coloured) tablet only
    nudged = [((b[0] + 3, b[1] + 2, b[2] + 3, b[3] + 2), s) for b, s in _pills([4])]
    sam.more["white pill"] = _pills([i for i in range(10) if i != 4]) + nudged  # the white ones, the odd one again
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and lay.pockets == [pocket_box(i) for i in range(10)] and len(lay.pills) == 10


def test_capsule_prompts():
    assert pill_prompts("Capsule") == ("capsule", "pill") and pill_prompts("tablet") == ("pill", "white pill")
    sam = FakeSam([(STRIP_BOX, 0.95)], [])
    sam.answers["capsule"] = _pills(range(10))
    cl = dict(CL, item="capsule")
    lay = find_layout(make_strip(), cl, sam)
    assert lay.ok and len(lay.pockets) == 10 and "white pill" not in sam.prompts


def test_template_extra_pill_inside_pocket_is_ok():
    inner = (pocket_box(3)[0] + 8, pocket_box(3)[1] + 8, pocket_box(3)[0] + 30, pocket_box(3)[1] + 30)
    lay = find_layout(make_strip(), CL, FakeSam([(STRIP_BOX, 0.95)], _pills(range(10)) + [(inner, 0.9)]), _template())
    assert lay.ok and lay.note == "" and len(lay.pockets) == 10 and len(lay.pills) == 11


def _turned(turn, strip=STRIP_BOX):
    """(the turned strip's upright box, its pill boxes) for a strip turned by `turn` degrees about its centre."""
    import cv2
    m = cv2.getRotationMatrix2D(((strip[0] + strip[2]) / 2, (strip[1] + strip[3]) / 2), turn, 1.0)
    corners = [m @ (x, y, 1) for x in strip[::2] for y in strip[1::2]]
    box = tuple(int(f(c[k] for c in corners)) for f, k in ((min, 0), (min, 1), (max, 0), (max, 1)))
    pills = []
    for i in range(10):
        b = pocket_box(i, strip=strip)
        cx, cy = m @ ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, 1)
        hw, hh = (b[2] - b[0]) // 2, (b[3] - b[1]) // 2
        pills.append(((round(cx) - hw, round(cy) - hh, round(cx) + hw, round(cy) + hh), 0.9))
    return box, pills


def test_turned_strip_pockets_follow_the_pills():
    """A hand-placed strip turned 3 degrees: each pocket is the pill found in it (as teach cropped it), not the taught
    box stretched over the upright strip box; the turn is measured from the pill centres."""
    box, pills = _turned(3.0)
    lay = find_layout(make_strip(), CL, FakeSam([(box, 0.95)], pills), _template())
    assert lay.ok and lay.pockets == [b for b, _ in pills] and len(lay.slots) == 10
    assert lay.in_pocket == [[b] for b, _ in pills] and abs(np.degrees(lay.angle) + 3.0) < 0.2, lay.angle
    empty = find_layout(make_strip(), CL, FakeSam([(box, 0.95)], pills[:6] + pills[7:]), _template())
    assert empty.ok and empty.pockets[6] == empty.slots[6] and empty.in_pocket[6] == []  # no pill: the taught box


def test_strip_box_at_the_frame_edge_is_not_straightened():
    """A close-up: SAM's strip box is the photo frame, so it is not the turned strip's upright box."""
    frame = ((0, 0, 640, 360), 0.95)
    template = template_from([find_layout(make_strip(), CL, FakeSam([frame], _pills(range(10))))])  # taught on it too
    _, pills = _turned(3.0)
    lay = find_layout(make_strip(), CL, FakeSam([frame], pills), template)
    assert lay.ok and lay.pockets == [b for b, _ in pills] and lay.angle == 0.0


def test_close_up_guard_counts_in_the_pixels_sam_saw():
    """SAM sees a copy at most 1280 px long: on a 1280 x 3487 photo one of its pixels is 2.7 of ours, so a strip box
    "at the frame edge" can be 2-5 px in. The guard allows 2 of SAM's pixels, not 1 of ours."""
    from d2i.pockets import framed
    tall = (3487, 1280, 3)
    assert framed((3, 4, 1277, 3483), tall) and not framed((12, 12, 1268, 3475), tall)
    assert framed((1, 70, 560, 290), (360, 640, 3)) and not framed((3, 70, 560, 290), (360, 640, 3))
