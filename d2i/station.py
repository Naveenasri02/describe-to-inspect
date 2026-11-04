"""The bench: inspect a strip, name the failed rule, self-test with fake defects, lock when the test is missed."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F

from d2i import trace
from d2i.audit import json_line
from d2i.challenge import load_set
from d2i.checklist import SEVERITY, current, rule_number
from d2i.defects import paint
from d2i.judge import Judge
from d2i.measure import _delta_e, empty_looking, features, pocket_check, strip_check, teach_profile, teach_strip
from d2i.pockets import _from_template, _on_turned, find_layout, framed, template_from, turn_of

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
FINDER_DOWN = {"rule": 1, "check": "finder", "pocket": None,
               "text": "Pocket finder (SAM 3.1) did not answer — check this strip by hand", "severity": "major"}
NOT_RUN = "Self-test could not run: the pocket finder (SAM 3.1) did not answer — try again"
# mean grey difference of 32 x 32 thumbnails up to which the camera still shows the inspected view: camera noise or blur
# 0.1-0.2, an identical strip 0.9; a strip moved 6 px 3.2, a lamp change 18-25, the strip taken away 66
SAME_VIEW = 3.0
SELF_TEST_WORKERS = 4  # painted strips judged at once (their SAM 3.1 calls are bounded by pockets.SAM_POOL)


def _finder(lay) -> str:
    return "grid" if lay.source == "grid" else "sam"  # "sam-pills" is SAM 3.1 too


def painted_prototypes(judge, images, layouts) -> dict:
    """Per pocket kind, the mean embedding of 3 pockets painted with it on each of the first 3 teach photos."""
    protos = {}
    for kind in POCKET_KINDS:
        embs = []
        for i in range(min(3, len(images))):
            rng = np.random.default_rng(1000 + i)
            lay, img = layouts[i], images[i]
            chosen = rng.choice(len(lay.pockets), size=3, replace=False)
            for p in chosen:
                img = paint(img, lay.pockets[p], kind, lay.strip_box, rng)
            embs.append(judge.pocket_embeddings(img, lay)[torch.as_tensor(chosen)])
        protos[kind] = F.normalize(torch.cat(embs).mean(0), dim=0).cpu()
    return protos


def similarity(emb, protos) -> dict:
    """Per kind, how near this pocket is to that kind's nearest prototype; a kind may hold several (one per row)."""
    return {k: float((protos[k].reshape(-1, emb.shape[-1]) @ emb).max()) for k in protos}


def nearest(emb, protos) -> str:
    """The kind whose prototype is nearest this pocket."""
    s = similarity(emb, protos)
    return max(s, key=s.get)


# how much nearer "empty" than "broken" the judge must find a pocket before its "empty" outweighs a measured different
# tablet: real empty domes won by 0.13 and 0.22 on wa5 (0.17-0.20 on the test strips), tinted real tablets by 0.00-0.02
EMPTY_MARGIN = 0.12


@dataclass
class Verdict:
    status: str  # PASS, REJECT, HOLD, LOCKED, NO_STRIP
    reasons: list = field(default_factory=list)
    pocket_scores: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # checks that could not run, e.g. "tablet measurements"
    strip_id: int | None = None
    image_name: str | None = None
    at: str = ""
    layout = None  # not a field (kept out of the log row): the Layout this strip was judged on


def _thumb(image):
    return cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)


def _append(path: Path, row: dict):
    with path.open("a", encoding="utf-8") as f:
        f.write(json_line(row))


