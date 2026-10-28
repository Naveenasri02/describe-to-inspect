"""Tablet checks by plain code: SAM pill boxes + pixel measurements against what the station was taught."""
import cv2
import numpy as np


def pills_in(pocket, pills) -> list:
    return [p for p in pills if pocket[0] <= (p[0] + p[2]) / 2 <= pocket[2] and pocket[1] <= (p[1] + p[3]) / 2 <= pocket[3]]


def _lab(crop):
    return cv2.cvtColor(crop.astype(np.float32) / 255, cv2.COLOR_BGR2LAB)  # L 0-100, a/b about -100..100


def _centre(image, pill, frac):
    x0, y0, x1, y1 = pill
    dx, dy = (x1 - x0) * (1 - frac) / 2, (y1 - y0) * (1 - frac) / 2
    return image[max(0, int(y0 + dy)):max(1, int(y1 - dy)), max(0, int(x0 + dx)):max(1, int(x1 - dx))]


def features(image, pocket, pill) -> dict | None:
    """None when the box cannot be measured (zero size, or outside the image)."""
    kw, kh = pocket[2] - pocket[0], pocket[3] - pocket[1]
    pw, ph = pill[2] - pill[0], pill[3] - pill[1]
    if min(kw, kh, pw, ph) <= 0 or pill[0] < 0 or pill[1] < 0 or pill[2] > image.shape[1] or pill[3] > image.shape[0]:
        return None  # zero size, or cut off by the image edge (it would be measured on a sliver)
    core, wide = _centre(image, pill, 0.5), _centre(image, pill, 0.7)
    if core.size == 0 or wide.size == 0:
        return None
    lab = _lab(core).reshape(-1, 3).mean(0)
    ell = _lab(wide)[..., 0]
    return {"size": float(np.sqrt((pw / kw) * (ph / kh))), "aspect": pw / ph, "area": (pw * ph) / (kw * kh),
            "lab": [float(v) for v in lab], "spot": float((ell < np.median(ell) - 35).mean())}


def _delta_e(a, b) -> float:
    return float(np.linalg.norm(np.array(a) - np.array(b)))


def teach_profile(images, layouts) -> dict | None:
    fs = [features(im, pk, pills_in(pk, lay.pills)[0]) for im, lay in zip(images, layouts)
          for pk in lay.pockets if len(pills_in(pk, lay.pills)) == 1]
    fs = [f for f in fs if f]
    if len(fs) < 5:
        return None
    col = lambda k: np.array([f[k] for f in fs])
    lab_med = np.median(col("lab"), axis=0)
    de = [_delta_e(f["lab"], lab_med) for f in fs]
    size_med, aspect_med = float(np.median(col("size"))), float(np.median(col("aspect")))
    return {"size_med": size_med, "size_tol": float(max(0.12 * size_med, 3 * col("size").std())),
            "aspect_med": aspect_med, "aspect_tol": float(max(0.15 * aspect_med, 3 * col("aspect").std())),
            "lab_med": [float(v) for v in lab_med], "colour_tol": float(max(12.0, 3 * np.std(de))),
            "spot_max": float(max(0.01, 2 * col("spot").max())), "area_med": float(np.median(col("area")))}


def pocket_check(image, pocket, pills, profile) -> str | None:
    """double_tablet, wrong_tablet or tablet_spot; None for no pill (the judge's job) or a normal one."""
    mine = pills_in(pocket, pills)
    if not mine:
        return None
    if len(mine) >= 2:
        return "double_tablet"
    f = features(image, pocket, mine[0])
    if f is None:
        return None
    if f["area"] >= 1.7 * profile["area_med"]:
        return "double_tablet"
    if (abs(f["size"] - profile["size_med"]) > profile["size_tol"]
            or abs(f["aspect"] - profile["aspect_med"]) > profile["aspect_tol"]
            or _delta_e(f["lab"], profile["lab_med"]) > profile["colour_tol"]):
        return "wrong_tablet"
    return "tablet_spot" if f["spot"] > profile["spot_max"] else None


def strip_features(lay) -> dict | None:
    """Strip aspect and the gaps (left, top, right, bottom) between the pill bounds and the strip box, as fractions.
    Margins are None unless every pocket holds exactly one pill (an empty or double pocket moves the bounds)."""
    if not lay.pills:
        return None
    sx0, sy0, sx1, sy1 = lay.strip_box
    w, h = sx1 - sx0, sy1 - sy0
    if min(w, h) <= 0:
        return None
    px0, py0 = min(p[0] for p in lay.pills), min(p[1] for p in lay.pills)
    px1, py1 = max(p[2] for p in lay.pills), max(p[3] for p in lay.pills)
    one_each = all(len(pills_in(pk, lay.pills)) == 1 for pk in lay.pockets)
    return {"aspect": max(w, h) / min(w, h),
            "margins": [(px0 - sx0) / w, (py0 - sy0) / h, (sx1 - px1) / w, (sy1 - py1) / h] if one_each else None}


def teach_strip(layouts) -> dict | None:
    fs = [strip_features(l) for l in layouts]
    if not fs or any(f is None for f in fs):
        return None
    a = np.array([f["aspect"] for f in fs])
    ms = [f["margins"] for f in fs if f["margins"] is not None]  # photos with an empty or double pocket add no margins
    m = np.array(ms) if ms else None
    return {"aspect_med": float(np.median(a)), "aspect_tol": float(max(0.06 * np.median(a), 3 * a.std())),
            "margin_med": [float(v) for v in np.median(m, axis=0)] if ms else None,
            "margin_tol": [float(max(0.04, 3 * v)) for v in m.std(axis=0)] if ms else None}


def strip_check(lay, sprofile) -> bool:
    """True when the strip is the wrong size or cut wrong (aspect or any margin out of range)."""
    f = strip_features(lay)
    if f is None:
        return False
    if abs(f["aspect"] - sprofile["aspect_med"]) > sprofile["aspect_tol"]:
        return True
    if f["margins"] is None or sprofile["margin_med"] is None:  # margins are judged only when every pocket has one tablet
        return False
    return any(abs(a - b) > t for a, b, t in zip(f["margins"], sprofile["margin_med"], sprofile["margin_tol"]))
