"""Shift Supervisor agent: when rejects rise or a self-test fails it measures, asks the operator and holds the line.

The model never decides pass/fail and never changes a verdict or the checklist. Re-teach, resume and quarantine
release are only requests until a QA person approves them with their password (`approve`).
"""
import json
import uuid
from contextlib import nullcontext
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from d2i.audit import _require_qa, release_quarantine
from d2i.judge import POCKET_SIZE, _crop
from d2i.pockets import find_layout
from d2i.record import _lines

SYSTEM = ("You supervise a blister-strip inspection bench on a packaging line. Investigate with tools before "
          "concluding. You never change a verdict or the checklist. To re-teach, resume or release quarantine, call "
          "the request_ tool; a QA person decides. End with one sentence naming the cause and the evidence.")
MAX_STEPS = 12
MAX_TOOL_CALLS = 20  # per investigation, on top of MAX_STEPS (one step may ask for several tools)
STEP_LIMIT = "Step limit reached — line held, call QA"
TOO_MANY = "investigation stopped: too many tool calls — call QA"
MODEL_DOWN = "Model unavailable — line held, call QA"
GLARE_FLOOR = 0.001  # a few hot pixels on a glare-free baseline are not drift


def _tool(name, description, **params):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": {k: {"type": t} for k, t in params.items()}, "required": list(params)}}}


TOOLS = [
    _tool("image_health", "Brightness, focus and glare of the current camera frame against the good teaching photos."),
    _tool("camera_shift", "How far the strip has moved in the camera frame since teaching."),
    _tool("run_self_test", "Self-test on the known-good strip now under the camera: painted fake defects must all "
                           "be caught and the good strip must pass."),
    _tool("reject_stats", "Verdict counts and failed rules over the last `window` strips.", window="integer"),
    _tool("compare_to_last_good", "Score of one pocket (1 = top left) on the current frame against its threshold; "
                                  "saves a side-by-side crop with a good photo for QA.", pocket="integer"),
    _tool("ask_operator", "Ask the operator at the bench one short question; returns their answer.", question="string"),
    _tool("hold_line", "Hold the line now (the safe direction).", reason="string"),
    _tool("request_reteach", "Ask QA to approve re-teaching from the good photos.", reason="string"),
    _tool("request_resume", "Ask QA to approve resuming the line.", reason="string"),
    _tool("request_release_quarantine", "Ask QA to approve releasing the quarantined strips.", reason="string"),
    _tool("draft_deviation", "Write a draft deviation note for QA review.", summary="string"),
]


def _measure(image) -> dict:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {"brightness": float(gray.mean()), "focus": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
            "glare": float((gray > 250).mean())}


def baseline_from(images) -> dict:
    """Mean brightness, focus (Laplacian variance) and glare fraction over the good teaching photos."""
    m = [_measure(im) for im in images]
    return {k: float(np.mean([x[k] for x in m])) for k in m[0]}


def image_health(frame, baseline: dict) -> dict:
    now = _measure(frame)
    change = {k: round(100 * (now[k] / baseline[k] - 1), 1) if baseline[k] else None for k in now}
    drift = (abs(change["brightness"] or 0) > 15 or (change["focus"] or 0) < -40
             or now["glare"] > max(2 * baseline["glare"], GLARE_FLOOR))
    return {**{k: round(v, 4) for k, v in now.items()}, "change": change, "drift": bool(drift)}


def hold_state(product_dir) -> tuple[bool, str]:
    """(held, who held it) from hold.json; an unreadable file counts as held (the safe direction)."""
    try:
        h = json.loads((Path(product_dir) / "hold.json").read_text(encoding="utf-8"))
        return h["held"] is not False, str(h.get("by") or "")
    except FileNotFoundError:
        return False, ""
    except Exception:
        return True, ""


def set_hold(product_dir, plc, on: bool, by: str):
    """PLC hold coil plus hold.json, so a restart keeps the line held."""
    plc.set_hold(on)
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (Path(product_dir) / "hold.json").write_text(json.dumps({"held": on, "at": at, "by": by}), encoding="utf-8")


def hold_line(product_dir, plc, audit, by: str, reason: str) -> dict:
    """Anyone may hold the line (the safe direction); only resume needs QA."""
    set_hold(product_dir, plc, True, by)
    audit.append("hold_line", by, {"hold": True}, reason)
    return {"held": True}


