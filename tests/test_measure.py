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


def test_crack_line_is_left_to_the_judge():
    img, box = one()
    cv2.line(img, (45, 72), (95, 66), (40, 40, 40), 3)  # a long dark line across the tablet: not a spot, same colour
    assert pocket_check(img, POCKET, [box], profile()) is None


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


def lab_bgr(L, a, b):
    return tuple(int(round(v * 255)) for v in cv2.cvtColor(np.float32([[[L, a, b]]]), cv2.COLOR_LAB2BGR)[0, 0])


PINK, SILVER = lab_bgr(91.1, 7.6, -1.8), lab_bgr(75.7, -1.0, -10.0)  # rehearsal web17: taught tablet, foil ring


def test_foil_coloured_pill_is_an_empty_pocket():
    """Rehearsal p17 pocket 8: SAM outlined the painted empty pocket as a pill of Lab (75.4, -1.2, -9.5), 19.6 from
    the taught tablet and 0.6 from the foil around it, and the station named it a different tablet (Rule 5)."""
    layouts, images = [], []
    for _ in range(6):
        img = np.full((140, 140, 3), SILVER, np.uint8)
        layouts.append(Layout((0, 0, 140, 140), [POCKET], "sam", True, "", [disc(img, 70, 70, 30, PINK)]))
        images.append(img)
    prof = teach_profile(images, layouts)
    box = (40, 40, 100, 100)
    bare = np.full((140, 140, 3), SILVER, np.uint8)  # the painted empty pocket: foil colour where the tablet was
    assert pocket_check(bare, POCKET, [box], prof) == "empty_pocket"
    dome = bare.copy()  # a real empty pocket: the clear dome over the foil, a little lighter, with a highlight
    disc(dome, 70, 70, 30, lab_bgr(79.0, -1.0, -9.0))
    cv2.ellipse(dome, (70, 70), (27, 27), 0, 200, 250, (250, 250, 250), 2)
    assert pocket_check(dome, POCKET, [box], prof) == "empty_pocket"
    tablet = bare.copy()
    disc(tablet, 70, 70, 30, PINK)
    assert pocket_check(tablet, POCKET, [box], prof) is None
    other = bare.copy()  # a tablet of another colour is still a different tablet
    disc(other, 70, 70, 30, lab_bgr(85.0, 5.0, 45.0))
    assert pocket_check(other, POCKET, [box], prof) == "wrong_tablet"


def test_a_real_empty_dome_is_empty_when_the_judge_says_so():
    """Real empty domes sit a median 17 Lab dE from the foil ring (100 Roboflow photos), past the 12 tolerance, so the
    colour test named them a different tablet (critical). Colour alone may not call them empty either: real peach,
    orange and gold tablets read nearer their foil ring than a white taught tablet. Empty = the judge failed the pocket
    and named it empty, and its colour is nearer the foil than the taught tablet."""
    img = foil()
    box = disc(img, 70, 70, 30, (120, 120, 120))  # the dome shows the table: 19 dE from the foil, 42 from the tablet
    assert pocket_check(img, POCKET, [box], profile()) == "wrong_tablet"
    assert pocket_check(img, POCKET, [box], profile(), judge_empty=True) == "empty_pocket"
    img, box = one()  # a good tablet: the judge's word alone does not make it empty
    assert pocket_check(img, POCKET, [box], profile(), judge_empty=True) is None


def test_teach_photo_with_an_outlined_empty_dome_is_found():
    from d2i.measure import empty_looking
    images, layouts = [], []
    for k in range(5):
        img, box = one()
        images.append(img)
        layouts.append(Layout((0, 0, 140, 140), [box], "sam", True, "", [box]))
    assert empty_looking(images, layouts) is None
    images[3] = foil()  # SAM outlined the bare dome where the tablet should be
    assert empty_looking(images, layouts) == (4, 1)


def test_an_empty_dome_that_only_differs_in_colour_is_empty_when_the_judge_is_sure():
    """Bench 3 Nov: a corner dome showed the table through the plastic, 16 dE from the tablet and 25 from the foil.
    Same size and shape as a pocket, so with the judge sure it is empty, it is empty, not a different tablet."""
    img = foil()
    box = disc(img, 70, 70, 30, (205, 200, 225))  # pocket-sized, off the tablet's colour, far from the foil too
    assert pocket_check(img, POCKET, [box], profile()) == "wrong_tablet"
    assert pocket_check(img, POCKET, [box], profile(), judge_empty=True) == "empty_pocket"




def test_a_tablet_coloured_like_the_foil_ring_is_not_empty_when_the_judge_passed_it():
    # a lamp made the silver foil read nearer the tablet than the taught tablet does (bench 3 Nov: 2.2 vs 4.8 dE)
    img = np.full((140, 140, 3), 222, np.uint8)  # foil, lit to near the tablet's colour
    box = disc(img, 70, 70, 30, (224, 224, 224))  # the taught tablet is (235, 235, 235): about 4 dE away
    p = profile()
    assert pocket_check(img, POCKET, [box], p) == "empty_pocket"  # judge failed it too: an outlined dome, as before
    assert pocket_check(img, POCKET, [box], p, judge_failed=False) is None  # the judge passed it: a tablet
