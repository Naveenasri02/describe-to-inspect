"""A worker's sentence about a GOOD strip becomes a numbered checklist; a supervisor approves each version."""
import json
from datetime import datetime
from pathlib import Path

CHECKS = ["empty_pocket", "broken_tablet", "foil_damage"]

RULE_TEXT = {
    "empty_pocket": "no pocket may be empty",
    "broken_tablet": "no {item} may be broken or chipped",
    "foil_damage": "foil must not be torn, cut or missing",
}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["product", "rows", "per_row", "item", "reject_if", "unclear"],
    "properties": {
        "product": {"type": "string"},
        "rows": {"type": "integer"},
        "per_row": {"type": "integer"},
        "item": {"type": "string"},
        "reject_if": {"type": "array", "items": {"type": "string", "enum": CHECKS}},
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = (
    "You set up a packaging-line QA bench for tablet blister strips. The worker describes a GOOD strip, "
    "in any language. Fill the schema in English: product name, rows of pockets, pockets per row, the item "
    "in each pocket (singular, e.g. 'tablet' or 'capsule'), and which checks to reject on "
    "(empty_pocket, broken_tablet, foil_damage). Never invent numbers: if the sentence does not give "
    "rows or pockets per row, use 0 and add a short question to 'unclear'. Anything else you are unsure "
    "about also goes to 'unclear' as a short question."
)


def make_checklist(sentence: str, client) -> dict:
    a = client.spark_json(SYSTEM, sentence, SCHEMA, name="checklist")
    rows, per_row = int(a["rows"]), int(a["per_row"])
    if not (1 <= rows <= 10 and 1 <= per_row <= 20):
        raise ValueError(f"The sentence needs clear numbers (rows 1-10, pockets per row 1-20); "
                         f"got rows={rows}, per_row={per_row}. {' '.join(a.get('unclear', []))}".strip())
    reject_if = [c for c in CHECKS if c in a["reject_if"]]
    if not reject_if:
        raise ValueError("The sentence needs at least one reject rule (empty pocket, broken tablet or foil damage).")
    item = a["item"].strip() or "tablet"
    count = rows * per_row
    rules = [f"Rule 1: exactly {count} {item}s in {rows} rows of {per_row}"]
    rules += [f"Rule {i}: " + RULE_TEXT[c].format(item=item) for i, c in enumerate(reject_if, start=2)]
    return {"product": a["product"], "rows": rows, "per_row": per_row, "count": count, "item": item,
            "reject_if": reject_if, "unclear": list(a.get("unclear", [])), "sentence": sentence, "rules": rules,
            "version": None, "approved_by": None, "approved_at": None}


def rule_number(cl: dict, check: str) -> int:
    if check == "count" or (check == "empty_pocket" and check not in cl["reject_if"]):
        return 1  # an empty pocket always breaks Rule 1 (the count), listed or not
    return cl["reject_if"].index(check) + 2


def _lines(path: Path) -> list[dict]:
    path = Path(path)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def approve(cl: dict, by: str, path: Path) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(cl, version=len(_lines(path)) + 1, approved_by=by, approved_at=datetime.now().isoformat(timespec="seconds"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")
    return out


def current(path: Path) -> dict | None:
    lines = _lines(path)
    return lines[-1] if lines else None
