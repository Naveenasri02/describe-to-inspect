"""One paid call to each Meta model the bench uses. Run once after the credits show on Billing."""
import argparse
import io
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from d2i.meta_api import Budget, MetaClient

ROOT = Path(__file__).resolve().parent.parent


def need(img):
    if img is None:
        raise ValueError("no photo found to send")
    return img


def run_calls(client, img, clip, wav):
    """One call per model; a failure is reported, never raised."""
    def video():
        need(img)
        return client.sam_video(Path(clip).read_bytes(), "pill pack")

    calls = [
        ("Muse Spark (spark_json)", lambda: client.spark_json(
            "Answer in JSON.", "Say ok.", {"type": "object", "additionalProperties": False,
                                           "required": ["reply"], "properties": {"reply": {"type": "string"}}},
            name="smoke", max_out=1500)),
        ("SAM 3.1 image", lambda: client.sam_image(need(img), "pill pack")),
        ("SAM 3.1 video", video),
        ("Transcribe", lambda: client.transcribe(wav)),
    ]
    out = []
    for name, fn in calls:
        t0 = time.perf_counter()
        try:
            r = fn()
            res = f"OK ({len(r) if hasattr(r, '__len__') else 'answered'} items)"
        except Exception as e:  # one failure must not stop the others
            res = f"ERROR {type(e).__name__}: {e}"
        out.append((name, res, time.perf_counter() - t0))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-credits-are-on-billing", action="store_true")
    if not ap.parse_args(argv).yes_credits_are_on_billing:
        print("Refusing: this makes paid calls. Check the credits on Billing, then pass --yes-credits-are-on-billing.")
        return 2
    good = sorted(p for p in (ROOT / "photos" / "good").glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    img = cv2.imread(str(good[0] if good else ROOT / "photos" / "real_wa" / "wa5.jpg"))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"\0\0" * 32000)  # 2 s of silence; an empty transcript is a valid answer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip = tmp / "clip.mp4"
        if img is not None:
            vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (img.shape[1], img.shape[0]))
            for _ in range(60):
                vw.write(img)
            vw.release()
        client = MetaClient(Budget())  # the shared spend file: these calls count toward the $50 stop
        for name, res, secs in run_calls(client, img, clip, buf.getvalue()):
            print(f"{name}: {res} in {secs:.1f}s")
        print(f"Meta spend so far (all runs): ${client.budget.spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
