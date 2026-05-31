"""capture.py — frame source for FiberTrace.

Scenario: the RC car sits on the ground and its camera looks at a fiber-optic
drone in the air. A thin fiber tether trails from the drone down toward its
operator on the ground. FiberTrace detects that fiber and extrapolates it to
guesstimate where the operator is — all offline.

Two modes, chosen by config.FT_MOCK_CAMERA:

  True  -> _SyntheticDroneScene: draws a small drone (drifting) with a bright
           fiber trailing down to an operator point near the ground, and
           scripts a ~13 s scenario:
               0.0–5.5 s  drone in view, fiber clear        (TRACKING)
               5.5–7.0 s  fiber briefly occluded            (SEARCHING)
               7.0–13 s   fiber returns, drone has drifted  (TRACKING)
           The operator sits off to one side, so the extrapolated fiber yields
           a left/right bearing with a confidence that firms up over time.

  False -> real webcam (config.CAMERA_INDEX): the car's actual camera looking
           at a real drone + fishing-line stand-in.

The detector downstream never knows which source it is — identical frames.
"""
from __future__ import annotations

import math
import time

import config

try:
    import cv2
    import numpy as np
    _HAVE_CV = True
except ImportError:  # pragma: no cover - cv2 is required to actually run
    _HAVE_CV = False


class _SyntheticDroneScene:
    """Generates BGR frames of a drone + trailing fiber (see module docstring)."""

    def __init__(self):
        self.t0 = time.time()
        self.w = config.FT_FRAME_W
        self.h = config.FT_FRAME_H
        self.rng = np.random.default_rng(7)

    # -- scenario geometry --------------------------------------------------
    def _drone_xy(self, t: float):
        """Drone position (drifts horizontally; sits in the upper frame)."""
        x = self.w * 0.5 + math.sin(t * 0.45) * self.w * 0.28
        y = self.h * 0.24 + math.sin(t * 0.9) * self.h * 0.04
        return float(x), float(y)

    def _operator_xy(self, t: float):
        """Operator on the ground, off to the right, drifting slightly."""
        x = self.w * 0.73 + math.sin(t * 0.2) * self.w * 0.05
        y = self.h * 0.98
        return float(x), float(y)

    def _fiber_alpha(self, t: float) -> float:
        """1.0 visible, 0.0 occluded (the 5.5–7.0 s dropout)."""
        if t < 5.5 or t >= 7.2:
            return 1.0
        if t < 5.9:
            return 1.0 - (t - 5.5) / 0.4
        if t < 6.8:
            return 0.0
        return (t - 6.8) / 0.4

    # -- drawing ------------------------------------------------------------
    def _background(self):
        # subtle vertical gradient (lighter sky up top, darker ground below)
        col = np.linspace(78, 50, self.h, dtype=np.int16).reshape(self.h, 1, 1)
        img = np.repeat(np.repeat(col, self.w, axis=1), 3, axis=2)
        noise = self.rng.normal(0, 5, (self.h, self.w, 3))
        return np.clip(img + noise, 0, 255).astype(np.uint8)

    def _draw_drone(self, img, dx, dy):
        x, y = int(dx), int(dy)
        cv2.rectangle(img, (x - 22, y - 7), (x + 22, y + 7), (40, 40, 46), -1)
        for ox in (-30, 30):                      # two arms + rotor discs
            cv2.line(img, (x, y), (x + ox, y - 12), (30, 30, 34), 3, cv2.LINE_AA)
            cv2.circle(img, (x + ox, y - 13), 12, (90, 90, 96), 2, cv2.LINE_AA)
        cv2.circle(img, (x, y + 9), 3, (60, 60, 70), -1)   # payload nub

    def _fiber_points(self, dx, dy, ox, oy):
        """A slightly sagging line from the drone down to the operator."""
        pts = []
        steps = 26
        for i in range(steps + 1):
            f = i / steps
            x = dx + (ox - dx) * f
            y = dy + (oy - dy) * f
            sag = math.sin(f * math.pi) * 10.0    # gentle catenary droop
            pts.append((int(x), int(y + sag)))
        return pts

    def frame(self):
        t = time.time() - self.t0
        img = self._background()
        dx, dy = self._drone_xy(t)
        ox, oy = self._operator_xy(t)

        alpha = self._fiber_alpha(t)
        if alpha > 0.05:
            overlay = img.copy()
            pts = self._fiber_points(dx, dy, ox, oy)
            for i in range(len(pts) - 1):
                cv2.line(overlay, pts[i], pts[i + 1], (212, 212, 224), 3, cv2.LINE_AA)
            cv2.addWeighted(overlay, alpha, img, 1 - alpha, 0, img)

        self._draw_drone(img, dx, dy)
        return img


class FrameSource:
    """Unified frame source: synthetic drone scene (mock) or real webcam."""

    def __init__(self):
        if not _HAVE_CV:
            raise RuntimeError(
                "FiberTrace needs OpenCV + NumPy. Install with:\n"
                "    pip install -r fibertrace/requirements.txt")
        self.mock = config.FT_MOCK_CAMERA
        self.cap = None
        if self.mock:
            self._syn = _SyntheticDroneScene()
            print("[capture] MOCK camera — synthetic drone+fiber scene (no webcam)")
        else:
            self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.FT_FRAME_W)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.FT_FRAME_H)
            if not self.cap.isOpened():
                raise RuntimeError(f"cannot open webcam index {config.CAMERA_INDEX}")
            print(f"[capture] webcam {config.CAMERA_INDEX} "
                  f"@ {config.FT_FRAME_W}x{config.FT_FRAME_H}")

    def read(self):
        """Return one BGR frame, or None if the webcam dropped."""
        if self.mock:
            return self._syn.frame()
        ok, frame = self.cap.read()
        if not ok:
            return None
        if frame.shape[1] != config.FT_FRAME_W or frame.shape[0] != config.FT_FRAME_H:
            frame = cv2.resize(frame, (config.FT_FRAME_W, config.FT_FRAME_H))
        return frame

    def release(self):
        if self.cap is not None:
            self.cap.release()
