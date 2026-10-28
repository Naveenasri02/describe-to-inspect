import cv2
import numpy as np

from d2i.measure import features, pills_in, pocket_check, strip_check, strip_features, teach_profile, teach_strip
from d2i.pockets import Layout

POCKET = (20, 20, 120, 120)   # 100 x 100


def disc(img, cx, cy, r, bgr=(235, 235, 235)):
    cv2.circle(img, (cx, cy), r, bgr, -1)
    return (cx - r, cy - r, cx + r, cy + r)


def foil():
    return np.full((140, 140, 3), 170, np.uint8)


def one(r=30, bgr=(235, 235, 235)):
    img = foil()
    return img, disc(img, 70, 70, r, bgr)


def profile(n=6):
    layouts, images = [], []
    for _ in range(n):
        img, box = one()
        images.append(img)
        layouts.append(Layout((0, 0, 140, 140), [POCKET], "sam", True, "", [box]))
    return teach_profile(images, layouts)


def test_features_size_and_colour():
    img, box = one(30)
    f = features(img, POCKET, box)
    assert abs(f["size"] - 0.6) < 0.01 and abs(f["aspect"] - 1) < 0.01 and abs(f["area"] - 0.36) < 0.01
    assert f["lab"][0] > 85 and abs(f["lab"][1]) < 5 and f["spot"] == 0


def test_teach_profile_floors():
    p = profile()
    assert abs(p["size_tol"] - 0.12 * p["size_med"]) < 1e-9
    assert abs(p["aspect_tol"] - 0.15 * p["aspect_med"]) < 1e-9
    assert p["colour_tol"] == 12.0 and p["spot_max"] == 0.01 and len(p["lab_med"]) == 3


def test_teach_profile_needs_five_pills():
    assert profile(4) is None and profile(5) is not None


def test_two_pills_is_double():
    img = foil()
    pills = [disc(img, 50, 70, 15), disc(img, 90, 70, 15)]
    assert pocket_check(img, POCKET, pills, profile()) == "double_tablet"


def test_large_area_is_double():
    img, box = one(int(30 * 1.8 ** 0.5))
    assert pocket_check(img, POCKET, [box], profile()) == "double_tablet"


def test_small_pill_is_wrong():
    img, box = one(21)
    assert pocket_check(img, POCKET, [box], profile()) == "wrong_tablet"


def test_colour_shift_is_wrong():
    img, box = one(30, (235, 235, 235))
    img2, box2 = one(30, (235, 190, 150))   # bluish-pink tint, about 30 delta E
    assert pocket_check(img2, POCKET, [box2], profile()) == "wrong_tablet"


def test_dark_dot_is_spot():
    img, box = one()
    cv2.circle(img, (75, 66), 5, (30, 30, 30), -1)
    assert pocket_check(img, POCKET, [box], profile()) == "tablet_spot"


def test_normal_pill_is_none():
    img, box = one()
    assert pocket_check(img, POCKET, [box], profile()) is None


def test_no_pill_is_none():
    assert pocket_check(foil(), POCKET, [], profile()) is None
    assert pills_in(POCKET, [(200, 200, 220, 220)]) == []


def test_features_degenerate_box_returns_none():
    img, _ = one()
    assert features(img, POCKET, (200, 200, 230, 230)) is None   # outside the image
    assert features(img, POCKET, (50, 70, 90, 70)) is None       # zero height
    assert pocket_check(img, POCKET, [(200, 200, 230, 230)], profile()) is None


STRIP = (0, 0, 400, 100)  # aspect 4


def slay(left=0.1, top=0.2, right=0.1, bottom=0.2, strip=STRIP):
    w, h = strip[2] - strip[0], strip[3] - strip[1]
    pill = (strip[0] + left * w, strip[1] + top * h, strip[2] - right * w, strip[3] - bottom * h)
    return Layout(strip, [pill], "sam", True, "", [pill])  # one pocket holding the one pill


def test_strip_features_margins():
    f = strip_features(slay())
    assert abs(f["aspect"] - 4) < 1e-9
    assert all(abs(a - b) < 1e-9 for a, b in zip(f["margins"], (0.1, 0.2, 0.1, 0.2)))
    assert strip_features(Layout(STRIP, [], "grid", True)) is None


def test_teach_strip_floors():
    p = teach_strip([slay() for _ in range(5)])
    assert abs(p["aspect_tol"] - 0.06 * 4) < 1e-9 and p["margin_tol"] == [0.04] * 4
    assert abs(p["aspect_med"] - 4) < 1e-9 and len(p["margin_med"]) == 4
    assert teach_strip([slay(), Layout(STRIP, [], "grid", True)]) is None


def test_short_strip_is_cut():
    p = teach_strip([slay() for _ in range(5)])
    assert not strip_check(slay(), p)
    assert strip_check(slay(strip=(0, 0, 320, 100)), p)  # aspect -20%


def test_shifted_pattern_is_cut():
    p = teach_strip([slay() for _ in range(5)])
    assert strip_check(slay(left=0.2), p)


def test_features_box_partly_outside_returns_none():
    img = foil()  # 140 x 140
    for box in [(-10, 40, 30, 80), (40, 120, 80, 150), (40, -50, 80, -10)]:
        assert features(img, POCKET, box) is None, box


def test_margins_only_when_every_pocket_has_one_pill():
    full = slay()
    empty = Layout(STRIP, full.pockets + [(300, 40, 340, 60)], "sam", True, "", full.pills)  # pocket without a pill
    double = Layout(STRIP, full.pockets, "sam", True, "", full.pills * 2)
    for lay in (empty, double):
        assert strip_features(lay)["margins"] is None
        assert abs(strip_features(lay)["aspect"] - 4) < 1e-9
    p = teach_strip([slay() for _ in range(5)])
    assert not strip_check(Layout(STRIP, empty.pockets, "sam", True, "", slay(left=0.5).pills), p)  # margin off, pocket empty
    assert strip_check(Layout((0, 0, 320, 100), empty.pockets, "sam", True, "", full.pills), p)  # aspect still caught


def test_teach_strip_skips_margins_of_photos_with_a_bad_pocket():
    empty = Layout(STRIP, slay().pockets + [(300, 40, 340, 60)], "sam", True, "", slay().pills)
    p = teach_strip([slay() for _ in range(4)] + [empty])
    assert p["margin_tol"] == [0.04] * 4 and all(abs(a - b) < 1e-9 for a, b in zip(p["margin_med"], (0.1, 0.2, 0.1, 0.2)))
    p = teach_strip([empty, empty])
    assert p["margin_med"] is None and not strip_check(slay(left=0.5), p) and strip_check(slay(strip=(0, 0, 320, 100)), p)
