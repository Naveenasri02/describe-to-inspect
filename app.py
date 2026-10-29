"""Bench web app: python app.py --source 0|<video>|<photo folder> --product blister --port 8000 [--host 0.0.0.0]"""
import argparse
import html
import hashlib
import json
import mimetypes
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from d2i import challenge, recheck, record, trace
from d2i.commands import KEYWORDS, match
from d2i.audit import (AuditLog, Users, _require_qa, log_physical_challenge, quarantine_list, release_quarantine,
                       check_signer, write_signature)
from d2i.batch import Batches, line_clearance
from d2i.checklist import approve, make_checklist
from d2i.llm import ToolChat
from d2i.meta_api import Budget, MetaClient
from d2i.ipqc import DRIFT_REJECTS, check_events, due, reconcile, reject_run, release_blockers
from d2i.openitems import (challenge_failure, last_started, missed_checks, missed_text, open_items, signed,
                           window_id, print_by_eye as by_eye_read)
from d2i.printcheck import check_print, differs, read_print
from d2i.pockets import find_layout
from d2i.station import FINDER_DOWN, NEED_TEACH, Station, Verdict
from d2i.supervisor import Supervisor, baseline_from, hold_line, hold_state, pending_requests, resume_line
from d2i.tray import inspect_clip, record_clip

STATIC = Path(__file__).parent / "static"
mimetypes.add_type("font/woff2", ".woff2")  # Windows may not know it: the bundled fonts would go out as text/plain
NEED_GOOD = 5
NO_CHECKLIST = "Describe and approve the good strip first"
NO_BATCH = "Start a batch first (QA)"
LANGS = {"English", "Tamil", "Hindi"}  # "Auto" = omit
ANSWER_WAIT = 120  # seconds the agent waits for the operator
INSPECT_ERROR = "inspection error — check by hand"
TRAY_SECONDS = 4  # 60 frames at 15 fps: one SAM 3.1 video call


