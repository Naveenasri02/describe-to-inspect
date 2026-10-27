import cv2
import numpy as np

from d2i.meta_api import SamTrackPoint
from d2i.station import Verdict
from d2i.tray import best_frames, inspect_clip, record_clip

SIZE = (100, 80)  # w, h


def test_best_frames_prefers_full_confident_box():
    pts = [SamTrackPoint(0, 3, (0, 10, 40, 60), 0.95),    # cut by the left edge
           SamTrackPoint(0, 5, (10, 10, 50, 60), 0.8),
           SamTrackPoint(0, 6, (10, 10, 50, 60), 0.7)]
    assert best_frames(pts, SIZE) == {0: 5}


class FakeClient:
    def __init__(self, pts):
        self.pts, self.prompts = pts, []

    def sam_video(self, mp4, prompt):
        self.prompts.append(prompt)
        return self.pts if len(self.prompts) == 2 else []


class FakeStation:
    def __init__(self):
        self.crops, self.logged = [], []

    def inspect(self, image, strip_id=None, image_name=None, held=False):
        self.crops.append(image)
        return self.log(Verdict(status="PASS", strip_id=strip_id), held)

    def log(self, v, held=False):
        if held and v.status == "PASS":
            v.status = "HOLD"
        self.logged.append(v)
        return v


def test_record_clip_pads_to_50_frames_and_inspect(tmp_path):
    src = tmp_path / "src.mp4"
    vw = cv2.VideoWriter(str(src), cv2.VideoWriter_fourcc(*"mp4v"), 15, SIZE)
    for _ in range(5):
        vw.write(np.full((SIZE[1], SIZE[0], 3), 90, np.uint8))
    vw.release()
    path = record_clip(str(src), 1, tmp_path / "out.mp4")
    cap = cv2.VideoCapture(str(path))
    assert cap.get(cv2.CAP_PROP_FRAME_COUNT) >= 50
    cap.release()

    pts = [SamTrackPoint(1, 7, (10, 10, 50, 60), 0.9), SamTrackPoint(2, 7, (60, 10, 90, 60), 0.3)]
    client, station = FakeClient(pts), FakeStation()
    out = inspect_clip(path, station, client)
    assert client.prompts == ["blister strip", "blister pack"]  # stops at first prompt with tracks
    assert [(v.strip_id, v.status) for v in out] == [(1, "PASS"), (2, "HOLD")]
    assert station.crops[0].shape[:2] == (58, 46)       # +8 % pad, clipped to frame
    assert "by hand" in out[1].reasons[0]["text"]
    assert station.logged == out  # the per-strip HOLD is logged too
    client.prompts = []
    assert [v.status for v in inspect_clip(path, FakeStation(), client, held=True)] == ["HOLD", "HOLD"]


def test_low_confidence_track_is_held(tmp_path):
    path = tmp_path / "c.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, SIZE)
    for _ in range(3):
        vw.write(np.zeros((SIZE[1], SIZE[0], 3), np.uint8))
    vw.release()
    client = FakeClient([SamTrackPoint(4, 1, (10, 10, 50, 60), 0.3)])
    client.prompts = ["x"]  # next call is the second prompt and returns the points
    (v,) = inspect_clip(path, FakeStation(), client)
    assert v.status == "HOLD" and v.strip_id == 4 and "by hand" in v.reasons[0]["text"]


def _clip(tmp_path):
    path = tmp_path / "e.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, SIZE)
    vw.write(np.zeros((SIZE[1], SIZE[0], 3), np.uint8))
    vw.release()
    return path


def test_api_error_gives_single_hold(tmp_path):
    from d2i.meta_api import BudgetExceeded, MetaError

    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def sam_video(self, mp4, prompt):
            raise self.exc

    for exc in (MetaError("http 503"), BudgetExceeded("limit")):
        station = FakeStation()
        (v,) = inspect_clip(_clip(tmp_path), station, Boom(exc))
        text = v.reasons[0]["text"]
        assert v.status == "HOLD" and "tray clip not checked (" in text and "check by hand" in text
        assert station.logged == [v]  # the clip-level HOLD is logged


def test_no_tracks_for_any_prompt_gives_one_hold(tmp_path):
    client = FakeClient([])
    (v,) = inspect_clip(_clip(tmp_path), FakeStation(), client)
    assert client.prompts == ["blister strip", "blister pack", "pill pack"]
    assert v.status == "HOLD" and "by hand" in v.reasons[0]["text"]


def _held(pts, tmp_path, frames=3):
    path = tmp_path / "f.mp4"
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, SIZE)
    for _ in range(frames):
        vw.write(np.zeros((SIZE[1], SIZE[0], 3), np.uint8))
    vw.release()
    client = FakeClient(pts)
    client.prompts = ["x"]
    return inspect_clip(path, FakeStation(), client)


def test_unreadable_frame_is_held_per_strip(tmp_path):
    out = _held([SamTrackPoint(1, 40, (10, 10, 50, 60), 0.9)], tmp_path)  # frame 40 not in a 3-frame clip
    assert [(v.strip_id, v.status) for v in out] == [(1, "HOLD")] and "by hand" in out[0].reasons[0]["text"]


def test_empty_crop_is_held(tmp_path):
    out = _held([SamTrackPoint(1, 1, (10, 10, 10, 10), 0.9)], tmp_path)  # zero-area box
    assert out[0].status == "HOLD" and "by hand" in out[0].reasons[0]["text"]


def test_missing_clip_file_is_held(tmp_path):
    (v,) = inspect_clip(tmp_path / "nope.mp4", FakeStation(), FakeClient([]))
    assert v.status == "HOLD" and "tray clip not checked (" in v.reasons[0]["text"]
