"""Batch record: line clearance (SAM 3.1 sees nothing left on the table), batch start/end, leak test result.
Append-only files in the product dir; the bench refuses to inspect without an open batch."""
import json
import re
from datetime import datetime
from pathlib import Path

from d2i.audit import json_line
from d2i.meta_api import BudgetExceeded, MetaError
from d2i.pockets import MIN_SCORE, POCKET_PROMPTS, SAM_POOL, STRIP_PROMPTS

CLEARANCE_MAX_AGE_S = 1800
# the whole strip chain and every tablet prompt, asked together: with three prompts a strip in view read CLEAR on 18/33
# real photos; foil-up strips no prompt finds still do (8/33)
CLEARANCE_PROMPTS = STRIP_PROMPTS + POCKET_PROMPTS + ("capsule",)
NEED_SAM = "Line clearance needs SAM 3.1 (Meta API) — it cannot be skipped"
NO_BATCH = "No batch is open — start one first"
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_MONTH_ERR = "Use month/year, e.g. 10/2026"


def parse_month(s: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}|[A-Za-z]{3})\s*[-/. ]\s*(\d{4}|\d{2})\s*", str(s))
    if not m:
        raise ValueError(_MONTH_ERR)
    mon, yr = m.groups()
    mon = MONTHS.index(mon.upper()) + 1 if mon.isalpha() and mon.upper() in MONTHS else int(mon) if mon.isdigit() else 0
    if not 1 <= mon <= 12:
        raise ValueError(_MONTH_ERR)
    return (int(yr) + (2000 if len(yr) == 2 else 0), mon)


_NUM = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
_STRENGTH = re.compile(rf"({_NUM})\s*(mg|mcg|[µμu]g|g|ml|iu|%)(?:\s*/\s*({_NUM})?\s*(ml|mg|g))?", re.I)


def parse_strength(s) -> tuple:
    """"500 mg" = "500mg" = "500MG" -> ((500.0, "mg", None),); "1,000 mg" and "500 mg + 125 mg" (parts in order) and
    "250 mg/5 ml" work too. Only known units (mg, g, mcg, ug, ml, iu, %); anything else (a garbled "5OOmg") is a ValueError."""
    parts = []
    for part in str(s).split("+"):
        m = _STRENGTH.fullmatch(part.strip())
        if not m:
            raise ValueError("Strength: a number and a unit, e.g. 500 mg")
        n, unit, per_n, per_unit = m.groups()
        unit = "mcg" if unit.lower() in ("µg", "μg", "ug") else unit.lower()
        per = (float(per_n.replace(",", "")) if per_n else 1.0, per_unit.lower()) if per_unit else None
        parts.append((float(n.replace(",", "")), unit, per))
    return tuple(parts)


def line_clearance(image, client) -> dict:
    """Not clear when SAM finds any strip, pill or capsule on the table."""
    if client is None:
        raise ValueError(NEED_SAM)
    found = []
    try:
        asked = [SAM_POOL.submit(client.sam_image, image, p) for p in CLEARANCE_PROMPTS]
        for prompt, answer in zip(CLEARANCE_PROMPTS, asked):
            n = sum(1 for o in answer.result() if o.score is None or o.score >= MIN_SCORE)  # unscored counts: unknown is not clear
            if n:
                found.append({"prompt": prompt, "count": n})
    except (MetaError, BudgetExceeded):
        raise ValueError(NEED_SAM) from None
    return {"clear": not found, "found": found}


def batch_no(s) -> str:
    """A batch number as the batch file keeps it: "b 1" = "B1"."""
    return re.sub(r"\s+", "", str(s)).upper()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


