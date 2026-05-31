"""Central configuration for the Uno Q brain.

Every tunable lives here so you are not hunting through modules at 3am.
Start in MOCK mode (no hardware), prove the pipeline, then flip flags off
one at a time as real hardware/models come online.
"""

# ----------------------------------------------------------------- SERIAL LINK
SERIAL_PORT = "/dev/ttyACM0"   # the UNO R3 (often ttyACM0; sometimes ttyUSB0)
BAUD        = 115200
# MOCK_SERIAL=True synthesizes a scripted threat so you can run the whole
# DETECT->DECIDE->DEFEAT pipeline on a laptop with NO car attached.
MOCK_SERIAL = True

# ---------------------------------------------------------------------- CAMERA
CAMERA_INDEX     = 0           # Logitech webcam on the Uno Q (via the USB hub)
# MOCK_VISION=True skips the camera/model and uses MOCK_VISION_LABEL below.
# False -> run the real drone_detector.onnx on the webcam (auto-falls back to
# MOCK if cv2/onnxruntime/model/camera are missing).
MOCK_VISION = False
MOCK_VISION_LABEL = "drone"             # what the fake classifier "sees"

# ------------------------------------------------------- ON-DEVICE DRONE MODEL
# Local YOLOv8 ONNX detector. Trained from the Roboflow Universe
# ahmedmohsen/drone-detection-new-peksv dataset (3 classes:
# AirPlane, Drone, Helicopter). Runs 100% offline.
ONNX_MODEL_PATH      = "models/drone_detector.onnx"
VISION_INPUT_SIZE    = 416              # must match the imgsz used at training
VISION_VOTE_WINDOW   = 5                # last N classify() calls considered
VISION_VOTE_MIN_HITS = 2                # require this many drone-hits in window
VISION_CLASS_NAMES   = ["drone"]
HOSTILE_CLASS        = "drone"          # which class triggers the hostile signal
DRONE_CONF_THRESHOLD = 0.60             # min per-box confidence to count as a hit
DRONE_NMS_IOU        = 0.45             # NMS overlap threshold

# ------------------------------------------------------------ DETECT THRESHOLDS
# Two DETECT backends:
#   USE_CNN_DETECT=False -> threshold/debounce on the car-mic amp/pitch features
#                           (detect.AcousticDetector). Works in MOCK_SERIAL mode.
#   USE_CNN_DETECT=True  -> Mel-spectrogram CNN on the Uno Q's OWN mic, in
#                           sliding windows (acoustic_cnn.CnnAcousticDetector).
#                           Needs a real mic + torch/librosa/sounddevice.
USE_CNN_DETECT = True

# CNN backend (only used when USE_CNN_DETECT=True)
ACOUSTIC_MODEL_PATH   = "models/drone_acoustic_cnn.pth"
ACOUSTIC_THRESHOLD    = 0.40   # P(threat) to count as signature (lower=more sensitive)
ACOUSTIC_INFER_PERIOD = 0.30   # seconds between sliding-window classifications
# OFFLINE TEST: set to a .wav path to loop a file through the CNN instead of
# opening the mic (no sounddevice/PortAudio needed). "" = use the live mic.
ACOUSTIC_SOURCE_WAV = ""

# Acoustic signature gate (threshold backend; tune against YOUR drone + room).
AMP_FLOOR    = 120             # min peak-to-peak amplitude to count as "loud"
PITCH_BAND   = (1200, 4500)    # Hz window typical of a high prop-whine
DETECT_HOLD  = 0.5             # seconds the signature must persist to fire

# ------------------------------------------------------------ DECIDE PARAMETERS
CLOSING_WINDOW_S    = 2.0      # history length for trend analysis
CLOSING_DROP_CM     = 15       # distance must fall this much to read "closing"
SCORE_HOSTILE       = 0.60     # fused score at/above this => HOSTILE verdict
# After acoustic wakes vision, how long to watch for a drone before giving up
# and standing the camera back down to resume audio listening.
VISION_DECIDE_TIMEOUT_S = 1.5

