"""Foil print check: Muse Spark only READS the printed text; plain code compares it with the open batch record.
A mismatch or an unreadable print is HOLD, never a pass or reject on the model's word. Spark is a generative model
(draft EU GMP Annex 22: it must not make a critical GMP decision alone), so a difference, a blank or a cut-off field
only flags the strip "Check the print by eye": QA looks at it and decides "Print matches" or "Wrong print" (app.py
/print/eye)."""
import re
import unicodedata
from datetime import date

from d2i.batch import parse_month, parse_strength

READ_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["batch", "mfg", "exp", "mrp", "product", "strength", "readable"],
               "properties": {"batch": {"type": "string"}, "mfg": {"type": "string"}, "exp": {"type": "string"},
                              "mrp": {"type": "string"}, "product": {"type": "string"},
                              "strength": {"type": "string"}, "readable": {"type": "boolean"}}}
READ_SYSTEM = ("You read the printed text on the foil side of a tablet blister strip. Copy exactly what is printed for: "
               "batch number (B.No., Batch, Lot), manufacturing date (MFG, Mfd), expiry date (EXP, Exp), MRP (price), product name (the brand name printed largest on the strip, not the ingredient or generic name line) and strength (number and unit). "
               "Use an empty string for anything not printed or not readable. Never guess or complete partial text. "
               "Set readable to false if the photo is too blurred to read.")
_LABELS = ("BATCHNO", "BNO", "BATCH", "LOT")


def read_print(image, client) -> dict:
    return client.spark_json(READ_SYSTEM, "Read this strip.", READ_SCHEMA, name="print", image=image)


def _batch_key(s) -> str:
    s = re.sub(r"[\s.]", "", str(s)).upper()
    return re.sub(rf"^(?:{"|".join(_LABELS)}):?", "", s)  # "B.No.: AB123" -> "AB123"


def _words(s, split: bool = False) -> list[str]:
    """Words, case and punctuation ignored: "Crocin® Tablets I.P." -> ["crocin", "tablets", "ip"]; split: punctuation
    also parts words ("DOLO-650" -> ["dolo", "650"])."""
    return "".join(c if unicodedata.category(c)[0] not in "PS" else " " * split for c in str(s)).casefold().split()


def _name_printed(read: str, typed: str) -> bool:
    """Every typed word is printed, in order: "Paracetamol" matches "Paracetamol Tablets IP"; or the typed name, spaces
    and hyphens ignored, is whole printed words in a row: "Dolo 650" matches "DOLO-650" (not "Dolo 65")."""
    printed, want = iter(_words(read)), _words(typed)
    if bool(want) and all(w in printed for w in want):  # `in` moves the iterator on: words in order
        return True
    t, parts = "".join(want), _words(read, split=True)
    return bool(t) and any("".join(parts[i:j]) == t for i in range(len(parts)) for j in range(i + 1, len(parts) + 1))


def _cut_off(read: str, typed: str) -> bool:
    """The read is only the start of the typed name ("Rapeed-" of "Rapeed-20"): cut off by the strip edge, not a different name."""
    r, t = "".join(_words(read)), "".join(_words(typed))
    return bool(r) and len(r) < len(t) and t.startswith(r)


def _month(s):
    """The first month/year in the text ("Mfg.Dt. OCT.2026", "USE BEFORE 09/2028"); ValueError when there is none."""
    s = re.sub(r"^\s*(?:MFG|MFD|EXP)\b\.?\s*:?", "", str(s), flags=re.I)
    m = re.search(r"(?:(?<!\d)\d{1,2}|(?<![A-Za-z])[A-Za-z]{3})\s*[-/. ]\s*(?:\d{4}|\d{2})(?!\d)", s)
    return parse_month(m.group(0) if m else "")


def _mrp(s) -> float:
    """The first price in the text ("M.R.P. Rs. 45.00", "Rs. 45.00 Incl. of all taxes"); ValueError when there is none."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", re.sub(r"^\s*mrp\.?\s*:?", "", str(s), flags=re.I))
    return round(float((m.group(0) if m else "").replace(",", "")), 2)


def check_print(read: dict, batch: dict, today: date) -> dict:
    if not read.get("readable"):
        return {"status": "HOLD", "reasons": ["Print not readable — check by hand"]}
    why = []

    def differ(label, got, want):
        why.append(f"{label} on strip is {got}, batch record says {want}")

    def unreadable(label):  # holds this check only: an unparsed value is not a proven mismatch
        why.append(f"{label} not readable — check by hand")

    fields = {"Batch no.": "batch", "MFG": "mfg", "EXP": "exp", "MRP": "mrp"}
    if batch.get("product"):  # batches started before these fields existed have nothing to compare
        fields |= {"Product name": "product"} | ({"Strength": "strength"} if batch.get("strength") else {})  # blank: not printed
    vals = {k: str(read.get(f) or "").strip() for k, f in fields.items()}
    for k, v in vals.items():
        if not v:
            why.append(f"{k} not printed or not readable")
    if vals["Batch no."]:
        if not _batch_key(vals["Batch no."]):
            unreadable("Batch no.")
        elif _batch_key(vals["Batch no."]) != _batch_key(batch["batch"]):
            differ("Batch no.", vals["Batch no."], batch["batch"])
    months = {}
    for k, f in (("MFG", "mfg"), ("EXP", "exp")):
        if vals[k]:
            try:
                months[k] = _month(vals[k])
            except ValueError:
                unreadable(k)
                continue
            if months[k] != parse_month(batch[f]):
                differ(k, vals[k], batch[f])
    if vals["MRP"]:
        try:
            if _mrp(vals["MRP"]) != round(float(batch["mrp"]), 2):
                differ("MRP", vals["MRP"], batch["mrp"])
        except ValueError:
            unreadable("MRP")
    if vals.get("Product name"):
        if _cut_off(vals["Product name"], batch["product"]):
            unreadable("Product name")
        elif not _name_printed(vals["Product name"], batch["product"]):
            differ("Product name", vals["Product name"], batch["product"])
    if vals.get("Strength"):
        try:
            if parse_strength(vals["Strength"]) != parse_strength(batch["strength"]):
                differ("Strength", vals["Strength"], batch["strength"])
        except ValueError:
            unreadable("Strength")
    m, e = months.get("MFG"), months.get("EXP")
    if m and e and e <= m:
        why.append("Expiry is not after manufacture")
    if e and e < (today.year, today.month):
        why.append("Strip is past expiry")
    return {"status": "HOLD" if why else "PRINT_OK", "reasons": why}


def differs(reasons: list[str]) -> str | None:
    """The first reason that says the read differs from the batch record (a mismatch or a bad date), for QA's check by
    eye; a missing or unreadable field is not one (check by hand)."""
    return next((r for r in reasons if " on strip is " in r or r in ("Expiry is not after manufacture", "Strip is past expiry")), None)
