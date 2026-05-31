"""main.py - Triple D brain (Mac).

State machine:
  IDLE -> (acoustic contact) -> DECIDING -> IDENTIFYING -> DEFEATING
       -> COOLDOWN -> IDLE

The decision after a drone is confirmed is AUTONOMOUS: no human is asked. In
IDENTIFYING, Triple D challenges the contact for a predefined shared key
(iff.py). A friendly drone (a second Arduino Uno holding the key) answers, so we
stand down and do nothing. Anything that can't present the key is a FOE, and the
DEFEAT stage engages on its own -- laser fiber-cut + decoy (defeat.py).

Run it:
    python main.py

With config.MOCK_SERIAL = True it walks the whole pipeline with NO hardware:
~3s in a scripted threat appears, gets classified, the contact is challenged,
and (if it can't present the key) the effects fire -- all autonomously.
"""
import enum
import time

import config
from comms import SensorLink
from detect import AcousticDetector
import decide
from iff import IFFChallenger
from defeat import Responder
from frontend.ui import Dashboard


def _make_detector():
    """Pick the DETECT backend. Falls back to the threshold gate if the CNN
    deps/mic/model aren't available, so the pipeline always comes up."""
    if not config.USE_CNN_DETECT:
        return AcousticDetector()
    try:
        from acoustic_cnn import CnnAcousticDetector
        return CnnAcousticDetector()
    except Exception as e:                                   # noqa: BLE001
        print(f"[detect] CNN backend unavailable ({e}); "
              f"falling back to amp/pitch gate")
        return AcousticDetector()


class State(enum.Enum):
    IDLE        = "IDLE"
    DECIDING    = "DECIDING"
    IDENTIFYING = "IDENTIFYING"
    DEFEATING   = "DEFEATING"
    COOLDOWN    = "COOLDOWN"


# Autonomous defeat heads, fired together once a contact is judged a FOE.
RESPONSE_PLAN = ["LASER_CUTOFF", "DECOY"]


