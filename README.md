<p align="center">
  <img src="brand/banner.svg" alt="Triple D — Detect · Decide · Defeat" width="100%">
</p>

<p align="center">
  <code>DETECT → DECIDE → DEFEAT</code> &nbsp;·&nbsp; <code>DISTRACT · DISABLE · DEFEND</code><br>
  <em>The safe action is automatic. The hard one asks you.</em>
</p>

---

# Triple D

Layered, non-kinetic defense against RF-silent (fiber-optic) suicide drones.
**Pipeline:** `DETECT → DECIDE → DEFEAT`. **Heads:** `DISTRACT · DISABLE · DEFEND`.

The brain runs on a **Mac** (camera + mic over USB); the car is an Arduino
**UNO R4 WiFi** on the Elegoo chassis that drives the motors and fires effects,
taking one-letter commands from the Mac over **Wi-Fi**. **One repo, two folders.**

> **Authors:** Brian Jing · Mikey Nguyen · Victor Lopez · Kevin Pyo

```
triple-d/
├── uno_r3/triple_d_car/triple_d_car.ino   # LEGACY: USB-serial R3 prototype (see Legacy below)
└── uno_q/                                  # python main.py; the brain (runs on the Mac)
    ├── main.py         # state machine (IDLE→DECIDING→IDENTIFYING→DEFEATING→COOLDOWN)
    ├── config.py       # ALL tunables, mock flags, the autonomy dial
    ├── car_client.py   # car control over Wi-Fi to the UNO R4 (deploy_decoy, etc.)
    ├── comms.py        # serial telemetry link + scripted mock telemetry
    ├── detect.py       # DETECT  (acoustic, from mic features)
    ├── decide.py       # DECIDE  (vision + closing-behavior + fusion)
    ├── iff.py          # IDENTIFY (autonomous friend-or-foe challenge)
    ├── defeat.py       # DEFEAT  (fires effects; DECOY → R4 distract over Wi-Fi)
    ├── frontend/       # V11 Converged Console — operator dashboard
    │   ├── index.html      # single-file dashboard (vanilla Canvas+JS, fully offline)
    │   ├── ui.py           # production server: index.html + /state.json + /cam.mjpeg
    │   └── mock_server.py  # offline demo server (scripted FSM + synthetic camera)
    └── models/         # trained detector weights
```

## Architecture
```
[Logitech cam + mic] --USB--> [Mac : Python brain] --Wi-Fi--> [UNO R4 WiFi on car]
                               DETECT  acoustic gate            command server :5050
                               DECIDE  webcam ML + fusion       drives Elegoo motors
                               IDENTIFY autonomous IFF          fires effects (distract)
                               DEFEAT  picks heads, sends cmds  (car battery powers motors)
```
The car never decides anything. All intelligence is Python on the Mac; the car
is a Wi-Fi actuator.

## Car control (Wi-Fi)
The UNO R4 WiFi runs a tiny TCP command server on the car. The Mac talks to it
through [`car_client.py`](triple-d/uno_q/car_client.py) — no USB cable to the car.
```
Mac -> R4 (TCP 192.168.1.90:5050):  one ASCII letter per command
   F = forward   B = backward   L = left   R = right   S = stop   D = distract
```
The AI pipeline only ever calls `car_client.deploy_decoy()` (the DISTRACT
action); it knows nothing about motors. Test the car alone with:
```bash
python car_client.py     # car battery ON + R4 on Wi-Fi -> car moves + distract
```

## Run it RIGHT NOW (no hardware)
```bash
cd uno_q
pip install pyserial
python main.py
```
`config.MOCK_SERIAL = True` synthesizes a scripted threat ~3s in. You'll watch
DETECT fire, a drone get confirmed, the autonomous IFF challenge resolve to FOE,
and the DEFEAT commands print. This is your skeleton working end-to-end, with no
car on Wi-Fi.

## Run the dashboard offline & locally

The operator view is the **V11 Converged Console** — one vanilla `index.html`
(inline CSS + JS + Canvas, **system monospace only, no webfonts, no CDNs, no
bundler**). It is **monitoring-only**: it has no buttons or "fire" controls; the
system is autonomous and the UI only *displays* state. The page is a pure
function of `GET /state.json` (polled ~15 Hz) plus a camera `<img>` on
`GET /cam.mjpeg`.

