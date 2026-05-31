"""Central configuration for the Uno Q brain.

Deployed for real hardware on the Arduino Uno Q: live mic, USB camera,
serial link to the car UNO R3, and IFF transceiver on a second serial port.
"""

# ----------------------------------------------------------------- SERIAL LINK
SERIAL_PORT = "/dev/ttyACM0"   # UNO R3 on the car (ttyACM0 or ttyUSB0 on Linux)
BAUD        = 115200
MOCK_SERIAL = False

# ---------------------------------------------------------------------- CAMERA
CAMERA_INDEX     = 1           # USB webcam on the Uno Q (via the USB hub)
MOCK_VISION      = False
MOCK_VISION_LABEL = "drone"    # unused when MOCK_VISION=False

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
# USE_CNN_DETECT=True -> Mel-spectrogram CNN on the Uno Q mic (acoustic_cnn.py).
# USE_CNN_DETECT=False -> amp/pitch gate from the car UNO R3 telemetry.
USE_CNN_DETECT = True

# CNN backend (only used when USE_CNN_DETECT=True)
ACOUSTIC_MODEL_PATH   = "models/drone_acoustic_cnn.pth"
ACOUSTIC_THRESHOLD    = 0.40   # P(threat) to count as signature (lower=more sensitive)
ACOUSTIC_INFER_PERIOD = 0.30   # seconds between sliding-window classifications
ACOUSTIC_SOURCE_WAV   = ""     # "" = live mic on the Uno Q

# Acoustic signature gate (threshold backend; only used when USE_CNN_DETECT=False)
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

# ---------------------------------------------------- AUTONOMOUS IFF (friend/foe)
# Once vision confirms a drone, Triple D challenges the contact for a shared key.
# A friendly drone (second Arduino Uno with the same secret) answers; anything
# else is treated as a FOE -> autonomous DEFEAT (see iff.py).
IFF_SHARED_SECRET       = "triple-d-shared-key-2025"  # preloaded on the friendly Uno
IFF_CHALLENGE_TIMEOUT_S = 1.0     # wait this long for the contact to present the key
MOCK_IFF          = False
MOCK_IFF_FRIENDLY = False         # only used when MOCK_IFF=True
IFF_PORT          = "/dev/ttyACM1"  # serial transceiver to the friendly/foe drone
IFF_BAUD          = 115200

# ------------------------------------------------------- AUTONOMOUS DEFEAT (foe)
# Trajectory -> laser aim solution. The fiber-optic tether trails BEHIND the
# drone along its flight path, so the cut is placed behind it and swept
# perpendicular to travel.
TRAJ_WINDOW         = 8           # frames of centroid history used to fit heading
LASER_CUTOFF_BACK_PX = 60        # how far BEHIND the drone (image px) to place the cut

# ----------------------------------------------------------------- AUTONOMY DIAL
# Legacy human-gate dial (kept for human.py / autonomy.py). The post-detection
# defend decision is AUTONOMOUS via IFF and no longer consults this dial.
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

# --------------------------------------------------------------------------- UI
# Live operator dashboard (ui.py). A tiny stdlib HTTP server runs in a background
# thread and publishes the brain's current state as JSON; a single-page canvas
# frontend renders it: a responsive mic/waveform while listening, a radar
# "SEARCHING" view while vision watches, and a bird's-eye tracking grid once a
# drone is found. Zero extra dependencies; terminal output is unaffected.
UI_ENABLED      = True
UI_HOST         = "127.0.0.1"
UI_PORT         = 8077
UI_OPEN_BROWSER = True         # best-effort auto-open the dashboard at startup
