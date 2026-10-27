"""Paint fake defects inside one pocket of a good-strip photo, so the station can test itself.

Plain image code, not a model: the pocket outline comes from SAM 3.1 (or the checklist grid).
"""
import cv2
import numpy as np


def _foil_colour(image, box, strip_box):
    """Median colour of the foil in a ring just outside the pocket, kept inside the strip."""
    x0, y0, x1, y1 = box
    dx, dy = max(3, int((x1 - x0) * 0.08)), max(3, int((y1 - y0) * 0.08))
    sx0, sy0, sx1, sy1 = strip_box
    rx0, ry0 = max(x0 - dx, sx0, 0), max(y0 - dy, sy0, 0)
    rx1, ry1 = min(x1 + dx, sx1, image.shape[1]), min(y1 + dy, sy1, image.shape[0])
    ring = np.ones((ry1 - ry0, rx1 - rx0), bool)
    ring[max(y0 - ry0, 0):y1 - ry0, max(x0 - rx0, 0):x1 - rx0] = False
    pixels = image[ry0:ry1, rx0:rx1][ring]
    return np.median(pixels, axis=0) if len(pixels) else np.array([170, 170, 170])


def _background_colour(image, strip_box):
    """Median colour of the table outside the strip."""
    sx0, sy0, sx1, sy1 = strip_box
    mask = np.ones(image.shape[:2], bool)
    mask[max(sy0, 0):sy1, max(sx0, 0):sx1] = False
    return np.median(image[mask], axis=0) if mask.any() else np.array([40, 40, 40])


def _fill(out, region, colour, rng):
    noise = rng.normal(0, 4, (int(region.sum()), 3))
    out[region] = np.clip(colour + noise, 0, 255).astype(np.uint8)


def paint(image, pocket_box, kind, strip_box, rng):
    out = image.copy()
    x0, y0, x1, y1 = pocket_box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    ax, ay = max(2, int((x1 - x0) / 2 * 0.85)), max(2, int((y1 - y0) / 2 * 0.85))
    shape = image.shape[:2]

    if kind in ("empty_pocket", "broken_tablet"):
        foil = _foil_colour(image, pocket_box, strip_box)
        region = np.zeros(shape, np.uint8)
        if kind == "empty_pocket":
            cv2.ellipse(region, (cx, cy), (ax, ay), 0, 0, 360, 255, -1)
        else:  # a missing wedge (about 1/6) of the tablet
            start = rng.uniform(0, 360)
            cv2.ellipse(region, (cx, cy), (ax, ay), 0, start, start + 60, 255, -1)
        _fill(out, region > 0, foil, rng)
        if kind == "broken_tablet":  # plus a dark jagged crack across the tablet
            n = int(rng.integers(5, 8))
            angle = rng.uniform(0, np.pi)
            t = np.linspace(-0.8, 0.8, n)
            jitter = rng.normal(0, 0.12, n)
            pts = np.stack([cx + t * ax * np.cos(angle) - jitter * ax * np.sin(angle),
                            cy + t * ay * np.sin(angle) + jitter * ay * np.cos(angle)], axis=1).astype(np.int32)
            dark = tuple(int(c) for c in np.clip(foil * 0.25, 0, 255))
            cv2.polylines(out, [pts], False, dark, int(rng.integers(2, 4)))
        return out

    if kind == "foil_damage":  # a jagged notch torn in from the nearest strip edge
        sx0, sy0, sx1, sy1 = strip_box
        w = x1 - x0
        depth = w * rng.uniform(0.3, 0.6)
        half = w * rng.uniform(0.10, 0.18)
        edges = {"top": cy - sy0, "bottom": sy1 - cy, "left": cx - sx0, "right": sx1 - cx}
        side = min(edges, key=edges.get)
        n = 6
        along = np.linspace(-half, half, n)
        tip = rng.normal(0, half * 0.25, 1)[0]
        inward = np.concatenate([[0], rng.uniform(0.25, 0.75, n - 2) * depth, [0]])
        inward[n // 2] = depth
        if side in ("top", "bottom"):
            sign, edge = (1, sy0) if side == "top" else (-1, sy1)
            pts = [(cx + a + (tip if i == n // 2 else 0), edge + sign * d) for i, (a, d) in enumerate(zip(along, inward))]
            pts = [(cx - half, edge - sign * 2)] + pts + [(cx + half, edge - sign * 2)]
        else:
            sign, edge = (1, sx0) if side == "left" else (-1, sx1)
            pts = [(edge + sign * d, cy + a + (tip if i == n // 2 else 0)) for i, (a, d) in enumerate(zip(along, inward))]
            pts = [(edge - sign * 2, cy - half)] + pts + [(edge - sign * 2, cy + half)]
        region = np.zeros(shape, np.uint8)
        cv2.fillPoly(region, [np.array(pts, np.int32)], 255)
        _fill(out, region > 0, _background_colour(image, strip_box), rng)
        return out

    if kind == "strip_cut":  # the last 15% of the long side is gone: the strip looks shorter
        sx0, sy0, sx1, sy1 = strip_box
        region = np.zeros(shape, bool)
        if sx1 - sx0 >= sy1 - sy0:
            region[max(sy0, 0):sy1, int(sx1 - 0.15 * (sx1 - sx0)):sx1] = True
        else:
            region[int(sy1 - 0.15 * (sy1 - sy0)):sy1, max(sx0, 0):sx1] = True
        _fill(out, region, _background_colour(image, strip_box), rng)
        return out

    if kind in ("double_tablet", "wrong_tablet", "tablet_spot"):
        foil = _foil_colour(image, pocket_box, strip_box)
        tablet = np.zeros(shape, np.uint8)
        cv2.ellipse(tablet, (cx, cy), (ax, ay), 0, 0, 360, 255, -1)
        if kind == "double_tablet":  # a copy of the tablet, 35% of the pocket width to the right, kept in the pocket
            move = np.float32([[1, 0, int((x1 - x0) * 0.35)], [0, 1, 0]])
            copy, mask = (cv2.warpAffine(a, move, (shape[1], shape[0])) for a in (image, tablet))
            mask[:, :x0] = 0
            mask[:, x1:] = 0
            out[mask > 0] = copy[mask > 0]
        elif kind == "wrong_tablet":  # the same tablet at 65% size
            patch = image[cy - ay:cy + ay + 1, cx - ax:cx + ax + 1]
            sx, sy = max(2, int(ax * 0.65)), max(2, int(ay * 0.65))
            small = cv2.resize(patch, (2 * sx + 1, 2 * sy + 1))
            mask = np.zeros(small.shape[:2], np.uint8)
            cv2.ellipse(mask, (sx, sy), (sx, sy), 0, 0, 360, 255, -1)
            _fill(out, tablet > 0, foil, rng)
            roi = out[cy - sy:cy + sy + 1, cx - sx:cx + sx + 1]
            roi[mask > 0] = small[mask > 0]
        else:  # a dark speck on the tablet
            r = max(2, int(0.08 * min(x1 - x0, y1 - y0)))
            ang, d = rng.uniform(0, 2 * np.pi), rng.uniform(0, 0.4) * min(ax, ay)
            cv2.circle(out, (int(cx + d * np.cos(ang)), int(cy + d * np.sin(ang))), r,
                       tuple(int(c) for c in np.clip(foil * 0.2, 0, 255)), -1)
        return out

    raise ValueError(f"unknown defect kind: {kind}")
