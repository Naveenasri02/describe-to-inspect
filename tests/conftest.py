import json

import cv2
import numpy as np
import pytest


@pytest.fixture(autouse=True)
def _own_spend_file(tmp_path, monkeypatch):
    """No test reads or writes the real meta_spend.json."""
    monkeypatch.setattr("d2i.meta_api.DEFAULT_SPEND", tmp_path / "meta_spend.json")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class FakeHTTP:
    """Stands in for `requests`: returns queued responses (an exception is raised) and records each call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None, files=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "files": files, "data": data,
                           "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item if isinstance(item, tuple) else (200, item)
        return FakeResponse(status, body)


def sam_text(objects, frame=0, size=(640, 360)):
    """One SAM API output line. objects = [(id, (left, top, right, bottom), score)], boxes half-open,
    masks fill the box; size = the source width/height the API reports in every box token."""
    from meta_sam_parser._mask_codec import _encode_raster

    parts = []
    for oid, (x1, y1, x2, y2), score in objects:
        w, h = x2 - x1, y2 - y1
        mask = _encode_raster([1] * (w * h), w, h)
        parts.append(f"{oid}<|box;x1={x1};y1={y1};x2={x2 - 1};y2={y2 - 1};w={size[0]};h={size[1]};c={score}|>"
                     f"<|mask;x=0;y=0;data={h},{w},{mask};c={score}|>")
    return f"<{frame}f>" + ",".join(parts)


def responses_body(text, prompt_tokens=10, output_tokens=5):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": prompt_tokens, "output_tokens": output_tokens}}


STRIP_BOX = (80, 70, 560, 290)  # make_strip() strip outline with shift=(0, 0)


def pocket_box(i, rows=2, per_row=5, margin=0.06, strip=STRIP_BOX):
    """Pocket i (row-major) of make_strip(), as a grid cell shrunk by `margin` on each side."""
    x0, y0, x1, y1 = strip
    cw, ch = (x1 - x0) / per_row, (y1 - y0) / rows
    row, col = divmod(i, per_row)
    return (round(x0 + (col + margin) * cw), round(y0 + (row + margin) * ch),
            round(x0 + (col + 1 - margin) * cw), round(y0 + (row + 1 - margin) * ch))


def make_strip(rows=2, per_row=5, missing=(), cracked=(), torn=False, shift=(0, 0), seed=0):
    """Synthetic 640x360 blister strip: dark table, silver foil, white tablets in a grid."""
    rng = np.random.default_rng(seed)
    img = np.full((360, 640, 3), 40, np.uint8)
    x0, y0, x1, y1 = 80 + shift[0], 70 + shift[1], 560 + shift[0], 290 + shift[1]
    foil = np.clip(rng.normal(170, 3, (y1 - y0, x1 - x0, 3)), 0, 255).astype(np.uint8)
    strip = np.zeros_like(img)
    strip[y0:y1, x0:x1] = foil
    shape = np.zeros(img.shape[:2], np.uint8)
    cv2.rectangle(shape, (x0 + 12, y0), (x1 - 12, y1), 255, -1)
    cv2.rectangle(shape, (x0, y0 + 12), (x1, y1 - 12), 255, -1)
    for cx, cy in [(x0 + 12, y0 + 12), (x1 - 12, y0 + 12), (x0 + 12, y1 - 12), (x1 - 12, y1 - 12)]:
        cv2.circle(shape, (cx, cy), 12, 255, -1)
    img[shape > 0] = strip[shape > 0]
    cw, ch = (x1 - x0) / per_row, (y1 - y0) / rows
    r = int(min(cw, ch) * 0.32)
    for i in range(rows * per_row):
        row, col = divmod(i, per_row)
        cx, cy = int(x0 + (col + 0.5) * cw), int(y0 + (row + 0.5) * ch)
        if i in missing:
            continue
        cv2.circle(img, (cx, cy), r, (235, 235, 235), -1)
        if i in cracked:
            pts = np.array([[cx - r, cy - 3], [cx - r // 2, cy + 4], [cx, cy - 4],
                            [cx + r // 2, cy + 5], [cx + r, cy - 2]], np.int32)
            cv2.polylines(img, [pts], False, (60, 60, 60), 3)
    if torn:
        tri = np.array([[x1 - 70, y1], [x1, y1 - 60], [x1, y1]], np.int32)
        cv2.fillPoly(img, [tri], (40, 40, 40))
    return img
