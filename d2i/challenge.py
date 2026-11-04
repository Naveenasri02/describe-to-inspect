"""Physical challenge: QA registers known-bad strips; a run puts each one under the camera, where the station must
reject it for its expected rule and the operator confirms it was ejected. A miss locks like a failed self-test."""
import json
import os
import re
from pathlib import Path

import cv2
import numpy as np

from d2i.batch import Batches
from d2i.checklist import rule_number

SET, RUNS = "challenge_set.json", "challenge_runs.jsonl"
NO_SET = "No physical challenge set registered"


def load_set(product_dir) -> dict | None:
    p = Path(product_dir) / SET
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None  # unreadable: raises (gate stays shut)


def runs(product_dir) -> list[dict]:
    return Batches(product_dir)._read(RUNS)


LEARNED = ("empty_pocket", "broken_tablet", "wrong_tablet")  # the kinds the judge names a failing pocket by (a real
# different tablet the judge cannot see by colour: bench 3 Nov, a yellow one matched its registered strip 0.91, broken 0.63)


def register(product_dir, cl: dict, label: str, kind: str, rule: str, image, by: str, at: str, learn=None) -> dict:
    """Add a strip to the set (the same label replaces it in place); its photo goes to challenge/<label>.jpg.
    learn: called after the checks for an empty / broken strip, it gives (the photo's failing pocket embedding,
    "pocket N") or (None, why not); the embedding is kept in challenge/<label>.npy: the judge then knows what a real
    one of this product looks like. The returned set has "learned": what QA is told (only for those kinds)."""
    label = str(label).strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{1,12}", label):
        raise ValueError("Strip label: 1 to 12 letters, digits or -")
    kinds = cl["reject_if"] + ([] if "empty_pocket" in cl["reject_if"] else ["empty_pocket"])  # Rule 1 covers empty
    if kind not in kinds:
        raise ValueError(f"Defect kind must be one of the checklist kinds: {', '.join(kinds)}")
    try:
        n = int(rule) if str(rule).strip() else rule_number(cl, kind)
    except ValueError:
        raise ValueError("Expected rule must be a rule number") from None
    top = len(cl["reject_if"]) + 1  # Rule 1 is the count; each reject_if kind is the next rule
    if not 1 <= n <= top:
        raise ValueError(f"Expected rule must be 1 to {top}")
    if image is None:
        raise ValueError("No camera frame to save.")
    vec, where = learn() if learn and kind in LEARNED else (None, None)  # first: a failure leaves the old photo
    d = Path(product_dir)
    (d / "challenge").mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(d / "challenge" / f"{label}.jpg"), image):
        raise OSError("challenge photo not written")
    strip = {"label": label, "kind": kind, "rule": n, "photo": f"challenge/{label}.jpg"}
    npy = d / "challenge" / f"{label}.npy"
    if vec is None:
        npy.unlink(missing_ok=True)  # a strip registered again never keeps its older photo's pocket
    else:
        np.save(npy, np.asarray(vec, np.float32))
        strip["prototype"] = f"challenge/{label}.npy"
    strips = list((load_set(d) or {"strips": []})["strips"])
    i = next((i for i, s in enumerate(strips) if s["label"] == label), len(strips))
    strips[i:i + 1] = [strip]
    out = {"strips": strips, "registered_by": by, "at": at}
    tmp = d / (SET + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / SET)  # one object, rewritten whole
    if where is None:
        return out
    return dict(out, learned=f"Learned the {kind.replace('_', ' ')} from {where}" if vec is not None
                else f"Nothing learned: {where} — register it again")


def place(strip: dict) -> str:
    return f"Place {strip['label']} ({strip['kind'].replace('_', ' ')}, expect Rule {strip['rule']})"


def judged(strip: dict, v) -> dict:
    """Caught = REJECT naming the expected rule (other rules may be named too)."""
    rules = sorted({r["rule"] for r in v.reasons if isinstance(r.get("rule"), int)})
    caught = v.status == "REJECT" and strip["rule"] in rules
    why = ("" if caught else "not rejected" if v.status != "REJECT"
           else f"rejected for Rule {', '.join(map(str, rules)) or '?'}, expected Rule {strip['rule']}")
    return {"label": strip["label"], "kind": strip["kind"], "expected_rule": strip["rule"], "status": v.status,
            "rules": rules, "caught": caught, "why": why}


def lock(station, res: dict, why: str):
    """Fail-safe: written as a failed self-test row, so the station locks and quarantine opens the same way; a passing
    self-test unlocks it and only a QA release closes the quarantine."""
    Batches(station.dir)._write("selftests.jsonl", {
        "passed": False, "challenge": res["label"], "failed": [f"{res['label']} {why}"], "good_passed": None,
        "results": [{"kind": res["kind"], "pocket": None, "caught": res["status"] == "REJECT",
                     "named_right": res["expected_rule"] in res["rules"]}],
        "strip_found": True, "skipped": [], "time": station.clock(), "at": station._now(),
        "checklist_version": station.checklist["version"], "taught_at": station.taught_at})
