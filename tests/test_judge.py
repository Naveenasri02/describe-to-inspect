import pytest

from d2i.judge import Judge
from d2i.pockets import Layout, find_layout
from tests.conftest import make_strip

CL = {"rows": 2, "per_row": 5, "count": 10}


def good(i):
    return make_strip(seed=i, shift=(i, 0))


@pytest.fixture(scope="module")
def taught():
    judge = Judge()
    images = [good(i) for i in range(6)]
    info = judge.teach(images, [find_layout(im, CL) for im in images])
    return judge, info


def test_teach_needs_five():
    images = [good(i) for i in range(4)]
    with pytest.raises(ValueError, match="5"):
        Judge().teach(images, [find_layout(im, CL) for im in images])


def test_teach_names_bad_photo(taught):
    judge, _ = taught
    images = [good(i) for i in range(6)]
    layouts = [find_layout(im, CL) for im in images]
    l4 = layouts[3]
    layouts[3] = Layout(l4.strip_box, l4.pockets[:9], "sam", False, "found 9 pockets, checklist says 10")
    with pytest.raises(ValueError, match="photo 4"):
        Judge().teach(images, layouts)


def test_good_strip_scores_below_threshold(taught):
    judge, info = taught
    img = make_strip(seed=6, shift=(3, 2))
    lay = find_layout(img, CL)
    assert all(s < info["pocket_threshold"] for s in judge.score_pockets(img, lay))
    assert judge.score_border(img, lay) < info["border_threshold"]


def test_missing_and_cracked_pockets_score_above(taught):
    judge, info = taught
    img = make_strip(seed=8, missing=(3,), cracked=(7,))
    scores = judge.score_pockets(img, find_layout(img, CL))
    above = {i for i, s in enumerate(scores) if s > info["pocket_threshold"]}
    assert above == {3, 7}


def test_torn_corner_border_above(taught):
    judge, info = taught
    img = make_strip(seed=9, torn=True)
    assert judge.score_border(img, find_layout(img, CL)) > info["border_threshold"]


def test_pocket_embeddings_one_per_pocket(taught):
    judge, _ = taught
    img = good(1)
    emb = judge.pocket_embeddings(img, find_layout(img, CL))
    assert tuple(emb.shape) == (10, judge.dim)


def test_save_and_load_keep_scores(taught, tmp_path):
    judge, info = taught
    judge.save(tmp_path / "model.pt")
    again = Judge.load(tmp_path / "model.pt")
    img = make_strip(seed=8, missing=(3,))
    lay = find_layout(img, CL)
    assert again.pocket_threshold == pytest.approx(info["pocket_threshold"])
    assert again.score_pockets(img, lay) == pytest.approx(judge.score_pockets(img, lay), abs=1e-4)
