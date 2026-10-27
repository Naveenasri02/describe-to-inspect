"""The bench: inspect a strip, name the failed rule, self-test with fake defects, lock when the test is missed."""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from d2i.checklist import current, rule_number
from d2i.defects import paint
from d2i.judge import Judge
from d2i.pockets import find_layout

POCKET_KINDS = ("empty_pocket", "broken_tablet")  # foil damage is caught on the border, not per pocket
TEXT = {
    "empty_pocket": "pocket {n} is empty",
    "broken_tablet": "pocket {n} has a broken {item}",
    "foil_damage": "foil is damaged",
}


@dataclass
class Verdict:
    status: str  # PASS, REJECT, HOLD, LOCKED, NO_STRIP
    reasons: list = field(default_factory=list)
    pocket_scores: list = field(default_factory=list)
    strip_id: int | None = None
    image_name: str | None = None
    at: str = ""


def _append(path: Path, row: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Station:
    def __init__(self, product_dir, client=None, judge=None, shift_hours: float = 8.0, clock=time.time):
        self.dir, self.client, self.shift_hours, self.clock = Path(product_dir), client, shift_hours, clock
        self.checklist = current(self.dir / "checklists.jsonl")
        model = self.dir / "model.pt"
        state = torch.load(model, map_location="cpu") if model.exists() else {}
        self.judge = judge or (Judge.load(model) if state else Judge())
        self.prototypes, self.taught_at = state.get("prototypes", {}), state.get("taught_at")

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")

    def _selftests(self) -> list[dict]:
        path = self.dir / "selftests.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []

    @property
    def lock_reason(self) -> str:
        """Empty string when the station is unlocked."""
        tests = self._selftests()
        if tests and not tests[-1]["passed"]:
            return "Self-test missed: " + (", ".join(tests[-1]["failed"]) or "see result")
        version = self.checklist and self.checklist["version"]
        ok = [t for t in tests if t["passed"] and t["checklist_version"] == version and t["taught_at"] == self.taught_at]
        if not ok:  # also covers a re-teach after the last pass
            return "No self-test this shift"
        if self.clock() - ok[-1]["time"] > self.shift_hours * 3600:
            return f"Last self-test was more than {self.shift_hours:g} hours ago"
        return ""

    @property
    def locked(self) -> bool:
        return bool(self.lock_reason)

    def teach(self, images: list) -> dict:
        if not self.checklist:
            raise ValueError("Approve a checklist before teaching.")
        layouts = [find_layout(im, self.checklist, self.client) for im in images]
        info = self.judge.teach(images, layouts)
        protos = {}
        for kind in POCKET_KINDS:
            embs = []
            for i in range(min(3, len(images))):
                rng = np.random.default_rng(1000 + i)
                lay, img = layouts[i], images[i]
                chosen = rng.choice(len(lay.pockets), size=3, replace=False)
                for p in chosen:
                    img = paint(img, lay.pockets[p], kind, lay.strip_box, rng)
                embs.append(self.judge.pocket_embeddings(img, lay)[torch.as_tensor(chosen)])
            protos[kind] = F.normalize(torch.cat(embs).mean(0), dim=0).cpu()
        self.judge.save(self.dir / "model.pt")
        self.taught_at = self.clock()
        state = torch.load(self.dir / "model.pt", map_location="cpu")
        state.update(prototypes=protos, taught_at=self.taught_at)
        torch.save(state, self.dir / "model.pt")
        self.prototypes = protos
        return info

    def _reason(self, check, pocket=None):
        rule = rule_number(self.checklist, check)
        n = pocket + 1 if pocket is not None else None
        text = TEXT[check].format(n=n, item=self.checklist["item"])
        return {"rule": rule, "check": check, "pocket": n, "text": f"Rule {rule} failed: {text}"}

    @staticmethod
    def _unlisted(pocket=None):
        where = f"Pocket {pocket + 1}" if pocket is not None else "Strip edge"
        return {"rule": None, "check": "unlisted_anomaly", "pocket": None if pocket is None else pocket + 1,
                "text": f"{where} looks unusual — check by hand"}

    def _find(self, check, listed, unlisted, pocket=None):
        if check in self.checklist["reject_if"]:
            listed.append(self._reason(check, pocket))
        else:
            unlisted.append(self._unlisted(pocket))

    def _judge(self, image, strip_id, image_name) -> Verdict:
        cl = self.checklist
        v = Verdict("PASS", strip_id=strip_id, image_name=image_name, at=self._now())
        lay = find_layout(image, cl, self.client)
        if lay.note == "no strip":
            v.status = "NO_STRIP"
            return v
        if not lay.ok:
            text = (f"Rule 1 failed: found {len(lay.pockets)} pockets, expected {cl['count']}"
                    if lay.pockets else f"Rule 1 failed: {lay.note}")
            v.status, v.reasons = "HOLD", [{"rule": 1, "check": "count", "pocket": None, "text": text}]
            return v
        v.pocket_scores = self.judge.score_pockets(image, lay)
        failing = [i for i, s in enumerate(v.pocket_scores) if s > self.judge.pocket_threshold]
        listed, unlisted = [], []
        if failing:
            embs = self.judge.pocket_embeddings(image, lay).cpu()
            for i in failing:
                kind = max(self.prototypes, key=lambda k: float(embs[i] @ self.prototypes[k]))
                self._find(kind, listed, unlisted, i)
        if self.judge.score_border(image, lay) > self.judge.border_threshold:
            self._find("foil_damage", listed, unlisted)
        v.reasons = listed + unlisted
        v.status = "REJECT" if listed else "HOLD" if unlisted else "PASS"
        return v

    def inspect(self, image, strip_id=None, image_name=None) -> Verdict:
        if self.locked:
            v = Verdict("LOCKED", [{"rule": None, "check": "locked", "pocket": None, "text": self.lock_reason}],
                        strip_id=strip_id, image_name=image_name, at=self._now())
        else:
            v = self._judge(image, strip_id, image_name)
        _append(self.dir / "log.jsonl", dict(asdict(v), checklist_version=self.checklist["version"]))
        return v

    def self_test(self, good_image, rng_seed: int | None = None) -> dict:
        cl = self.checklist
        rng = np.random.default_rng(rng_seed)
        lay = find_layout(good_image, cl, self.client)
        results = []
        if lay.ok:
            per = cl["per_row"]
            edge = [i for i in range(len(lay.pockets))
                    if i // per in (0, cl["rows"] - 1) or i % per in (0, per - 1)]
            for kind in cl["reject_if"]:
                p = int(rng.choice(edge if kind == "foil_damage" else len(lay.pockets)))
                v = self._judge(paint(good_image, lay.pockets[p], kind, lay.strip_box, rng), None, None)
                results.append({"kind": kind, "pocket": p + 1, "caught": v.status == "REJECT",
                                "named_right": any(r["check"] == kind for r in v.reasons)})
        good_passed = lay.ok and self._judge(good_image, None, None).status == "PASS"
        failed = [r["kind"] for r in results if not (r["caught"] and r["named_right"])]
        if not good_passed:
            failed.append("good strip not passed")
        out = {"passed": not failed, "results": results, "good_passed": good_passed, "failed": failed}
        _append(self.dir / "selftests.jsonl", dict(out, time=self.clock(), at=self._now(),
                                                   checklist_version=cl["version"], taught_at=self.taught_at))
        return out
