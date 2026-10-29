"""Find the strip and its pockets: SAM 3.1 when online, the checklist grid when offline."""
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

import cv2
import numpy as np

from d2i.meta_api import SAM_SIDE, BudgetExceeded, MetaError

MIN_SCORE = 0.5
# "blister strip" finds nothing on close-ups; "blister" found 13/25 real photos alone (missed 4 tablet-side), so it is last
STRIP_PROMPTS = ("blister strip", "blister pack", "pill pack", "blister")
PAD = 0.08  # pill-outline strip: padding as a fraction of the pill bounds
# shortcut: only white tablets measured (27 Oct); add colour words when a coloured product is taught
POCKET_PROMPTS = ("pill", "white pill")  # "blister pocket" and "tablet" find nothing
GROW = 0.5  # a pill follows a taught pocket when its centre is inside that pocket grown by half a pocket each way
# every SAM 3.1 image call goes through here: one image's prompts together, at most 10 calls in flight station-wide
SAM_POOL = ThreadPoolExecutor(10, thread_name_prefix="sam")


@dataclass
class Layout:
    strip_box: tuple
    pockets: list  # what is cropped and judged: the pill found in each pocket, else the taught pocket box
    source: str   # "sam", "sam-pills" (strip = padded pill bounds) or "grid"
    ok: bool
    note: str = ""
    pills: list = field(default_factory=list)  # SAM pill boxes inside the strip (empty on the grid path)
    slots: list = field(default_factory=list)  # taught pocket boxes on this strip, same order (the measuring scale)
    in_pocket: list = field(default_factory=list)  # per pocket, the pills that sit in it, nearest first
    angle: float = 0.0  # radians the strip is turned against the taught pattern, from the pill centres


def pill_prompts(item: str) -> tuple:
    return ("capsule", "pill") if "capsule" in item.lower() else POCKET_PROMPTS


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


