"""detect_line.py — DETECT stage for FiberTrace.

Turns a camera frame into a single LineEstimate describing the fiber tether
(or fishing-line stand-in) trailing from the drone:

    preprocess (gray -> CLAHE -> blur -> optional ROI mask)
      -> Canny edges
      -> HoughLinesP
      -> keep near-vertical segments (the fiber from a drone down to the
         operator looks roughly vertical; near-horizontal clutter is rejected)
      -> aggregate into one estimate: center_x at a reference row, signed tilt
         (negative = leans left, positive = leans right), length, count.

ConfidenceTracker adds temporal smoothing across frames so a one-frame glitch
doesn't yank the operator prediction.

UPGRADE PATH: swap LineDetector.detect() for a trained thin-line segmentation
model; keep the LineEstimate interface and nothing downstream changes.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import config

try:
    import cv2
    import numpy as np
    _HAVE_CV = True
except ImportError:  # pragma: no cover
    _HAVE_CV = False


@dataclass
class LineEstimate:
    """One frame's read on the fiber (or a smoothed read from the tracker)."""
    detected: bool = False
    center_x: float = 0.0       # px column of the line at the reference row
    angle_deg: float = 0.0      # signed tilt from vertical; <0 left, >0 right
    length: float = 0.0         # representative segment length (px)
    num_lines: int = 0          # supporting Hough segments this frame
    confidence: float = 0.0     # 0..1
    raw_lines: list = field(default_factory=list)  # kept segments, for overlay


def _x_at_y(x1, y1, x2, y2, y_ref):
    """Column where the segment crosses row y_ref (clamped to the segment)."""
    if y1 == y2:
        return (x1 + x2) / 2.0
    t = (y_ref - y1) / (y2 - y1)
    t = max(0.0, min(1.0, t))
    return x1 + t * (x2 - x1)


def _signed_tilt(x1, y1, x2, y2):
    """Degrees from vertical; negative = leans left, positive = leans right."""
    if y1 <= y2:
        x_top, y_top, x_bot, y_bot = x1, y1, x2, y2
    else:
        x_top, y_top, x_bot, y_bot = x2, y2, x1, y1
    dx_up = x_top - x_bot
    dy = max(1, y_bot - y_top)
    return math.degrees(math.atan2(dx_up, dy))


class LineDetector:
    """Classic OpenCV line detector (no model needed)."""

    def __init__(self):
        if not _HAVE_CV:
            raise RuntimeError("FiberTrace detect needs OpenCV + NumPy "
                               "(pip install -r fibertrace/requirements.txt)")
        self._clahe = cv2.createCLAHE(clipLimit=config.FT_CLAHE_CLIP,
                                      tileGridSize=(8, 8))

    def preprocess(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = self._clahe.apply(gray)
        k = config.FT_GAUSS_KERNEL | 1   # force odd
        gray = cv2.GaussianBlur(gray, (k, k), 0)
        edges = cv2.Canny(gray, config.FT_CANNY_LO, config.FT_CANNY_HI)
        roi_top = int(edges.shape[0] * config.FT_ROI_TOP_FRAC)
        if roi_top > 0:
            edges[:roi_top, :] = 0       # ignore background above the ROI
        return edges

    def detect(self, frame) -> LineEstimate:
        h = frame.shape[0]
        edges = self.preprocess(frame)
        lines = cv2.HoughLinesP(
            edges, 1, math.pi / 180,
            threshold=config.FT_HOUGH_THRESHOLD,
            minLineLength=config.FT_HOUGH_MIN_LINE,
            maxLineGap=config.FT_HOUGH_MAX_GAP,
        )
        if lines is None:
            return LineEstimate()

        y_ref = int(h * 0.80)
        kept, xs, tilts, lens, wts = [], [], [], [], []
        for ln in lines[:, 0, :]:
            x1, y1, x2, y2 = (int(v) for v in ln)
            tilt = _signed_tilt(x1, y1, x2, y2)
            if abs(tilt) > config.FT_MAX_TILT_DEG:
                continue                 # reject near-horizontal clutter
            length = math.hypot(x2 - x1, y2 - y1)
            kept.append((x1, y1, x2, y2))
            xs.append(_x_at_y(x1, y1, x2, y2, y_ref))
            tilts.append(tilt)
            lens.append(length)
            wts.append(length)

        if not kept:
            return LineEstimate()

        wsum = sum(wts) or 1.0
        center_x = sum(w * x for w, x in zip(wts, xs)) / wsum
        angle = sum(w * a for w, a in zip(wts, tilts)) / wsum
        mean_len = sum(lens) / len(lens)

        norm_len = min(1.0, mean_len / (0.55 * h))
        num_term = min(1.0, len(kept) / 3.0)
        conf = max(0.0, min(1.0, 0.60 * norm_len + 0.40 * num_term))

        return LineEstimate(
            detected=True,
            center_x=center_x,
            angle_deg=angle,
            length=mean_len,
            num_lines=len(kept),
            confidence=conf,
            raw_lines=kept,
        )


class ConfidenceTracker:
    """Temporal smoothing over the last FT_SMOOTH_WINDOW frames."""

    def __init__(self):
        self._conf = deque(maxlen=config.FT_SMOOTH_WINDOW)
        self._cx = deque(maxlen=config.FT_SMOOTH_WINDOW)
        self._ang = deque(maxlen=config.FT_SMOOTH_WINDOW)
        self._last_cx = config.FT_FRAME_W / 2.0
        self._last_ang = 0.0

    def update(self, est: LineEstimate) -> LineEstimate:
        self._conf.append(est.confidence if est.detected else 0.0)
        if est.detected:
            self._cx.append(est.center_x)
            self._ang.append(est.angle_deg)
            self._last_cx = est.center_x
            self._last_ang = est.angle_deg

        conf_s = sum(self._conf) / len(self._conf)
        cx_s = (sum(self._cx) / len(self._cx)) if self._cx else self._last_cx
        ang_s = (sum(self._ang) / len(self._ang)) if self._ang else self._last_ang
        detected = conf_s >= config.FT_MIN_CONFIDENCE

        return LineEstimate(
            detected=detected,
            center_x=cx_s,
            angle_deg=ang_s,
            length=est.length,
            num_lines=est.num_lines,
            confidence=conf_s,
            raw_lines=est.raw_lines,
        )