class FrameSource:
    """Latest camera/video/photo-folder frame, read by a background thread."""

    def __init__(self, source):
        self.frame, self.lock, self.stop = None, threading.Lock(), threading.Event()
        self.source = str(source)
        threading.Thread(target=self._run, daemon=True).start()

    def get(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def _set(self, f):
        with self.lock:
            self.frame = f

    def _run(self):
        p = Path(self.source)
        if p.is_dir():  # photo folder: cycle every 2 s
            files = sorted(x for x in p.iterdir() if x.suffix.lower() in (".jpg", ".jpeg", ".png"))
            i = 0
            while files and not self.stop.is_set():
                im = cv2.imread(str(files[i % len(files)]))
                if im is not None:
                    self._set(im)
                i += 1
                self.stop.wait(2)
            return
        cap = cv2.VideoCapture(int(self.source) if self.source.isdigit() else self.source)
        while not self.stop.is_set():
            ok, f = cap.read()
            if not ok:
                if not self.source.isdigit():
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # video file: loop
                self.stop.wait(1)
                continue
            self._set(f)
            self.stop.wait(0.03)


def source_kind(source: str) -> str:
    """What a live frame came from, for the record: a camera (an index), a photo folder or a video (a file or stream)."""
    return "camera" if source.isdigit() else "photo folder" if Path(source).is_dir() else "video"


RECORD_EXPIRED = "Record expired — open it again before signing"


class _NoPlc:  # no PLC attached (tests): the hold still lives in hold.json
    def set_hold(self, on):
        pass


RED, BLUE, GREEN = (42, 42, 194), (200, 120, 30), (74, 128, 16)  # BGR: screen reject red, strip blue, pass green


def draw_pockets(f, boxes, bad, rules=None):
    """Pocket boxes: failing ones red (with their rule number when `rules` is given), the rest green. Shared by /stream and the reject photo."""
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        cv2.rectangle(f, (x0, y0), (x1, y1), RED if i + 1 in bad else GREEN, 3)
        if rules and i + 1 in rules:
            cv2.putText(f, f"R{rules[i + 1]}", (x0 + 4, y0 + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, RED, 2)


def create_app(product_dir: Path, source, client=None, plc=None, clock=time.time, chat=None,
               budget: Budget | None = None) -> FastAPI:
    product_dir = Path(product_dir)
    product_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=STATIC), name="static")  # station.css / .js and the bundled fonts
    trace.set_path(product_dir / "trace.jsonl")
    audit, users = AuditLog(product_dir / "audit.jsonl"), Users(product_dir.parent / "users.json")
    sessions = {}  # token -> (name, role); in memory, lost on restart
    inv = {"running": False, "question": None, "result": None, "answer": None, "event": threading.Event(), "sup": None}
    sup_lock = threading.RLock()  # supervisor build/cache and the running flag, together
    q_lock = threading.Lock()  # the open operator question, its answer and the Event, together
    records = {}  # record id -> (html, sha256) of what /record served; last 20
    st = Station(product_dir, client=client, clock=clock)
    batches = Batches(product_dir)
    budget = budget or Budget()  # one spend file for every paid call (Meta client and agent chat): the $50 stop
    src = FrameSource(source) if source is not None else None
    lock = threading.RLock()  # one slow station operation at a time
    meta = threading.RLock()  # short: checklist / pending / last-verdict references
    state = {"pending": None, "last": None, "client": client, "overlay": (0.0, [])}
    app.state.station, app.state.source = st, src

    def get_client():  # built lazily so a missing key is reported when a paid call is needed
        if state["client"] is None:
            state["client"] = MetaClient(budget)
        return state["client"]

    def image_from(upload: UploadFile | None):
        if upload is not None:
            return cv2.imdecode(np.frombuffer(upload.file.read(), np.uint8), cv2.IMREAD_COLOR)
        return src.get() if src else None

    def who(request: Request):
        return sessions.get(request.cookies.get("d2i_session"), ("operator", None))

    def held_now():
        return hold_state(product_dir)[0]

    if plc and held_now():
        plc.set_hold(True)  # a restart keeps the line held

    def quarantine_count():
        try:
            return len(quarantine_list(product_dir, audit)["strips"]), False
        except Exception:
            return None, True  # unknown is shown, never hidden

    def sync_lock():  # PLC coil 1 = the station's real lock state
        if plc:
            try:
                locked = st.locked
            except Exception:  # unreadable self-test log: locked (the safe direction)
                locked = True
            plc.set_locked(locked)

    def publish(verdicts, count=True):
        """Each verdict to the PLC in order; the last one is shown on the screen. count=False: re-checked strips."""
        for v in verdicts:
            if plc:
                plc.set_verdict(v, count=count)
        with meta:
            state["last"] = asdict(verdicts[-1])
        return state["last"]

    def station_hold(reason, by="station"):  # the existing hold (hold.json + PLC coil + audit), raised by the station itself
        if not held_now():  # already held: the first hold stays open, no second one
            hold_line(product_dir, plc or _NoPlc(), audit, by, reason)
        if reason.startswith("Critical"):  # wrong tablet / wrong print, held line or not: re-check since the last good check
            recheck.open_window(product_dir, audit, reason, by)

    def log_rows():  # log.jsonl verdict rows; an unreadable line is skipped, so it never errors /status or /inspect
        rows = []
        try:
            for line in (product_dir / "log.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("status"), str):
                        rows.append(row)
                except ValueError:
                    pass
        except OSError:
            pass
        return rows

    def after_verdicts(vs, no, rechecked=False):  # call inside `lock`, after the verdicts are logged (the last len(vs) log rows)
        crit = next((r["text"] for v in vs for r in v.reasons if r.get("severity") == "critical"), None)
        if crit and not rechecked:  # also on a held line: a critical opens a re-check (a re-checked strip is in one already)
            return station_hold("Critical: " + crit)
        if held_now():  # already held: the first hold stays open, no second one
            return
        rows, since = log_rows(), recheck.resumed_at(audit)  # Resume line or an approved agent resume
        if any(reject_run(rows[:n], no, since) >= DRIFT_REJECTS for n in range(len(rows) - len(vs) + 1, len(rows) + 1)):
            station_hold("3 rejects in a row — check the machine")  # checked after each new verdict: a later pass cannot hide it

    def need_batch():  # inspecting without an open batch would leave verdicts no batch owns: refuse, log nothing
        b = batches.open
        return (b["batch"], None) if b else (None, JSONResponse({"error": NO_BATCH}, status_code=409))

    def blockers_for(b):  # what still stops this open batch from being inspected
        signed = any(e["action"] == "specimen_signed" and e["details"].get("batch") == b["batch"] for e in audit.entries())
        chal = challenge.runs(product_dir) if challenge.load_set(product_dir) else None  # no set: no challenge run needed
        return release_blockers(b, st._selftests(), record._lines(product_dir / "prints.jsonl"), signed, chal)

    def print_by_eye(b):  # the open batch's latest print read, when it waits for QA's check by eye
        return by_eye_read(product_dir, b["batch"])

    def not_released(b):
        left = blockers_for(b)
        return JSONResponse({"error": "Batch not released: " + ", ".join(left)}, status_code=409) if left else None

    def paid(fn):  # the screen never sees a 500: any failure becomes plain text
        try:
            return fn()
        except Exception as ex:
            return {"error": str(ex) or type(ex).__name__}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/lamp", response_class=HTMLResponse)
    def lamp():
        return (STATIC / "lamp.html").read_text(encoding="utf-8")

    @app.get("/good/{i}")
    def good_photo(i: int):  # the saved teaching photos, by position, for the Setup screen
        files = sorted((product_dir / "good").glob("*.jpg"))
        if not 0 <= i < len(files):
            return JSONResponse({"error": "No such photo"}, status_code=404)
        return FileResponse(files[i], media_type="image/jpeg")

    @app.get("/status")
    def status(request: Request):
        rows = log_rows()
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        try:
            spent = budget.spent
        except Exception:  # unreadable spend file: shown as unknown; paid calls are refused
            spent = None
        with meta:
            cl, pending, last = st.checklist, state["pending"], state["last"]
        try:
            open_batch = batches.open
        except Exception:  # unreadable batch file: shown as no batch (inspection is refused then)
            open_batch = None
        try:
            locked = st.locked
            reason = st.lock_reason if cl else NO_CHECKLIST
            tests = st._selftests()
            uncovered = tests[-1].get("skipped", []) if tests else []
            last_test = tests[-1] if tests else None
        except Exception as ex:  # unreadable self-test log: stay locked
            locked, reason, uncovered, last_test = True, f"Cannot read self-test log: {ex}", [], None
        if plc:
            plc.set_locked(locked)  # every poll, so shift expiry reaches coil 1
        (held, by), (qn, qerr) = hold_state(product_dir), quarantine_count()
        try:
            blockers = blockers_for(open_batch) if open_batch else []
            eye = (print_by_eye(open_batch) or {}).get("reasons") if open_batch else None
            overdue = due(open_batch, check_events(product_dir, open_batch["batch"]), clock()) if open_batch else []
            totals = reconcile(rows, open_batch["batch"]) if open_batch else None
            recon = "open" if open_batch else next((r["totals"].get("status") for r in reversed(batches.rows)
                                                      if r["event"] == "end"), None)
            hold_reason = next((e["reason"] for e in reversed(audit.entries()) if e["action"] == "hold_line" and e["user"] == by),
                               "") if held and by != "supervisor" else ""  # the supervisor's hold has no hold_line row
        except Exception:  # unreadable records: not released (the safe direction)
            blockers, overdue, totals, hold_reason, recon, eye = ["Cannot read the batch records"], [], None, "", None, None
        if held:
            by = "the Shift Supervisor" if by == "supervisor" else by
            # held and locked: LOCKED, as the board says STOPPED (the more serious state; the lamp shows this word)
            banner, text = "LOCKED" if locked else "HOLD", (f"Line held by {by} — QA must resume." if by else
                                                             "Line held — QA must resume.") + (f" {reason}" if locked else "")
        elif locked:
            banner, text = "LOCKED", reason
        elif blockers:
            banner, text = "NOT RELEASED", "Batch not released: " + ", ".join(blockers)
        elif last and last["status"] in ("REJECT", "HOLD"):
            banner, text = last["status"], " ".join(r["text"] for r in last["reasons"])
        elif last and last["status"] in ("PASS", "NO_STRIP"):
            banner, text = last["status"].replace("_", " "), ""  # NO STRIP is grey, never green
        else:  # unlocked (so taught and self-tested), not held, no strip judged yet this session
            banner, text = "READY", ""
        return {"locked": locked, "lock_reason": reason if locked else "",
                "checklist_version": cl and cl["version"], "spent_usd": spent,
                "counts": counts, "batch": open_batch, "blockers": blockers, "print_by_eye": eye, "due": overdue, "last_verdict": last, "selftest_skipped": uncovered, "state": banner, "text": text,
                "rules": cl["rules"] if cl else [], "pending": pending["rules"] if pending else None,
                "held": held, "quarantine_count": qn, "quarantine_error": qerr,
                "checklist": cl, "pending_sentence": pending and pending.get("sentence"), "totals": totals, "reconciliation": recon,
                "selftest": last_test, "taught": st._template is not None, "step": st.step,  # what a long action is doing
                "good_photos": len(list((product_dir / "good").glob("*.jpg"))), "hold_reason": hold_reason,
                "machine_link": plc.connected if plc else None,
                "user": dict(zip(("name", "role"), who(request))) if request.cookies.get("d2i_session") in sessions else None}

    @app.post("/describe")
    async def describe(request: Request):
        def run(text, audio, langs):
            c = get_client()
            if audio is not None:
                text = c.transcribe(audio, languages=langs)
            cl = make_checklist(text, c)
            with meta:
                state["pending"] = cl
            return {"checklist": cl, "transcript": text}
        try:
            qa(request)  # teaching is QA-only, from the sentence on (and no paid call for anyone else)
        except PermissionError as ex:
            return {"error": str(ex)}
        try:
            if "multipart" in request.headers.get("content-type", ""):
                form = await request.form()
                if "audio" not in form:
                    return {"error": "No audio was received."}
                audio = await form["audio"].read()
                lang = form.get("languages")
                args = (None, audio, [lang] if lang in LANGS else None)
            else:
                args = (str((await request.json()).get("text", "")), None, None)
        except Exception as ex:
            return {"error": f"Could not read the request: {ex}"}
        return await run_in_threadpool(paid, lambda: run(*args))

    @app.post("/approve")
    def approve_ep(request: Request):
        def run():
            name = qa(request)  # the logged-in QA user is the approver
            with meta:  # check and set together
                if not state["pending"]:
                    return {"error": "Nothing to approve: describe the good strip first."}
                st.checklist = approve(state["pending"], name, product_dir / "checklists.jsonl")
                state["pending"] = None
                sync_lock()
                audit.append("checklist_approve", name, {"approved_by": name, "version": st.checklist["version"]})
                return {"checklist": st.checklist}
        return paid(run)

    @app.post("/teach")
    def teach(request: Request, body: dict | None = None):
        def run():
            name = qa(request)  # QA teaches, including saving the photos
            good = product_dir / "good"
            good.mkdir(exist_ok=True)
            if (body or {}).get("capture"):
                f = src.get() if src else None
                if f is None:
                    return {"error": "No camera frame to save."}
                cv2.imwrite(str(good / f"{int(clock() * 1000)}.jpg"), f)
            files = sorted(good.glob("*.jpg"))
            if len(files) < NEED_GOOD:
                return {"saved": len(files), "need": NEED_GOOD}
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            imgs, skipped = [], []
            for p in files:
                im = cv2.imread(str(p))
                if im is None:
                    skipped.append(p.name)
                else:
                    imgs.append(im)
            if len(imgs) < NEED_GOOD:
                return {"error": f"Only {len(imgs)} readable photos; need {NEED_GOOD}.", "skipped": skipped}
            with lock:
                out = {"taught": len(imgs), "skipped": skipped, "info": st.teach(imgs)}
                sync_lock()
                audit.append("teach", name, {"photos": len(imgs), "taught_by": name})
            with sup_lock:
                if not inv["running"]:
                    inv["sup"] = None  # rebuilt from the new good photos at the next use; requests.json keeps its requests
            return out
        return paid(run)

    @app.post("/selftest")
    def selftest(request: Request, image: UploadFile | None = File(None)):
        def run():
            name = qa(request)  # a self-test unlocks or locks the station: QA only
            im = image_from(image)
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            if im is None:
                return {"error": "No good strip in view."}
            with lock:
                r = st.self_test(im)
                sync_lock()
                if r.get("not_run"):  # the finder did not answer: not a failed self-test
                    audit.append("selftest_not_run", name, {"not_run": r["not_run"]})
                else:
                    audit.append("selftest", name, {"passed": r.get("passed")})
                return r
        return paid(run)

    @app.post("/inspect")
    def inspect(request: Request, image: UploadFile | None = File(None)):
        def run():
            user = login_name(request)
            im = image_from(image)
            if not st.checklist:
                return {"status": "NO_CHECKLIST", "reasons": [{"text": NO_CHECKLIST}]}
            if im is None:
                return {"error": "No image."}
            with lock:  # inspect and publish together so verdicts land in order; the open batch read under it too
                no, refused = need_batch()
                refused = refused or not_released(batches.open) or ejected_first()
                if refused:
                    return refused
                rc = recheck.current(product_dir) if held_now() else None  # a re-check runs on a held line only
                mark = {"recheck": True, "window_id": rc["window_id"]} if rc else None
                try:  # a re-checked strip keeps its real verdict while the line is held; camera: not an upload
                    v = st.inspect(im, held=held_now() and not rc, batch=no, user=user, extra=mark, camera=image is None)
                except Exception:  # logged and held, never a 500 that leaves the PLC on the previous verdict
                    v = Verdict("HOLD", [{"rule": None, "check": "inspection_error", "pocket": None,
                                          "text": INSPECT_ERROR}], at=st._now())
                    out = publish([v], count=not rc)  # the hold reaches the PLC even if the log cannot be written either
                    st.log(v, batch=no, image=im, user=user, extra=mark)
                    if rc:
                        recheck.count(product_dir, rc, v.status)
                    return out
                out = publish([v], count=not rc)  # a re-checked strip was counted the first time
                if rc:
                    recheck.count(product_dir, rc, v.status)
                after_verdicts([v], no, rechecked=bool(rc))
                return out
        return paid(run)

    @app.post("/print")
    def print_check(request: Request, image: UploadFile | None = File(None)):
        def run():
            name = who(request)[0] if who(request)[1] else None
            if name is None:
                return {"error": "Log in first"}
            im = image_from(image)
            if im is None:
                return {"error": "No camera frame to check."}
            b = batches.open
            if not b:
                return JSONResponse({"error": NO_BATCH}, status_code=409)
            read = read_print(im, get_client())  # MetaError / BudgetExceeded reach paid(): plain text, nothing logged
            out = check_print(read, b, date.fromtimestamp(clock()))
            out["by_eye"] = out["status"] == "HOLD"  # the model only flags (a difference, a blank, a cut-off): QA decides at /print/eye
            with meta:
                waiting = print_by_eye(b)  # read again before QA decided: that read is superseded, never decided
                batches._write("prints.jsonl", {"at": datetime.fromtimestamp(clock()).isoformat(timespec="seconds"), "time": clock(),
                                                "batch": b["batch"], "read": read, "status": out["status"],
                                                "reasons": out["reasons"], "by": name, "by_eye": out["by_eye"],
                                                **({"supersedes": waiting["at"]} if waiting else {})})
            return out
        return paid(run)

    @app.post("/print/eye")
    def print_eye(body: dict, request: Request):
        """QA's decision on a flagged print read: "matches" (note needed) is PRINT_OK, "wrong" a critical wrong print."""
        def run():
            name = qa(request)
            _require_qa(users, name, str(body.get("password", "")))  # re-typed password
            result, note = str(body.get("result", "")), str(body.get("note", "")).strip()
            if result not in ("matches", "wrong"):
                return {"error": "Result must be matches or wrong"}
            if result == "matches" and not note:
                return {"error": "Write a note: what you checked on the strip"}
            with meta:
                b = batches.open
                if not b:
                    return JSONResponse({"error": NO_BATCH}, status_code=409)
                held = print_by_eye(b)
                if not held:
                    return {"error": "No print check waits for a check by eye"}
                diff = differs(held["reasons"]) or "; ".join(held["reasons"])  # an unreadable read: what was flagged
                row = batches._write("prints.jsonl", {
                    "at": datetime.fromtimestamp(clock()).isoformat(timespec="seconds"), "time": clock(), "batch": b["batch"],
                    "read": held["read"], "status": "PRINT_OK" if result == "matches" else "WRONG_PRINT",
                    "reasons": ["Print matches (checked by eye): " + note if result == "matches" else "Wrong print (checked by eye): " + diff],
                    "by": name, "checked_by_eye": result, "read_at": held["at"]})
                audit.append("print_checked_by_eye", name, {"batch": b["batch"], "result": result, "read_at": held["at"],
                                                            "reasons": held["reasons"]}, note)
            if result == "wrong":  # after the row is written: a failing hold must not lose the decision
                with lock:
                    station_hold("Critical: wrong print — " + diff, by=name)
            return row
        return paid(run)

    @app.post("/tray")
    def tray(request: Request):
        def run():
            user = login_name(request)
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            s = src.source if src else ""
            if not s or Path(s).is_dir() or Path(s).suffix.lower() in (".jpg", ".jpeg", ".png"):
                return {"error": "tray clip needs a camera or video source"}
            with lock:  # one slow station operation at a time; verdicts land in order; the open batch read under it too
                no, refused = need_batch()
                refused = refused or not_released(batches.open) or ejected_first()
                if refused:
                    return refused
                if recheck.current(product_dir):  # each re-checked strip is counted one at a time
                    return {"error": "Re-check open: inspect the strips one at a time with Inspect strip"}
                if st.locked:  # no paid SAM video call for a locked station
                    v = st.log(Verdict("LOCKED", [{"rule": None, "check": "locked", "pocket": None, "text": st.lock_reason}]), batch=no, user=user)
                    publish([v])
                    return {"verdicts": [asdict(v)]}
                clip = record_clip(int(s) if s.isdigit() else s, TRAY_SECONDS, product_dir / "tray.mp4")
                vs = inspect_clip(clip, st, get_client(), held=held_now(), batch=no, user=user)  # each one is already logged
                publish(vs)
                after_verdicts(vs, no)
                return {"verdicts": [asdict(v) for v in vs]}
        return paid(run)

    def make_record(batch, operator, hours):
        now = clock()
        th = lambda v: "not taught" if v is None else f"{v:.4g}"
        cfg = [["Pocket threshold", th(getattr(st.judge, "pocket_threshold", None))],
               ["Border threshold", th(getattr(st.judge, "border_threshold", None))],
               ["Camera source", src.source if src else "none"]]
        since = now - hours * 3600
        fp = record.fingerprint(product_dir, since, now)  # taken first: a change in between only makes a signature "changed"
        return record.shift_record(product_dir, since, now, batch, operator, state["client"], audit, config=cfg), fp

    @app.get("/record", response_class=HTMLResponse)
    def shift_record(batch: str = "", operator: str = "", hours: float = 8.0):
        try:
            shown = record.shown_batch(product_dir, batch)
            if batch.strip() and not any(r["event"] == "start" and r["batch"] == shown for r in batches.rows):
                raise ValueError(f"No batch {shown} was started")  # its record would show another batch's sections
            body, fp = make_record(shown, operator, hours)
            sha, rid = hashlib.sha256(body.encode("utf-8")).hexdigest(), uuid.uuid4().hex[:8]
            records[rid] = (body, sha, shown, fp)
            while len(records) > 20:
                records.pop(next(iter(records)))
            note = f"<p>Record id: {rid} · SHA-256 of the record above: {sha}</p>"
            return HTMLResponse(body.replace("</body>", note + "</body>"), headers={"X-Record-Id": rid})
        except Exception as ex:
            return f"<!doctype html><meta charset='utf-8'><p>The shift record could not be made: {html.escape(str(ex))}</p>"

    @app.post("/ask")
    def ask(body: dict):
        return paid(lambda: {"answer": record.ask(str(body.get("question", "")), product_dir, get_client())})

    def login_name(request: Request):  # any logged-in user (operator or QA): verdicts are attributed
        name, role = who(request)
        if role not in ("operator", "qa"):
            raise PermissionError("Log in first")
        return name

    def qa(request: Request):
        name, role = who(request)
        if role != "qa":
            raise PermissionError("Log in as a QA user first")
        return name

    @app.post("/clearance")
    def clearance(request: Request, image: UploadFile | None = File(None)):
        def run():
            name = qa(request)
            im = image_from(image)
            if im is None:
                return {"error": "No camera frame to check."}
            out = line_clearance(im, get_client())
            with meta:
                batches.clearance(out, name, clock())
                audit.append("line_clearance", name, dict(out, cleared_by=name))
            return out
        return paid(run)

    @app.post("/batch/start")
    def batch_start(request: Request, batch: str = Form(""), mfg: str = Form(""), exp: str = Form(""),
                    mrp: str = Form(""), product: str = Form(""), strength: str = Form("")):
        def run():
            name = qa(request)
            with meta:
                row = batches.start(batch, mfg, exp, mrp, name, clock(), product=product, strength=strength)
                audit.append("batch_start", name, {k: row[k] for k in ("batch", "mfg", "exp", "mrp", "product", "strength", "clearance_at")})
            return row
        return paid(run)

    @app.post("/leak")
    def leak(request: Request, result: str = Form(""), note: str = Form(""), samples: int = Form(0)):
        def run():
            name = qa(request)
            with meta:
                row = batches.leak(result, name, note, clock(), samples)
                audit.append("leak_test", name, {"batch": row["batch"], "result": result, "note": note, "samples": samples})
            if result == "fail":  # after the row is written, like a wrong print
                with lock:
                    station_hold("Leak test failed — check the sealing")
            return row
        return paid(run)

    @app.post("/specimen/sign")
    def specimen_sign(body: dict, request: Request):
        def run():
            name = qa(request)
            _require_qa(users, name, str(body.get("password", "")))  # re-typed password
            with meta:
                b = batches.open
                if not b:
                    return JSONResponse({"error": NO_BATCH}, status_code=409)
                ok = next((r for r in reversed(record._lines(product_dir / "prints.jsonl"))
                           if r.get("batch") == b["batch"] and r.get("status") == "PRINT_OK"), None)
                if not ok:
                    return {"error": "The start-up specimen needs a PRINT_OK print check first"}
                return audit.append("specimen_signed", name, {"batch": b["batch"], "print_at": ok["at"]})
        return paid(run)

    @app.post("/foil-change")
    def foil_change(request: Request):
        def run():
            name, role = who(request)
            if not role:
                return {"error": "Log in first"}
            with meta:
                row = batches.foil_change(name, clock())
                audit.append("foil_change", name, {"batch": row["batch"]})
            return row
        return paid(run)

    @app.post("/batch/end")
    async def batch_end(request: Request):
        f = dict(await request.form())  # typed counts, limits, clearance tick, and password + reason when outside a limit

        def run():
            name = qa(request)
            keys = ("received", "good_tray", "reject_box", "samples", "unused",
                    "foil_issued", "foil_used", "foil_returned", "foil_destroyed")
            for k in ("received", "good_tray", "reject_box", "foil_issued"):
                if not str(f.get(k, "")).strip():
                    return {"error": f"{k.replace('_', ' ').capitalize()} must be entered"}  # blank is never read as 0
            def num(k, blank):  # a plain number: float() also reads "1_0", "1e1", "nan" and "inf"
                s = str(f.get(k, "")).strip() or blank
                if not re.fullmatch(r"-?(\d+(\.\d+)?|\.\d+)", s):
                    raise ValueError(s)
                return float(s)

            try:
                counts = {k: num(k, "0") for k in keys}
                limits = {"strips": num("strips_limit", "0"), "foil": num("foil_limit", "0.5")}
            except ValueError:
                return {"error": "Counts and limits must be numbers"}
            if min(*counts.values(), *limits.values()) < 0:
                return {"error": "Counts and limits cannot be negative"}
            if any(counts[k] != int(counts[k]) for k in keys[:5]):
                return {"error": "Strip counts must be whole numbers"}
            for k in keys[:5]:
                counts[k] = int(counts[k])
            if str(f.get("cleared", "")).lower() not in ("true", "1", "on"):
                return {"error": "Tick the after-batch line clearance: line cleared, leftover printed foil returned or destroyed"}
            with lock, meta:  # the station lock: a strip being inspected lands before the totals, or is refused after
                cur = batches.open
                if not cur:
                    return JSONResponse({"error": NO_BATCH}, status_code=409)
                if recheck.active(product_dir, cur):  # the re-checked strips belong to this batch's counts
                    return {"error": "Finish or waive the re-check first."}
                if print_by_eye(cur):  # its decision belongs to this batch, and /print/eye needs the batch open
                    return {"error": "Decide the print check by eye first."}
                totals = reconcile(record._lines(product_dir / "log.jsonl"), cur["batch"], counts, limits)
                totals["cleared"] = True
                if totals["status"] == "outside":
                    why = str(f.get("reason", "")).strip()
                    off = "; ".join(f"{l['name']} off by {l['difference']:g} {l['unit']} (site policy limit {l['limit']:g})"
                                    for l in totals["lines"] if l["outside"])
                    if not why:
                        return {"error": f"Outside limit: {off}. Enter the QA password and an investigation reason to end the batch.",
                                "outside": True}
                    _require_qa(users, name, str(f.get("password", "")))
                    totals["investigated"] = {"by": name, "reason": why}
                row = batches.end(name, clock(), totals)
                audit.append("batch_end", name, {"batch": row["batch"], "totals": totals})
            return row
        return await run_in_threadpool(paid, run)

    @app.post("/login")
    def login(body: dict):
        name = str(body.get("name", "")).strip()
        role = users.check(name, str(body.get("password", ""))) if name else None
        if role is None:
            return {"error": "Wrong name or password"}
        token = secrets.token_urlsafe(24)
        sessions[token] = (name, role)
        r = JSONResponse({"name": name, "role": role})
        r.set_cookie("d2i_session", token, httponly=True, samesite="strict")
        return r

    @app.post("/logout")
    def logout(request: Request):
        sessions.pop(request.cookies.get("d2i_session"), None)
        r = JSONResponse({"ok": True})
        r.delete_cookie("d2i_session")
        return r

    @app.get("/quarantine")
    def quarantine():
        return paid(lambda: quarantine_list(product_dir, audit))

    @app.post("/release")
    def release(body: dict, request: Request):
        return paid(lambda: release_quarantine(product_dir, audit, users, qa(request), str(body.get("password", "")),
                                               str(body.get("reason", ""))))

    @app.post("/sign")
    def sign(body: dict, request: Request):
        def run():
            name = qa(request)
            rec = records.get(str(body.get("record_id", "")))
            if rec is None:
                return {"error": RECORD_EXPIRED}
            meaning = str(body.get("meaning", ""))
            check_signer(users, name, str(body.get("password", "")), meaning)  # the slow hash, before the lock
            with lock:  # holds, quarantine, challenge results and requests are written under it: none lands in between
                now, batch = clock(), rec[2]
                left = [i["text"] for i in open_items(product_dir, now) if i["kind"] != "record_unsigned"]
                if batch and batch != last_started(product_dir):  # an older batch: its own missed checks too
                    left += [f"Batch {batch}: " + missed_text(m) for m in missed_checks(product_dir, now, batch=batch)]
                if left:  # the board's "waiting for a person" list: each one is closed first
                    more = f"; and {len(left) - 5} more" if len(left) > 5 else ""
                    return {"error": "Sign refused — still waiting: " + "; ".join(left[:5]) + more}
                return write_signature(rec[0], audit, name, meaning, product_dir=product_dir, batch=batch, extra=rec[3])
        return paid(run)

    @app.get("/missed")
    def missed(batch: str = ""):  # a batch's missed hourly windows still waiting for a QA review note
        return paid(lambda: {"missed": missed_checks(product_dir, clock(), batch=batch or None)})

    @app.post("/review-missed")
    def review_missed(body: dict, request: Request):
        def run():
            name = qa(request)
            _require_qa(users, name, str(body.get("password", "")))  # re-typed password, as Resume line
            note = " ".join(str(body.get("note", "")).splitlines()).strip()  # U+2028 and kin would split the JSONL row
            if not note:
                raise ValueError("Write the review note")
            raw, kind = body.get("window"), str(body.get("kind", ""))
            try:  # the window's start time, or its id as GET /missed gives it
                w = window_id(kind, float(raw))
            except (TypeError, ValueError, OverflowError, OSError):
                w = str(raw)
            batch, now = str(body.get("batch", "")) or None, clock()
            with meta:
                m = next((m for m in missed_checks(product_dir, now, batch=batch) if m["window"] == w and m["kind"] == kind),
                         None)  # one note per missed window
                if m is None:
                    return {"error": "No missed check waits for a note in that window"}
                row = batches._write("reviews.jsonl", {"time": now, "at": datetime.fromtimestamp(now).isoformat(
                    timespec="seconds"), "kind": "missed_check", "window": w, "by": name, "note": note, "batch": m["batch"]})
            audit.append("review_missed", name, {"window": w, "batch": m["batch"]}, note)
            return row
        return paid(run)

    @app.get("/trace")
    def trace_ep():
        return trace.recent(50)

    # --- Shift Supervisor: runs in a background thread; the screen polls /investigation ---
    def ask_operator(question):
        with q_lock:
            qid = uuid.uuid4().hex[:8]
            inv.update(question={"id": qid, "text": question}, answer=None)
            inv["event"].clear()
        try:
            if not inv["event"].wait(ANSWER_WAIT):
                raise TimeoutError("no answer")
            with q_lock:
                return inv["answer"]
        finally:
            with q_lock:
                inv["question"] = None

    def get_sup():
        with sup_lock:
            if inv["sup"] is None:
                good = [im for im in (cv2.imread(str(p)) for p in sorted((product_dir / "good").glob("*.jpg")))
                        if im is not None]
                if not good or not st.checklist:
                    raise ValueError(NEED_TEACH)
                h, w = good[0].shape[:2]
                ref = st.strip_box or (0.0, 0.0, float(w), float(h))  # taught (SAM) strip box; else the full frame
                inv["sup"] = Supervisor(st, plc or _NoPlc(), audit, chat or ToolChat(budget), ask_operator,
                                        baseline_from(good), ref, lambda: src.get() if src else None,
                                        product_dir / "good", tool_lock=lock)
            return inv["sup"]

    @app.post("/investigate")
    def investigate(body: dict):
        def run():
            with sup_lock:  # build, check and set `running` as one step
                if inv["running"]:
                    return {"error": "an investigation is already running"}
                sup = get_sup()
                inv.update(running=True, result=None)

            def work():
                try:
                    inv["result"] = sup.investigate(str(body.get("trigger", "")) or "started from the bench screen")
                except Exception as ex:
                    inv["result"] = {"error": str(ex) or type(ex).__name__}
                inv["running"] = False
            threading.Thread(target=work, daemon=True).start()
            return {"id": uuid.uuid4().hex[:8]}
        return paid(run)

    @app.get("/investigation")
    def investigation():
        sup = inv["sup"]
        return {"running": inv["running"], "steps": sup.steps if sup else [], "question": inv["question"],
                "pending": list((sup.pending if sup else pending_requests(product_dir)).values()), "result": inv["result"]}

    @app.post("/ask-operator-answer")
    def operator_answer(body: dict):
        with q_lock:
            q = inv["question"]
            if q is None or q["id"] != body.get("id"):
                return {"error": "That question is closed"}
            inv["answer"] = str(body.get("answer", ""))
            inv["event"].set()
        return {"ok": True}

    @app.post("/approve-request")
    def approve_request(body: dict, request: Request):
        def run():
            with lock:
                try:
                    return get_sup().approve(str(body.get("id", "")), users, qa(request), str(body.get("password", "")))
                finally:
                    sync_lock()  # a re-teach locks the station until a self-test passes
        return paid(run)

    @app.post("/decline-request")
    def decline_request(body: dict, request: Request):
        def run():
            name = qa(request)
            with lock:  # requests are written under the station lock, as approve
                return get_sup().decline(str(body.get("id", "")), users, name, str(body.get("password", "")),
                                         str(body.get("reason", "")))
        return paid(run)

    @app.post("/hold")
    def hold_ep(request: Request, body: dict | None = None):
        def run():
            if plc:  # at once, the safe direction: a tray clip, teach or self-test may hold the lock for minutes
                plc.set_hold(True)
            with lock:  # hold.json + PLC only: works before teaching and without the agent
                return hold_line(product_dir, plc or _NoPlc(), audit, who(request)[0],
                                 str((body or {}).get("reason", "")) or "operator voice command")
        return paid(run)

    @app.post("/command")
    async def command(request: Request):
        try:
            form = await request.form()
            if "audio" not in form:
                return {"error": "No audio was received."}
            audio = await form["audio"].read()
            lang = form.get("languages")
        except Exception as ex:
            return {"error": f"Could not read the request: {ex}"}

        def run():
            text = get_client().transcribe(audio, keywords=KEYWORDS, languages=[lang] if lang in LANGS else None,
                                           mode="ENDPOINTING")
            cmd = match(text)
            if cmd == "inspect":
                login_name(request)  # hold, record etc. stay open to anyone at the bench
            return {"transcript": text, "command": cmd}
        return await run_in_threadpool(paid, run)

    @app.post("/resume")
    def resume(body: dict, request: Request):
        def run():
            with lock:
                name, w, waiver = qa(request), recheck.current(product_dir), str(body.get("waiver", "")).strip()
                if w and not waiver:  # the re-check is finished, or QA gives a reason to waive the rest
                    return {"error": recheck.not_done(w)}
                out = resume_line(st, plc or _NoPlc(), audit, users, name, str(body.get("password", "")),
                                  str(body.get("reason", "")))
                if w and out["resumed"]:
                    recheck.waive(product_dir, audit, w, name, waiver)
                return out
        return paid(run)

    def snap(image, v):
        """Annotated JPEG of a REJECT/HOLD strip in rejects/; returns its file name. Drawn on the layout the strip was
        judged on; the offline finder (as the stream) only for a verdict without one, e.g. an inspection error."""
        lay = v.layout
        if lay is None:
            try:
                lay = find_layout(image, st.checklist, None, st._template)
            except Exception:
                lay = None
        f = image.copy()
        sb = tuple(int(v) for v in lay.strip_box) if lay and lay.strip_box else None
        if sb:
            cv2.rectangle(f, sb[:2], sb[2:], BLUE, 3)
        bad = {r["pocket"]: r["rule"] for r in v.reasons if r.get("pocket") is not None}
        draw_pockets(f, lay.pockets if lay else [], set(bad), bad)
        if sb and any(r.get("pocket") is None for r in v.reasons):
            cv2.rectangle(f, sb[:2], sb[2:], RED, 5)  # a strip-level reason: the whole outline
        d = product_dir / "rejects"
        d.mkdir(exist_ok=True)
        stamp = datetime.fromtimestamp(clock()).strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{len(list(d.glob(stamp + '-*.jpg'))) + 1}.jpg"
        if not cv2.imwrite(str(d / name), f):
            raise OSError("reject photo not written")  # log() then leaves "photo" out
        return name

    st.snap = snap

    @app.get("/rejects/{name}")
    def reject_photo(name: str, request: Request):
        if not who(request)[1]:
            return JSONResponse({"error": "Log in first"}, status_code=401)
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9]+\.jpg", name) or not (product_dir / "rejects" / name).is_file():
            return JSONResponse({"error": "No such photo"}, status_code=404)
        return FileResponse(product_dir / "rejects" / name, media_type="image/jpeg")

    def overlay_boxes(frame):
        """Pocket boxes for the stream, recomputed at most once a second, without paid calls."""
        t, boxes = state["overlay"]
        with meta:
            cl, tpl = st.checklist, st._template
        if cl and time.time() - t > 1:
            try:
                lay = find_layout(frame, cl, None, tpl)
                boxes = lay.pockets if lay.ok else []
            except Exception:
                boxes = []
            state["overlay"] = (time.time(), boxes)
        return boxes

    def frames():
        while True:
            f = src.get() if src else None
            if f is not None:
                last = state["last"] or {}
                bad = {r.get("pocket") for r in last.get("reasons", [])}
                draw_pockets(f, overlay_boxes(f), bad)
                if last:
                    cv2.putText(f, last["status"], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                ok, buf = cv2.imencode(".jpg", f)
                if ok:
                    yield b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
            time.sleep(0.1)

    @app.get("/stream")
    def stream():
        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=f")

    # --- Physical challenge: QA registers known-bad strips; a logged-in user runs each one through the verdict path ---
    chal = {"run": None}  # the run in progress, in memory: a run lost to a restart never passed (the gate stays shut)

    def local_now():
        return datetime.fromtimestamp(clock()).isoformat(timespec="seconds")

    def live_run():  # the run in progress, unless its batch is no longer the open one (then it is dropped)
        r, b = chal["run"], batches.open
        return r if r and b and r["batch"] == b["batch"] else None

    def ejected_first():  # a challenge strip waits for its Ejected tick: no production strip is judged before it
        r = live_run()
        return {"error": f"Tick Ejected for {r['pending']['label']} and record it first"} if r and r["pending"] else None

    @app.get("/challenge")
    def challenge_state():
        def run():
            s, r = challenge.load_set(product_dir), live_run()
            nxt = None
            if s and not (r and r["pending"]):
                nxt = r["strips"][len(r["results"])] if r else s["strips"][0]
            return {"set": s, "next": nxt and challenge.place(nxt), "strip": nxt, "pending": r["pending"] if r else None,
                    "results": r["results"] if r else [], "last": (challenge.runs(product_dir) or [None])[-1]}
        return paid(run)

    @app.post("/challenge/register")
    def challenge_register(request: Request, label: str = Form(""), kind: str = Form(""), rule: str = Form(""),
                           image: UploadFile | None = File(None)):
        def run():
            name = qa(request)
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            im = image_from(image)
            with lock:  # the run in progress is checked and changed under the same lock as /challenge/inspect
                out = challenge.register(product_dir, st.checklist, label, kind, rule, im, name, local_now(),
                                         learn=lambda: st.failing_pocket(im))  # a SAM 3.1 call when online
                learned = out.pop("learned", None)
                audit.append("challenge_register", name, {"strips": out["strips"], "learned": learned})
                notes = [learned] if learned else []
                if live_run():  # the set changed: the new set is run in full
                    chal["run"] = None
                    notes.append("The challenge run in progress was stopped: run the new set in full")
                if notes:
                    out = dict(out, message="\n".join(notes))
            return out
        return paid(run)

    @app.post("/challenge/inspect")
    def challenge_inspect(request: Request, image: UploadFile | None = File(None)):
        def run():
            name, role = who(request)
            if not role:
                return {"error": "Log in first"}
            s = challenge.load_set(product_dir)
            if not s:
                return {"error": challenge.NO_SET}
            if src is not None and image is not None:  # a challenge proves a physical strip under the live camera
                return {"error": "Place the strip under the camera: a challenge uses the live camera, not an uploaded photo"}
            b = batches.open
            if not b:
                return JSONResponse({"error": NO_BATCH}, status_code=409)
            im = image_from(image)
            if im is None:
                return {"error": "No camera frame to check."}
            with lock:  # one slow station operation at a time
                r = chal["run"] = live_run()
                if r and r["pending"]:
                    return {"error": f"Tick Ejected for {r['pending']['label']} and record it first"}
                if st.locked:
                    return {"error": "Station locked: " + st.lock_reason}
                strip = r["strips"][len(r["results"])] if r else s["strips"][0]
                # the normal verdict, never logged or published: no batch count, no drift rule, no production banner
                with st._doing(st._finding):  # /status shows the step, as for Inspect
                    v = st._judge(im, None, None)
                if FINDER_DOWN in v.reasons:  # like a self-test that could not run: nothing is recorded
                    return {"error": "Could not inspect: the pocket finder (SAM 3.1) did not answer — try again"}
                res = challenge.judged(strip, v)
                if r is None:
                    r = chal["run"] = {"by": name, "batch": b["batch"], "strips": s["strips"], "results": [],
                                       "pending": None, "source": source_kind(src.source) if src is not None else "upload"}
                if not res["caught"]:  # fail-safe: locked and audited now, before anyone ticks anything
                    challenge.lock(st, res, res["why"])
                    sync_lock()
                if plc:  # every challenge strip is known-bad: the reject output pulses even on a miss, no counter moves
                    plc.eject()
                if not res["caught"]:
                    log_physical_challenge(audit, name, res["label"], None, batch=r["batch"], source=r["source"],
                                           missed=res["why"], **{k: res[k] for k in ("expected_rule", "status", "rules")},
                                           passed=False)
                r["pending"] = res
                msg = (f"Rejected for Rule {res['expected_rule']}: tick Ejected once the strip is out" if res["caught"]
                       else f"Missed: {res['label']} {res['why']} — station locked, quarantine open. "
                            f"Take {res['label']} out — it is a known-bad strip")
                return dict(res, message=msg)
        return paid(run)

    @app.post("/challenge/eject")
    def challenge_eject(request: Request, body: dict | None = None):
        def run():
            name, role = who(request)
            if not role:
                return {"error": "Log in first"}
            with lock:
                r = chal["run"] = live_run()
                if not (r and r["pending"]):
                    return {"error": "Inspect the challenge strip first"}
                p, ej = r["pending"], bool((body or {}).get("ejected"))
                res = {"label": p["label"], "expected_rule": p["expected_rule"], "status": p["status"],
                       "rules": p["rules"], "passed": p["caught"] and ej, "ejected": ej}
                if p["caught"] and not ej:  # rejected but still on the line: the reject path failed
                    challenge.lock(st, p, "not ejected")
                    sync_lock()
                if p["caught"]:  # a missed strip has its one row already, written at the miss
                    log_physical_challenge(audit, name, p["label"], ej, batch=r["batch"], source=r["source"],
                                           **{k: res[k] for k in ("expected_rule", "status", "rules", "passed")})
                r["results"].append(res)
                r["pending"] = None
                if res["passed"] and len(r["results"]) < len(r["strips"]):
                    return {"result": res, "next": challenge.place(r["strips"][len(r["results"])])}
                row = batches._write(challenge.RUNS, {"time": clock(), "at": local_now(), "by": r["by"], "batch": r["batch"],
                                                      "source": r["source"], "results": r["results"],
                                                      "passed": res["passed"]})  # a run stops at its first miss
                chal["run"] = None
                return {"result": res, "run": row}
        return paid(run)

    @app.post("/challenge/close")
    def challenge_close(body: dict, request: Request):
        def run():  # an ended batch's failed run: QA closes it with a note (the open batch closes its own by a passing run)
            name = qa(request)
            _require_qa(users, name, str(body.get("password", "")))  # re-typed password, as Resume line
            why = " ".join(str(body.get("reason", "")).split())
            if not why:
                raise ValueError("Write the reason")
            with lock:
                f, b = challenge_failure(product_dir, audit.entries()), batches.open
                if f is None:
                    return {"error": "No failed challenge run waits for a note"}
                if b and f["since"] >= b["time"]:
                    return {"error": "This batch is open: a passing run closes its failed challenge"}
                return audit.append("challenge_closed", name, {"failure": f["text"], "failed_at": f["since"]}, why)
        return paid(run)

    @app.get("/challenge/photo/{label}")
    def challenge_photo(label: str, request: Request):
        if not who(request)[1]:
            return JSONResponse({"error": "Log in first"}, status_code=401)
        f = product_dir / "challenge" / f"{label}.jpg"
        if not re.fullmatch(r"[A-Z0-9-]{1,12}", label) or not f.is_file():
            return JSONResponse({"error": "No such photo"}, status_code=404)
        return FileResponse(f, media_type="image/jpeg")

    # --- Re-check window: opened by a critical hold (or an approved agent request); read by the screen ---
    @app.get("/recheck")
    def recheck_state():
        def run():
            w = recheck.load(product_dir)
            return dict(w, text=recheck.text(w), count=recheck.progress(w)) if w else {"open": False}
        return paid(run)

    # --- Line board (read-only): the stack light with reasons. After every POST and on every /line poll the facts are
    # read and any states.jsonl row written under the station `lock` - the lock every hold and resume holds across its
    # hold.json write and its audit row - so a row never shows half an action, and never an older snapshot ---
    from collections import Counter
    from d2i.ipqc import due_at
    from d2i.linestate import finder_down, investigating, line_state, read_rows, record_change, release_items, segments
    from d2i.openitems import _t

    states_path = product_dir / "states.jsonl"
    app.state.station_lock = lock
    line_cache = {}  # the last /line answer, served while a slow station operation holds the lock

    def batch_rows(b):
        return [r for r in log_rows() if b and r.get("batch") == b["batch"]]

    def hold_since():  # whole second of the last hold.json write, 0 when unknown
        try:
            return int(_t(json.loads((product_dir / "hold.json").read_text(encoding="utf-8"))["at"]))
        except Exception:
            return 0

    def line_facts(request):
        """(status, line state, open items, now, cause, agent): read-only. cause(previous row) -> (cause, by, rule,
        strip) for a states.jsonl row; a hold or a resume takes them from its own audit row (at or after hold.json)."""
        s, now = status(request), clock()
        held, held_by = s["held"], hold_state(product_dir)[1]
        since, rows = hold_since(), audit.entries()

        def mine(e, *actions):  # written by the action that last wrote hold.json
            try:
                return e.get("action") in actions and e.get("user") == held_by and _t(e["at"]) >= since
            except (KeyError, TypeError, ValueError):
                return False

        hold = next((e for e in reversed(rows) if mine(e, "hold_line")), None)
        reason = ("Shift Supervisor investigating" if held_by == "supervisor" else (hold or {}).get("reason") or "Line held") \
            if held else ""
        with sup_lock:  # a copy: the agent's thread adds steps
            sup = inv["sup"]
            agent = {"running": inv["running"], "tools": [x["tool"] for x in sup.steps] if sup else []}
        items = open_items(product_dir, now)
        agent["pending"] = [i for i in items if i["kind"] == "request"]
        try:
            suspended = finder_down(batch_rows(s["batch"]), st._selftests())
        except Exception:  # an unreadable self-test log already locks the station: STOPPED says so
            suspended = False
        ls = line_state({"checklist": bool(s["checklist"]), "taught": s["taught"], "locked": s["locked"],
                         "lock_reason": s["lock_reason"], "batch_open": bool(s["batch"]), "blockers": s["blockers"],
                         "held": held, "held_by": held_by, "hold_reason": reason,
                         "quarantine": s["quarantine_count"] or s["quarantine_error"], "suspended": suspended,
                         "challenge_set": (product_dir / challenge.SET).exists(),
                         "record_unsigned": any(i["kind"] == "record_unsigned" for i in items)})
        strip = s["totals"]["inspected"] if s["totals"] else None

        def cause(prev):
            if prev and prev["to"] == "HELD" and not held:  # the hold was lifted: by whom and why
                r = next((e for e in reversed(rows) if mine(e, "resume_line") or (
                    mine(e, "agent_approve") and isinstance(e.get("details"), dict)
                    and e["details"].get("kind") == "resume" and e["details"].get("approved"))), None)
                if r is None:
                    return "Hold lifted", held_by or None, None, strip
                if r["action"] == "resume_line":
                    return "Resumed: " + r["reason"], r["user"], None, strip
                return (f'Resumed by {r["user"]} (approved agent request): the agent wrote '
                        f'"{str(r["reason"]).removeprefix("Agent request: ")}"', r["user"], None, strip)
            text = ls["reason"] or {"STARTING": "Batch released", "SUSPENDED": "Pocket finder answered",
                                    "STOPPED": "Station unlocked"}.get(prev and prev["to"], "Line running")
            # a rule number only from the station's own words (a verdict reason it held on), never from typed text
            m = re.search(r"\bRule (\d+)\b", text) if ls["state"] == "HELD" and held_by == "station" else None
            return text, ls["by"], int(m.group(1)) if m else None, strip

        return s, ls, items, now, cause, agent

    def line_now(request, wait):
        """line_facts plus at most one states.jsonl row, all under the station lock; None (nothing recorded) when the
        lock stays busy for `wait` s (0 = do not wait)."""
        if not (lock.acquire(timeout=wait) if wait else lock.acquire(blocking=False)):
            return None
        try:
            got = line_facts(request)
            record_change(states_path, got[3], got[1]["state"], got[4])
            return got
        finally:
            lock.release()

    @app.middleware("http")
    async def line_state_log(request: Request, call_next):
        response = await call_next(request)
        if request.method == "POST":
            try:
                await run_in_threadpool(line_now, request, 2.0)
            except Exception:
                pass  # the state log never fails a request; the next poll writes the change
        return response

    @app.get("/board", response_class=HTMLResponse)
    def board():
        return (STATIC / "line.html").read_text(encoding="utf-8")

    def line_json(s, ls, items, now, cause, agent):
        try:
            cur = s["batch"] or next((r for r in reversed(batches.rows) if r["event"] == "start"), None)  # else the last
        except Exception:  # unreadable batch file: the open batch only (none, as /status reads it)
            cur = s["batch"]
        mine, rows = batch_rows(cur), log_rows()
        last = mine[-1] if mine else None
        first = next((r for r in (last or {}).get("reasons", []) if r.get("rule") is not None), None)
        names = {int(m[1]): m[2] for m in (re.match(r"Rule (\d+)(?: \(\w+\))?: (.*)", t) for t in s["rules"]) if m}

        def per_rule(rs):  # a strip counts once per rule
            return Counter(k for r in rs if r["status"] == "REJECT" for k in {x.get("rule") for x in r["reasons"]}
                           if k is not None)

        def recent(r):
            try:
                return _t(r["at"]) >= now - 3600
            except (KeyError, TypeError, ValueError):
                return False

        nb, nh = per_rule(mine), per_rule([r for r in rows if recent(r)])
        due = []
        if s["batch"]:
            try:
                at = due_at(s["batch"], check_events(product_dir, s["batch"]["batch"]))
                due = [{"name": n, "in": None if at[k] is None else at[k] - now}
                       for k, n in (("leak", "Leak test"), ("print", "Print check"))]
            except Exception:  # shown, never a blank board
                due = [{"name": "Hourly checks", "in": None, "text": "cannot read the records"}]
        try:
            until = None if s["locked"] else st.selftest_until  # a locked station has no valid self-test
        except Exception:
            until = None
        due.append({"name": "Self-test expires", "in": None if until is None else until - now})
        try:
            runs = record._lines(product_dir / "challenge_runs.jsonl")
        except Exception:  # the waiting list already says the challenge records cannot be read
            runs = []
        run_ = runs[-1] if runs else None
        need = release_items((product_dir / challenge.SET).exists())
        known = bool(s["batch"]) and all(b in need for b in s["blockers"])  # an unreadable record ticks nothing
        phys = next((e for e in reversed(audit.entries()) if e.get("action") == "physical_challenge"), None)
        states_ = read_rows(states_path)
        day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        segs, lost = segments(states_, day, now), {}
        for g in segs:
            if g["state"] in ("HELD", "STOPPED"):
                lost[(g["state"], g["cause"])] = lost.get((g["state"], g["cause"]), 0) + g["seconds"]
        ok, bad = audit.verify()
        unsigned = any(i["kind"] == "record_unsigned" for i in items)
        sign = next((e for e in reversed(audit.entries()) if signed(e, cur["batch"])), None) \
            if cur and not s["batch"] and not unsigned else None
        cl = s["checklist"] or {}
        return {
            "now": now, "state": ls["state"], "reason": ls["reason"], "by": ls["by"],
            "since": states_[-1]["time"] if states_ and states_[-1]["to"] == ls["state"] else now,
            "waiting_for": ls["waiting_for"], "also": ls["also"],
            "investigating": investigating(agent["running"], agent["tools"], len(agent["pending"])),
            "product": cl.get("product"), "count": cl.get("count"), "checklist_version": s["checklist_version"],
            "batch": cur and {k: cur[k] for k in ("batch", "mfg", "exp", "mrp")} | {"open": bool(s["batch"])},
            "release": [{"item": x, "done": known and x not in s["blockers"]} for x in need],
            "chip": last and {"status": last["status"], "rule": first and first["rule"], "at": last.get("at"),
                              "text": first["text"] if first else "; ".join(r.get("text", "") for r in last["reasons"]),
                              "strip": sum(r["status"] != "LOCKED" and not r.get("recheck") for r in mine)},  # as the Run screen counts
            "waiting": [dict(i, age=now - i["since"]) for i in items], "due": due,
            "challenge": {"time": run_["time"], "passed": run_["passed"]} if run_ else
            {"time": _t(phys["at"]), "label": phys["details"].get("strip_label"), "ejected": phys["details"].get("ejected")}
            if phys else None,
            "challenge_set": (product_dir / challenge.SET).exists(),
            "counts": reconcile(mine, cur["batch"]) if cur else None,
            "reconciliation": s["reconciliation"],
            "reject_run": {"n": reject_run(rows, s["batch"]["batch"], recheck.resumed_at(audit)) if s["batch"] else 0,
                           "of": DRIFT_REJECTS},
            "rejects_by_rule": [{"rule": k, "text": names.get(k, ""), "batch": nb[k], "hour": nh[k]}
                                for k in sorted(set(nb) | set(nh), key=lambda k: (-nb[k], -nh[k], k))],
            "day_start": day, "segments": segs,
            "lost": [{"state": k[0], "cause": k[1], "seconds": round(v, 1)} for k, v in lost.items()],
            "footer": {"audit_ok": ok, "audit_entries": len(audit.entries()), "audit_broken_at": bad,
                       "record": None if not cur else "draft" if s["batch"] or unsigned else "signed",
                       "signed_by": sign and sign["user"], "signed_at": sign and _t(sign["at"])}}

    @app.get("/line")
    def line(request: Request):
        def run():
            got = line_now(request, 0)
            if got is None and line_cache:  # a slow station operation holds the lock: the last answer, clocks moved on
                now, old = clock(), line_cache["line"]
                return dict(old, now=now, waiting=[dict(i, age=now - i["since"]) for i in old["waiting"]])
            out = line_json(*(got or line_facts(request)))  # first poll ever while busy: shown, not recorded
            line_cache["line"] = out
            return out
        return paid(run)

    return app


if __name__ == "__main__":
    import uvicorn
    from d2i.plc import Plc

    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    ap.add_argument("--product", default="blister")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 lets a phone on the same network open /lamp")
    ap.add_argument("--add-user", metavar="NAME")
    ap.add_argument("--role", choices=["qa", "operator"], default="operator")
    a = ap.parse_args()
    pdir = Path("data") / a.product
    if a.add_user:
        import getpass
        pw = getpass.getpass("Password: ")
        if not pw or pw != getpass.getpass("Again: "):
            raise SystemExit("Passwords empty or not matching; no user added.")
        Users(pdir.parent / "users.json").add(a.add_user, a.role, pw)
        raise SystemExit(f"Added {a.role} user {a.add_user}.")
    plc = Plc()
    plc.start()
    budget = Budget()
    uvicorn.run(create_app(pdir, a.source, MetaClient(budget), plc, budget=budget), host=a.host, port=a.port)
