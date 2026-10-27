import cv2
import numpy as np

from d2i.measure import features, pills_in, pocket_check, teach_profile
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
