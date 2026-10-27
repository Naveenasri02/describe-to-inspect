import pytest

from d2i.batch import CLEARANCE_MAX_AGE_S, Batches, line_clearance, parse_month
from d2i.meta_api import MetaError, SamObject


@pytest.mark.parametrize("s", ["10/2026", "10-2026", "10.2026", "OCT 2026", "Oct.2026", "10/26"])
def test_parse_month_formats(s):
    assert parse_month(s) == (2026, 10)


def test_parse_month_rejects_junk():
    with pytest.raises(ValueError, match="Use month/year, e.g. 10/2026"):
        parse_month("2026")


def clear_batches(tmp_path, now=1000.0):
    b = Batches(tmp_path)
    b.clearance({"clear": True, "found": []}, "qa1", now)
    return b


def test_start_needs_recent_clear_clearance(tmp_path):
    b = Batches(tmp_path)
    with pytest.raises(ValueError, match="line clearance"):
        b.start("B1", "01/2026", "01/2028", "45", "qa1", 1000.0)
    b.clearance({"clear": False, "found": [{"prompt": "pill", "count": 1}]}, "qa1", 1000.0)
    with pytest.raises(ValueError, match="not clear"):
        b.start("B1", "01/2026", "01/2028", "45", "qa1", 1010.0)
    b.clearance({"clear": True, "found": []}, "qa1", 2000.0)
    with pytest.raises(ValueError, match="older than 30 minutes"):
        b.start("B1", "01/2026", "01/2028", "45", "qa1", 2000.0 + CLEARANCE_MAX_AGE_S + 1)
    row = b.start("B1", "01/2026", "01/2028", "45", "qa1", 2060.0)
    assert (row["event"], row["batch"], row["clearance_at"]) == ("start", "B1", b.clearances()[-1]["at"])
    assert b.open == row


def test_start_validates_fields(tmp_path):
    b = clear_batches(tmp_path)
    with pytest.raises(ValueError):
        b.start("B1", "05/2026", "05/2026", "45", "qa1", 1060.0)
    with pytest.raises(ValueError):
        b.start("B1", "05/2026", "01/2026", "45", "qa1", 1060.0)
    with pytest.raises(ValueError):
        b.start("B1", "05/2026", "05/2028", "abc", "qa1", 1060.0)
    with pytest.raises(ValueError):
        b.start("", "05/2026", "05/2028", "45", "qa1", 1060.0)
    assert b.open is None
    row = b.start("b 12/x", "5-2026", "OCT 2028", "Rs. 45", "qa1", 1060.0)
    assert (row["batch"], row["mrp"], row["mfg"], row["exp"]) == ("B12/X", "45.00", "05/2026", "10/2028")


def test_one_batch_at_a_time(tmp_path):
    b = clear_batches(tmp_path)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0)
    with pytest.raises(ValueError, match="A batch is already open: B1"):
        b.start("B2", "01/2026", "01/2028", "45", "qa1", 1061.0)
    b.end("qa1", 1100.0)
    assert b.open is None
    b.start("B2", "01/2026", "01/2028", "45", "qa1", 1200.0)
    assert b.open["batch"] == "B2" and len(b.rows) == 3


def test_leak_and_end_need_open_batch(tmp_path):
    b = clear_batches(tmp_path)
    with pytest.raises(ValueError, match="No batch is open"):
        b.leak("pass", "qa1", "", 1060.0)
    with pytest.raises(ValueError, match="No batch is open"):
        b.end("qa1", 1060.0)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0)
    with pytest.raises(ValueError):
        b.leak("maybe", "qa1", "", 1070.0)
    r = b.leak("fail", "qa1", "bubbles at pocket 3", 1070.0)
    assert (r["event"], r["batch"], r["result"], r["note"]) == ("leak", "B1", "fail", "bubbles at pocket 3")


class FakeSam:
    def __init__(self, by_prompt):
        self.by_prompt = by_prompt

    def sam_image(self, image, prompt):
        return self.by_prompt.get(prompt, [])


def test_clearance_found_pill_not_clear():
    c = FakeSam({"pill": [SamObject(1, (0, 0, 5, 5), 0.9, None), SamObject(2, (0, 0, 5, 5), 0.2, None), SamObject(3, (0, 0, 5, 5), None, None)]})
    assert line_clearance(None, c) == {"clear": False, "found": [{"prompt": "pill", "count": 2}]}


def test_clearance_nothing_found_clear():
    assert line_clearance(None, FakeSam({"capsule": [SamObject(1, (0, 0, 5, 5), 0.3, None)]})) == {"clear": True, "found": []}


def test_clearance_without_client_refuses():
    class Down:
        def sam_image(self, image, prompt):
            raise MetaError("offline")
    for c in (None, Down()):
        with pytest.raises(ValueError, match="Line clearance needs SAM 3.1"):
            line_clearance(None, c)
