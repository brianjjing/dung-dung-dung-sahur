"""decide.py - DECIDE stage (peaceful vs hostile).

Three signals are fused into one verdict:
  1. acoustic confidence   (from detect.AcousticDetector)
  2. vision: drone present (VisionClassifier -> on-device YOLOv8 ONNX)
  3. closing behaviour     (ClosingTracker: distance falling + amplitude rising)

Nothing here fires an effect. It only RECOMMENDS. The human (operator.py)
authorizes before anything in defeat.py runs.
"""
import math
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


@dataclass
class AimSolution:
    """Where the laser should cut the trailing fiber-optic tether.

    available  : True once enough track exists to fit a heading
    target_xy  : last known drone centroid (image px)
    heading_deg: drone flight direction (atan2 over the recent track)
    cutoff_xy  : point BEHIND the drone where the tether runs -> aim here
    sweep_deg  : laser sweep axis, PERPENDICULAR to the flight path
    """
    available: bool
    target_xy: tuple
    heading_deg: float
    cutoff_xy: tuple
    sweep_deg: float
    note: str = ""


class TrajectoryTracker:
    """Records the drone's image-plane centroid over recent frames and derives a
    laser aim solution: a cut point BEHIND the drone, swept PERPENDICULAR to its
    flight path -- where the trailing fiber-optic tether runs.

    Pure geometry on the vision boxes; the actual laser drive is a placeholder in
    defeat.py (real hardware lives elsewhere)."""
    def __init__(self):
        self.pts = deque(maxlen=config.TRAJ_WINDOW)   # (t, cx, cy)

    def update(self, detections):
        """Feed the current frame's detections; tracks the highest-conf box."""
        if not detections:
            return
        best = max(detections, key=lambda d: d["conf"])
        x1, y1, x2, y2 = best["box"]
        self.pts.append((time.time(), (x1 + x2) / 2.0, (y1 + y2) / 2.0))

    def reset(self):
        self.pts.clear()

    def aim(self) -> AimSolution:
        """Best laser aim solution from the track so far.

        The drone is assumed to fly roughly straight, so the GENERAL DIRECTION is
        the principal axis of all the tracked centroids (a least-squares line fit),
        which averages out per-frame detection jitter far better than a single
        first-vs-last segment. The axis is then oriented along the net start->end
        travel so it points the way the drone is actually heading."""
        if len(self.pts) < 2:
            return AimSolution(False, (0.0, 0.0), 0.0, (0.0, 0.0), 0.0,
                               "insufficient track -- placeholder aim")
        xs = [p[1] for p in self.pts]
        ys = [p[2] for p in self.pts]
        x1, y1 = xs[-1], ys[-1]                 # current (latest) position
        dx_net, dy_net = xs[-1] - xs[0], ys[-1] - ys[0]
        if math.hypot(dx_net, dy_net) < 1e-6:
            return AimSolution(False, (x1, y1), 0.0, (x1, y1), 0.0,
                               "drone effectively stationary -- placeholder aim")

        # Principal axis of the tracked points = general flight direction.
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        syy = sum((y - my) ** 2 for y in ys)
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        theta = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
        ux, uy = math.cos(theta), math.sin(theta)
        if ux * dx_net + uy * dy_net < 0:       # orient along net travel
            ux, uy = -ux, -uy

        heading = math.degrees(math.atan2(uy, ux))
        back = config.LASER_CUTOFF_BACK_PX
        cutoff = (x1 - ux * back, y1 - uy * back)   # point BEHIND the drone
        sweep = (heading + 90.0) % 180.0            # PERPENDICULAR to flight path
        return AimSolution(True, (x1, y1), heading, cutoff, sweep, "tracked")


class VisionClassifier:
    """Logitech webcam -> payload class. Falls back to mock if no model/cam.

    The vision model stays DORMANT until activate() is called. That activation
    is the signal raised by the listening model (DETECT) the instant it judges
    a threat -- only then do we power up the camera and load the classifier.
    While IDLE the webcam stays dark and no inference runs.
    """
    def __init__(self):
        self.mock = (config.MOCK_VISION
                     or not (_HAVE_CV and _HAVE_ORT))
        self.cap = None
        self.session = None
        self._input_name = None
        self.last_detections = []          # [{box, conf, cls}, ...]
        self.last_frame = None             # most recent raw BGR frame (for UI cam)
        self._vote_hist = deque(maxlen=config.VISION_VOTE_WINDOW)  # (hit, conf)

        self.active = False
        if self.mock:
            reason = "config" if config.MOCK_VISION else "missing deps"
            print(f"[decide] vision in MOCK mode ({reason}) "
                  f"-> '{config.MOCK_VISION_LABEL}'")
            return

        self.model_path = config.ONNX_MODEL_PATH
        if not os.path.isabs(self.model_path):
            self.model_path = os.path.join(os.path.dirname(__file__),
                                           self.model_path)
        if not os.path.exists(self.model_path):
            print(f"[decide] no ONNX at {self.model_path}; falling back to MOCK "
                  f"(train+export first)")
            self.mock = True

    def activate(self):
        """Wake the vision model. Called when the listening model signals a
        threat. Idempotent -- repeated signals while already active are no-ops.
        Returns True if the model is ready (live or mock)."""
        if self.active:
            return True
        self.active = True
        self._vote_hist.clear()          # fresh ring buffer per threat window
        print("[decide] >> VISION ACTIVATED (signalled by acoustic threat)")
        if self.mock:
            return True

        try:
            self.cap = cv2.VideoCapture(config.CAMERA_INDEX)
            if not self.cap.isOpened():
                raise RuntimeError(f"camera {config.CAMERA_INDEX} not available")
            self.session = ort.InferenceSession(
                self.model_path, providers=["CPUExecutionProvider"]
            )
            self._input_name = self.session.get_inputs()[0].name
            print(f"[decide] vision online -> {os.path.basename(self.model_path)} "
                  f"(offline, classes={config.VISION_CLASS_NAMES})")
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
        self.last_frame = None             # camera window goes dark in the UI
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
        self.last_frame = frame            # latest live frame for the UI cam window

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

    def frame_jpeg(self, max_w: int = 400):
        """JPEG bytes of the latest live frame for the UI camera window, or None
        when no real frame is available (mock mode / camera dark). Called from the
        dashboard's HTTP thread, so encoding never blocks the control loop."""
        frame = self.last_frame
        if frame is None or self.mock or not _HAVE_CV:
            return None
        h, w = frame.shape[:2]
        if w > max_w:
            frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None


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