def _centre(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def upright(box, angle) -> tuple:
    """(width, height) of a strip turned by a small angle (radians) inside its upright box; the box's own size when
    the turn is large or the sizes do not fit one."""
    w, h = box[2] - box[0], box[3] - box[1]
    a = angle if abs(angle) < 0.3 else 0.0
    c, s = abs(np.cos(a)), abs(np.sin(a))
    tw, th = (w * c - h * s) / (c * c - s * s), (h * c - w * s) / (c * c - s * s)
    return (tw, th) if min(tw, th) > 0 else (w, h)


def framed(strip, shape) -> bool:
    """A strip box at the frame edge is the frame (a close-up), not a turned strip's upright box. Within 2 of the
    pixels SAM 3.1 saw: its copy is at most SAM_SIDE long, so one of its pixels can be several of ours."""
    e = 2 * max(1.0, max(shape[:2]) / SAM_SIDE)
    return strip[0] <= e or strip[1] <= e or strip[2] >= shape[1] - e or strip[3] >= shape[0] - e


def _on_turned(strip, template, angle):
    """The taught pocket boxes on a strip turned by `angle` (radians): centres turned with the strip about its centre,
    sizes from its own straightened size, so a tablet measures the same however the strip lies."""
    (cx, cy), (tw, th), c, s = _centre(strip), upright(strip, angle), np.cos(angle), np.sin(angle)
    out = []
    for x0, y0, x1, y1 in template:
        x, y = ((x0 + x1) / 2 - 0.5) * tw, ((y0 + y1) / 2 - 0.5) * th
        px, py, hw, hh = cx + c * x - s * y, cy + s * x + c * y, (x1 - x0) * tw / 2, (y1 - y0) * th / 2
        out.append((round(px - hw), round(py - hh), round(px + hw), round(py + hh)))
    return out


def turn_of(lay, template) -> float:
    """How far a taught photo's strip is turned against the taught pockets (radians), from its pills in order."""
    return _turn([_centre(s) for s in _from_template(lay.strip_box, template)], [_centre(b) for b in lay.pockets])


def _turn(ref, found) -> float:
    """Least-squares rotation (radians) that turns the reference centres onto the found ones; 0 with fewer than 2."""
    if len(ref) < 2:
        return 0.0
    q, p = np.array(ref, float), np.array(found, float)
    q, p = q - q.mean(0), p - p.mean(0)
    return float(np.arctan2((q[:, 0] * p[:, 1] - q[:, 1] * p[:, 0]).sum(), (q * p).sum()))


def _follow(slots, pills):
    """(per slot its pills, nearest first; stray pills). A pill belongs to the slot with the nearest centre when it
    lies inside that slot grown by GROW. The nearest one is the slot's tablet wherever the strip turned; another one
    counts only inside the taught box (a second tablet), else it is outside the pocket pattern."""
    cs = [_centre(s) for s in slots]
    d2 = lambda b, i: (_centre(b)[0] - cs[i][0]) ** 2 + (_centre(b)[1] - cs[i][1]) ** 2
    mine, stray = [[] for _ in slots], []
    for b in pills:
        i = min(range(len(slots)), key=lambda k: d2(b, k))
        (x0, y0, x1, y1), (cx, cy) = slots[i], _centre(b)
        gx, gy = (x1 - x0) * GROW, (y1 - y0) * GROW
        (mine[i] if x0 - gx <= cx <= x1 + gx and y0 - gy <= cy <= y1 + gy else stray).append(b)
    for i, m in enumerate(mine):
        m.sort(key=lambda b: d2(b, i))
        mine[i] = m[:1] + [b for b in m[1:] if _inside_centre(b, slots[i])]
        stray += [b for b in m[1:] if not _inside_centre(b, slots[i])]
    return mine, stray


def _inside_centre(b, box):
    cx, cy = _centre(b)
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


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
            # asked together (one wait, not one per prompt), except the later strip prompts: each is asked only when
            # the ones before found no strip (two whole-strip outlines in flight took 15 s each instead of 9)
            first, later = STRIP_PROMPTS[:2], STRIP_PROMPTS[2:]
            asked = {p: SAM_POOL.submit(client.sam_image, image, p) for p in first + pill_prompts(cl["item"])}
            wait(asked.values())  # no call outlives its layout
            # answers are read in the order they used to be asked; one that is not needed is not opened, nor its error
            found = lambda p: [o.box for o in asked[p].result() if o.score is None or o.score >= MIN_SCORE]
            boxes = next((f for f in map(found, first) if f), None)
            for p in later:
                if boxes is not None:
                    break
                asked[p] = SAM_POOL.submit(client.sam_image, image, p)
                boxes = found(p) or None
            strip = tuple(max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))) if boxes else None
            pills = next((f for f in map(found, pill_prompts(cl["item"])) if f), [])
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
            at_edge = framed(strip, image.shape)  # a close-up: nothing to straighten
            centres = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in pills]
            pills = [b for b, (cx, cy) in zip(pills, centres)
                     if strip[0] <= cx <= strip[2] and strip[1] <= cy <= strip[3]]
            if template is None:
                pockets = _row_major(pills)
                ok = len(pockets) == cl["count"]
                note = "" if ok else f"found {len(pockets)} pills, checklist says {cl['count']}"
                return Layout(strip, pockets, source, ok, note, pills)  # its turn: Station.teach, against the taught pockets
            slots = _from_template(strip, template)
            mine, stray = _follow(slots, pills)  # crops follow the real tablets: a turned strip is not judged askew
            held = [(s, m[0]) for s, m in zip(slots, mine) if m]
            angle = 0.0 if at_edge else _turn([_centre(s) for s, _ in held], [_centre(b) for _, b in held])
            if angle:  # the taught pockets on the strip as it lies, not stretched over the bigger upright box around it
                slots = _on_turned(strip, template, angle)  # (tablets read 6% small at 3 deg)
                mine, stray = _follow(slots, pills)
            pockets = [m[0] if m else s for s, m in zip(slots, mine)]  # a pocket with no pill keeps its taught box
            note = "object outside the pocket pattern" if stray else ""
            return Layout(strip, pockets, source, not note, note, pills, slots, mine, angle)
        except (MetaError, BudgetExceeded):
            pass  # keep inspecting offline with the checklist grid
    strip = _strip_by_contour(image)
    if strip is None:
        return Layout((0, 0, 0, 0), [], "grid", False, "no strip")
    return Layout(strip, grid(strip, cl["rows"], cl["per_row"]), "grid", True, "")