class TripleD:
    def __init__(self):
        self.sensors    = SensorLink()
        self.detector   = _make_detector()
        self.closing    = decide.ClosingTracker()
        self.trajectory = decide.TrajectoryTracker()
        self.vision     = decide.VisionClassifier()
        self.iff        = IFFChallenger()
        self.responder  = Responder()
        self.dash       = Dashboard()
        # Let the dashboard serve the live webcam frames for its camera window.
        self.dash.set_frame_provider(self.vision.frame_jpeg)

        self.state = State.IDLE
        self.cooldown_until = 0.0
        self.decide_deadline = 0.0
        self.verdict = None
        self.aim = None
        self.period = 1.0 / config.LOOP_HZ

        # UI-facing summary of the current contact, carried across states.
        self.iff_status = None     # None | "friendly" | "foe"
        self.overlay    = ""
        self.engaging   = False

    def run(self):
        print(f"[triple-d] online. post-detection decision is AUTONOMOUS (IFF). "
              f"Ctrl+C to stop.\n")
        try:
            while True:
                t0 = time.time()
                telem = self.sensors.read_telemetry()
                self.closing.update(telem)
                contact = self.detector.update(telem)
                self.step(telem, contact)
                self._publish(telem)
                time.sleep(max(0.0, self.period - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\n[triple-d] shutting down -- all effects OFF")
        finally:
            self.responder.all_off()
            self.vision.release()
            if hasattr(self.detector, "close"):
                self.detector.close()
            self.iff.close()
            self.sensors.close()
            self.dash.stop()

    def step(self, telem, contact):
        s = self.state
        # After the initial visual confirmation we keep the camera locked on the
        # contact (IDENTIFYING through COOLDOWN), re-running detection every loop
        # so the straight-line heading and the laser solution stay live.
        if s in (State.IDENTIFYING, State.DEFEATING, State.COOLDOWN):
            self._track()
        if s is State.IDLE:
            self._status(telem, "scanning")
            # fresh contact slate while listening
            self.iff_status = None
            self.overlay = ""
            self.engaging = False
            if contact:
                print("\n[DETECT] acoustic contact -- RF-silent signature")
                # Listening model judged a threat: signal the vision model to
                # wake up, then watch for a drone for up to VISION_DECIDE_TIMEOUT_S.
                self.vision.activate()
                self.trajectory.reset()
                self.decide_deadline = time.time() + config.VISION_DECIDE_TIMEOUT_S
                self.state = State.DECIDING

        elif s is State.DECIDING:
            # Vision is awake: run the drone detector on a fresh frame each loop,
            # smoothing hits over its ring-buffer vote window. Keep watching until
            # the detector confirms a drone or the watch window expires.
            label, conf = self.vision.classify()
            self.trajectory.update(self.vision.last_detections)
            if label == "drone":
                is_closing = self.closing.is_closing()
                self.verdict = decide.assess(self.detector.confidence,
                                             label, conf, is_closing)
                print(f"\n[DECIDE] drone confirmed (threat score "
                      f"{self.verdict.score}) -- identifying friend or foe")
                for r in self.verdict.reasons:
                    print(f"         - {r}")
                # Drone confirmed -> hand off to the AUTONOMOUS friend-or-foe call.
                self.state = State.IDENTIFYING
            elif time.time() >= self.decide_deadline:
                print(f"\n[DECIDE] no drone seen in "
                      f"{config.VISION_DECIDE_TIMEOUT_S:.1f}s -- back to listening\n")
                self.vision.deactivate()   # webcam dark until next audio threat
                self.state = State.IDLE
            # else: stay in DECIDING and keep watching on the next loop

        elif s is State.IDENTIFYING:
            # AUTONOMOUS decision: challenge the contact for the shared key.
            result = self.iff.challenge()
            if result.friendly:
                print(f"[IFF] FRIENDLY -- {result.detail}. Standing down, "
                      f"no action.\n")
                self.iff_status = "friendly"
                self.overlay = "Friendly drone — returning to listening"
                self.vision.deactivate()
                self._enter_cooldown()
            else:
                print(f"[IFF] FOE -- {result.detail}. Engaging autonomously.")
                self.iff_status = "foe"
                self.overlay = "Tracking enemy drone — neutralizing threat"
                self.aim = self.trajectory.aim()
                self.state = State.DEFEATING

        elif s is State.DEFEATING:
            # Reached only on a FOE verdict from IFF -- fully autonomous.
            self.engaging = True
            self.aim = self.trajectory.aim()      # freshest heading at firing
            print(f"[DEFEAT] firing: {', '.join(RESPONSE_PLAN)}")
            for action in RESPONSE_PLAN:
                self.responder.fire(action, self.aim)
            # Keep vision tracking through cooldown so the cut point stays behind
            # the drone and perpendicular to its path; vision stands down on re-arm.
            self._enter_cooldown()

        elif s is State.COOLDOWN:
            # Keep refining the laser solution from the live track while engaging.
            if self.engaging:
                self.aim = self.trajectory.aim()
            if time.time() >= self.cooldown_until and not contact:
                self.responder.all_off()
                self.vision.deactivate()   # webcam dark until next threat
                print("[triple-d] re-armed.\n")
                self.state = State.IDLE

    def _track(self):
        """Keep the visual lock alive after the initial confirmation: re-run the
        detector on a fresh frame and feed the centroid into the trajectory so we
        always hold a current position and an updated straight-line heading.
        No-op once vision has been stood down (e.g. friendly stand-down)."""
        if not self.vision.active:
            return
        self.vision.classify()
        self.trajectory.update(self.vision.last_detections)

    def _enter_cooldown(self):
        self.cooldown_until = time.time() + config.COOLDOWN_S
        self.state = State.COOLDOWN

    # ---- UI publishing ------------------------------------------------------

    def _grid_xy(self):
        """Latest drone centroid -> normalised grid coords (-1..1, unit centered),
        or None when there is no live detection."""
        dets = self.vision.last_detections
        if not dets:
            return None
        best = max(dets, key=lambda d: d["conf"])
        x1, y1, x2, y2 = best["box"]
        s = float(config.VISION_INPUT_SIZE)
        cx = (x1 + x2) / 2.0 / s
        cy = (y1 + y2) / 2.0 / s
        return {"x": max(-1.0, min(1.0, cx * 2 - 1)),
                "y": max(-1.0, min(1.0, cy * 2 - 1))}

    def _publish(self, telem):
        """Build the published state contract (the same shape the offline
        mock_server.py serves) from the brain's live signals, and hand it to the
        dashboard. Monitoring-only: nothing here actuates anything."""
        telem = telem or {}
        s = float(config.VISION_INPUT_SIZE)

        # --- acoustic / mic ---
        amp_raw = telem.get("amp", 0) or 0
        amp_n = max(0.0, min(1.0, amp_raw / 450.0))
        acoustic = float(self.detector.confidence or 0.0)

        # --- vision vote + smoothed confidence ---
        vh = list(getattr(self.vision, "_vote_hist", []))
        vote_hits = sum(1 for h, _ in vh if h)
        hit_confs = [c for h, c in vh if h]
        vis_conf = (sum(hit_confs) / len(hit_confs)) if hit_confs else 0.0

        closing = self.closing.is_closing()
        tracking = self.state in (State.IDENTIFYING, State.DEFEATING, State.COOLDOWN)

        # --- threat score (verdict once we have one; else a low live estimate) ---
        if tracking and self.verdict is not None:
            score = float(self.verdict.score)
        elif self.state is State.DECIDING:
            score = round(min(1.0, 0.30 * acoustic + 0.20), 2)
        else:
            score = round(min(1.0, 0.30 * acoustic), 2)

        # --- contact ---
        grid = self._grid_xy()
        has_contact = tracking and (grid is not None or self.iff_status is not None)
        contact = None
        if has_contact:
            iff = {"friendly": "friend", "foe": "foe"}.get(self.iff_status, "unknown")
            heading_deg = None
            aim = self.aim
            if aim is not None and aim.available:
                heading_deg = (aim.heading_deg + 90.0) % 360.0   # image-plane -> compass
            trail = [{"x": max(-1.0, min(1.0, p[1] / s * 2 - 1)),
                      "y": max(-1.0, min(1.0, p[2] / s * 2 - 1))}
                     for p in getattr(self.trajectory, "pts", [])]
            contact = {
                "iff": iff,
                "grid": grid or {"x": 0.0, "y": 0.0},
                "heading_deg": heading_deg,
                "trail": trail,
                "vote": {"hits": vote_hits, "window": config.VISION_VOTE_WINDOW},
            }

        # --- defeat (only on a FOE verdict; non-kinetic distract + laser track) ---
        foe = (self.iff_status == "foe")
        engaging = bool(self.engaging and foe)
        laser_aim, sweep = None, 0.0
        if engaging and self.aim is not None and self.aim.available:
            cx, cy = self.aim.cutoff_xy
            laser_aim = [max(-1.0, min(1.0, cx / s * 2 - 1)),
                         max(-1.0, min(1.0, cy / s * 2 - 1))]
            sweep = float(self.aim.sweep_deg)

        vision_ok = not getattr(self.vision, "mock", False)
        dist = telem.get("dist", -1)

        contract = {
            "mode": "mock" if getattr(config, "MOCK_SERIAL", False) else "live",
            "state": self.state.value,
            "link": {"serial": "up" if telem else "down",
                     "baud": getattr(config, "BAUD", 115200)},
            "health": {"vision_model": vision_ok, "acoustic_model": True,
                       "mic": True, "camera": vision_ok},
            "sensors": {
                "mic": {"amplitude": round(amp_n, 3),
                        "confidence": round(acoustic, 3),
                        "waveform": None},       # dashboard renders a live level trace
                "camera": {"awake": bool(self.vision.active), "stream": "/cam.mjpeg"},
                "distance_cm": (dist if (dist is not None and dist >= 0) else None),
            },
            "threat": {
                "score": round(score, 3),
                "threshold": getattr(config, "SCORE_HOSTILE", 0.60),
                "closing": bool(closing),
                "components": {"sound": round(min(1.0, acoustic), 3),
                               "camera": round(min(1.0, vis_conf), 3),
                               "closing": 1.0 if closing else 0.0},
            },
            "contact": contact,
            "defeat": {"active": engaging, "decoy": engaging,
                       "laser": {"active": engaging, "aim": laser_aim,
                                 "sweep_deg": sweep}},
        }
        self.dash.publish(contract)

    def _status(self, telem, note):
        if telem is None:
            return
        print(f"\r[{note}] amp={telem.get('amp', 0):4d} "
              f"pitch={telem.get('pitch', 0):5d}Hz "
              f"dist={telem.get('dist', -1):4d}cm "
              f"conf={self.detector.confidence:.2f}   ", end="", flush=True)


if __name__ == "__main__":
    TripleD().run()