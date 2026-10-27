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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-credits-are-on-billing", action="store_true")
    if not ap.parse_args(argv).yes_credits_are_on_billing:
        print("Refusing: this makes paid calls. Check the credits on Billing, then pass --yes-credits-are-on-billing.")
        return 2
    good = sorted((ROOT / "photos" / "good").glob("*.jpg"))
    img = cv2.imread(str(good[0] if good else ROOT / "photos" / "real_wa" / "wa5.jpg"))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(b"\0\0" * 32000)  # 2 s of silence; an empty transcript is a valid answer
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        clip = tmp / "clip.mp4"
        vw = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 15, (img.shape[1], img.shape[0]))
        for _ in range(60):
            vw.write(img)
        vw.release()
        client = MetaClient(Budget(tmp / "spend.json"))
        calls = [
            ("Muse Spark (spark_json)", lambda: client.spark_json(
                "Answer in JSON.", "Say ok.", {"type": "object", "additionalProperties": False,
                                               "required": ["reply"], "properties": {"reply": {"type": "string"}}},
                name="smoke", max_out=1500)),
            ("SAM 3.1 image", lambda: client.sam_image(img, "tablet")),
            ("SAM 3.1 video", lambda: client.sam_video(clip.read_bytes(), "tablet")),
            ("Transcribe", lambda: client.transcribe(buf.getvalue())),
        ]
        for name, fn in calls:
            t0 = time.perf_counter()
            try:
                r = fn()
                res = f"OK ({len(r) if hasattr(r, '__len__') else 'answered'} items)"
            except Exception as e:  # one failure must not stop the others
                res = f"ERROR {type(e).__name__}: {e}"
            print(f"{name}: {res} in {time.perf_counter() - t0:.1f}s")
        print(f"Total spend: ${client.budget.spent:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
