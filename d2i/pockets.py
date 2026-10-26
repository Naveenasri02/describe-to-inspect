"""Find the strip and its pockets: SAM 3.1 when online, the checklist grid when offline."""
from dataclasses import dataclass

import cv2
import numpy as np

from d2i.meta_api import BudgetExceeded, MetaError

MIN_SCORE = 0.5


@dataclass
class Layout:
    strip_box: tuple
    pockets: list
    source: str   # "sam" or "grid"
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


def find_layout(image: np.ndarray, cl: dict, client=None) -> Layout:
    if client is not None:
        try:
            strips = client.sam_image(image, "blister strip")
            if not strips:
                return Layout((0, 0, 0, 0), [], "sam", False, "no strip")
            strip = max(strips, key=lambda o: (o.box[2] - o.box[0]) * (o.box[3] - o.box[1])).box
            found = client.sam_image(image, "blister pocket")
            pockets = _row_major([o.box for o in found
                                  if (o.score is None or o.score >= MIN_SCORE) and _inside(o.box, strip)])
            ok = len(pockets) == cl["count"]
            note = "" if ok else f"found {len(pockets)} pockets, checklist says {cl['count']}"
            return Layout(tuple(strip), pockets, "sam", ok, note)
        except (MetaError, BudgetExceeded):
            pass  # keep inspecting offline with the checklist grid
    strip = _strip_by_contour(image)
    if strip is None:
        return Layout((0, 0, 0, 0), [], "grid", False, "no strip")
    return Layout(strip, grid(strip, cl["rows"], cl["per_row"]), "grid", True, "")
