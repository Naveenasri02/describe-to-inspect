"""Teach on real photos of one product, then count what the bench catches. The only source of accuracy numbers.

usage: python tools/eval_strips.py photos/good photos/bad [--sam] --rows R --per-row P [--item tablet]
"""
import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from d2i.checklist import CHECKS
from d2i.station import Station

HOLDOUT, MIN_GOOD = 3, 8


class TooFewPhotos(ValueError):
    pass


class SelfTestFailed(RuntimeError):
    pass


def photos(folder) -> list[Path]:
    return sorted(p for p in Path(folder).glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))


def split_good(paths) -> tuple[list, list]:
    paths = sorted(paths)
    if len(paths) < MIN_GOOD:
        raise TooFewPhotos(f"need at least {MIN_GOOD} good photos, found {len(paths)}")
    return paths[:-HOLDOUT], paths[-HOLDOUT:]


def evaluate(station, good_paths, bad_paths, load=cv2.imread) -> dict:
    teach, hold = split_good(good_paths)
    if not bad_paths:
        raise TooFewPhotos("need at least 1 bad photo, found 0")
    imgs = [load(p) for p in teach]
    station.teach(imgs)
    test = station.self_test(imgs[0], rng_seed=0)
    if not test["passed"]:  # a failed self-test locks the station: every verdict would be LOCKED
        raise SelfTestFailed("Self-test failed — station locked; no numbers reported. Re-teach and try again.")
    rows = []
    for kind, ps in (("good", hold), ("bad", sorted(bad_paths))):
        for p in ps:
            v = station.inspect(load(p), image_name=Path(p).name, user="eval")
            rows.append({"kind": kind, "name": Path(p).name, "status": v.status,
                         "reasons": [r["text"] for r in v.reasons]})
    n = lambda kind, *st: sum(r["kind"] == kind and r["status"] in st for r in rows)  # noqa: E731
    return {"taught": len(teach), "self_test": test, "rows": rows,
            "bad": sum(r["kind"] == "bad" for r in rows),
            "held_out": len(hold), "caught": n("bad", "REJECT", "HOLD"), "held": n("bad", "HOLD"),
            "false_rejects": n("good", "REJECT", "HOLD"), "held_good": n("good", "HOLD"),
            "not_judged": sum(r["status"] not in ("PASS", "REJECT", "HOLD") for r in rows)}


def summary_lines(res) -> list[str]:
    out = [f"taught on {res['taught']} good photos; self-test passed: {res['self_test']['passed']}"]
    out += [f"{r['kind']:4} {r['name']}: {r['status']} {'; '.join(r['reasons'])}".rstrip() for r in res["rows"]]
    out += [f"caught {res['caught']} of {res['bad']} bad ({res['held']} held for a person)",
            f"false rejects {res['false_rejects']} of {res['held_out']} good ({res['held_good']} held for a person)"]
    if res["not_judged"]:
        out.append(f"not judged: {res['not_judged']}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("good")
    ap.add_argument("bad")
    ap.add_argument("--sam", action="store_true")
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--per-row", type=int, required=True)
    ap.add_argument("--item", default="tablet")
    a = ap.parse_args(argv)
    good, bad = photos(a.good), photos(a.bad)
    try:
        split_good(good)
        if not bad:
            raise TooFewPhotos("need at least 1 bad photo, found 0")
    except TooFewPhotos as e:
        print(f"Cannot run: {e} ({a.good}, {a.bad}).")
        return 2
    count = a.rows * a.per_row
    cl = {"product": "eval", "rows": a.rows, "per_row": a.per_row, "count": count, "item": a.item,
          "reject_if": list(CHECKS), "unclear": [], "sentence": "eval", "version": 1, "approved_by": "eval",
          "approved_at": datetime.now().isoformat(timespec="seconds"),
          "rules": [f"Rule 1: exactly {count} {a.item}s in {a.rows} rows of {a.per_row}"]
                   + [f"Rule {i}: {c}" for i, c in enumerate(CHECKS, start=2)]}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "checklists.jsonl").write_text(__import__("json").dumps(cl) + "\n", encoding="utf-8")
        client = None
        if a.sam:
            from d2i.meta_api import Budget, MetaClient
            client = MetaClient(Budget())  # the shared spend file: these calls count toward the $50 stop
        try:
            res = evaluate(Station(tmp, client=client), good, bad)
        except SelfTestFailed as e:
            print(e)
            return 1
        print("\n".join(summary_lines(res)))
        if client:
            print(f"Meta spend so far (all runs): ${client.budget.spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