# ----------------------------------------------------------------- AUTONOMY DIAL
# 0 teleop | 1 single-action assist | 2 detect+recommend, human gates ALL
# 3 human-on-the-loop (acts, human can veto) | 4 auto-DISTRACT only, gate rest
# 5 multi-agent (swarm hand-off)   --- see README "Degrees of autonomy"
AUTONOMY_LEVEL = 2

# --------------------------------------------------------------------- OPERATOR
AUTH_TIMEOUT_S = 8.0           # how long to wait for a human y/n before aborting
USE_JOYSTICK   = False         # True -> Modulino Joystick; False -> keyboard

# ------------------------------------------------------------------- L3 TUNING
# These only apply when running autonomy.py (AUTONOMY_LEVEL = 3).
VETO_WINDOW_S    = 5.0         # seconds the veto window stays open before auto-fire
MIN_VETO_S       = 1.5         # minimum hold before operator early-fire is respected
                               # (prevents accidental fire from a stray Enter keystroke)
AUDIT_LOG_PATH   = "triple_d_audit.jsonl"  # JSONL record of all operator decisions
USE_COLOR        = True        # ANSI colour in the veto-window UI; set False for logs
JOYSTICK_I2C_BUS = 1           # Linux I2C bus number for the Modulino Joystick

# ----------------------------------------------------------------------- TIMING
LOOP_HZ      = 20              # main control-loop rate
COOLDOWN_S   = 4.0            # after a response, how long before re-arming

# ============================================================ FIBERTRACE ("look down")
# Separate capability (run `python -m fibertrace.trace_main` from this folder).
# The RC car sits on the ground; its camera looks at a fiber-optic drone and
# detects the thin fiber trailing from it, then EXTRAPOLATES that line to
# guesstimate where the operator is — fully offline. All FiberTrace tunables
# are FT_-prefixed to avoid clashing with the Triple D names above; it reuses
# CAMERA_INDEX for the live webcam.

# ---- camera ----
FT_MOCK_CAMERA = True          # True -> synthetic drone+fiber scene (no webcam)
FT_FRAME_W     = 640
FT_FRAME_H     = 480
FT_TARGET_FPS  = 15            # main-loop rate cap
FT_CAMERA_HFOV_DEG = 60.0      # assumed horizontal field of view -> bearing math

# ---- vision / fiber detection ----
FT_ROI_TOP_FRAC    = 0.0       # use full frame (the fiber spans drone -> ground)
FT_CLAHE_CLIP      = 2.0       # contrast-limited adaptive histogram equalization
FT_GAUSS_KERNEL    = 5         # blur kernel (odd) to kill speckle before Canny
FT_CANNY_LO        = 50
FT_CANNY_HI        = 150
FT_HOUGH_THRESHOLD = 40        # min votes for a Hough line
FT_HOUGH_MIN_LINE  = 55        # min line length (px)
FT_HOUGH_MAX_GAP   = 40        # max gap to bridge collinear segments (px)
FT_MAX_TILT_DEG    = 55        # keep lines within this many deg of vertical
FT_MIN_CONFIDENCE  = 0.35      # smoothed confidence below this => "no fiber"
FT_SMOOTH_WINDOW   = 5         # frames of temporal smoothing

# ---- operator prediction (extrapolate the fiber, fully offline) ----
FT_PREDICT_MIN_HITS           = 3    # tracked frames before we commit a guess
FT_OPERATOR_SIDE_DEADBAND_DEG = 4.0  # |bearing| under this reads "ahead"

# ---- dashboard (Flask + WebSocket) ----
FT_DASH_HOST = "0.0.0.0"       # bind address; 0.0.0.0 = reachable on the LAN
FT_DASH_PORT = 5050            # open http://localhost:5050 (5000 = macOS AirPlay)

# ---- output ----
FT_OPERATOR_ESTIMATE_JSON = "operator_estimate.json"  # final guesstimate on exit

# ---- demo ----
# With FT_MOCK_CAMERA=True, stop after this many seconds so the scripted
# scenario (track -> brief occlusion -> track) plays once, then summarize.
FT_MOCK_RUN_SECONDS = 13.0
