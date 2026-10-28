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
    b.clearance({"clear": True, "found": []}, "qa1", 1150.0)  # one clearance per batch
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


@pytest.mark.parametrize("raw,want", [("1,250", "1250.00"), ("Rs. 45", "45.00"), ("₹ 24.50", "24.50"), ("45/-", "45.00")])
def test_mrp_thousands_and_symbols(tmp_path, raw, want):
    assert clear_batches(tmp_path).start("B1", "01/2026", "01/2028", raw, "qa1", 1060.0)["mrp"] == want


@pytest.mark.parametrize("raw", ["45-50", "abc", "0", "4 5 x", "1.2.3"])
def test_mrp_refuses_ranges_and_text(tmp_path, raw):
    with pytest.raises(ValueError, match="MRP must be one price, e.g. 24.50"):
        clear_batches(tmp_path).start("B1", "01/2026", "01/2028", raw, "qa1", 1060.0)


def test_clearance_age_exactly_1800_accepted(tmp_path):
    b = clear_batches(tmp_path)
    assert b.start("B1", "01/2026", "01/2028", "45", "qa1", 1000.0 + CLEARANCE_MAX_AGE_S)["batch"] == "B1"


def test_clearance_before_last_batch_end_refused(tmp_path):
    b = clear_batches(tmp_path)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0)
    b.end("qa1", 1100.0)
    with pytest.raises(ValueError, match="Run line clearance again for this batch"):
        b.start("B2", "01/2026", "01/2028", "45", "qa1", 1200.0)
    b.clearance({"clear": True, "found": []}, "qa1", 1300.0)
    assert b.start("B2", "01/2026", "01/2028", "45", "qa1", 1310.0)["batch"] == "B2"


def test_batch_number_cannot_repeat(tmp_path):
    b = clear_batches(tmp_path)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0)
    b.end("qa1", 1100.0)
    b.clearance({"clear": True, "found": []}, "qa1", 1300.0)
    with pytest.raises(ValueError, match="Batch no. B1 was already used"):
        b.start("b 1", "01/2026", "01/2028", "45", "qa1", 1310.0)


def test_mrp_space_between_digits_refused(tmp_path):
    for bad in ("45 50", "1 250", "Rs. 45 50/-"):
        with pytest.raises(ValueError, match="MRP must be one price, e.g. 24.50"):
            clear_batches(tmp_path).start("B1", "01/2026", "01/2028", bad, "qa1", 1010.0)
    assert clear_batches(tmp_path).start("B1", "01/2026", "01/2028", " ₹ 1,250 /-", "qa1", 1010.0)["mrp"] == "1250.00"


def test_foil_change_leak_samples_and_end_totals(tmp_path):
    b = clear_batches(tmp_path)
    with pytest.raises(ValueError, match="No batch"):
        b.foil_change("op", 1100.0)
    b.start("B1", "01/2026", "01/2028", "45", "qa1", 1010.0)
    assert b.foil_change("op", 1100.0)["event"] == "foil_change"
    assert b.leak("pass", "qa1", "", 1200.0, samples=5)["samples"] == 5
    with pytest.raises(ValueError, match="Samples must be 0 or more"):
        b.leak("pass", "qa1", "", 1250.0, samples=-1)
    assert b.end("qa1", 1300.0, {"inspected": 3})["totals"] == {"inspected": 3}


def test_start_records_product_and_strength(tmp_path):
    b = clear_batches(tmp_path)
    row = b.start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0, product=" Paracetamol  Tablets ", strength="500mg")
    assert (row["product"], row["strength"]) == ("Paracetamol Tablets", "500mg")


def test_start_refuses_bad_product_or_strength(tmp_path):
    for p, s in (("", "500 mg"), ("X", ""), ("X", "strong")):
        with pytest.raises(ValueError):
            clear_batches(tmp_path).start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0, product=p, strength=s)


def test_start_limits_and_strength_forms(tmp_path):
    for p, s in (("X" * 81, "500 mg"), ("X", "5OOmg"), ("X", "5" * 41 + "mg")):
        with pytest.raises(ValueError):
            clear_batches(tmp_path).start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0, product=p, strength=s)
    for s in ("1,000 mg", "500 mg + 125 mg", "250 mg/5 ml"):
        assert clear_batches(tmp_path / s.replace("/", "_")).start("B1", "01/2026", "01/2028", "45", "qa1", 1060.0, product="X", strength=s)["strength"] == s
