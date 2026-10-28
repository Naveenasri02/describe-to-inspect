import numpy as np

from d2i.meta_api import MetaError, SamObject
from d2i.pockets import Layout, find_layout, grid, pill_prompts, template_from
from tests.conftest import STRIP_BOX, make_strip, pocket_box

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
                        http=FakeHTTP([requests.ConnectionError("no route to api.meta.ai")] * 2))  # one retry
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
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and lay.strip_box == STRIP_BOX and lay.source == "sam"
    assert sam.prompts == ["blister strip", "blister pack", "pill"]


def test_pills_bound_the_strip_when_no_strip_prompt_works():
    sam = FakeSam([], _pills(range(10)))
    lay = find_layout(make_strip(), CL, sam)
    x0, y0, x1, y1 = (min(pocket_box(i)[0] for i in range(10)), min(pocket_box(i)[1] for i in range(10)),
                      max(pocket_box(i)[2] for i in range(10)), max(pocket_box(i)[3] for i in range(10)))
    px, py = int((x1 - x0) * 0.08), int((y1 - y0) * 0.08)
    assert lay.strip_box == (x0 - px, y0 - py, x1 + px, y1 + py)
    assert lay.source == "sam-pills" and lay.ok and len(lay.pockets) == 10
    assert sam.prompts == ["blister strip", "blister pack", "pill pack", "pill"]


def test_no_strip_and_no_pills_is_no_strip():
    lay = find_layout(make_strip(), CL, FakeSam([], []))
    assert lay.ok is False and lay.note == "no strip" and lay.source == "sam"


def test_pill_prompts_stop_at_first_that_finds_pills():
    for strips in ([(STRIP_BOX, 0.95)], []):
        sam = FakeSam(strips, _pills(range(10)))
        find_layout(make_strip(), CL, sam)
        assert sam.prompts.count("pill") == 1 and "white pill" not in sam.prompts


def test_white_pill_used_when_pill_finds_nothing():
    sam = FakeSam([(STRIP_BOX, 0.95)], [])
    sam.more["white pill"] = _pills(range(10))
    lay = find_layout(make_strip(), CL, sam)
    assert lay.ok and len(lay.pockets) == 10
    assert sam.prompts == ["blister strip", "pill", "white pill"]


def test_white_pill_not_called_when_pill_works():
    sam = FakeSam([(STRIP_BOX, 0.95)], _pills(range(10)))
    find_layout(make_strip(), CL, sam)
    assert "white pill" not in sam.prompts


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
