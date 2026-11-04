"""Pocket boxes and labels drawn on a camera frame: the live stream, reject photos and self-test pictures."""
import cv2

RED, BLUE, GREEN = (42, 42, 194), (200, 120, 30), (125, 212, 43)  # BGR: screen reject red, strip blue, a green that reads on foil
TAB = (30, 60, 10)  # BGR dark green behind a good pocket's number
DEFECT = {"empty_pocket": "empty", "broken_tablet": "broken tablet", "double_tablet": "two tablets",
          "wrong_tablet": "different tablet", "tablet_spot": "spot on tablet", "unlisted_anomaly": "looks unusual"}


def _pocket_no(f, n, x0, y0, colour, s, font):  # the pocket number in a small filled tab at the box's top-left corner
    th, pad = max(1, round(s)), max(2, round(3 * s))
    (tw, tht), base = cv2.getTextSize(str(n), font, 0.5 * s, th)
    cv2.rectangle(f, (x0, y0), (x0 + tw + 2 * pad, y0 + tht + base + 2 * pad), colour, -1)
    cv2.putText(f, str(n), (x0 + pad, y0 + pad + tht), font, 0.5 * s, (255, 255, 255), th, cv2.LINE_AA)


def draw_pockets(f, boxes, bad):
    """Pocket boxes: good ones a thin green outline, failing ones a thick red one under a filled label "Pocket N: <defect>".
    Each box carries its number in a tab (dark green, red when failing). `bad` maps a failing pocket number to its check.
    Shared by /stream and the reject photo."""
    s, font = f.shape[1] / 1280, cv2.FONT_HERSHEY_DUPLEX  # sizes follow the frame: the same on screen at any camera size
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        if i + 1 not in bad:
            cv2.rectangle(f, (x0, y0), (x1, y1), GREEN, max(2, round(2 * s)))
            _pocket_no(f, i + 1, x0, y0, TAB, s, font)
    for i, (x0, y0, x1, y1) in enumerate(boxes):  # failing pockets last, so their labels sit on top
        if i + 1 in bad:
            cv2.rectangle(f, (x0, y0), (x1, y1), RED, max(4, round(6 * s)))
            _pocket_no(f, i + 1, x0, y0, RED, s, font)
            text = f"Pocket {i + 1}: {DEFECT.get(bad[i + 1], str(bad[i + 1]).replace('_', ' '))}"
            scale, th, pad = 0.8 * s, max(1, round(2 * s)), round(10 * s)
            (tw, tht), base = cv2.getTextSize(text, font, scale, th)
            w, h, cx = tw + 2 * pad, tht + base + 2 * pad, int((x0 + x1) // 2)
            lx = int(min(max(0, cx - w // 2), f.shape[1] - w))
            # above the strip with a leader line, so no pocket is covered; below it when the strip is at the frame top
            top, low = min(b[1] for b in boxes), max(b[3] for b in boxes)
            ly = int(top - h - 3 * pad if top - h - 3 * pad >= 0 else low + 3 * pad)
            cv2.line(f, (cx, ly + h if ly < y0 else ly), (cx, int(y0 if ly < y0 else y1)), RED, th)
            cv2.rectangle(f, (lx, ly), (lx + w, ly + h), RED, -1)
            cv2.putText(f, text, (lx + pad, ly + pad + tht), font, scale, (255, 255, 255), th, cv2.LINE_AA)
