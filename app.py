"""Bench web app: python app.py --source 0|<video>|<photo folder> --product blister --port 8000 [--host 0.0.0.0]"""
import argparse
import html
import hashlib
import json
import math
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

from d2i import record, trace
from d2i.commands import KEYWORDS, match
from d2i.audit import (AuditLog, Users, _require_qa, log_physical_challenge, quarantine_list, release_quarantine,
                       sign_record)
from d2i.batch import Batches, line_clearance
from d2i.checklist import approve, make_checklist
from d2i.llm import ToolChat
from d2i.meta_api import Budget, MetaClient
from d2i.ipqc import DRIFT_REJECTS, check_events, due, reconcile, reject_run, release_blockers
from d2i.printcheck import check_print, read_print, wrong_print
from d2i.pockets import find_layout
from d2i.station import NEED_TEACH, Station, Verdict
from d2i.supervisor import Supervisor, baseline_from, hold_line, hold_state, resume_line
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

    def publish(verdicts):
        """Each verdict to the PLC in order; the last one is shown on the screen."""
        for v in verdicts:
            if plc:
                plc.set_verdict(v)
        with meta:
            state["last"] = asdict(verdicts[-1])
        return state["last"]

    def station_hold(reason):  # the existing hold (hold.json + PLC coil + audit), raised by the station itself
        if held_now():  # already held: the first hold stays open, no second one
            return
        hold_line(product_dir, plc or _NoPlc(), audit, "station", reason)

    def last_resume():  # Unix time of the latest QA "Resume line" (audit log, UTC), 0 if none
        return next((datetime.fromisoformat(e["at"]).timestamp() for e in reversed(audit._rows()) if e["action"] == "resume_line"), 0.0)

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

    def after_verdicts(vs, no):  # call inside `lock`, after the verdicts are logged (the last len(vs) log rows)
        if held_now():  # already held: the first hold stays open, no second one
            return
        crit = next((r["text"] for v in vs for r in v.reasons if r.get("severity") == "critical"), None)
        rows, since = log_rows(), last_resume()
        if crit:
            station_hold("Critical: " + crit)
        elif any(reject_run(rows[:n], no, since) >= DRIFT_REJECTS for n in range(len(rows) - len(vs) + 1, len(rows) + 1)):
            station_hold("3 rejects in a row — check the machine")  # checked after each new verdict: a later pass cannot hide it

    def need_batch():  # inspecting without an open batch would leave verdicts no batch owns: refuse, log nothing
        b = batches.open
        return (b["batch"], None) if b else (None, JSONResponse({"error": NO_BATCH}, status_code=409))

    def blockers_for(b):  # what still stops this open batch from being inspected
        signed = any(e["action"] == "specimen_signed" and e["details"].get("batch") == b["batch"] for e in audit.entries())
        return release_blockers(b, st._selftests(), record._lines(product_dir / "prints.jsonl"), signed)

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
            overdue = due(open_batch, check_events(product_dir, open_batch["batch"]), clock()) if open_batch else []
            totals = reconcile(rows, open_batch["batch"]) if open_batch else None
            recon = "open" if open_batch else next((r["totals"].get("status") for r in reversed(batches.rows)
                                                      if r["event"] == "end"), None)
            hold_reason = next((e["reason"] for e in reversed(audit.entries()) if e["action"] == "hold_line" and e["user"] == by),
                               "") if held and by != "supervisor" else ""  # the supervisor's hold has no hold_line row
        except Exception:  # unreadable records: not released (the safe direction)
            blockers, overdue, totals, hold_reason, recon = ["Cannot read the batch records"], [], None, "", None
        if held:
            by = "the Shift Supervisor" if by == "supervisor" else by
            banner, text = "HOLD", (f"Line held by {by} — QA must resume." if by else "Line held — QA must resume.") \
                + (f" {reason}" if locked else "")
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
                "counts": counts, "batch": open_batch, "blockers": blockers, "due": overdue, "last_verdict": last, "selftest_skipped": uncovered, "state": banner, "text": text,
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
                    inv["sup"] = None  # rebuilt from the new good photos at the next use
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
    def inspect(image: UploadFile | None = File(None)):
        def run():
            im = image_from(image)
            if not st.checklist:
                return {"status": "NO_CHECKLIST", "reasons": [{"text": NO_CHECKLIST}]}
            if im is None:
                return {"error": "No image."}
            no, refused = need_batch()
            refused = refused or not_released(batches.open)
            if refused:
                return refused
            with lock:  # inspect and publish together so verdicts land in order
                try:
                    v = st.inspect(im, held=held_now(), batch=no)
                except Exception:  # logged and held, never a 500 that leaves the PLC on the previous verdict
                    v = Verdict("HOLD", [{"rule": None, "check": "inspection_error", "pocket": None,
                                          "text": INSPECT_ERROR}], at=st._now())
                    out = publish([v])  # the hold reaches the PLC even if the log cannot be written either
                    st.log(v, batch=no, image=im)
                    return out
                out = publish([v])
                after_verdicts([v], no)
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
            with meta:
                batches._write("prints.jsonl", {"at": datetime.fromtimestamp(clock()).isoformat(timespec="seconds"), "time": clock(),
                                                "batch": b["batch"], "read": read, "status": out["status"],
                                                "reasons": out["reasons"], "by": name})
            bad = wrong_print(out["reasons"])
            if bad:  # after the row is written: a failing hold must not lose the read
                with lock:
                    station_hold("Critical: wrong print — " + bad)
            return out
        return paid(run)

    @app.post("/tray")
    def tray():
        def run():
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            s = src.source if src else ""
            if not s or Path(s).is_dir() or Path(s).suffix.lower() in (".jpg", ".jpeg", ".png"):
                return {"error": "tray clip needs a camera or video source"}
            no, refused = need_batch()
            refused = refused or not_released(batches.open)
            if refused:
                return refused
            with lock:  # one slow station operation at a time; verdicts land in order
                if st.locked:  # no paid SAM video call for a locked station
                    v = st.log(Verdict("LOCKED", [{"rule": None, "check": "locked", "pocket": None, "text": st.lock_reason}]), batch=no)
                    publish([v])
                    return {"verdicts": [asdict(v)]}
                clip = record_clip(int(s) if s.isdigit() else s, TRAY_SECONDS, product_dir / "tray.mp4")
                vs = inspect_clip(clip, st, get_client(), held=held_now(), batch=no)  # each one is already logged
                publish(vs)
                after_verdicts(vs, no)
                return {"verdicts": [asdict(v) for v in vs]}
        return paid(run)

    def make_record(batch, operator, hours):
        now = clock()
        return record.shift_record(product_dir, now - hours * 3600, now, batch, operator, state["client"], audit)

    @app.get("/record", response_class=HTMLResponse)
    def shift_record(batch: str = "", operator: str = "", hours: float = 8.0):
        try:
            body = make_record(batch, operator, hours)
            sha, rid = hashlib.sha256(body.encode("utf-8")).hexdigest(), uuid.uuid4().hex[:8]
            records[rid] = (body, sha)
            while len(records) > 20:
                records.pop(next(iter(records)))
            note = f"<p>Record id: {rid} · SHA-256 of the record above: {sha}</p>"
            return HTMLResponse(body.replace("</body>", note + "</body>"), headers={"X-Record-Id": rid})
        except Exception as ex:
            return f"<!doctype html><meta charset='utf-8'><p>The shift record could not be made: {html.escape(str(ex))}</p>"

    @app.post("/ask")
    def ask(body: dict):
        return paid(lambda: {"answer": record.ask(str(body.get("question", "")), product_dir, get_client())})

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
                    mrp: str = Form("")):
        def run():
            name = qa(request)
            with meta:
                row = batches.start(batch, mfg, exp, mrp, name, clock())
                audit.append("batch_start", name, {k: row[k] for k in ("batch", "mfg", "exp", "mrp", "clearance_at")})
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
            try:
                counts = {k: float(f.get(k) or 0) for k in keys}
                limits = {"strips": float(f.get("strips_limit") or 0), "foil": float(f.get("foil_limit") or 0.5)}
            except ValueError:
                return {"error": "Counts and limits must be numbers"}
            if not all(map(math.isfinite, (*counts.values(), *limits.values()))):
                return {"error": "Counts and limits must be numbers"}
            if min(*counts.values(), *limits.values()) < 0:
                return {"error": "Counts and limits cannot be negative"}
            if any(counts[k] != int(counts[k]) for k in keys[:5]):
                return {"error": "Strip counts must be whole numbers"}
            for k in keys[:5]:
                counts[k] = int(counts[k])
            if str(f.get("cleared", "")).lower() not in ("true", "1", "on"):
                return {"error": "Tick the after-batch line clearance: line cleared, leftover printed foil returned or destroyed"}
            with meta:
                cur = batches.open
                if not cur:
                    return JSONResponse({"error": NO_BATCH}, status_code=409)
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

    @app.post("/challenge")
    def challenge(body: dict, request: Request):
        name, role = who(request)
        if not role:
            return {"error": "Log in first"}
        label = str(body.get("strip_label", "")).strip()
        if not label:
            return {"error": "Enter the challenge strip label."}
        return paid(lambda: log_physical_challenge(audit, name, label, bool(body.get("ejected"))))

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
            return sign_record(rec[0], audit, users, name, str(body.get("password", "")), product_dir=product_dir)
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
                "pending": list(sup.pending.values()) if sup else [], "result": inv["result"]}

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

    @app.post("/hold")
    def hold_ep(request: Request, body: dict | None = None):
        def run():
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
            return {"transcript": text, "command": match(text)}
        return await run_in_threadpool(paid, run)

    @app.post("/resume")
    def resume(body: dict, request: Request):
        def run():
            with lock:
                return resume_line(st, plc or _NoPlc(), audit, users, qa(request), str(body.get("password", "")),
                                   str(body.get("reason", "")))
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
