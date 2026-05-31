# FiberTrace

**Most counter-drone systems look up. FiberTrace looks down the line.**

Fiber-optic drones beat RF jamming because they're radio-silent — but they
trail a physical fiber back to their operator. FiberTrace is an offline edge-AI
payload: the RC car sits on the ground, its camera watches a fiber-optic drone,
**detects the thin fiber trailing from it, and extrapolates that line to
guesstimate where the operator is** — a left/right bearing with a confidence
that firms up the longer it's tracked. No GPS, no internet, no model server.

It's a second capability for the same hardware Triple D uses, reusing the shared
`config.py`. It changes no Triple D files.

```
SEE the drone ──▶ DETECT the fiber ──▶ EXTRAPOLATE the line ──▶ GUESS the operator
  (camera)         (OpenCV Hough)        (project to ground)      (bearing + conf)
```

## Run it now (no camera, no car)

```bash
cd triple-d/uno_q
pip install -r fibertrace/requirements.txt
python -m fibertrace.trace_main
```

Open **http://localhost:5050**. A synthetic scene plays for ~13 s: a drone
drifts overhead with a fiber trailing to an operator off to one side. The
dashboard shows the live camera with the detected fiber + extrapolation ray,
and a **bearing fan** pinning the predicted operator direction. On exit you get
`operator_estimate.json` with the final guesstimate.

Headless (no dashboard): `python -m fibertrace.trace_main --no-dashboard`

Example exit summary:

```
  OPERATOR GUESS  : right 16deg off-axis   confidence 0.86
```

## Go live (real drone + fishing line + car)

In `config.py`, FiberTrace section, set `FT_MOCK_CAMERA = False` to use the
real webcam (`CAMERA_INDEX`). Point the car's camera at the drone with a
fishing-line stand-in for the fiber, and run the same command. Tune
`FT_CAMERA_HFOV_DEG` to your lens so the bearing degrees are accurate.

## Files

| File | Role |
|------|------|
| `capture.py` | webcam **or** synthetic drone+fiber scene (`FT_MOCK_CAMERA`) |
| `detect_line.py` | preprocess → Canny → Hough → one `LineEstimate` + smoothing |
| `predict.py` | extrapolate the fiber to the ground → **operator bearing guess** |
| `dashboard.py` | Flask page + MJPEG video + WebSocket bearing fan; frame overlay |
| `trace_main.py` | SEARCHING/TRACKING loop wiring it together |

## Tuning

All knobs are the `FT_*` block in `triple-d/uno_q/config.py` — detection
(`FT_CANNY_*`, `FT_HOUGH_*`, `FT_MAX_TILT_DEG`), smoothing
(`FT_SMOOTH_WINDOW`), and prediction (`FT_CAMERA_HFOV_DEG`,
`FT_PREDICT_MIN_HITS`).

## Honest limitations

- **One camera = a bearing, not a fix.** FiberTrace estimates the *direction*
  to the operator, not the distance. A second vehicle (cross-bearing) or moving
  the car for parallax turns two bearings into a position.
- **Thin transparent fiber is hard** in real light. Use a visible stand-in
  (fishing line / reflective thread) for the demo; a trained thin-line
  segmentation model is the drop-in upgrade behind the `LineEstimate` interface.
- **Bearing accuracy depends on `FT_CAMERA_HFOV_DEG`** matching your real lens.