You don't need any hardware — `mock_server.py` (Python **stdlib only**) serves
the dashboard and a fully scripted data feed:

```bash
cd triple-d/uno_q/frontend
python3 mock_server.py            # -> http://127.0.0.1:8077
```

Open **http://127.0.0.1:8077**. With no flag it loops the whole pipeline:
`IDLE → DECIDING (vote climbs) → IDENTIFYING → DEFEATING (laser + decoy) →
COOLDOWN → IDLE`, alternating the IFF branch foe/friend each lap. Force a single
path with `?scenario=`:

| URL | What it shows |
|-----|----------------|
| `http://127.0.0.1:8077/` | full looping scenario (foe ↔ friend) |
| `…/?scenario=foe` | always resolves FOE → red track, laser-behind-drone, sweep, flashing decoy |
| `…/?scenario=friend` | always resolves FRIEND → green circle, **never** a laser or active decoy |
| `…/?scenario=linkdown` | `link.serial = "down"` → dashboard enters **LINK DOWN**, last data marked stale |
| `…/?scenario=degraded` | `health.camera` / `vision_model` false → **CAM-01 OFFLINE / VISION MODEL NOT LOADED** |

> Verification aid: append `&at=<seconds>` (e.g. `?scenario=foe&at=11.5`) to pin a
> specific point in the 20-second cycle for a deterministic screenshot.

### Zero external network requests (and how to verify)
Everything is served from `127.0.0.1` — there are **no** webfonts, analytics,
CDNs, or third-party calls, so the console works on a fully air-gapped device.
To confirm: open the browser **DevTools → Network** tab, hard-reload, and check
that every request's domain is `127.0.0.1:8077` (`index.html`, repeated
`state.json` polls, and the `cam.mjpeg` stream) — nothing else. You can also run
the device offline (no Wi-Fi/Ethernet) and the dashboard renders identically.

### How the real device serves the same contract
In production the mock is replaced by the brain itself — **no UI change**.
`main.py` builds the exact same state contract from live sensors and hands it to
`frontend/ui.py`, a tiny stdlib HTTP server (background daemon thread) that
serves the **identical endpoints**:

```
GET /            -> the same frontend/index.html (served from disk)
GET /state.json  -> live contract snapshot (read under a lock, ~poll at 15 Hz)
GET /cam.mjpeg   -> live camera as multipart/x-mixed-replace JPEG (503 while dark)
```

So the browser is **display-only** and never touches hardware: the Python brain
owns every I/O path (USB camera + mic in, Wi-Fi to the UNO R4 car out) and
actuates the car at the control-loop rate; the dashboard just mirrors the
published state. Swapping `mock_server.py` for the real device changes nothing
in `index.html`. The contract:

```jsonc
{ "ts", "mode": "live|mock", "state": "IDLE|DECIDING|IDENTIFYING|DEFEATING|COOLDOWN",
  "link": {"serial","baud"}, "health": {"vision_model","acoustic_model","mic","camera"},
  "sensors": {"mic": {"amplitude","confidence","waveform"}, "camera": {"awake","stream"}, "distance_cm"},
  "threat": {"score","threshold","closing","components": {"sound","camera","closing"}},
  "contact": {"iff","grid": {"x","y"},"heading_deg","trail","vote": {"hits","window"}} /* or null */,
  "defeat": {"active","decoy","laser": {"active","aim","sweep_deg"}}, "log": [] }
```

`main.py` already starts this server (`UI_ENABLED`/`UI_HOST`/`UI_PORT` in
`config.py`, default `127.0.0.1:8077`), so `python main.py` brings up the same
dashboard at the same URL with real telemetry.

## Runs fully offline

The entire system runs with **no internet and no cloud** — detection, decision,
IFF, and the operator console are all local. Verified across the runtime
pipeline (`main · comms · detect · decide · iff · defeat · car_client ·
frontend/ui`): there are **no** HTTP calls, no SDKs that phone home, and no
weight downloads.

