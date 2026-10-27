"""Tray clip: SAM 3.1 video tracks each strip; the clearest frame of each is inspected on its own."""
import time
from pathlib import Path

import cv2

from .meta_api import SAM_MIN_FRAMES, BudgetExceeded, MetaError
from .pockets import STRIP_PROMPTS
from .station import Verdict

MARGIN, PAD = 4, 0.08


def record_clip(source, seconds: float, path: Path, fps: int = 15) -> Path:
    """Record from a camera index or video file; always at least SAM_MIN_FRAMES (last frame repeated)."""
    cap = cv2.VideoCapture(source)
    vw, last, n = None, None, 0
    want, end = max(int(seconds * fps), SAM_MIN_FRAMES), time.time() + seconds
    while n < want:
        ok, frame = cap.read() if time.time() < end or not isinstance(source, int) else (False, None)
        if ok:
            last = frame
        elif last is None:
            cap.release()
            raise ValueError(f"no frame could be read from {source!r}")
        if vw is None:
            vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (last.shape[1], last.shape[0]))
        vw.write(last)
        n += 1
    cap.release()
    vw.release()
    return Path(path)


def best_frames(points, frame_size, min_score: float = 0.6) -> dict[int, int]:
    """Per track id, the highest-scoring frame whose box lies fully inside the frame."""
    w, h = frame_size
    best: dict[int, tuple[float, int]] = {}
    for p in points:
        x0, y0, x1, y1 = p.box
        s = p.score or 0.0
        if s >= min_score and x0 >= MARGIN and y0 >= MARGIN and x1 <= w - MARGIN and y1 <= h - MARGIN \
                and s > best.get(p.id, (-1, 0))[0]:
            best[p.id] = (s, p.frame)
    return {i: f for i, (_, f) in best.items()}


def inspect_clip(path: Path, station, client, held: bool = False) -> list[Verdict]:
    """Every verdict returned is already in log.jsonl; held: the line is held, so no strip comes back PASS."""
    def _hold(strip_id, check, text) -> Verdict:
        return station.log(Verdict(status="HOLD", strip_id=strip_id,
                                   reasons=[{"rule": None, "check": check, "pocket": None, "text": text}]))

    points = []
    try:
        mp4 = Path(path).read_bytes()
        for prompt in STRIP_PROMPTS:  # each call is billed >= 50 frames: stop at the first that finds tracks
            points = client.sam_video(mp4, prompt)
            if points:
                break
    except (MetaError, BudgetExceeded, OSError) as e:
        return [_hold(None, "clip_error", f"tray clip not checked ({e}) — check by hand")]
    if not points:
        return [_hold(None, "no_strips", "no strips found in the tray clip — check by hand")]
    cap = cv2.VideoCapture(str(path))
    try:
        size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        chosen, verdicts = best_frames(points, size), []
        for tid in sorted({p.id for p in points}):
            if tid not in chosen:
                verdicts.append(_hold(tid, "not_seen_clearly", f"strip {tid} not seen clearly — check by hand"))
                continue
            frame_no = chosen[tid]
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ok, frame = cap.read()
            x0, y0, x1, y1 = next(p.box for p in points if p.id == tid and p.frame == frame_no)
            px, py = int((x1 - x0) * PAD), int((y1 - y0) * PAD)
            crop = frame[max(y0 - py, 0):min(y1 + py, size[1]), max(x0 - px, 0):min(x1 + px, size[0])] if ok else None
            if crop is None or crop.size == 0:
                verdicts.append(_hold(tid, "frame_unreadable", f"strip {tid} frame unreadable — check by hand"))
                continue
            verdicts.append(station.inspect(crop, strip_id=tid, held=held))
        return verdicts
    finally:
        cap.release()
