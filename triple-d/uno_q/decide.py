"""decide.py - DECIDE stage (peaceful vs hostile).

Three signals are fused into one verdict:
  1. acoustic confidence   (from detect.AcousticDetector)
  2. payload class         (VisionClassifier: 'package' vs 'munition')
  3. closing behaviour     (ClosingTracker: distance falling + amplitude rising)

Nothing here fires an effect. It only RECOMMENDS. The human (operator.py)
authorizes before anything in defeat.py runs.
"""
from collections import deque
from dataclasses import dataclass, field
import time
import config

try:
    import cv2
    import numpy as np
    _HAVE_CV = True
except ImportError:
    _HAVE_CV = False

try:
    import tflite_runtime.interpreter as tflite
    _HAVE_TFLITE = True
except ImportError:
    try:
        from tensorflow.lite.python.interpreter import Interpreter as _TFInterp
        tflite = None
        _HAVE_TFLITE = True
    except ImportError:
        _HAVE_TFLITE = False


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
    """Logitech webcam -> payload class. Falls back to mock if no model/cam.

    The vision model stays DORMANT until activate() is called. That activation
    is the signal raised by the listening model (DETECT) the instant it judges
    a threat -- only then do we power up the camera and load the classifier.
    While IDLE the webcam stays dark and no inference runs.
    """
    def __init__(self):
        self.mock = config.MOCK_VISION or not (_HAVE_CV and _HAVE_TFLITE)
        self.cap = None
        self.interp = None
        self.active = False
        if self.mock:
            print(f"[decide] vision in MOCK mode -> '{config.MOCK_VISION_LABEL}'")

    def activate(self):
        """Wake the vision model. Called when the listening model signals a
        threat. Idempotent -- repeated signals while already active are no-ops.
        Returns True if the model is ready (live or mock)."""
        if self.active:
            return True
        self.active = True
        print("[decide] >> VISION ACTIVATED (signalled by acoustic threat)")
        if self.mock:
            return True
        try:
            self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
            self.interp = tflite.Interpreter(model_path=config.VISION_MODEL_PATH)
            self.interp.allocate_tensors()
            self._in = self.interp.get_input_details()[0]
            self._out = self.interp.get_output_details()[0]
            print("[decide] vision model loaded")
        except Exception as e:                       # noqa: BLE001
            print(f"[decide] vision init failed ({e}); falling back to MOCK")
            self.mock = True
        return True

    def deactivate(self):
        """Stand the vision model back down (e.g. on cooldown): release the
        camera so the webcam goes dark again until the next threat signal."""
        if not self.active:
            return
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.active = False
        print("[decide] vision stood down")

    def classify(self):
        """Return (label, confidence). Dormant until activated."""
        if not self.active:
            return "unknown", 0.0
        if self.mock:
            return config.MOCK_VISION_LABEL, 0.80
        ok, frame = self.cap.read()
        if not ok:
            return "unknown", 0.0
        size = config.VISION_INPUT_SIZE
        img = cv2.resize(frame, (size, size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype("float32") / 255.0
        img = np.expand_dims(img, 0)
        self.interp.set_tensor(self._in["index"], img)
        self.interp.invoke()
        probs = self.interp.get_tensor(self._out["index"])[0]
        idx = int(probs.argmax())
        return config.VISION_LABELS[idx], float(probs[idx])

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