- **On-device inference, local weights.** Vision uses `onnxruntime` on a local
  `models/drone_detector.onnx` (CPU); acoustic uses a local `torch` `.pth`. The
  model files ship in the repo — first run fetches nothing.
- **Dashboard is localhost-only.** `frontend/ui.py` (and `mock_server.py`) bind
  `127.0.0.1`; `index.html` is one self-contained file — **system monospace
  only, no webfonts, no CDNs, no bundler** — and makes **zero external requests**
  (check DevTools → Network: every request is `127.0.0.1:8077`).
- **The only network is your own LAN.** The single networked link is
  `car_client.py` → the UNO R4 car over Wi-Fi (`192.168.1.90:5050`) — a private
  link to your own actuator, not the internet. Run it on an isolated
  hotspot/router with no WAN and everything still works.

Two setup-time notes (neither needed at demo time): Python dependencies
(`onnxruntime`, `torch`, `opencv`, `librosa`, `pyserial`, …) must be
`pip install`ed **once** beforehand; and for a pure-laptop showing with no rig,
`python3 mock_server.py` renders the identical console with no radios at all.

## Bring-up plan (flip one thing on at a time)
1. **Mock pipeline** — as above. Prove the state machine end to end.
2. **Car link** — power the car battery, confirm the UNO R4 is on Wi-Fi at its
   IP, then `python car_client.py`. The car should drive and run distract mode.
3. **Acoustic** — point your drone-noise source at the mic; tune `AMP_FLOOR`
   and `PITCH_BAND` until DETECT is reliable.
4. **Vision** — set `MOCK_VISION = False` and confirm the webcam (`CAMERA_INDEX`)
   detects a drone.
5. **Effects** — confirm a FOE verdict drives the car's DISTRACT (`deploy_decoy`)
   over Wi-Fi.
6. **Tune fusion + autonomy level**, then rehearse the demo.

## Hardware notes
- **Car:** UNO R4 WiFi on the Elegoo chassis. It controls the motors directly
  and exposes the `F/B/L/R/S/D` command server; the **car battery must be ON**
  for the wheels to move. Its IP/port live in `car_client.py` (`R4_IP`, `PORT`).
- **Mac peripherals:** the Logitech camera + mic connect to the Mac over USB
  (`CAMERA_ID = 0` / `cv2.VideoCapture(0)`); the mic is read by the acoustic
  stage. No USB cable runs from the Mac to the car anymore.

## Legacy: UNO R3 serial prototype
The original car used a USB-serial **UNO R3** (`uno_r3/triple_d_car.ino`) that
streamed `TEL,...` telemetry up and took `CMD,...` actions down at 115200 baud.
That bridge has been **replaced** by the UNO R4 WiFi link above; the sketch and
the serial protocol are kept here only for reference.
```
UP   (car -> brain):  TEL,AMP:512,PITCH:2200,DIST:84,LINE:0
DOWN (brain -> car):  CMD,DISTRACT_ON  (also DISTRACT_OFF, DAZZLE_ON/OFF,
                                         DRIVE_F/B/L/R/S, ALL_OFF, IDLE)
```

## Degrees of autonomy (the dial in `config.AUTONOMY_LEVEL`)
| L | Behavior | Who fires |
|---|----------|-----------|
| 0 | Teleop | human does everything |
| 1 | Single-action assist | human triggers each effect |
| 2 | Detect + recommend | **human gates ALL actions** (default / target demo) |
| 3 | Human-on-the-loop | system acts, human can veto in a window |
| 4 | Bounded autonomy | auto-DISTRACT only (harmless); gate DISABLE/DEFEND |
| 5 | Multi-agent | swarm hands off targets over the mesh |

Design principle: **the safe action can be automated; the harmful one always
asks a human first.** That split (L4) is the responsible-autonomy story.

## Honesty notes (don't oversell to judges)
- **DEFEND is tracking-only.** Cutting a hair-thin moving fiber is out of scope
  for this hardware; the demo is precision lock-and-track. No "cut" command
  exists by design.
- **DISABLE** = defeat the seeker (IR dazzle), not "disarm the bomb."
- **One mic = detection, not bearing.** Direction-finding needs multiple nodes.
- Always rehearse with **L0 teleop** available as a fallback.
