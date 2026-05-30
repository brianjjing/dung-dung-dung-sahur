"""main.py - Triple D brain (Uno Q).

State machine:
  IDLE -> (acoustic contact) -> DECIDING -> AUTHORIZING -> DEFEATING
       -> COOLDOWN -> IDLE

Run it:
    python main.py

With config.MOCK_SERIAL = True it walks the whole pipeline with NO hardware:
~3s in a scripted threat appears, gets classified, you authorize, effects fire.
"""
import enum
import time

import config
from comms import CarLink
from detect import AcousticDetector
import decide
import human as op
from defeat import Responder


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
    AUTHORIZING = "AUTHORIZING"
    DEFEATING   = "DEFEATING"
    COOLDOWN    = "COOLDOWN"


# Heads attempted, in order, once a verdict is HOSTILE.
RESPONSE_PLAN = ["DISTRACT", "DAZZLE", "DEFEND"]


class TripleD:
    def __init__(self):
        self.car       = CarLink()
        self.detector  = _make_detector()
        self.closing   = decide.ClosingTracker()
        self.vision    = decide.VisionClassifier()
        self.responder = Responder(self.car)

        self.state = State.IDLE
        self.cooldown_until = 0.0
        self.verdict = None
        self.authorized = []
        self.period = 1.0 / config.LOOP_HZ

    def run(self):
        print(f"[triple-d] online. autonomy level L{config.AUTONOMY_LEVEL}. "
              f"Ctrl+C to stop.\n")
        try:
            while True:
                t0 = time.time()
                telem = self.car.read_telemetry()
                self.closing.update(telem)
                contact = self.detector.update(telem)
                self.step(telem, contact)
                time.sleep(max(0.0, self.period - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\n[triple-d] shutting down -- all effects OFF")
        finally:
            self.responder.all_off()
            self.vision.release()
            if hasattr(self.detector, "close"):
                self.detector.close()
            self.car.close()

    def step(self, telem, contact):
        s = self.state
        if s is State.IDLE:
            self._status(telem, "scanning")
            if contact:
                print("\n[DETECT] acoustic contact -- RF-silent signature")
                self.state = State.DECIDING

        elif s is State.DECIDING:
            label, conf = self.vision.classify()
            is_closing = self.closing.is_closing()
            self.verdict = decide.assess(self.detector.confidence,
                                         label, conf, is_closing)
            print(f"[DECIDE] {'HOSTILE' if self.verdict.hostile else 'BENIGN'} "
                  f"score={self.verdict.score}")
            for r in self.verdict.reasons:
                print(f"         - {r}")
            if self.verdict.hostile:
                self.state = State.AUTHORIZING
            else:
                print("[DECIDE] judged benign (supplies?) -- standing down\n")
                self._enter_cooldown()

        elif s is State.AUTHORIZING:
            self.authorized = []
            for action in RESPONSE_PLAN:
                if op.authorize(action, self.verdict):
                    self.authorized.append(action)
                else:
                    print(f"[operator] '{action}' denied")
            self.state = State.DEFEATING

        elif s is State.DEFEATING:
            if self.authorized:
                print(f"[DEFEAT] firing: {', '.join(self.authorized)}")
                for action in self.authorized:
                    self.responder.fire(action)
            else:
                print("[DEFEAT] no actions authorized -- holding")
            self._enter_cooldown()

        elif s is State.COOLDOWN:
            if time.time() >= self.cooldown_until and not contact:
                self.responder.all_off()
                print("[triple-d] re-armed.\n")
                self.state = State.IDLE

    def _enter_cooldown(self):
        self.cooldown_until = time.time() + config.COOLDOWN_S
        self.state = State.COOLDOWN

    def _status(self, telem, note):
        if telem is None:
            return
        print(f"\r[{note}] amp={telem.get('amp', 0):4d} "
              f"pitch={telem.get('pitch', 0):5d}Hz "
              f"dist={telem.get('dist', -1):4d}cm "
              f"conf={self.detector.confidence:.2f}   ", end="", flush=True)


if __name__ == "__main__":
    TripleD().run()
