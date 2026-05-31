"""main.py - Triple D brain (Uno Q).

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
from comms import CarLink
from detect import AcousticDetector
import decide
from iff import IFFChallenger
from defeat import Responder
from ui import Dashboard


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
        self.car        = CarLink()
        self.detector   = _make_detector()
        self.closing    = decide.ClosingTracker()
        self.trajectory = decide.TrajectoryTracker()
        self.vision     = decide.VisionClassifier()
        self.iff        = IFFChallenger()
        self.responder  = Responder(self.car)
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
                telem = self.car.read_telemetry()
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
            self.car.close()
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

    _UI_MODE = {
        State.IDLE:        "listening",
        State.DECIDING:    "searching",
        State.IDENTIFYING: "tracking",
        State.DEFEATING:   "tracking",
        State.COOLDOWN:    "tracking",   # hold the verdict view until re-armed
    }

    def _norm_xy(self, xy):
        """Image-plane (0..VISION_INPUT_SIZE) point -> normalised [0,1] grid."""
        if xy is None:
            return None
        s = float(config.VISION_INPUT_SIZE)
        return [max(0.0, min(1.0, xy[0] / s)),
                max(0.0, min(1.0, xy[1] / s))]

    def _drone_xy(self):
        """Normalised drone position from the latest vision detection, or None
        (mock/no detection) -> the dashboard simulates a moving track."""
        dets = self.vision.last_detections
        if not dets:
            return None
        best = max(dets, key=lambda d: d["conf"])
        x1, y1, x2, y2 = best["box"]
        return self._norm_xy(((x1 + x2) / 2.0, (y1 + y2) / 2.0))

    def _publish(self, telem):
        telem = telem or {}
        now = time.time()
        engaging = self.engaging
        laser_xy = (self._norm_xy(self.aim.cutoff_xy)
                    if engaging and self.aim is not None and self.aim.available
                    else None)
        self.dash.update(
            mode=self._UI_MODE.get(self.state, "listening"),
            raw_state=self.state.value,
            amp=telem.get("amp", 0),
            pitch=telem.get("pitch", 0),
            dist=telem.get("dist", -1),
            acoustic_conf=self.detector.confidence,
            search_remaining=(max(0.0, self.decide_deadline - now)
                              if self.state is State.DECIDING else 0.0),
            search_total=config.VISION_DECIDE_TIMEOUT_S,
            iff=self.iff_status,
            overlay=self.overlay,
            engaging=engaging,
            drone_xy=self._drone_xy(),
            laser_xy=laser_xy,
            decoy_xy=None,           # placeholder offset; UI sims it if None
            # Camera window is live exactly while vision is awake: it turns on
            # when acoustic noise wakes vision and goes dark when the 1.5s watch
            # expires or the contact is no longer being tracked.
            camera_on=self.vision.active,
        )

    def _status(self, telem, note):
        if telem is None:
            return
        print(f"\r[{note}] amp={telem.get('amp', 0):4d} "
              f"pitch={telem.get('pitch', 0):5d}Hz "
              f"dist={telem.get('dist', -1):4d}cm "
              f"conf={self.detector.confidence:.2f}   ", end="", flush=True)


if __name__ == "__main__":
    TripleD().run()
