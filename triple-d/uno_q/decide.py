"""decide.py - DECIDE stage (peaceful vs hostile).

Three signals are fused into one verdict:
  1. acoustic confidence   (from detect.AcousticDetector)
  2. vision: drone present (VisionClassifier -> Roboflow hosted inference)
  3. closing behaviour     (ClosingTracker: distance falling + amplitude rising)

Nothing here fires an effect. It only RECOMMENDS. The human (operator.py)
authorizes before anything in defeat.py runs.
"""
import base64
from collections import deque
from dataclasses import dataclass, field
import time
import config

try:
    import cv2
    _HAVE_CV = True
except ImportError:
    _HAVE_CV = False

try:
    import requests
    _HAVE_REQUESTS = True
except ImportError:
    _HAVE_REQUESTS = False


@dataclass
class Verdict:
    hostile: bool
    score: float
    payload: str
    reasons: list = field(default_factory=list)


class ClosingTracker:
    """Tracks distance + amplitude history to detect a terminal dive."""
    def __init__(self):
        self.hist = deque()  # (t, dist, amp)

    def update(self, telem: dict):
        if telem is None:
            return
        now = time.time()
        self.hist.append((now, telem.get("dist", -1), telem.get("amp", 0)))
        cutoff = now - config.CLOSING_WINDOW_S
        while self.hist and self.hist[0][0] < cutoff:
            self.hist.popleft()

    def is_closing(self) -> bool:
        valid = [(d, a) for _, d, a in self.hist if d >= 0]
        if len(valid) < 3:
            return False
        first_d, _ = valid[0]
        last_d,  _ = valid[-1]
        amps = [a for _, a in valid]
        dist_falling = (first_d - last_d) >= config.CLOSING_DROP_CM
        amp_rising   = amps[-1] > amps[0]
        return dist_falling and amp_rising


class VisionClassifier:
    """Logitech webcam -> Roboflow drone detector.

    Uses the hosted inference API for ahmedmohsen/drone-detection-new-peksv v3.
    Returns ('drone', max_conf) when any detection clears the confidence
    threshold, ('no_drone', 0.0) when the frame is clean, or ('unknown', 0.0)
    when we can't get an answer (no key, no camera, network failure).
    Falls back to MOCK when configured or when prerequisites are missing.
    """
    def __init__(self):
        can_run = (_HAVE_CV and _HAVE_REQUESTS
                   and bool(config.ROBOFLOW_API_KEY))
        self.mock = config.MOCK_VISION or not can_run
        self.cap = None
        self._endpoint = (
            f"{config.ROBOFLOW_URL}/{config.ROBOFLOW_MODEL}/"
            f"{config.ROBOFLOW_VERSION}"
        )
        if self.mock:
            reason = "config" if config.MOCK_VISION else "missing deps/api key"
            print(f"[decide] vision in MOCK mode ({reason}) "
                  f"-> '{config.MOCK_VISION_LABEL}'")
            return
        try:
            self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
            if not self.cap.isOpened():
                raise RuntimeError(f"camera {config.CAMERA_INDEX} not available")
            print(f"[decide] vision online -> Roboflow "
                  f"{config.ROBOFLOW_MODEL} v{config.ROBOFLOW_VERSION}")
        except Exception as e:                       # noqa: BLE001
            print(f"[decide] vision init failed ({e}); falling back to MOCK")
            self.mock = True

    def classify(self):
        """Return (label, confidence)."""
        if self.mock:
            return config.MOCK_VISION_LABEL, 0.80
        ok, frame = self.cap.read()
        if not ok:
            return "unknown", 0.0
        size = config.VISION_INPUT_SIZE
        img = cv2.resize(frame, (size, size))
        ok, buf = cv2.imencode(".jpg", img)
        if not ok:
            return "unknown", 0.0
        payload = base64.b64encode(buf.tobytes())
        try:
            resp = requests.post(
                self._endpoint,
                params={"api_key": config.ROBOFLOW_API_KEY},
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=config.ROBOFLOW_TIMEOUT_S,
            )
            resp.raise_for_status()
            preds = resp.json().get("predictions", [])
        except Exception as e:                       # noqa: BLE001
            print(f"[decide] roboflow call failed: {e}")
            return "unknown", 0.0
        if not preds:
            return "no_drone", 0.0
        top = max(float(p.get("confidence", 0.0)) for p in preds)
        if top < config.DRONE_CONF_THRESHOLD:
            return "no_drone", top
        return "drone", top

    def release(self):
        if self.cap is not None:
            self.cap.release()


def assess(acoustic_conf: float, payload: str, payload_conf: float,
           closing: bool) -> Verdict:
    """Weighted fusion -> Verdict. Tune the weights to taste."""
    reasons = []
    score = 0.0

    score += 0.30 * acoustic_conf
    if acoustic_conf > 0:
        reasons.append(f"acoustic contact (conf {acoustic_conf:.2f})")

    if payload == "munition":
        score += 0.40 * payload_conf
        reasons.append(f"payload looks like munition (conf {payload_conf:.2f})")
    elif payload == "package":
        score -= 0.20 * payload_conf
        reasons.append(f"payload looks like supplies (conf {payload_conf:.2f})")
    else:
        reasons.append("payload unclassified")

    if closing:
        score += 0.30
        reasons.append("closing fast (terminal-dive signature)")

    score = max(0.0, min(1.0, score))
    return Verdict(
        hostile=score >= config.SCORE_HOSTILE,
        score=round(score, 2),
        payload=payload,
        reasons=reasons,
    )
