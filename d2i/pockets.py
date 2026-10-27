"""Find the strip and its pockets: SAM 3.1 when online, the checklist grid when offline."""
from dataclasses import dataclass

import cv2
import numpy as np

from d2i.meta_api import BudgetExceeded, MetaError

MIN_SCORE = 0.5
STRIP_PROMPTS = ("blister strip", "blister pack", "pill pack")  # "blister strip" finds nothing on close-ups
PAD = 0.08  # pill-outline strip: padding as a fraction of the pill bounds
# shortcut: only white tablets measured (27 Oct); add colour words when a coloured product is taught
POCKET_PROMPTS = ("pill", "white pill")  # "blister pocket" and "tablet" find nothing


@dataclass
class Layout:
    strip_box: tuple
    pockets: list
    source: str   # "sam", "sam-pills" (strip = padded pill bounds) or "grid"
    ok: bool
    note: str = ""


def grid(strip_box, rows: int, per_row: int, margin: float = 0.06) -> list[tuple]:
    x0, y0, x1, y1 = strip_box
    cw, ch = (x1 - x0) / per_row, (y1 - y0) / rows
    return [(round(x0 + (c + margin) * cw), round(y0 + (r + margin) * ch),
             round(x0 + (c + 1 - margin) * cw), round(y0 + (r + 1 - margin) * ch))
            for r in range(rows) for c in range(per_row)]


def _row_major(boxes):
    """Sort boxes into rows (by centre y), then left to right."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b[1] + b[3]) / 2)
    gap = np.median([b[3] - b[1] for b in boxes]) / 2
    rows, last = [[boxes[0]]], (boxes[0][1] + boxes[0][3]) / 2
    for b in boxes[1:]:
        cy = (b[1] + b[3]) / 2
        if cy - last > gap:
            rows.append([])
        rows[-1].append(b)
        last = cy
    return [b for row in rows for b in sorted(row, key=lambda b: b[0])]


def _inside(box, outer, slack=4):
    return (box[0] >= outer[0] - slack and box[1] >= outer[1] - slack
            and box[2] <= outer[2] + slack and box[3] <= outer[3] + slack)


def _strip_by_contour(image):
    """Largest blob that does not touch the frame edge, in either brightness polarity."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if gray.std() < 8:  # a flat frame has no strip in it
        return None
    h, w = gray.shape
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    best = None
    for mode in (cv2.THRESH_BINARY, cv2.THRESH_BINARY_INV):
        _, bw = cv2.threshold(blur, 0, 255, mode + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if x <= 1 or y <= 1 or x + cw >= w - 1 or y + ch >= h - 1:
                continue
            area = cv2.contourArea(c)
            if best is None or area > best[0]:
                best = (area, (x, y, x + cw, y + ch))
    if best is None or best[0] < 0.10 * h * w:
        return None
    return best[1]


def template_from(layouts: list[Layout]) -> list[tuple]:
    """Per-pocket median of each pocket box as fractions of its strip box (ok layouts, same pocket count)."""
    good = [l for l in layouts if l.ok]
    if not good or len({len(l.pockets) for l in good}) != 1:
        raise ValueError("Need ok layouts with the same number of pockets to build a template.")
    fr = []
    for l in good:
        x0, y0, x1, y1 = l.strip_box
        w, h = x1 - x0, y1 - y0
        fr.append([((b[0] - x0) / w, (b[1] - y0) / h, (b[2] - x0) / w, (b[3] - y0) / h) for b in l.pockets])
    return [tuple(float(v) for v in m) for m in np.median(np.array(fr), axis=0)]


def _from_template(strip, template):
    x0, y0, x1, y1 = strip
    w, h = x1 - x0, y1 - y0
    return [(round(x0 + t[0] * w), round(y0 + t[1] * h), round(x0 + t[2] * w), round(y0 + t[3] * h))
            for t in template]


def find_layout(image: np.ndarray, cl: dict, client=None, template=None) -> Layout:
    if client is not None:
        try:
            strip = None
            for prompt in STRIP_PROMPTS:
                found = [o.box for o in client.sam_image(image, prompt) if o.score is None or o.score >= MIN_SCORE]
                if found:
                    strip = tuple(max(found, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])))
                    break
            for prompt in POCKET_PROMPTS:
                pills = [o.box for o in client.sam_image(image, prompt) if o.score is None or o.score >= MIN_SCORE]
                if pills:
                    break
            source = "sam"
            if strip is None:
                if not pills:
                    return Layout((0, 0, 0, 0), [], "sam", False, "no strip")
                h, w = image.shape[:2]
                x0, y0 = min(b[0] for b in pills), min(b[1] for b in pills)
                x1, y1 = max(b[2] for b in pills), max(b[3] for b in pills)
                px, py = int((x1 - x0) * PAD), int((y1 - y0) * PAD)
                strip = (max(0, x0 - px), max(0, y0 - py), min(w, x1 + px), min(h, y1 + py))
                source = "sam-pills"
            centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in pills]
            pills = [b for b, (cx, cy) in zip(pills, centres)
                     if strip[0] <= cx <= strip[2] and strip[1] <= cy <= strip[3]]
            if template is None:
                pockets = _row_major(pills)
                ok = len(pockets) == cl["count"]
                note = "" if ok else f"found {len(pockets)} pills, checklist says {cl['count']}"
                return Layout(strip, pockets, source, ok, note)
            pockets = _from_template(strip, template)
            note = ""
            if len(pills) > len(pockets):
                note = f"{len(pills)} pills found, only {len(pockets)} pockets"
            elif any(not any(p[0] <= (b[0] + b[2]) / 2 <= p[2] and p[1] <= (b[1] + b[3]) / 2 <= p[3]
                             for p in pockets) for b in pills):
                note = "object outside the pocket pattern"
            return Layout(strip, pockets, source, not note, note)
        except (MetaError, BudgetExceeded):
            pass  # keep inspecting offline with the checklist grid
    strip = _strip_by_contour(image)
    if strip is None:
        return Layout((0, 0, 0, 0), [], "grid", False, "no strip")
    return Layout(strip, grid(strip, cl["rows"], cl["per_row"]), "grid", True, "")
