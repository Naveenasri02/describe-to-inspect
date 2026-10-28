from datetime import date

from d2i.printcheck import READ_SYSTEM, check_print, read_print, wrong_print

BATCH = {"batch": "AB123", "mfg": "10/2026", "exp": "10/2027", "mrp": "45.00"}
GOOD = {"batch": "AB123", "mfg": "10/2026", "exp": "10/2027", "mrp": "45.00", "readable": True}
TODAY = date(2026, 11, 1)


def run(**over):
    return check_print(dict(GOOD, **over), BATCH, TODAY)


def test_print_ok():
    assert run() == {"status": "PRINT_OK", "reasons": []}


def test_formats_normalised():
    r = run(batch="B.No.: ab 123", exp="EXP OCT 2027", mfg="MFG: 10/2026", mrp="Rs. 45")
    assert r["status"] == "PRINT_OK", r
    assert run(mrp="₹45/-")["status"] == "PRINT_OK"


def test_wrong_batch_holds():
    r = run(batch="AB124")
    assert r["status"] == "HOLD" and r["reasons"] == ["Batch no. on strip is AB124, batch record says AB123"]


def test_wrong_mrp_and_mfg_hold():
    r = run(mrp="50", mfg="09/2026")
    assert r["status"] == "HOLD" and len(r["reasons"]) == 2


def test_past_expiry_holds():
    b = dict(BATCH, mfg="01/2024", exp="01/2025")
    r = check_print(dict(GOOD, mfg="01/2024", exp="01/2025"), b, TODAY)
    assert r["status"] == "HOLD" and "Strip is past expiry" in r["reasons"]


def test_expiry_before_mfg_holds():
    r = run(exp="09/2026")
    assert "Expiry is not after manufacture" in r["reasons"]


def test_unreadable_holds():
    r = run(readable=False)
    assert r["status"] == "HOLD" and r["reasons"][0] == "Print not readable — check by hand"


def test_missing_field_holds():
    r = run(exp="")
    assert r["status"] == "HOLD" and "EXP not printed or not readable" in r["reasons"]


def test_garbled_date_holds():
    assert run(mfg="1O/2O26")["status"] == "HOLD"


def test_prompt_never_contains_expected_values():
    seen = []

    class Fake:
        def spark_json(self, system, user, schema, name="result", **kw):
            seen.append(system + user)
            assert kw["image"] is not None
            return GOOD

    out = read_print(object(), Fake())
    assert out == GOOD and seen[0].startswith(READ_SYSTEM)
    for v in BATCH.values():
        assert v not in seen[0]


def test_wrong_print_vs_unreadable_print():
    assert wrong_print(run(batch="AB124")["reasons"]) == "Batch no. on strip is AB124, batch record says AB123"
    assert wrong_print(run(exp="09/2026")["reasons"]) and wrong_print(run(exp="09/2026", mfg="10/2026")["reasons"])
    assert wrong_print(run(mrp="")["reasons"]) is None
    assert wrong_print(run(readable=False)["reasons"]) is None