class Station:
    def __init__(self, product_dir, client=None, judge=None, shift_hours: float = 8.0, clock=time.time):
        self.dir, self.client, self.shift_hours, self.clock = Path(product_dir), client, shift_hours, clock
        self.snap = None  # set by the app: (image, verdict) -> file name of the saved reject photo
        self._last_view = None  # (layout, frame size, thumbnail) of the last whole camera frame inspected
        self.last_failing = None  # (layout, pocket number) of the last registered challenge strip's defect
        self.step = ""  # what a long action is doing now, for the screen
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
        # a model.pt saved before the source was kept: every product so far was taught with SAM 3.1
        self.layout_source = state.get("layout_source", "sam")

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
            what = "Physical challenge missed: " if tests[-1].get("challenge") else "Self-test missed: "
            return what + (", ".join(tests[-1]["failed"]) or "see result")
        until = self.selftest_until
        if until is None:  # also covers a re-teach after the last pass
            return "No self-test this shift"
        if self.clock() > until:
            return f"Last self-test was more than {self.shift_hours:g} hours ago"
        return ""

    @property
    def selftest_until(self) -> float | None:
        """When the latest passed self-test of this checklist and teaching runs out; None when there is none."""
        version = self.checklist and self.checklist["version"]
        ok = [t for t in self._selftests() if t["passed"] and t["checklist_version"] == version
              and t["taught_at"] == self.taught_at]
        return ok[-1]["time"] + self.shift_hours * 3600 if ok else None

    @property
    def locked(self) -> bool:
        return bool(self.lock_reason)

    @contextmanager
    def _doing(self, step):
        self.step = step
        try:
            yield
        finally:
            self.step = ""

    @property
    def _finding(self) -> str:
        return "Finding the strip" + ("" if self.layout_source == "grid" else " (SAM 3.1)")

    def _layout(self, image):
        """A product taught on the checklist grid stays on it: no paid SAM call whose pockets it was not taught on."""
        return find_layout(image, self.checklist, None if self.layout_source == "grid" else self.client, self._template)

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
        with self._doing(f"Teaching: finding the pockets in {len(images)} photos" + (" (SAM 3.1)" if self.client else "")):
            layouts = [find_layout(im, self.checklist, self.client) for im in images]
        sources = [_finder(l) for l in layouts]
        if "sam" in sources and "grid" in sources:  # SAM 3.1 failed on some photos: their pockets came from the grid
            missed = [str(n) for n, s in enumerate(sources, 1) if s == "grid"]
            raise ValueError(f"photo {missed[0]}: the pocket finder (SAM 3.1) did not answer — save that photo again"
                             if len(missed) == 1 else f"photos {', '.join(missed)}: the pocket finder (SAM 3.1) did not "
                                                      f"answer — save those photos again")
        # all from the grid with a key set: SAM failed on every photo; only a bench with no key is taught offline.
        # Every client says whether it has a key (MetaClient reads it; a test fake sets it): no default either way
        if sources and "sam" not in sources and self.client is not None and self.client.has_key:
            raise ValueError("the pocket finder (SAM 3.1) did not answer — check the network and save the photos again")
        if bad := empty_looking(images, [l if l.ok else replace(l, pills=[]) for l in layouts]):
            raise ValueError(f"photo {bad[0]}: pocket {bad[1]} looks empty — teach on full strips only")
        if all(l.ok for l in layouts) and len({len(l.pockets) for l in layouts}) == 1:  # else judge.teach says which
            # each photo's turn against the taught pockets, as inspect measures it (real pockets are not on the checklist
            # grid: straight photos read 0.55 deg against it)
            t = template_from(layouts)
            layouts = [replace(l, angle=0.0 if framed(l.strip_box, im.shape) else turn_of(l, t))
                       for l, im in zip(layouts, images)]
        t0 = time.perf_counter()
        with self._doing("Teaching: learning the good pockets (DINOv2)"):
            info = self.judge.teach(images, layouts)  # refuses photos whose layout is not ok
        for _ in images:
            trace.record("dinov2-small", "local", "teach", (time.perf_counter() - t0) * 1000 / len(images))
        template = template_from(layouts)
        strip_box = tuple(float(v) for v in np.median([l.strip_box for l in layouts], axis=0))  # SAM's, when online
        protos = painted_prototypes(self.judge, images, layouts)
        # tablets measured on the taught slots, as inspect measures them: on its own pill box every tablet reads 1.0 with
        # no spread, and good strips then read up to +12% at inspection against a 12% tolerance
        on_slots = [replace(l, pockets=_on_turned(l.strip_box, template, l.angle) if l.angle
                            else _from_template(l.strip_box, template)) for l in layouts]
        profile, strip_profile = teach_profile(images, on_slots), teach_strip(layouts)
        self.judge.save(self.dir / "model.pt")
        self.taught_at = self.clock()
        state = torch.load(self.dir / "model.pt", map_location="cpu")
        state.update(prototypes=protos, taught_at=self.taught_at, template=[list(b) for b in template],
                     strip_box=list(strip_box), profile=profile, strip_profile=strip_profile, layout_source=sources[0])
        torch.save(state, self.dir / "model.pt")
        self.prototypes, self.template, self.strip_box, self.profile = protos, template, strip_box, profile
        self.strip_profile, self.layout_source = strip_profile, sources[0]
        return info

    def _prototypes(self) -> dict:
        """The painted prototypes plus the failing pocket of each registered empty / broken challenge strip: a real
        empty dome of this product (a painted one named about 13 of 82 real empty pockets empty, a real one 77 of 82)."""
        out = dict(self.prototypes)
        for s in (load_set(self.dir) or {"strips": []})["strips"]:  # unreadable: raises (the gate is shut then too)
            if s.get("prototype"):
                v = torch.from_numpy(np.load(self.dir / s["prototype"])).float()
                out[s["kind"]] = torch.vstack([out[s["kind"]].reshape(-1, len(v)), v]) if s["kind"] in out else v[None]
        return out

    def failing_pocket(self, image) -> tuple:
        """(embedding, "pocket N") of the worst pocket of a known-bad strip photo when the judge fails it; else (None,
        why nothing was learned), for QA's screen."""
        if self.taught_at is None:
            return None, "the station is not taught"
        lay = self._layout(image)
        if _finder(lay) != self.layout_source:
            return None, "SAM 3.1 did not answer"
        if not lay.ok:
            return None, f"strip not found in full ({lay.note})"
        scores = self.judge.score_pockets(image, lay)
        i = int(np.argmax(scores))  # shortcut: the worst pocket is taken as the defect; QA registers one defect a strip
        if scores[i] <= self.judge.pocket_threshold:
            # the measurements may fail a pocket the judge passes (a yellow tablet of the taught shape)
            v = self._judge_one(image, lay, Verdict("PASS", at=self._now()))
            hit = next((r["pocket"] for r in v.reasons if r.get("pocket")), None)
            if hit is None:
                return None, "no pocket failed"
            i = hit - 1
        self.keep_view(image, lay)  # the live camera shows the registered strip with its pockets
        self.last_failing = (lay, i + 1)
        return self.judge.pocket_embeddings(image, lay)[i].cpu().numpy(), f"pocket {i + 1}"

    def _reason(self, check, pocket=None):
        rule = rule_number(self.checklist, check)
        n = pocket + 1 if pocket is not None else None
        text = TEXT[check].format(n=n, item=self.checklist["item"])
        return {"rule": rule, "check": check, "pocket": n, "text": f"Rule {rule} failed: {text}",
                "severity": SEVERITY[check]}

    @staticmethod
    def _unlisted(pocket=None):
        where = f"Pocket {pocket + 1}" if pocket is not None else "Strip edge"
        return {"rule": None, "check": "unlisted_anomaly", "pocket": None if pocket is None else pocket + 1,
                "text": f"{where} looks unusual — check by hand", "severity": "major"}

    def _find(self, check, listed, unlisted, pocket=None):
        if check in self.checklist["reject_if"] or check == "empty_pocket":  # empty = Rule 1, always a reject
            listed.append(self._reason(check, pocket))
        else:
            unlisted.append(self._unlisted(pocket))

    def _name(self, image, lay, i, sims):
        """The nearest kind; "different tablet" only when the tablet's colour differs from the taught one: a crushed
        white tablet sat as near a learned yellow tablet as a broken one (under a lamp: 0.67 vs 0.69)."""
        kind = max(sims, key=sims.get)
        mine = lay.in_pocket[i] if lay.pills and self.profile else []
        if kind == "wrong_tablet" and len(mine) == 1 and len(sims) > 1:
            f = features(image, lay.slots[i], mine[0])
            if f and _delta_e(f["lab"], self.profile["lab_med"]) <= self.profile["colour_tol"]:
                rest = {k: v for k, v in sims.items() if k != "wrong_tablet"}
                kind = max(rest, key=rest.get)
        return kind

    def _judge(self, image, strip_id, image_name) -> Verdict:
        v = Verdict("PASS", strip_id=strip_id, image_name=image_name, at=self._now())
        v.layout = lay = self._layout(image)  # its SAM 3.1 calls are traced by the Meta client, one row each
        t0 = time.perf_counter()
        try:
            return self._judge_one(image, lay, v)
        finally:
            if v.pocket_scores:  # DINOv2 ran: its own time only
                trace.record("dinov2-small", "local", "judge", (time.perf_counter() - t0) * 1000)

    def _judge_one(self, image, lay, v) -> Verdict:
        if _finder(lay) != self.layout_source:  # never judged with a different pocket finder than the one taught
            v.status, v.reasons = "HOLD", [dict(FINDER_DOWN)]
            return v
        if lay.note == "no strip":
            v.status = "NO_STRIP"
            return v
        cut = bool(lay.pills and self.strip_profile and strip_check(lay, self.strip_profile))
        if not lay.ok and not cut:  # a strip cut wrong no longer fits the taught pattern: it is named cut, below
            v.status, v.reasons = "HOLD", [{"rule": 1, "check": "count", "pocket": None,
                                            "text": f"Rule 1 failed: {lay.note}", "severity": "major"}]
            return v
        v.pocket_scores = self.judge.score_pockets(image, lay)
        listed, unlisted, measured = [], [], set()
        failing = [i for i, s in enumerate(v.pocket_scores) if s > self.judge.pocket_threshold]
        sims = {}  # per pocket the judge fails: how near each painted or registered real defect is
        if failing:
            embs, protos = self.judge.pocket_embeddings(image, lay).cpu(), self._prototypes()
            sims = {i: similarity(embs[i], protos) for i in failing}
        named = {i: self._name(image, lay, i, s) for i, s in sims.items()}  # the judge's name: the nearest one
        if cut:
            # the taught pocket template no longer fits a mis-sized strip, so per-pocket findings would be false
            self._find("strip_cut", listed, unlisted)
            v.skipped.append("pocket checks (strip cut)")
            v.reasons = listed + unlisted
            v.status = "REJECT" if listed else "HOLD"
            return v
        if lay.pills and self.profile:
            usable = 0
            for i, (slot, mine) in enumerate(zip(lay.slots, lay.in_pocket)):  # measured on the taught pocket scale
                usable += len(mine) > 1 or (len(mine) == 1 and features(image, slot, mine[0]) is not None)
                # the judge is sure it is empty: a measurement may not turn an outlined empty dome into a different tablet
                sure = i in sims and sims[i]["empty_pocket"] - sims[i]["broken_tablet"] >= EMPTY_MARGIN
                kind = pocket_check(image, slot, mine, self.profile, sure, i in failing)
                if kind:
                    self._find(kind, listed, unlisted, i)
                    measured.add(i)
            if not usable:
                v.skipped.append("tablet measurements")
        else:
            v.skipped.append("tablet measurements")
        if not (lay.pills and self.strip_profile):
            v.skipped.append("strip size")
        for i in failing:
            if i not in measured:
                self._find(named[i], listed, unlisted, i)
        if lay.pills and self.profile:
            # fail-safe: SAM 3.1 saw no tablet in a pocket the judge passed (a same-shape tablet of another
            # colour: 14 pills of 15, judge 0.34 < 0.43) - never passed, held for a hand check
            for i, mine in enumerate(lay.in_pocket):
                if not mine and i not in failing and i not in measured:
                    unlisted.append(dict(self._unlisted(i), text=f"Pocket {i + 1}: no tablet outlined — check by hand"))
        if self.judge.score_border(image, lay) > self.judge.border_threshold:
            self._find("foil_damage", listed, unlisted)
        v.reasons = listed + unlisted
        v.status = "REJECT" if listed else "HOLD" if unlisted else "PASS"
        return v

    def show_layout(self, frame) -> int:
        """Teaching: find the pockets on the photo just saved, so the live camera draws them while the strip stays
        put. Display only (a paid SAM 3.1 call); a failure shows no boxes and never stops the save. Pockets found."""
        try:
            lay = self._layout(frame)
        except Exception:
            return 0
        self.keep_view(frame, lay)
        return len(lay.pockets)

    def keep_view(self, frame, layout):
        """The live camera draws this layout while it still shows this frame (teach photo, challenge strip)."""
        self._last_view = (layout, frame.shape[:2], _thumb(frame))

    def layout_for(self, frame):
        """The layout of the last camera inspection while the camera still shows that view (same size, nearly the
        same picture): the supervisor's tools look there, unpaid. None when there is none, or the strip moved, was
        swapped or taken away."""
        if self._last_view is None or frame is None:
            return None
        lay, size, thumb = self._last_view
        return lay if frame.shape[:2] == size and np.abs(_thumb(frame) - thumb).mean() <= SAME_VIEW else None

    def inspect(self, image, strip_id=None, image_name=None, held: bool = False, batch=None, user=None,
                extra=None, camera: bool = False) -> Verdict:
        """camera: the image is a whole camera frame (not an upload or a tray crop), so its layout is kept for the
        supervisor."""
        if self.locked:
            v = Verdict("LOCKED", [{"rule": None, "check": "locked", "pocket": None, "text": self.lock_reason}],
                        strip_id=strip_id, image_name=image_name, at=self._now())
        else:
            with self._doing(self._finding):
                v = self._judge(image, strip_id, image_name)
            if camera:
                self._last_view = (v.layout, image.shape[:2], _thumb(image))
        return self.log(v, held, batch, image, user, extra)

    def log(self, v: Verdict, held: bool = False, batch=None, image=None, user=None, extra=None) -> Verdict:
        """Append one verdict to log.jsonl. held: the line is held, so a PASS is logged (and returned) as HOLD.
        extra: more fields for the row (a re-check marks its strips)."""
        line_held = held and v.status == "PASS"  # a good strip: no photo, so it never pushes real reject photos out
        if line_held:
            v.status, v.reasons = "HOLD", [{"rule": None, "check": "line_held", "pocket": None, "text": LINE_HELD}]
        v.at = v.at or self._now()
        row = dict(asdict(v), checklist_version=self.checklist["version"], batch=batch, user=user, **(extra or {}))
        if image is not None and self.snap and v.status in ("REJECT", "HOLD") and not line_held:
            try:
                row["photo"] = self.snap(image, v)
            except Exception:  # the photo is extra evidence: never lose the verdict row over it
                pass
        _append(self.dir / "log.jsonl", row)
        return v

    def self_test(self, good_image, rng_seed: int | None = None, record: bool | str = True) -> dict:
        """record=False: a diagnostic run that writes nothing. record="failures": a failed run is written like any
        failed self-test (it locks and opens quarantine), a pass never is (so it cannot unlock), and with no strip
        in view nothing is written."""
        if self.taught_at is None:
            raise ValueError(NEED_TEACH)
        with self._doing("Self-test: painting fake defects and checking each one" +
                         ("" if self.layout_source == "grid" else " (SAM 3.1)")):
            return self._self_test(good_image, rng_seed, record)

    def _self_test(self, good_image, rng_seed, record) -> dict:
        cl = self.checklist
        rng = np.random.default_rng(rng_seed)
        lay = self._layout(good_image)
        results, skipped, painted, good = [], [], [], None
        down = _finder(lay) != self.layout_source  # the taught pocket finder did not answer: nothing can be tested
        if lay.ok and not down:
            per = cl["per_row"]
            edge = [i for i in range(len(lay.pockets))
                    if i // per in (0, cl["rows"] - 1) or i % per in (0, per - 1)]
            measuring = bool(lay.pills and self.profile)
            for kind in cl["reject_if"] + ([] if "empty_pocket" in cl["reject_if"] else ["empty_pocket"]):  # Rule 1
                if kind in ("foil_damage", "strip_cut", "double_tablet"):  # listed as not covered, never painted: a painted cut slices tablets, a painted copy merges with the tablet or lands between pockets; real strips prove these
                    # shortcut: good strips' borders differ about as much as a painted tear (tears up to
                    # 1.5 tablets deep caught 17 of 24), so a tear test fails by chance. Upgrade: a sharper border check.
                    skipped.append(kind)
                    continue
                if kind == "strip_cut":  # painted once on the whole strip; pocket choice ignored
                    if not (lay.pills and self.strip_profile):
                        skipped.append(kind)
                        continue
                    p = None
                elif kind in MEASURED and not measuring:  # no SAM pills or no profile: it cannot be seen, so it is skipped
                    skipped.append(kind)
                    continue
                else:
                    # shortcut: wrong_tablet self-test changes size only (SAM may miss recoloured pills); the colour path is covered by unit tests
                    p = int(rng.choice(edge if kind == "foil_damage" else len(lay.pockets)))
                painted.append((kind, p, paint(good_image, lay.pockets[p or 0], kind, lay.strip_box, rng)))
            with ThreadPoolExecutor(SELF_TEST_WORKERS) as pool:  # judged together, read back in order
                judged = [pool.submit(self._judge, im, None, None) for _, _, im in painted]
                good_one = pool.submit(self._judge, good_image, None, None)
                for (kind, p, _), f in zip(painted, judged):
                    v = f.result()
                    if down := FINDER_DOWN in v.reasons:
                        for f in judged + [good_one]:
                            f.cancel()
                        break
                    results.append({"kind": kind, "pocket": None if p is None else p + 1,
                                    "caught": v.status == "REJECT", "named_right": any(r["check"] == kind for r in v.reasons)})
                good = None if down else good_one.result()
        if down or (good and FINDER_DOWN in good.reasons):  # written nowhere: it neither locks nor unlocks
            return {"passed": False, "not_run": NOT_RUN, "results": results, "good_passed": False,
                    "failed": ["pocket finder did not answer"], "strip_found": lay.ok, "skipped": skipped}
        good_passed = lay.ok and good.status == "PASS"
        failed = [r["kind"] for r in results if not (r["caught"] and r["named_right"])]
        if not good_passed:
            failed.append("good strip not passed")
        out = {"passed": not failed, "results": results, "good_passed": good_passed, "failed": failed,
               "strip_found": lay.ok, "skipped": skipped}
        if record is True or (record == "failures" and failed and lay.ok):
            _append(self.dir / "selftests.jsonl", dict(out, time=self.clock(), at=self._now(),
                                                       checklist_version=cl["version"], taught_at=self.taught_at))
        return out
