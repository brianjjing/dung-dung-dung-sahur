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
    ├── frontend/ui.py  # live operator dashboard (stdlib HTTP + canvas HUD)
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
