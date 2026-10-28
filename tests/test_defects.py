import numpy as np
import pytest

from d2i.checklist import CHECKS
from d2i.defects import paint
from tests.conftest import STRIP_BOX, make_strip, pocket_box


def padded(box, pad, shape):
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * pad, (y1 - y0) * pad
    return (max(0, int(x0 - dx)), max(0, int(y0 - dy)), min(shape[1], int(x1 + dx)), min(shape[0], int(y1 + dy)))


@pytest.mark.parametrize("kind", ["empty_pocket", "broken_tablet", "foil_damage"])
@pytest.mark.parametrize("pocket", [0, 7])  # an edge pocket in each row
def test_each_kind_changes_only_near_pocket(kind, pocket):
    img = make_strip()
    box = pocket_box(pocket)
    out = paint(img, box, kind, STRIP_BOX, np.random.default_rng(1))
    diff = np.abs(out.astype(int) - img.astype(int)).max(axis=2)
    x0, y0, x1, y1 = padded(box, 0.3, img.shape)
    near = np.zeros(diff.shape, bool)
    near[y0:y1, x0:x1] = True
    assert (diff[near] > 20).sum() >= 300, "the defect must be clearly visible"
    assert (diff[~near] > 20).sum() == 0 and diff[~near].mean() < 0.5, "nothing changes away from the pocket"


def test_empty_pocket_looks_like_foil():
    img = make_strip()
    x0, y0, x1, y1 = box = pocket_box(3)
    out = paint(img, box, "empty_pocket", STRIP_BOX, np.random.default_rng(2))
    assert abs(out[y0:y1, x0:x1].mean() - 170) < 15
    assert (out[y0:y1, x0:x1] > 220).mean() < 0.01  # no white tablet left


def test_paint_is_seeded():
    img = make_strip()
    a = paint(img, pocket_box(5), "broken_tablet", STRIP_BOX, np.random.default_rng(7))
    b = paint(img, pocket_box(5), "broken_tablet", STRIP_BOX, np.random.default_rng(7))
    assert np.array_equal(a, b) and not np.array_equal(a, img)


@pytest.mark.parametrize("kind", ["wrong_tablet", "tablet_spot"])
@pytest.mark.parametrize("pocket", [0, 7])
def test_tablet_kinds_change_only_inside_pocket(kind, pocket):
    img = make_strip()
    x0, y0, x1, y1 = box = pocket_box(pocket)
    out = paint(img, box, kind, STRIP_BOX, np.random.default_rng(1))
    diff = np.abs(out.astype(int) - img.astype(int)).max(axis=2)
    inside = np.zeros(diff.shape, bool)
    inside[y0:y1, x0:x1] = True
    assert (diff[inside] > 20).sum() >= 50, "the defect must be clearly visible"
    assert (diff[~inside] > 0).sum() == 0


def test_paint_strip_cut_changes_end_only():
    img = make_strip()
    out = paint(img, pocket_box(0), "strip_cut", STRIP_BOX, np.random.default_rng(1))
    diff = np.abs(out.astype(int) - img.astype(int)).max(axis=2)
    cut = STRIP_BOX[2] - 0.15 * (STRIP_BOX[2] - STRIP_BOX[0])
    assert (diff[:, :int(cut) - 1] > 0).sum() == 0, "only the end changes"
    assert (diff[:, int(cut):] > 20).sum() >= 300
    assert abs(out[STRIP_BOX[1]:STRIP_BOX[3], int(cut) + 2:STRIP_BOX[2]].mean() - np.median(img[:20])) < 15


@pytest.mark.parametrize("pocket", [2, 4])  # a middle pocket and one at the strip's end
def test_double_tablet_shows_past_a_pocket_box_that_hugs_the_tablet(pocket):
    """Rehearsal p17 pocket 5: the pocket box is SAM's pill box, which hugs one tablet; the copy clipped to it was a
    tablet with a straight cut edge, SAM saw one normal pill and the station named it broken. Two tablets in one
    pocket overlap past that box; the copy stays on the strip and in the pocket's rows."""
    img = make_strip()
    cx, cy = 128 + 96 * pocket, 125  # make_strip tablet centre, radius 30
    x0, y0, x1, y1 = box = (cx - 39, cy - 39, cx + 39, cy + 39)  # an ImageSam / SAM pill box
    out = paint(img, box, "double_tablet", STRIP_BOX, np.random.default_rng(1))
    diff = np.abs(out.astype(int) - img.astype(int)).max(axis=2)
    assert (diff[y0:y1, x1:] > 20).sum() >= 300, "the second tablet shows past the box"
    assert (diff[:, :x0] > 0).sum() == 0 and (diff[:y0] > 0).sum() == 0 and (diff[y1:] > 0).sum() == 0
    assert (diff[:, STRIP_BOX[2]:] > 0).sum() == 0, "kept on the strip"