def resume_line(station, plc, audit, users, name: str, password: str, reason: str) -> dict:
    """QA releases the hold directly, with or without an agent request; refused while the station is LOCKED."""
    _require_qa(users, name, password)
    if not reason.strip():
        raise ValueError("a reason is required")
    if station.locked:
        return {"resumed": False, "message": f"Station locked: {station.lock_reason}"}
    set_hold(station.dir, plc, False, name)
    audit.append("resume_line", name, {"hold": False}, reason)
    return {"resumed": True, "message": "Line resumed"}


def camera_shift(layout, ref_box) -> dict:
    """Strip-centre movement against the taught strip box; drift past 5 % of the strip width."""
    (x0, y0, x1, y1), (r0, s0, r1, s1) = layout.strip_box, ref_box
    dx, dy = (x0 + x1 - r0 - r1) / 2, (y0 + y1 - s0 - s1) / 2
    limit = 0.05 * (r1 - r0)
    return {"dx": dx, "dy": dy, "drift": abs(dx) > limit or abs(dy) > limit}


class Supervisor:
    def __init__(self, station, plc, audit, chat, ask_operator, baseline, ref_box, get_frame, good_dir,
                 tool_lock=None):
        self.station, self.plc, self.audit, self.chat = station, plc, audit, chat
        self.ask, self.baseline, self.ref_box = ask_operator, baseline, ref_box
        self.get_frame, self.good_dir = get_frame, Path(good_dir)
        self.tool_lock = tool_lock  # any context manager; held around each tool call except ask_operator (it waits)
        self.pending = {}  # request id -> request, until QA approves it
        self._trigger, self._steps, self._new = "", [], []
        if hold_state(self.station.dir)[0]:
            plc.set_hold(True)  # a restart keeps the line held
        self._tools = {
            "image_health": lambda: image_health(self.get_frame(), self.baseline),
            "camera_shift": self._camera_shift,
            "run_self_test": self._self_test,
            "reject_stats": self._reject_stats,
            "compare_to_last_good": self._compare,
            "ask_operator": self._ask,
            "hold_line": self._hold,
            "request_reteach": lambda reason: self._request("reteach", reason),
            "request_resume": lambda reason: self._request("resume", reason),
            "request_release_quarantine": lambda reason: self._request("release_quarantine", reason),
            "draft_deviation": self._draft,
        }

    @property
    def steps(self) -> list:
        return list(self._steps)

    def investigate(self, trigger: str) -> dict:
        self._set_hold(True, "supervisor")  # held for the whole investigation; only QA releases it
        self._trigger, self._steps, self._new = trigger, [], []
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Trigger: {trigger}"}]
        conclusion, platform, over = STEP_LIMIT, "none", False
        for _ in range(MAX_STEPS):
            try:
                msg = self.chat.complete(messages, TOOLS)
                platform = self.chat.last_platform or platform
                calls = msg.get("tool_calls") or []
                if not calls:
                    conclusion = msg.get("content") or MODEL_DOWN
                    break
                messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
                for c in calls:
                    if len(self._steps) >= MAX_TOOL_CALLS:
                        over = True
                        break
                    fn = c.get("function") or {}
                    name = fn.get("name")
                    if not isinstance(name, str):
                        raise TypeError(f"malformed tool call: {str(c)[:200]}")
                    args, result = self._run(name, fn.get("arguments"))
                    text = json.dumps(result, default=str)
                    self._steps.append({"tool": name, "args": args, "result": result})
                    self.audit.append("agent_step", "supervisor",
                                      {"tool": name, "args": args, "result_summary": text[:300]})
                    messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": text})
            except Exception:  # model down (both platforms, timeout, spend stop) or a malformed reply: stay held
                conclusion = MODEL_DOWN
                break
            if over:
                self._set_hold(True, "supervisor")
                conclusion = TOO_MANY
                break
        fallback = conclusion == MODEL_DOWN
        self.audit.append("agent_conclusion", "supervisor",
                          {"trigger": trigger, "conclusion": conclusion, "platform": platform, "fallback": fallback})
        return {"steps": self._steps, "conclusion": conclusion, "pending": self._new, "fallback": fallback,
                "platform": platform}

    def _run(self, name, raw):
        """(args, result); bad arguments, unknown tools and tool errors go back to the model as an error result."""
        try:
            args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}, {"error": f"arguments are not valid JSON: {str(raw)[:200]}"}
        if name not in self._tools:
            return args, {"error": f"unknown tool {name!r}"}
        try:
            with (nullcontext() if name == "ask_operator" or self.tool_lock is None else self.tool_lock):
                return args, self._tools[name](**args)
        except Exception as e:
            return args, {"error": f"{type(e).__name__}: {e}"}

    def _layout(self, frame):
        return find_layout(frame, self.station.checklist, None, self.station.template)  # offline, no paid call

    def _camera_shift(self):
        lay = self._layout(self.get_frame())
        return dict(camera_shift(lay, self.ref_box), note=lay.note)

    def _self_test(self):
        r = self.station.self_test(self.get_frame(), record="failures")  # the operator has placed a known-good strip
        if not r["strip_found"]:
            return "no good strip under the camera"
        note = ("diagnostic only — does not unlock the station" if r["passed"]
                else "recorded as a failed self-test — station locked, quarantine open")
        return {"passed": r["passed"], "missed": r["failed"], "note": note}

    def _reject_stats(self, window):
        rows = _lines(self.station.dir / "log.jsonl")[-max(1, int(window)):]
        rules = Counter(f"rule {r['rule']}" for v in rows if v["status"] == "REJECT"
                        for r in v["reasons"] if r.get("rule") is not None)
        return {"strips": len(rows), "status": dict(Counter(v["status"] for v in rows)), "reject_rules": dict(rules)}

    def _compare(self, pocket):
        frame, st, i = self.get_frame(), self.station, int(pocket) - 1
        lay = self._layout(frame)
        if not lay.ok or not 0 <= i < len(lay.pockets):
            return {"error": lay.note or f"pocket must be 1 to {len(lay.pockets)}"}
        good = cv2.imread(str(sorted(self.good_dir.glob("*.jpg"))[0]))
        side = np.hstack([_crop(frame, lay.pockets[i], POCKET_SIZE),
                          _crop(good, self._layout(good).pockets[i], POCKET_SIZE)])
        path = self._stamped("evidence", f"-pocket{i + 1}.jpg")
        cv2.imwrite(str(path), side)
        return {"pocket": i + 1, "score": round(st.judge.score_pockets(frame, lay)[i], 4),
                "threshold": round(st.judge.pocket_threshold, 4), "evidence": str(path)}

    def _ask(self, question):
        try:
            return self.ask(question)  # the screen's callable raises TimeoutError after 120 s
        except TimeoutError:
            return "no answer"

    def _hold(self, reason):
        self._set_hold(True, "supervisor")
        return {"held": True}

    def _set_hold(self, on: bool, by: str):
        set_hold(self.station.dir, self.plc, on, by)  # never cached: the screen's /hold and /resume write it too

    def _request(self, kind, reason):
        req = {"id": uuid.uuid4().hex[:8], "kind": kind, "reason": f"Agent request: {reason}"}
        self.pending[req["id"]] = req
        self._new.append(req)
        return {"id": req["id"], "status": "waiting for a QA person to approve"}

    def _draft(self, summary):
        path = self._stamped("deviations", ".md")
        steps = "\n".join(f"{n}. {s['tool']} {json.dumps(s['args'])} -> {json.dumps(s['result'], default=str)[:300]}"
                          for n, s in enumerate(self._steps, 1))
        path.write_text(f"# DRAFT — needs QA review\n\nTrigger: {self._trigger}\n\nSummary: {summary}\n\n"
                        f"## Steps\n\n{steps}\n\nBuilt with Muse Spark Open\n", encoding="utf-8")
        return {"path": str(path)}

    def _stamped(self, folder, suffix) -> Path:
        d = self.station.dir / folder
        d.mkdir(exist_ok=True)
        return d / f"{datetime.now():%Y%m%d-%H%M%S-%f}{suffix}"

    def approve(self, request_id: str, users, name: str, password: str) -> dict:
        """QA only. A request is approved once; a refused or failed one goes back to pending."""
        _require_qa(users, name, password)
        req = self.pending.pop(request_id, None)
        if req is None:
            raise ValueError(f"no pending request {request_id}")
        kind, st = req["kind"], self.station
        try:
            if kind == "resume" and st.locked:
                self.pending[request_id] = req
                out = {"approved": False, "message": f"Station locked: {st.lock_reason}"}
            else:
                if kind == "reteach":
                    photos = [cv2.imread(str(p)) for p in sorted(self.good_dir.glob("*.jpg"))]
                    st.teach([im for im in photos if im is not None])
                    message = "Re-taught; the station stays locked until a self-test passes"
                elif kind == "resume":
                    self._set_hold(False, name)
                    message = "Line resumed"
                else:
                    release_quarantine(st.dir, self.audit, users, name, password, req["reason"])
                    message = "Quarantine released"
                out = {"approved": True, "message": message}
        except BaseException:
            self.pending[request_id] = req
            raise
        out = {"id": request_id, "kind": kind, "approved_by": name, **out}
        self.audit.append("agent_approve", name, out, req["reason"])
        return out
