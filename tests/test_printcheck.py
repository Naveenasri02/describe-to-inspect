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


def test_common_label_text_is_print_ok():
    for k, v in [("mrp", "M.R.P. Rs. 45.00"), ("mrp", "Rs. 45.00 Incl. of all taxes"), ("mfg", "MFG.DATE: 10/2026"),
                 ("mfg", "Mfg.Dt. OCT.2026"), ("mfg", "MFG10/2026")]:
        assert run(**{k: v}) == {"status": "PRINT_OK", "reasons": []}, v
    r = check_print(dict(GOOD, exp="USE BEFORE 09/2028"), dict(BATCH, exp="09/2028"), TODAY)
    assert r == {"status": "PRINT_OK", "reasons": []}, r


def test_unparseable_value_is_not_readable_not_a_mismatch():
    for k, label in [("mfg", "MFG"), ("exp", "EXP"), ("mrp", "MRP"), ("batch", "Batch no.")]:
        r = run(**{k: "B.No." if k == "batch" else "1O/2O26" if k != "mrp" else "Rs. --"})
        assert r == {"status": "HOLD", "reasons": [f"{label} not readable — check by hand"]}, r
        assert wrong_print(r["reasons"]) is None  # holds this check only, not the line


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


PB = dict(BATCH, product="Paracetamol Tablets IP", strength="500 mg")
PG = dict(GOOD, product="PARACETAMOL  tablets ip", strength="500MG")


def runp(**over):
    return check_print(dict(PG, **over), PB, TODAY)


def test_product_and_strength_normalised():
    assert runp() == {"status": "PRINT_OK", "reasons": []}
    assert runp(strength="500 Mg")["status"] == "PRINT_OK" and runp(strength="500.0mg")["status"] == "PRINT_OK"


def test_wrong_product_or_strength_is_wrong_print():
    r = runp(strength="650 mg")
    assert r["reasons"] == ["Strength on strip is 650 mg, batch record says 500 mg"] and wrong_print(r["reasons"])
    r = runp(product="Ibuprofen Tablets")
    assert r["status"] == "HOLD" and wrong_print(r["reasons"])
    assert runp(strength="500 g")["status"] == "HOLD"  # unit matters


def test_unparsed_strength_or_missing_product_is_check_by_hand():
    r = runp(strength="mg")
    assert r["reasons"] == ["Strength not readable — check by hand"] and wrong_print(r["reasons"]) is None
    r = runp(product="")
    assert r["status"] == "HOLD" and wrong_print(r["reasons"]) is None


def test_prompt_never_contains_product_or_strength():
    seen = []

    class Fake:
        def spark_json(self, system, user, schema, name="result", **kw):
            seen.append(system + user)
            return PG

    read_print(object(), Fake())
    assert "500" not in seen[0] and "Paracetamol" not in seen[0]


def test_garbled_strength_is_not_readable_not_wrong_print():
    for bad in ("5OOmg", "5O0 mg", "S00 mg", "500 xx"):
        r = runp(strength=bad)
        assert r["reasons"] == ["Strength not readable — check by hand"], (bad, r)
        assert wrong_print(r["reasons"]) is None


def test_strength_forms():
    pb = dict(BATCH, product="Amox", strength="1,000 mg")
    ok = lambda got, want: check_print(dict(GOOD, product="Amox", strength=got), dict(pb, strength=want), TODAY)["status"]  # noqa: E731
    assert ok("1000mg", "1,000 mg") == "PRINT_OK" and ok("1000 mg", "1000 mg") == "PRINT_OK"
    assert ok("500 mg + 125 mg", "500mg+125mg") == "PRINT_OK"
    assert ok("125 mg + 500 mg", "500mg+125mg") == "HOLD"  # order matters
    assert ok("500 mg", "500 mg + 125 mg") == "HOLD"
    assert ok("250 mg/5 ml", "250mg / 5ml") == "PRINT_OK" and ok("250 mg/5 ml", "125 mg/5 ml") == "HOLD"
    for u in ("5 mcg", "5 ug", "100 IU", "5 %", "2 g", "10 ml"):
        assert ok(u, u.upper()) == "PRINT_OK", u


def test_prompt_separates_exp_and_mrp():
    assert "(EXP, Exp), MRP" in READ_SYSTEM
