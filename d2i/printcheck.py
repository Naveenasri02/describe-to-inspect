"""Foil print check: Muse Spark only READS the printed text; plain code compares it with the open batch record.
A mismatch or an unreadable print is HOLD, never a pass or reject on the model's word."""
import re
from datetime import date

from d2i.batch import parse_month

READ_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["batch", "mfg", "exp", "mrp", "readable"],
               "properties": {"batch": {"type": "string"}, "mfg": {"type": "string"}, "exp": {"type": "string"},
                              "mrp": {"type": "string"}, "readable": {"type": "boolean"}}}
READ_SYSTEM = ("You read the printed text on the foil side of a tablet blister strip. Copy exactly what is printed for: "
               "batch number (B.No., Batch, Lot), manufacturing date (MFG, Mfd), expiry date (EXP, Exp) and MRP (price). "
               "Use an empty string for anything not printed or not readable. Never guess or complete partial text. "
               "Set readable to false if the photo is too blurred to read.")
_LABELS = ("BATCHNO", "BNO", "BATCH", "LOT")


def read_print(image, client) -> dict:
    return client.spark_json(READ_SYSTEM, "Read this strip.", READ_SCHEMA, name="print", image=image)


def _batch_key(s) -> str:
    s = re.sub(r"[\s.]", "", str(s)).upper()
    return re.sub(rf"^(?:{"|".join(_LABELS)}):?", "", s)  # "B.No.: AB123" -> "AB123"


def _month(s):
    return parse_month(re.sub(r"^\s*(?:MFG|MFD|EXP)\b\.?\s*:?", "", str(s), flags=re.I))


def _mrp(s) -> float:
    s = re.sub(r"^\s*mrp\.?\s*:?", "", str(s), flags=re.I)
    return round(float(re.sub(r"rs\.?|₹|/-|[\s,]", "", s, flags=re.I)), 2)


def check_print(read: dict, batch: dict, today: date) -> dict:
    if not read.get("readable"):
        return {"status": "HOLD", "reasons": ["Print not readable — check by hand"]}
    why = []

    def differ(label, got, want):
        why.append(f"{label} on strip is {got}, batch record says {want}")

    fields = {"Batch no.": "batch", "MFG": "mfg", "EXP": "exp", "MRP": "mrp"}
    vals = {k: str(read.get(f) or "").strip() for k, f in fields.items()}
    for k, v in vals.items():
        if not v:
            why.append(f"{k} not printed or not readable")
    if vals["Batch no."] and _batch_key(vals["Batch no."]) != _batch_key(batch["batch"]):
        differ("Batch no.", vals["Batch no."], batch["batch"])
    months = {}
    for k, f in (("MFG", "mfg"), ("EXP", "exp")):
        if vals[k]:
            try:
                months[k] = _month(vals[k])
            except ValueError:
                months[k] = None
            if months[k] != parse_month(batch[f]):
                differ(k, vals[k], batch[f])
    if vals["MRP"]:
        try:
            ok = _mrp(vals["MRP"]) == round(float(batch["mrp"]), 2)
        except ValueError:
            ok = False
        if not ok:
            differ("MRP", vals["MRP"], batch["mrp"])
    m, e = months.get("MFG"), months.get("EXP")
    if m and e and e <= m:
        why.append("Expiry is not after manufacture")
    if e and e < (today.year, today.month):
        why.append("Strip is past expiry")
    return {"status": "HOLD" if why else "PRINT_OK", "reasons": why}
