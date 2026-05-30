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
MOCK_VISION       = True
MOCK_VISION_LABEL = "munition"          # what the fake classifier "sees"
VISION_MODEL_PATH = "models/payload_classifier.tflite"
VISION_LABELS     = ["package", "munition"]   # index order must match training
VISION_INPUT_SIZE = 96                  # square input expected by your model

# ------------------------------------------------------------ DETECT THRESHOLDS
# Acoustic signature gate (tune against YOUR drone + room).
AMP_FLOOR    = 120             # min peak-to-peak amplitude to count as "loud"
PITCH_BAND   = (1200, 4500)    # Hz window typical of a high prop-whine
DETECT_HOLD  = 0.5             # seconds the signature must persist to fire

# ------------------------------------------------------------ DECIDE PARAMETERS
CLOSING_WINDOW_S    = 2.0      # history length for trend analysis
CLOSING_DROP_CM     = 15       # distance must fall this much to read "closing"
SCORE_HOSTILE       = 0.60     # fused score at/above this => HOSTILE verdict

# ----------------------------------------------------------------- AUTONOMY DIAL
# 0 teleop | 1 single-action assist | 2 detect+recommend, human gates ALL
# 3 human-on-the-loop (acts, human can veto) | 4 auto-DISTRACT only, gate rest
# 5 multi-agent (swarm hand-off)   --- see README "Degrees of autonomy"
AUTONOMY_LEVEL = 2

# --------------------------------------------------------------------- OPERATOR
AUTH_TIMEOUT_S = 8.0           # how long to wait for a human y/n before aborting
USE_JOYSTICK   = False         # True -> Modulino Joystick; False -> keyboard

# ----------------------------------------------------------------------- TIMING
LOOP_HZ      = 20              # main control-loop rate
COOLDOWN_S   = 4.0            # after a response, how long before re-arming