class Batches:
    def __init__(self, product_dir):
        self.dir = Path(product_dir)

    def _read(self, name):
        p = self.dir / name
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def _write(self, name, row):
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / name).open("a", encoding="utf-8") as f:
            f.write(json_line(row))
        return row

    def clearances(self) -> list[dict]:
        return self._read("clearances.jsonl")

    @property
    def rows(self) -> list[dict]:
        return self._read("batches.jsonl")

    @property
    def open(self) -> dict | None:
        cur = None
        for r in self.rows:
            if r["event"] == "start":
                cur = r
            elif r["event"] == "end":
                cur = None
        return cur

    def clearance(self, result: dict, by: str, now: float) -> dict:
        return self._write("clearances.jsonl", {"at": _iso(now), "time": now, "by": by,
                                                "clear": bool(result["clear"]), "found": result["found"]})

    def start(self, batch, mfg, exp, mrp, by, now, product=None, strength=None) -> dict:
        no = batch_no(batch)
        if not re.fullmatch(r"[A-Z0-9/-]{1,20}", no):
            raise ValueError("Batch number: 1 to 20 letters, digits, / or -")
        extra = {}
        if product is not None or strength is not None:  # None = an older caller with no foil text to compare
            prod = re.sub(r"\s+", " ", str(product or "")).strip()
            if not 1 <= len(prod) <= 80:
                raise ValueError("Product name: 1 to 80 characters")
            if len(str(strength)) > 40:
                raise ValueError("Strength: 40 characters at most")
            parse_strength(strength or "")
            extra = {"product": prod, "strength": re.sub(r"\s+", " ", str(strength)).strip()}
        m, e = parse_month(mfg), parse_month(exp)
        if e <= m:
            raise ValueError("Expiry must be after the manufacturing month")
        bare = re.sub(r"rs\.?|₹|/-", "", str(mrp), flags=re.I).strip()  # "Rs. 1,250/-" -> 1,250
        clean = "" if re.search(r"\d\s+\d", bare) else re.sub(r"[\s,]", "", bare)  # "45 50" is not 4550
        price = float(clean) if re.fullmatch(r"\d+(\.\d+)?|\.\d+", clean) else 0.0
        if price <= 0:
            raise ValueError("MRP must be one price, e.g. 24.50")
        cur = self.open
        if cur:
            raise ValueError(f"A batch is already open: {cur['batch']}")
        if any(r["event"] == "start" and r["batch"] == no for r in self.rows):
            raise ValueError(f"Batch no. {no} was already used")
        last = (self.clearances() or [None])[-1]
        if last is None:
            raise ValueError("Run line clearance first")
        if not last["clear"]:
            raise ValueError("The last line clearance was not clear — clear the table and run it again")
        ends = [r["time"] for r in self.rows if r["event"] == "end"]
        if ends and last["time"] <= ends[-1]:  # one clearance per batch
            raise ValueError("Run line clearance again for this batch")
        if now - last["time"] > CLEARANCE_MAX_AGE_S:
            raise ValueError("Line clearance is older than 30 minutes — run it again")
        return self._write("batches.jsonl", {"event": "start", "batch": no, "mfg": "%02d/%d" % (m[1], m[0]),
                                             "exp": "%02d/%d" % (e[1], e[0]), "mrp": f"{price:.2f}", "by": by,
                                             "at": _iso(now), "time": now, "clearance_at": last["at"], **extra})

    def leak(self, result, by, note, now, samples: int = 0) -> dict:
        if result not in ("pass", "fail"):
            raise ValueError("Leak test result must be pass or fail")
        if samples < 0:
            raise ValueError("Samples must be 0 or more")
        cur = self.open
        if not cur:
            raise ValueError(NO_BATCH)
        return self._write("batches.jsonl", {"event": "leak", "batch": cur["batch"], "result": result,
                                             "note": str(note or ""), "samples": int(samples), "by": by, "at": _iso(now), "time": now})

    def foil_change(self, by, now) -> dict:
        cur = self.open
        if not cur:
            raise ValueError(NO_BATCH)
        return self._write("batches.jsonl", {"event": "foil_change", "batch": cur["batch"], "by": by,
                                             "at": _iso(now), "time": now})

    def end(self, by, now, totals: dict | None = None) -> dict:
        cur = self.open
        if not cur:
            raise ValueError(NO_BATCH)
        return self._write("batches.jsonl", {"event": "end", "batch": cur["batch"], "by": by,
                                             "at": _iso(now), "time": now, "totals": totals or {}})
