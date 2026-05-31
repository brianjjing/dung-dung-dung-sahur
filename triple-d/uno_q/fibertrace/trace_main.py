"""trace_main.py — FiberTrace controller (detect fiber -> guesstimate operator).

The RC car sits on the ground and watches a fiber-optic drone. Each frame:

    capture -> detect fiber line -> smooth -> extrapolate to ground
            -> operator guesstimate (bearing + confidence) -> dashboard

State is just whether we currently have the fiber:

    SEARCHING --fiber acquired--> TRACKING --fiber lost--> SEARCHING

No driving and no dead-reckoning: the prediction is pure image geometry, so it
works offline and stationary. (The car *could* later turn to keep the drone
centred, but that is deliberately out of scope here — "just predict".)

Run from triple-d/uno_q:
    python -m fibertrace.trace_main
    python -m fibertrace.trace_main --no-dashboard
"""
from __future__ import annotations

import argparse
import enum
import json
import threading
import time

import config

from fibertrace.capture import FrameSource
from fibertrace.detect_line import LineDetector, ConfidenceTracker
from fibertrace.predict import OperatorPredictor
from fibertrace.dashboard import Dashboard, draw_overlay


class State(enum.Enum):
    SEARCHING = "SEARCHING"
    TRACKING = "TRACKING"


class FiberTrace:
    def __init__(self):
        self.src = FrameSource()
        self.detector = LineDetector()
        self.tracker = ConfidenceTracker()
        self.predictor = OperatorPredictor()
        self.dash = Dashboard()

        self.state = State.SEARCHING
        self.frame_idx = 0
        self.period = 1.0 / max(1, config.FT_TARGET_FPS)
        self.last_operator = None

    def _enter(self, new_state):
        if new_state is not self.state:
            print(f"[fibertrace] {self.state.value} -> {new_state.value}")
            self.state = new_state

    def _state_dict(self, est, operator):
        return {
            "state": self.state.value,
            "detection": {
                "fiber_detected": est.detected,
                "confidence": round(est.confidence, 2),
                "num_lines": est.num_lines,
            },
            "operator": operator,
        }

    def run(self):
        print(f"[fibertrace] online — state {self.state.value}. Ctrl+C to stop.")
        if self.dash.enabled:
            threading.Thread(target=self.dash.run, daemon=True).start()
            time.sleep(0.4)  # let the server bind before the loop floods it

        t_start = time.time()
        try:
            while True:
                t0 = time.time()
                frame = self.src.read()
                if frame is None:
                    print("[fibertrace] frame source ended")
                    break

                self.frame_idx += 1
                est = self.tracker.update(self.detector.detect(frame))
                self._enter(State.TRACKING if est.detected else State.SEARCHING)

                operator = self.predictor.predict(est)
                if operator is not None:
                    self.last_operator = operator

                if self.dash.enabled:
                    annotated = draw_overlay(frame, est, self.state.value, operator)
                    self.dash.update_frame(annotated)
                    self.dash.update_state(self._state_dict(est, operator))

                if (self.src.mock
                        and time.time() - t_start >= config.FT_MOCK_RUN_SECONDS):
                    print("[fibertrace] mock scenario complete")
                    break

                time.sleep(max(0.0, self.period - (time.time() - t0)))
        except KeyboardInterrupt:
            print("\n[fibertrace] interrupted")
        finally:
            self._shutdown()

    def _shutdown(self):
        self.src.release()

        op = self.last_operator
        with open(config.FT_OPERATOR_ESTIMATE_JSON, "w") as fh:
            json.dump({"final_operator_estimate": op}, fh, indent=2)

        print("\n" + "=" * 56)
        print("  FiberTrace summary")
        print("=" * 56)
        print(f"  final state     : {self.state.value}")
        if op:
            print(f"  OPERATOR GUESS  : {op['side']} {abs(op['bearing_deg']):.0f}deg "
                  f"off-axis   confidence {op['confidence']:.2f}")
        else:
            print("  OPERATOR GUESS  : (no confident fiber seen)")
        print(f"  estimate JSON   : {config.FT_OPERATOR_ESTIMATE_JSON}")
        print("=" * 56)

        if self.dash.enabled and self.src.mock:
            print("[fibertrace] dashboard still live — Ctrl+C to exit.")
            try:
                while True:
                    time.sleep(1.0)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FiberTrace controller")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="run headless (no Flask dashboard)")
    args = parser.parse_args()

    ft = FiberTrace()
    if args.no_dashboard:
        ft.dash.enabled = False
    ft.run()
