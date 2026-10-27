"""Bench web app: python app.py --source 0|<video>|<photo folder> --product blister --port 8000"""
import argparse
import html
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from d2i import record, trace
from d2i.commands import KEYWORDS, match
from d2i.audit import (AuditLog, Users, log_physical_challenge, quarantine_list, release_quarantine,
                       sign_record)
from d2i.checklist import approve, make_checklist
from d2i.llm import ToolChat
from d2i.meta_api import Budget, MetaClient
from d2i.pockets import find_layout
from d2i.station import Station, Verdict
from d2i.supervisor import Supervisor, baseline_from

STATIC = Path(__file__).parent / "static"
NEED_GOOD = 5
NO_CHECKLIST = "Describe and approve the good strip first"
LANGS = {"English", "Tamil", "Hindi"}  # "Auto" = omit
NEED_TEACH = "Teach the station first"
ANSWER_WAIT = 120  # seconds the agent waits for the operator
INSPECT_ERROR = "inspection error — check by hand"


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


def create_app(product_dir: Path, source, client=None, plc=None, clock=time.time, chat=None) -> FastAPI:
    product_dir = Path(product_dir)
    product_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    trace.set_path(product_dir / "trace.jsonl")
    audit, users = AuditLog(product_dir / "audit.jsonl"), Users(product_dir.parent / "users.json")
    sessions = {}  # token -> (name, role); in memory, lost on restart
    inv = {"running": False, "question": None, "result": None, "answer": None, "event": threading.Event(), "sup": None}
    sup_lock = threading.RLock()  # supervisor build/cache and the running flag, together
    q_lock = threading.Lock()  # the open operator question, its answer and the Event, together
    records = {}  # record id -> (html, sha256) of what /record served; last 20
    st = Station(product_dir, client=client, clock=clock)
    spend_path = product_dir / "meta_spend.json"
    src = FrameSource(source) if source is not None else None
    lock = threading.RLock()  # one slow station operation at a time
    meta = threading.RLock()  # short: checklist / pending / last-verdict references
    state = {"pending": None, "last": None, "client": client, "overlay": (0.0, [])}
    app.state.station, app.state.source = st, src

    def get_client():  # built lazily so a missing key is reported when a paid call is needed
        if state["client"] is None:
            state["client"] = MetaClient(Budget(spend_path))
        return state["client"]

    def image_from(upload: UploadFile | None):
        if upload is not None:
            return cv2.imdecode(np.frombuffer(upload.file.read(), np.uint8), cv2.IMREAD_COLOR)
        return src.get() if src else None

    def who(request: Request):
        return sessions.get(request.cookies.get("d2i_session"), ("operator", None))

    def held_now():
        try:
            return json.loads((product_dir / "hold.json").read_text(encoding="utf-8"))["held"] is not False
        except FileNotFoundError:
            return False
        except Exception:  # unreadable hold file: held (the safe direction)
            return True

    if plc and held_now():
        plc.set_hold(True)  # a restart keeps the line held

    def quarantine_count():
        try:
            return len(quarantine_list(product_dir, audit)["strips"]), False
        except Exception:
            return None, True  # unknown is shown, never hidden

    def publish(verdicts):
        """Each verdict to the PLC in order; the last one is shown on the screen."""
        for v in verdicts:
            if plc:
                plc.set_verdict(v)
        with meta:
            state["last"] = asdict(verdicts[-1])
        return state["last"]

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

    @app.get("/status")
    def status(request: Request):
        counts = {}
        try:
            for line in (product_dir / "log.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    k = json.loads(line)["status"]
                    counts[k] = counts.get(k, 0) + 1
                except Exception:
                    pass  # unreadable line: skip
        except OSError:
            pass
        try:
            spent = float(json.loads(spend_path.read_text())["spent_usd"]) if spend_path.exists() else 0.0
        except Exception:
            spent = None
        with meta:
            cl, pending, last = st.checklist, state["pending"], state["last"]
        try:
            locked = st.locked
            reason = st.lock_reason if cl else NO_CHECKLIST
        except Exception as ex:  # unreadable self-test log: stay locked
            locked, reason = True, f"Cannot read self-test log: {ex}"
        held, (qn, qerr) = held_now(), quarantine_count()
        if held:
            banner, text = "HOLD", "Line held by the Shift Supervisor — QA must resume." + (f" {reason}" if locked else "")
        elif locked:
            banner, text = "LOCKED", reason
        elif last and last["status"] in ("REJECT", "HOLD"):
            banner, text = last["status"], " ".join(r["text"] for r in last["reasons"])
        else:
            banner, text = "READY", ""
        return {"locked": locked, "lock_reason": reason if locked else "",
                "checklist_version": cl and cl["version"], "spent_usd": spent,
                "counts": counts, "last_verdict": last, "state": banner, "text": text,
                "rules": cl["rules"] if cl else [], "pending": pending["rules"] if pending else None,
                "held": held, "quarantine_count": qn, "quarantine_error": qerr,
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
    def approve_ep(body: dict, request: Request):
        by = str(body.get("by", "")).strip()

        def run():
            with meta:  # check and set together
                if not state["pending"]:
                    return {"error": "Nothing to approve: describe the good strip first."}
                if not by:
                    return {"error": "Enter the approver's name."}
                st.checklist = approve(state["pending"], by, product_dir / "checklists.jsonl")
                state["pending"] = None
                audit.append("checklist_approve", who(request)[0], {"approved_by": by, "version": st.checklist["version"]})
                return {"checklist": st.checklist}
        return paid(run)

    @app.post("/teach")
    def teach(request: Request, body: dict | None = None):
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

        def run():
            with lock:
                out = {"taught": len(imgs), "skipped": skipped, "info": st.teach(imgs)}
                audit.append("teach", who(request)[0], {"photos": len(imgs)})
            with sup_lock:
                if not inv["running"]:
                    inv["sup"] = None  # rebuilt from the new good photos at the next use
            return out
        return paid(run)

    @app.post("/selftest")
    def selftest(request: Request, image: UploadFile | None = File(None)):
        def run():
            im = image_from(image)
            if not st.checklist:
                return {"error": NO_CHECKLIST}
            if im is None:
                return {"error": "No good strip in view."}
            with lock:
                r = st.self_test(im)
                audit.append("selftest", who(request)[0], {"passed": r.get("passed")})
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
            with lock:  # inspect and publish together so verdicts land in order
                try:
                    v = st.inspect(im, held=held_now())
                except Exception:  # logged and held, never a 500 that leaves the PLC on the previous verdict
                    v = Verdict("HOLD", [{"rule": None, "check": "inspection_error", "pocket": None,
                                          "text": INSPECT_ERROR}], at=st._now())
                    out = publish([v])  # the hold reaches the PLC even if the log cannot be written either
                    st.log(v)
                    return out
                return publish([v])
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
        label = str(body.get("strip_label", "")).strip()
        if not label:
            return {"error": "Enter the challenge strip label."}
        return paid(lambda: log_physical_challenge(audit, who(request)[0], label, bool(body.get("ejected"))))

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
            return sign_record(rec[0], audit, users, name, str(body.get("password", "")))
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
                boxes = [l.strip_box for l in (find_layout(im, st.checklist, None, st.template) for im in good) if l.ok]
                if not boxes:
                    raise ValueError(NEED_TEACH)
                inv["sup"] = Supervisor(st, plc or _NoPlc(), audit, chat or ToolChat(Budget(spend_path)), ask_operator,
                                        baseline_from(good), tuple(float(v) for v in np.median(boxes, axis=0)),
                                        lambda: src.get() if src else None, product_dir / "good", tool_lock=lock)
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
                return get_sup().approve(str(body.get("id", "")), users, qa(request), str(body.get("password", "")))
        return paid(run)

    @app.post("/hold")
    def hold_ep(request: Request, body: dict | None = None):
        def run():
            with lock:
                return get_sup().hold(who(request)[0], str((body or {}).get("reason", "")) or "operator voice command")
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
                return get_sup().resume(users, qa(request), str(body.get("password", "")), str(body.get("reason", "")))
        return paid(run)

    def overlay_boxes(frame):
        """Pocket boxes for the stream, recomputed at most once a second, without paid calls."""
        t, boxes = state["overlay"]
        with meta:
            cl, tpl = st.checklist, st.template
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
                for i, (x0, y0, x1, y1) in enumerate(overlay_boxes(f)):
                    cv2.rectangle(f, (x0, y0), (x1, y1), (0, 0, 220) if i + 1 in bad else (0, 200, 0), 2)
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
    uvicorn.run(create_app(pdir, a.source, MetaClient(Budget(pdir / "meta_spend.json")), plc), host="0.0.0.0", port=a.port)
