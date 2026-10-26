"""Offline, frozen DINOv2 judge: scores each pocket and the strip border against good strips.

Each patch is compared only with the same spot (and its 8 neighbours) on the good examples,
so a missing tablet does not look "normal" just because plain foil exists elsewhere.
"""
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
POCKET_SIZE, BORDER_SIZE = 224, 448
WORST = 0.02     # score = mean of the worst 2% of patch distances
MARGIN = 1.3     # threshold = worst leave-one-photo-out score x 1.3
BLANK = 128      # grey used to hide pockets when judging the border


def _load_model(name, device):
    from transformers import AutoModel
    try:
        model = AutoModel.from_pretrained(name, local_files_only=True)
    except OSError:
        model = AutoModel.from_pretrained(name)
    return model.to(device).eval()


def _crop(image, box, size):
    x0, y0, x1, y1 = (int(v) for v in box)
    h, w = image.shape[:2]
    crop = image[max(y0, 0):min(y1, h), max(x0, 0):min(x1, w)]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)


def _border_crop(image, layout):
    img = image.copy()
    for x0, y0, x1, y1 in layout.pockets:
        img[int(y0):int(y1), int(x0):int(x1)] = BLANK
    return _crop(img, layout.strip_box, BORDER_SIZE)


def _distances(query, bank):
    """query [H, W, D], bank [N, H, W, D] (unit vectors) -> [H, W] distance to the nearest
    bank patch at the same spot or one patch away."""
    best = None
    padded = F.pad(bank.permute(0, 3, 1, 2), (1, 1, 1, 1), mode="replicate").permute(0, 2, 3, 1)
    h, w = query.shape[:2]
    for dy in range(3):
        for dx in range(3):
            sim = torch.einsum("hwd,nhwd->nhw", query, padded[:, dy:dy + h, dx:dx + w]).amax(0)
            best = sim if best is None else torch.maximum(best, sim)
    return 1.0 - best


def _score(dist):
    flat = dist.flatten()
    k = max(1, int(round(flat.numel() * WORST)))
    return float(flat.topk(k).values.mean())


class Judge:
    def __init__(self, model: str = "facebook/dinov2-small", device: str | None = None):
        self.name = model
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = _load_model(model, self.device)
        self.dim = self.model.config.hidden_size
        self.pocket_bank = self.border_bank = None
        self.pocket_threshold = self.border_threshold = None

    @torch.no_grad()
    def _patches(self, crops):
        """BGR crops (same size) -> [N, H, W, D] unit patch features."""
        out = []
        for i in range(0, len(crops), 16):
            batch = np.stack([(c[:, :, ::-1].astype(np.float32) / 255 - MEAN) / STD for c in crops[i:i + 16]])
            x = torch.from_numpy(batch).permute(0, 3, 1, 2).to(self.device)
            tokens = self.model(pixel_values=x).last_hidden_state[:, 1:, :]
            side = int(tokens.shape[1] ** 0.5)
            out.append(F.normalize(tokens.reshape(len(batch), side, side, -1), dim=-1))
        return torch.cat(out)

    def teach(self, images: list, layouts: list) -> dict:
        if len(images) < 5:
            raise ValueError(f"Teaching needs at least 5 photos of good strips; got {len(images)}.")
        counts = [len(l.pockets) for l in layouts]
        for i, lay in enumerate(layouts, start=1):
            if not lay.ok or len(lay.pockets) != counts[0]:
                raise ValueError(f"photo {i}: pockets not found cleanly ({lay.note or f'{len(lay.pockets)} pockets'}); "
                                 f"retake it with the whole strip in view.")
        n = counts[0]
        pockets = self._patches([_crop(im, b, POCKET_SIZE) for im, l in zip(images, layouts) for b in l.pockets])
        borders = self._patches([_border_crop(im, l) for im, l in zip(images, layouts)])
        photo_of = torch.arange(len(images), device=self.device).repeat_interleave(n)
        worst_pocket = worst_border = 0.0
        for p in range(len(images)):  # leave one photo out
            keep = photo_of != p
            for q in pockets[~keep]:
                worst_pocket = max(worst_pocket, _score(_distances(q, pockets[keep])))
            others = torch.tensor([j for j in range(len(images)) if j != p], device=self.device)
            worst_border = max(worst_border, _score(_distances(borders[p], borders[others])))
        self.pocket_bank, self.border_bank = pockets, borders
        self.pocket_threshold, self.border_threshold = worst_pocket * MARGIN, worst_border * MARGIN
        return {"pocket_threshold": self.pocket_threshold, "border_threshold": self.border_threshold,
                "photos": len(images)}

    def score_pockets(self, image, layout) -> list[float]:
        feats = self._patches([_crop(image, b, POCKET_SIZE) for b in layout.pockets])
        return [_score(_distances(q, self.pocket_bank)) for q in feats]

    def score_border(self, image, layout) -> float:
        return _score(_distances(self._patches([_border_crop(image, layout)])[0], self.border_bank))

    def pocket_embeddings(self, image, layout) -> torch.Tensor:
        """One unit vector per pocket: the mean of its patch features."""
        feats = self._patches([_crop(image, b, POCKET_SIZE) for b in layout.pockets])
        return F.normalize(feats.mean(dim=(1, 2)), dim=-1)

    def save(self, path):
        torch.save({"model": self.name, "pocket_bank": self.pocket_bank.cpu(), "border_bank": self.border_bank.cpu(),
                    "pocket_threshold": self.pocket_threshold, "border_threshold": self.border_threshold}, Path(path))

    @classmethod
    def load(cls, path, device: str | None = None):
        state = torch.load(Path(path), map_location="cpu")
        judge = cls(state["model"], device)
        judge.pocket_bank = state["pocket_bank"].to(judge.device)
        judge.border_bank = state["border_bank"].to(judge.device)
        judge.pocket_threshold, judge.border_threshold = state["pocket_threshold"], state["border_threshold"]
        return judge
