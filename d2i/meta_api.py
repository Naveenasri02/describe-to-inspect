"""Meta Model API client (Muse Spark, SAM 3.1, Muse Voice Transcribe) with a hard spend stop.

Every paid call reserves its worst-case price first, then settles to the real price.
"""
import base64
import io
import json
import os
import re
import tempfile
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import meta_sam_parser as sam

API = "https://api.meta.ai/v1"
KEY_FILE = Path.home() / ".secrets" / "meta_model_api_key.txt"
LIMIT_USD = 50.0
CREDIT_END_UTC = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc).timestamp()

SPARK_MODEL = "muse-spark-1.3"
SPARK_IN, SPARK_OUT = 1.25e-6, 4.25e-6       # USD per token
SAM_IMAGE = 2.50 / 1000                        # USD per image
SAM_FRAME, SAM_MIN_FRAMES = 0.20 / 1000, 50    # USD per video frame, billed minimum
VOICE_HOUR = 0.18                              # USD per audio hour


class BudgetExceeded(RuntimeError):
    pass


class MetaError(RuntimeError):
    pass


class Budget:
    def __init__(self, path: Path, limit: float = LIMIT_USD, clock=time.time):
        self.path, self.limit, self.clock = Path(path), limit, clock
        self.spent = json.loads(self.path.read_text())["spent_usd"] if self.path.exists() else 0.0

    def _save(self):
        self.path.write_text(json.dumps({"spent_usd": round(self.spent, 6)}))

    def reserve(self, worst: float) -> None:
        if self.clock() > CREDIT_END_UTC and os.environ.get("D2I_ALLOW_PAID") != "1":
            raise BudgetExceeded("free credit window ended (set D2I_ALLOW_PAID=1 to pay with the card)")
        if self.spent + worst > self.limit:
            raise BudgetExceeded(f"spend stop: ${self.spent:.2f} used of ${self.limit:.2f}")
        self.spent += worst
        self._save()

    def settle(self, worst: float, actual: float) -> None:
        self.spent += actual - worst
        self._save()


@dataclass
class SamObject:
    id: int
    box: tuple
    score: float | None
    mask: np.ndarray | None


@dataclass
class SamTrackPoint:
    id: int
    frame: int
    box: tuple
    score: float | None


