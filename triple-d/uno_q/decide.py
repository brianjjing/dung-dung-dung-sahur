"""decide.py - DECIDE stage (peaceful vs hostile).

Three signals are fused into one verdict:
  1. acoustic confidence   (from detect.AcousticDetector)
  2. vision: drone present (VisionClassifier -> on-device YOLOv8 ONNX)
  3. closing behaviour     (ClosingTracker: distance falling + amplitude rising)

Nothing here fires an effect. It only RECOMMENDS. The human (operator.py)
authorizes before anything in defeat.py runs.
"""
import os
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
    import onnxruntime as ort
    _HAVE_ORT = True
except ImportError:
    _HAVE_ORT = False


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
    """Logitech webcam -> on-device YOLOv8 ONNX drone detector.

    Runs 100% offline. Returns:
      ('drone',    smoothed_conf) — drone seen in >= VOTE_MIN_HITS of last
                                    VOTE_WINDOW frames (kills 1-frame FPs)
      ('no_drone', best_conf)     — frame is clean (or only non-hostile classes)
      ('unknown',  0.0)           — camera/model unavailable, fusion will ignore

    Falls back to MOCK when configured or when prerequisites are missing.
    Exposes .last_detections for the demo overlay / debug print.
    """
    def __init__(self):
        self.mock = (config.MOCK_VISION
                     or not (_HAVE_CV and _HAVE_ORT))
        self.cap = None
        self.session = None
        self._input_name = None
        self.last_detections = []          # [{box, conf, cls}, ...]
        self._vote_hist = deque(maxlen=config.VISION_VOTE_WINDOW)  # (hit, conf)

        if self.mock:
            reason = "config" if config.MOCK_VISION else "missing deps"
            print(f"[decide] vision in MOCK mode ({reason}) "
                  f"-> '{config.MOCK_VISION_LABEL}'")
            return

        model_path = config.ONNX_MODEL_PATH
        if not os.path.isabs(model_path):
            model_path = os.path.join(os.path.dirname(__file__), model_path)
        if not os.path.exists(model_path):
            print(f"[decide] no ONNX at {model_path}; falling back to MOCK "
                  f"(train+export first)")
            self.mock = True
            return

        try:
            self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
            if not self.cap.isOpened():
                raise RuntimeError(f"camera {config.CAMERA_INDEX} not available")
            self.session = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            self._input_name = self.session.get_inputs()[0].name
            print(f"[decide] vision online -> {os.path.basename(model_path)} "
                  f"(offline, classes={config.VISION_CLASS_NAMES})")
        except Exception as e:                       # noqa: BLE001
            print(f"[decide] vision init failed ({e}); falling back to MOCK")
            self.mock = True

    def classify(self):
        """Return (label, confidence). label in {'drone','no_drone','unknown'}."""
        if self.mock:
            return config.MOCK_VISION_LABEL, 0.80
        ok, frame = self.cap.read()
        if not ok:
            return "unknown", 0.0

        size = config.VISION_INPUT_SIZE
        img = cv2.resize(frame, (size, size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = np.transpose(img, (2, 0, 1))[None, ...]

        try:
            outputs = self.session.run(None, {self._input_name: tensor})
        except Exception as e:                       # noqa: BLE001
            print(f"[decide] onnx run failed: {e}")
            return "unknown", 0.0

        dets = _postprocess_yolo(
            outputs[0],
            conf_thresh=config.DRONE_CONF_THRESHOLD,
            iou_thresh=config.DRONE_NMS_IOU,
        )
        self.last_detections = dets

        # Only the HOSTILE_CLASS counts as a positive signal. Other classes
        # (AirPlane, Helicopter) are visible exonerations -> no_drone.
        hostile = [d for d in dets
                   if config.VISION_CLASS_NAMES[d["cls"]] == config.HOSTILE_CLASS]
        frame_hit_conf = max((d["conf"] for d in hostile), default=0.0)
        self._vote_hist.append((bool(hostile), frame_hit_conf))

        hits = sum(1 for h, _ in self._vote_hist if h)
        if hits >= config.VISION_VOTE_MIN_HITS:
            # smoothed confidence = mean conf of the hits in the window
            avg = sum(c for h, c in self._vote_hist if h) / hits
            return "drone", avg
        if dets:
            return "no_drone", max(d["conf"] for d in dets)
        return "no_drone", 0.0

    def release(self):
        if self.cap is not None:
            self.cap.release()


def _postprocess_yolo(output, conf_thresh: float, iou_thresh: float):
    """YOLOv8 ONNX output decoder for the multi-class drone detector.

    Accepts (1, 4+C, N) or (1, N, 4+C). Returns
    [{box:[x1,y1,x2,y2], conf:float, cls:int}, ...] after conf filter + NMS.
    """
    arr = np.squeeze(output, axis=0)
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T                          # -> (N, 4+C)
    xywh = arr[:, :4]
    class_scores = arr[:, 4:]
    confs = class_scores.max(axis=1)
    cls_ids = class_scores.argmax(axis=1)

    mask = confs >= conf_thresh
    if not mask.any():
        return []
    xywh = xywh[mask]; confs = confs[mask]; cls_ids = cls_ids[mask]

    xy = xywh[:, :2]; wh = xywh[:, 2:]
    xyxy = np.concatenate([xy - wh / 2, xy + wh / 2], axis=1)

    nms_in = np.stack([
        xyxy[:, 0], xyxy[:, 1],
        xyxy[:, 2] - xyxy[:, 0], xyxy[:, 3] - xyxy[:, 1],
    ], axis=1)
    keep = cv2.dnn.NMSBoxes(nms_in.tolist(), confs.tolist(),
                            conf_thresh, iou_thresh)
    if len(keep) == 0:
        return []
    keep = np.array(keep).flatten()
    return [
        {"box": xyxy[i].astype(int).tolist(),
         "conf": float(confs[i]),
         "cls": int(cls_ids[i])}
        for i in keep
    ]


def assess(acoustic_conf: float, vision_label: str, vision_conf: float,
           closing: bool) -> Verdict:
    """Weighted fusion -> Verdict. Tune the weights to taste.

    vision_label is the on-device drone-detection result:
      'drone'    -> visual confirmation, strong positive weight
      'no_drone' -> frame is clean (or only AirPlane/Helicopter), small neg
      'unknown'  -> vision channel unavailable, ignored
    """
    reasons = []
    score = 0.0

    score += 0.30 * acoustic_conf
    if acoustic_conf > 0:
        reasons.append(f"acoustic contact (conf {acoustic_conf:.2f})")

    if vision_label == "drone":
        score += 0.40 * vision_conf
        reasons.append(f"vision: drone confirmed across frames "
                       f"(avg conf {vision_conf:.2f})")
    elif vision_label == "no_drone":
        # only a soft negative — vision is one signal, not a gate
        score -= 0.10
        if vision_conf > 0:
            reasons.append(f"vision: non-hostile object seen "
                           f"(conf {vision_conf:.2f})")
        else:
            reasons.append("vision: frame clean")
    else:
        reasons.append("vision: channel unavailable")

    if closing:
        score += 0.30
        reasons.append("closing fast (terminal-dive signature)")

    score = max(0.0, min(1.0, score))
    return Verdict(
        hostile=score >= config.SCORE_HOSTILE,
        score=round(score, 2),
        payload=vision_label,
        reasons=reasons,
    )
