"""drone_detect_test.py - standalone sanity tool.

Pure smoke test for the YOLOv8 ONNX drone detector. No coupling to detect.py,
decide.py, or main.py — this just proves the model + webcam pipeline works
before we wire it into the DECIDE stage.

Run:
    python drone_detect_test.py
Keys:
    q   quit
    s   save current frame to /tmp/drone_detect_<ts>.jpg

Tunables live at the top. Defaults match the trained model
(yolov8n on drone-detection-new-peksv v5, 3 classes).
"""
import os
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort


MODEL_PATH    = os.path.join(os.path.dirname(__file__),
                             "models", "drone_detector.onnx")
CLASS_NAMES   = ["AirPlane", "Drone", "Helicopter"]
INPUT_SIZE    = 416
CONF_THRESH   = 0.60
NMS_IOU       = 0.45
CAMERA_INDEX  = 0
# BGR box colors per class
COLORS = {
    "AirPlane":   (255, 200,   0),   # sky blue-ish
    "Drone":      (  0,   0, 255),   # red — the one we care about
    "Helicopter": (  0, 200, 255),   # amber
}


def preprocess(frame: np.ndarray, size: int):
    """BGR uint8 frame -> NCHW float32 [0,1]. Letterbox to preserve aspect."""
    h, w = frame.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = cv2.resize(frame, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    top = (size - nh) // 2
    left = (size - nw) // 2
    canvas[top:top + nh, left:left + nw] = resized
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    tensor = np.transpose(rgb, (2, 0, 1))[None, ...]
    return tensor, scale, left, top


def postprocess(output: np.ndarray, scale: float, pad_x: int, pad_y: int,
                conf_thresh: float, iou_thresh: float):
    """Decode a YOLOv8 ONNX output: (1, 4+nc, N) -> [(box_xyxy, conf, cls), ...]"""
    arr = np.squeeze(output, axis=0)
    if arr.shape[0] < arr.shape[1]:
        arr = arr.T                                  # -> (N, 4+nc)
    boxes_xywh = arr[:, :4]
    class_scores = arr[:, 4:]
    confs = class_scores.max(axis=1)
    cls_ids = class_scores.argmax(axis=1)

    mask = confs >= conf_thresh
    if not mask.any():
        return []
    boxes_xywh = boxes_xywh[mask]
    confs = confs[mask]
    cls_ids = cls_ids[mask]

    # xywh (center, in 640-input pixel space) -> xyxy in ORIGINAL frame
    xy = boxes_xywh[:, :2]
    wh = boxes_xywh[:, 2:]
    xyxy = np.concatenate([xy - wh / 2, xy + wh / 2], axis=1)
    xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / scale
    xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / scale

    # NMSBoxes wants [x, y, w, h] of original-image coords
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
        (xyxy[i].astype(int).tolist(), float(confs[i]), int(cls_ids[i]))
        for i in keep
    ]


def draw(frame, dets):
    for box, conf, cls_id in dets:
        name = CLASS_NAMES[cls_id] if cls_id < len(CLASS_NAMES) else str(cls_id)
        color = COLORS.get(name, (0, 255, 0))
        x1, y1, x2, y2 = box
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f"{name} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
        cv2.putText(frame, label, (x1 + 2, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)


def main():
    if not os.path.exists(MODEL_PATH):
        sys.exit(f"[err] no model at {MODEL_PATH}\n"
                 f"      train first: see models/README.md")
    print(f"[init] loading {MODEL_PATH}")
    session = ort.InferenceSession(MODEL_PATH,
                                   providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name

    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        sys.exit(f"[err] camera {CAMERA_INDEX} did not open")
    print("[init] webcam open. press q to quit, s to save frame.")

    last_t = time.time()
    fps = 0.0
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[warn] frame grab failed")
            break

        tensor, scale, pad_x, pad_y = preprocess(frame, INPUT_SIZE)
        outputs = session.run(None, {input_name: tensor})
        dets = postprocess(outputs[0], scale, pad_x, pad_y,
                           CONF_THRESH, NMS_IOU)
        draw(frame, dets)

        now = time.time()
        fps = 0.9 * fps + 0.1 * (1.0 / max(1e-6, now - last_t))
        last_t = now
        cv2.putText(frame, f"{fps:4.1f} FPS  | {len(dets)} det",
                    (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (255, 255, 255), 2)

        cv2.imshow("drone_detect_test", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            path = f"/tmp/drone_detect_{int(now)}.jpg"
            cv2.imwrite(path, frame)
            print(f"[save] {path}")

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
