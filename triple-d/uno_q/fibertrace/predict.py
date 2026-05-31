"""predict.py — operator guesstimate for FiberTrace.

Given the detected fiber line in the image, extrapolate it toward the ground
to estimate where the operator is. The drone is up in the frame; the fiber
trails down toward the operator, so projecting the line downward past the
bottom of the frame points at the operator's position on the ground.

Output is honest about its limits: a single camera gives a BEARING (left/right
+ degrees off the camera axis), not a metric fix. The confidence rises as the
line is tracked over more frames. Everything here is pure geometry — no model,
no network, fully offline.
"""
from __future__ import annotations

import math
from collections import deque

import config


def _norm_deg(a: float) -> float:
    return ((a + 180.0) % 360.0) - 180.0


class OperatorPredictor:
    def __init__(self):
        self.w = config.FT_FRAME_W
        self.h = config.FT_FRAME_H
        self._hits = 0
        self._bear = deque(maxlen=max(1, config.FT_SMOOTH_WINDOW))

    def predict(self, est):
        """Return an operator guesstimate dict, or None if not enough signal.

        dict = {ground_x, bearing_deg, side, confidence, ray:[[x,y],[x,y]]}
        """
        if not est.detected:
            self._hits = max(0, self._hits - 1)
            if self._hits == 0:
                self._bear.clear()
            return None

        self._hits = min(30, self._hits + 1)
        if self._hits < config.FT_PREDICT_MIN_HITS:
            return None

        # Reconstruct the line and project it down to (and past) the ground row.
        # detect_line measures center_x at y_ref = 0.80*H; going DOWN one row,
        # x changes by -tan(tilt) (tilt is signed degrees from vertical).
        y_ref = self.h * 0.80
        slope = -math.tan(math.radians(est.angle_deg))   # dx per dy (downward)
        y_ground = float(self.h)
        x_ground = est.center_x + slope * (y_ground - y_ref)

        reach = self.h * 0.18                             # project a bit beyond
        y_beyond = y_ground + reach
        x_beyond = est.center_x + slope * (y_beyond - y_ref)

        # Map horizontal position to a bearing using an assumed camera HFOV.
        norm = (x_ground - self.w / 2.0) / (self.w / 2.0)        # -1..1
        bearing = norm * (config.FT_CAMERA_HFOV_DEG / 2.0)
        self._bear.append(bearing)
        bearing_s = sum(self._bear) / len(self._bear)

        dead = config.FT_OPERATOR_SIDE_DEADBAND_DEG
        side = ("left" if bearing_s < -dead
                else "right" if bearing_s > dead else "ahead")

        persist = min(1.0, self._hits / 12.0)
        conf = max(0.0, min(1.0, 0.55 * est.confidence + 0.45 * persist))

        return {
            "ground_x": round(x_ground, 1),
            "bearing_deg": round(bearing_s, 1),
            "side": side,
            "confidence": round(conf, 2),
            "ray": [[round(est.center_x, 1), round(y_ref, 1)],
                    [round(x_beyond, 1), round(y_beyond, 1)]],
        }
