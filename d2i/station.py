"""The bench: inspect a strip, name the failed rule, self-test with fake defects, lock when the test is missed."""
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from d2i import trace
from d2i.checklist import current, rule_number
from d2i.defects import paint
from d2i.judge import Judge
from d2i.measure import features, pills_in, pocket_check, strip_check, teach_profile, teach_strip
from d2i.pockets import find_layout, template_from

MEASURED = ("double_tablet", "wrong_tablet", "tablet_spot")  # decided by measure.py on SAM pill boxes
POCKET_KINDS = ("empty_pocket", "broken_tablet")  # foil damage is caught on the border, not per pocket
TEXT = {
    "empty_pocket": "pocket {n} is empty",
    "broken_tablet": "pocket {n} has a broken {item}",
    "double_tablet": "pocket {n} has two {item}s",
    "wrong_tablet": "pocket {n} has a different {item}",
    "tablet_spot": "pocket {n} has a spot on the {item}",
    "foil_damage": "foil is damaged",
    "strip_cut": "strip is cut wrong or the wrong size",
}
LINE_HELD = "Line held — QA must resume"
NEED_TEACH = "Teach the station first"


@dataclass
class Verdict:
    status: str  # PASS, REJECT, HOLD, LOCKED, NO_STRIP
    reasons: list = field(default_factory=list)
    pocket_scores: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # checks that could not run, e.g. "tablet measurements"
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
        t = state.get("template")
        self.template = [tuple(b) for b in t] if t else None
        self.profile = state.get("profile")
        self.strip_profile = state.get("strip_profile")
        self.strip_box = tuple(state["strip_box"]) if state.get("strip_box") else None  # median taught strip box

    def _now(self) -> str:
        return datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds")

    def _selftests(self) -> list[dict]:
        path = self.dir / "selftests.jsonl"
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []

    @property
    def _template(self):
        """The taught pocket pattern, unless the checklist changed since (then it no longer fits)."""
        stale = self.template and self.checklist and len(self.template) != self.checklist["count"]
        return None if stale else self.template

    @property
    def lock_reason(self) -> str:
        """Empty string when the station is unlocked."""
        if self.template and self._template is None:
            return "Checklist changed — teach the station again"
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

    def crosscheck(self, image) -> dict:
        """Two models count the pockets: SAM 3.1 outlines vs Muse Spark pointing. Paid, so never inside inspect."""
        if self.client is None:
            raise ValueError("crosscheck needs the Meta client")
        sam = len(find_layout(image, self.checklist, self.client).pockets)
        spark = self.client.spark_count(image, self.checklist["item"])
        return {"sam": sam, "spark": spark, "agree": sam == spark}

    def teach(self, images: list, crosscheck: bool = False) -> dict:
        if not self.checklist:
            raise ValueError("Approve a checklist before teaching.")
        if crosscheck:
            for n, im in enumerate(images, 1):
                c = self.crosscheck(im)
                if not c["agree"]:
                    raise ValueError(f"photo {n}: SAM 3.1 found {c['sam']} pockets, Muse Spark counted {c['spark']}")
        layouts = [find_layout(im, self.checklist, self.client) for im in images]
        t0 = time.perf_counter()
        info = self.judge.teach(images, layouts)  # refuses photos whose layout is not ok
        for _ in images:
            trace.record("dinov2-small", "local", "teach", (time.perf_counter() - t0) * 1000 / len(images))
        template = template_from(layouts)
        strip_box = tuple(float(v) for v in np.median([l.strip_box for l in layouts], axis=0))  # SAM's, when online
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
        profile, strip_profile = teach_profile(images, layouts), teach_strip(layouts)
        self.judge.save(self.dir / "model.pt")
        self.taught_at = self.clock()
        state = torch.load(self.dir / "model.pt", map_location="cpu")
        state.update(prototypes=protos, taught_at=self.taught_at, template=[list(b) for b in template],
                     strip_box=list(strip_box), profile=profile, strip_profile=strip_profile)
        torch.save(state, self.dir / "model.pt")
        self.prototypes, self.template, self.strip_box, self.profile = protos, template, strip_box, profile
        self.strip_profile = strip_profile
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
        if check in self.checklist["reject_if"] or check == "empty_pocket":  # empty = Rule 1, always a reject
            listed.append(self._reason(check, pocket))
        else:
            unlisted.append(self._unlisted(pocket))

    def _judge(self, image, strip_id, image_name) -> Verdict:
        t0 = time.perf_counter()
        try:
            return self._judge_one(image, strip_id, image_name)
        finally:
            trace.record("dinov2-small", "local", "judge", (time.perf_counter() - t0) * 1000)

    def _judge_one(self, image, strip_id, image_name) -> Verdict:
        cl = self.checklist
        v = Verdict("PASS", strip_id=strip_id, image_name=image_name, at=self._now())
        lay = find_layout(image, cl, self.client, self._template)
        if lay.note == "no strip":
            v.status = "NO_STRIP"
            return v
        if not lay.ok:
            v.status, v.reasons = "HOLD", [{"rule": 1, "check": "count", "pocket": None,
                                            "text": f"Rule 1 failed: {lay.note}"}]
            return v
        v.pocket_scores = self.judge.score_pockets(image, lay)
        listed, unlisted, measured = [], [], set()
        if lay.pills and self.profile:
            usable = 0
            for i, pocket in enumerate(lay.pockets):
                mine = pills_in(pocket, lay.pills)
                usable += len(mine) > 1 or (len(mine) == 1 and features(image, pocket, mine[0]) is not None)
                kind = pocket_check(image, pocket, lay.pills, self.profile)
                if kind:
                    self._find(kind, listed, unlisted, i)
                    measured.add(i)
            if not usable:
                v.skipped.append("tablet measurements")
        else:
            v.skipped.append("tablet measurements")
        if lay.pills and self.strip_profile:
            if strip_check(lay, self.strip_profile):
                self._find("strip_cut", listed, unlisted)
        else:
            v.skipped.append("strip size")
        failing = [i for i, s in enumerate(v.pocket_scores) if s > self.judge.pocket_threshold and i not in measured]
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

    def inspect(self, image, strip_id=None, image_name=None, held: bool = False, batch=None) -> Verdict:
        if self.locked:
            v = Verdict("LOCKED", [{"rule": None, "check": "locked", "pocket": None, "text": self.lock_reason}],
                        strip_id=strip_id, image_name=image_name, at=self._now())
        else:
            v = self._judge(image, strip_id, image_name)
        return self.log(v, held, batch)

    def log(self, v: Verdict, held: bool = False, batch=None) -> Verdict:
        """Append one verdict to log.jsonl. held: the line is held, so a PASS is logged (and returned) as HOLD."""
        if held and v.status == "PASS":
            v.status, v.reasons = "HOLD", [{"rule": None, "check": "line_held", "pocket": None, "text": LINE_HELD}]
        v.at = v.at or self._now()
        _append(self.dir / "log.jsonl", dict(asdict(v), checklist_version=self.checklist["version"], batch=batch))
        return v

    def self_test(self, good_image, rng_seed: int | None = None, record: bool | str = True) -> dict:
        """record=False: a diagnostic run that writes nothing. record="failures": a failed run is written like any
        failed self-test (it locks and opens quarantine), a pass never is (so it cannot unlock), and with no strip
        in view nothing is written."""
        if self.taught_at is None:
            raise ValueError(NEED_TEACH)
        cl = self.checklist
        rng = np.random.default_rng(rng_seed)
        lay = find_layout(good_image, cl, self.client, self._template)
        results, skipped = [], []
        if lay.ok:
            per = cl["per_row"]
            edge = [i for i in range(len(lay.pockets))
                    if i // per in (0, cl["rows"] - 1) or i % per in (0, per - 1)]
            measuring = bool(lay.pills and self.profile)
            for kind in cl["reject_if"] + ([] if "empty_pocket" in cl["reject_if"] else ["empty_pocket"]):  # Rule 1
                if kind == "strip_cut":  # painted once on the whole strip; pocket choice ignored
                    if not (lay.pills and self.strip_profile):
                        skipped.append(kind)
                        continue
                    v = self._judge(paint(good_image, lay.pockets[0], kind, lay.strip_box, rng), None, None)
                    results.append({"kind": kind, "pocket": None, "caught": v.status == "REJECT",
                                    "named_right": any(r["check"] == kind for r in v.reasons)})
                    continue
                if kind in MEASURED and not measuring:  # no SAM pills or no profile: it cannot be seen, so it is skipped
                    skipped.append(kind)
                    continue
                # shortcut: wrong_tablet self-test changes size only (SAM may miss recoloured pills); the colour path is covered by unit tests
                p = int(rng.choice(edge if kind == "foil_damage" else len(lay.pockets)))
                v = self._judge(paint(good_image, lay.pockets[p], kind, lay.strip_box, rng), None, None)
                results.append({"kind": kind, "pocket": p + 1, "caught": v.status == "REJECT",
                                "named_right": any(r["check"] == kind for r in v.reasons)})
        good_passed = lay.ok and self._judge(good_image, None, None).status == "PASS"
        failed = [r["kind"] for r in results if not (r["caught"] and r["named_right"])]
        if not good_passed:
            failed.append("good strip not passed")
        out = {"passed": not failed, "results": results, "good_passed": good_passed, "failed": failed,
               "strip_found": lay.ok, "skipped": skipped}
        if record is True or (record == "failures" and failed and lay.ok):
            _append(self.dir / "selftests.jsonl", dict(out, time=self.clock(), at=self._now(),
                                                       checklist_version=cl["version"], taught_at=self.taught_at))
        return out