class MetaClient:
    def __init__(self, budget: Budget, key: str | None = None, http=None):
        self.budget, self._key = budget, key
        if http is None:
            import requests
            http = requests
        self.http = http

    def _headers(self):
        key = self._key or os.environ.get("MODEL_API_KEY")
        if not key and KEY_FILE.exists():
            key = KEY_FILE.read_text().strip()
        if not key:
            raise MetaError(f"no Meta API key: set MODEL_API_KEY or put the key in {KEY_FILE}")
        return {"Authorization": f"Bearer {key}"}

    def _post(self, path, worst, **kw):
        headers = self._headers()
        self.budget.reserve(worst)  # a failed call keeps its reservation
        r = self.http.post(f"{API}{path}", headers=headers, timeout=180, **kw)
        if r.status_code >= 400:
            raise MetaError(f"Meta API {r.status_code}: {r.text[:300]}")
        return r.json()

    def spark_json(self, system: str, user: str, schema: dict, name: str = "result", max_out: int = 4000,
                   effort: str = "low") -> dict:
        worst = (len(system) + len(user) + len(json.dumps(schema))) / 2 * SPARK_IN + max_out * SPARK_OUT
        body = self._post("/chat/completions", worst, json={
            "model": SPARK_MODEL, "max_tokens": max_out, "reasoning_effort": effort,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "schema": schema, "strict": True}}})
        u = body.get("usage", {})
        self.budget.settle(worst, u.get("prompt_tokens", 0) * SPARK_IN + u.get("completion_tokens", 0) * SPARK_OUT)
        try:
            out = json.loads(body["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise MetaError(f"Muse Spark did not return JSON: {str(body)[:300]}") from e
        missing = [k for k in schema.get("required", []) if k not in out]
        if missing:
            raise MetaError(f"Muse Spark answer is missing {missing}")
        return out

    def _sam(self, worst, actual, content):
        body = self._post("/responses", worst, json={
            "model": "sam-3.1", "metadata": {"include_confidence": "true"},
            "input": [{"role": "user", "content": content}]})
        self.budget.settle(worst, actual)
        if body.get("output_text"):
            return body["output_text"]
        return "".join(c.get("text", "") for item in body.get("output", [])
                       for c in item.get("content", []) if c.get("type") == "output_text")

    def sam_image(self, image: np.ndarray, prompt: str) -> list[SamObject]:
        ok, jpg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 92])
        url = "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode()
        text = self._sam(SAM_IMAGE, SAM_IMAGE, [{"type": "input_text", "text": prompt},
                                                {"type": "input_image", "image_url": url}])
        h, w = image.shape[:2]
        sx, sy = _scale(text, w, h)
        records = _parse(sam.image_segmentation_format(), text)
        boxes = {r.object_id: r for r in records if r.kind == "box"}
        out = []
        for r in records:
            if r.kind != "mask":
                continue
            b = boxes.get(r.object_id)
            box = _box(r.bounds, sx, sy)
            full = np.zeros((h, w), bool)
            small = np.frombuffer(sam.decode_mask_to_raster(r.mask), np.uint8).reshape(r.mask.height, r.mask.width)
            bw, bh = box[2] - box[0], box[3] - box[1]
            if bw > 0 and bh > 0:  # masks are box-local; stretch over the box
                full[box[1]:box[3], box[0]:box[2]] = cv2.resize(small, (bw, bh), interpolation=cv2.INTER_NEAREST) > 0
            score = b.confidence if b is not None and b.confidence is not None else r.confidence
            out.append(SamObject(int(r.object_id), box, score, full))
        return out

    def sam_video(self, mp4: bytes, prompt: str) -> list[SamTrackPoint]:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(mp4)
        cap = cv2.VideoCapture(f.name)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        os.unlink(f.name)
        cost = max(frames, SAM_MIN_FRAMES) * SAM_FRAME
        url = "data:video/mp4;base64," + base64.b64encode(mp4).decode()
        text = self._sam(cost, cost, [{"type": "input_text", "text": prompt},
                                      {"type": "input_video", "video_url": url}])
        sx, sy = _scale(text, w, h)
        return [SamTrackPoint(int(r.object_id), r.frame.frame_index if r.frame else 0, _box(r, sx, sy), r.confidence)
                for r in _parse(sam.video_segmentation_format(), text) if r.kind == "box"]

    def transcribe(self, wav: bytes, keywords=(), languages: list[str] | None = None) -> str:
        with wave.open(io.BytesIO(wav)) as wf:
            seconds = wf.getnframes() / wf.getframerate()
        cost = seconds / 3600 * VOICE_HOUR
        req = {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV", "mode": "PUSH_TO_TALK",
               "keywords": list(keywords)}
        if languages:
            req["languageBias"] = list(languages)
        body = self._post("/asr/transcribe", cost, files={
            "request": (None, json.dumps(req), "application/json"),
            "audio": ("audio.wav", wav, "audio/wav")})
        self.budget.settle(cost, cost)
        return body.get("transcript", "")


def _parse(fmt, text):
    p = fmt.create_parser()
    p.push(text if text.endswith("\n") else text + "\n")
    return p.finish(sam.CompletedOutcome()).result.records


_BOX_TOKEN = re.compile(r"<\|box;([^|]*)\|>")


def _scale(text, w, h):
    """SAM reports boxes in its own source size (box fields w, h); map them onto our image size."""
    m = _BOX_TOKEN.search(text)
    if not m:
        return 1.0, 1.0
    f = dict(p.strip().partition("=")[::2] for p in m.group(1).split(";"))
    try:
        return w / int(f["w"]), h / int(f["h"])
    except (KeyError, ValueError, ZeroDivisionError):
        return 1.0, 1.0


def _box(b, sx, sy):
    return (round(b.left * sx), round(b.top * sy), round(b.right * sx), round(b.bottom * sy))
