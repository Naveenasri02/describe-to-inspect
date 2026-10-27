"""Bench web app: python app.py --source 0|<video>|<photo folder> --product blister --port 8000"""
import argparse
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from d2i import record
from d2i.checklist import approve, make_checklist
from d2i.meta_api import Budget, BudgetExceeded, MetaClient, MetaError
from d2i.pockets import find_layout
from d2i.station import Station

STATIC = Path(__file__).parent / "static"
NEED_GOOD = 5
NO_CHECKLIST = "Describe and approve the good strip first"
LANGS = {"English", "Tamil", "Hindi"}  # "Auto" = omit


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


def create_app(product_dir: Path, source, client=None, plc=None, clock=time.time) -> FastAPI:
    product_dir = Path(product_dir)
    product_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI()
    st = Station(product_dir, client=client, clock=clock)
    spend_path = product_dir / "meta_spend.json"
    src = FrameSource(source) if source is not None else None
    lock = threading.RLock()  # one station operation at a time
    state = {"pending": None, "last": None, "client": client, "overlay": (0.0, [])}
    app.state.station, app.state.source = st, src

    def get_client():  # built lazily so a missing key is reported when a paid call is needed
        if state["client"] is None:
            state["client"] = MetaClient(Budget(spend_path))
        return state["client"]

    async def image_from(upload: UploadFile | None):
        if upload is not None:
            return cv2.imdecode(np.frombuffer(await upload.read(), np.uint8), cv2.IMREAD_COLOR)
        return src.get() if src else None

    def paid(fn):
        try:
            return fn()
        except (MetaError, BudgetExceeded, ValueError) as ex:
            return {"error": str(ex)}

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/lamp", response_class=HTMLResponse)
    def lamp():
        return (STATIC / "lamp.html").read_text(encoding="utf-8")

    @app.get("/status")
    def status():
        counts = {}
        log = product_dir / "log.jsonl"
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    s = json.loads(line)["status"]
                    counts[s] = counts.get(s, 0) + 1
        spent = json.loads(spend_path.read_text())["spent_usd"] if spend_path.exists() else 0.0
        last, locked = state["last"], st.locked
        reason = st.lock_reason if st.checklist else NO_CHECKLIST
        if locked:
            banner, text = "LOCKED", reason
        elif last and last["status"] in ("REJECT", "HOLD"):
            banner, text = last["status"], " ".join(r["text"] for r in last["reasons"])
        else:
            banner, text = "READY", ""
        return {"locked": locked, "lock_reason": reason if locked else "",
                "checklist_version": st.checklist and st.checklist["version"], "spent_usd": spent,
                "counts": counts, "last_verdict": last, "state": banner, "text": text,
                "rules": st.checklist["rules"] if st.checklist else [],
                "pending": state["pending"]["rules"] if state["pending"] else None}

    @app.post("/describe")
    async def describe(request: Request):
        def run(text, audio, langs):
            c = get_client()
            if audio is not None:
                text = c.transcribe(audio, languages=langs)
            state["pending"] = make_checklist(text, c)
            return {"checklist": state["pending"], "transcript": text}
        if "multipart" in request.headers.get("content-type", ""):
            form = await request.form()
            audio = await form["audio"].read()
            lang = form.get("languages")
            return paid(lambda: run(None, audio, [lang] if lang in LANGS else None))
        body = await request.json()
        return paid(lambda: run(str(body.get("text", "")), None, None))

    @app.post("/approve")
    def approve_ep(body: dict):
        by = str(body.get("by", "")).strip()
        if not state["pending"]:
            return {"error": "Nothing to approve: describe the good strip first."}
        if not by:
            return {"error": "Enter the approver's name."}
        with lock:
            st.checklist = approve(state["pending"], by, product_dir / "checklists.jsonl")
            state["pending"] = None
        return {"checklist": st.checklist}

    @app.post("/teach")
    def teach(body: dict | None = None):
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
        with lock:
            return paid(lambda: {"taught": len(files), "info": st.teach([cv2.imread(str(p)) for p in files])})

    @app.post("/selftest")
    async def selftest(image: UploadFile | None = File(None)):
        im = await image_from(image)
        if not st.checklist:
            return {"error": NO_CHECKLIST}
        if im is None:
            return {"error": "No good strip in view."}
        with lock:
            return paid(lambda: st.self_test(im))

    @app.post("/inspect")
    async def inspect(image: UploadFile | None = File(None)):
        im = await image_from(image)
        if not st.checklist:
            return {"status": "NO_CHECKLIST", "reasons": [{"text": NO_CHECKLIST}]}
        if im is None:
            return {"error": "No image."}
        with lock:
            v = st.inspect(im)
        if plc:
            plc.set_verdict(v)
        state["last"] = asdict(v)
        return state["last"]

    @app.get("/record", response_class=HTMLResponse)
    def shift_record(batch: str = "", operator: str = "", hours: float = 8.0):
        now = clock()
        return record.shift_record(product_dir, now - hours * 3600, now, batch, operator, state["client"])

    @app.post("/ask")
    def ask(body: dict):
        return paid(lambda: {"answer": record.ask(str(body.get("question", "")), product_dir, get_client())})

    def overlay_boxes(frame):
        """Pocket boxes for the stream, recomputed at most once a second, without paid calls."""
        t, boxes = state["overlay"]
        if st.checklist and time.time() - t > 1:
            try:
                lay = find_layout(frame, st.checklist, None, st.template)
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
    a = ap.parse_args()
    pdir = Path("data") / a.product
    plc = Plc()
    plc.start()
    uvicorn.run(create_app(pdir, a.source, MetaClient(Budget(pdir / "meta_spend.json")), plc), host="0.0.0.0", port=a.port)
