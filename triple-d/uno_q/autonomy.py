"""autonomy.py - L3 (human-on-the-loop) extension for Triple D.

Run this instead of main.py when you want L3 behaviour:

    # in config.py: AUTONOMY_LEVEL = 3
    python autonomy.py

Nothing in uno_q/ is modified. This module subclasses TripleD and injects a
VETO_WINDOW pseudo-state between AUTHORIZING and DEFEATING. All other states
delegate to the parent untouched.

L3 state flow:

  ... DECIDING -> AUTHORIZING -> [VETO_WINDOW] -> DEFEATING -> COOLDOWN ...
                                        ^
                                  system pre-approves the full response plan;
                                  operator has VETO_WINDOW_S seconds to narrow it

Operator controls inside the veto window:
  1 / 2 / 3  + Enter   toggle veto on that specific action (can un-veto too)
  a          + Enter   veto ALL actions (stand down entirely)
  blank Enter          approve whatever remains, fire immediately
  (timeout)            system fires everything that was not vetoed
"""

import select
import sys
import time

import config
from main import TripleD, State, RESPONSE_PLAN

# ---------------------------------------------------------------------------
# Tunable — add VETO_WINDOW_S to config.py to override the default here.
# ---------------------------------------------------------------------------
_VETO_WINDOW_S: float = getattr(config, "VETO_WINDOW_S", 5.0)

# Sentinel object used as the VETO_WINDOW pseudo-state.
# A plain object (not part of main.State) so main.py stays byte-for-byte
# identical — State.AUTHORIZING comparisons in the parent still work.
_VETO = object()

_HARMLESS = {"DISTRACT"}   # mirrors human.py's split; luring hurts no one


# ---------------------------------------------------------------------------
# Veto window UI
# ---------------------------------------------------------------------------

def _print_menu(pending: list, veto_map: dict, vetoed: set) -> None:
    print("  Pending actions (fire unless vetoed):")
    for key, action in veto_map.items():
        tag  = "VETOED   " if action in vetoed else "WILL FIRE"
        risk = "harmless" if action in _HARMLESS else "HARMFUL "
        print(f"    [{key}] {action:<10}  {risk}  {tag}")
    live = [a for a in pending if a not in vetoed] or ["nothing"]
    print(f"  Will auto-fire: {live}")
    print(f"  [number]+Enter=toggle veto  [a]+Enter=veto all  Enter=fire now")


def collect_vetos(pending: list, verdict, timeout_s: float) -> set:
    """Present the veto window; return the set of action names the operator vetoed."""
    veto_map = {str(i + 1): a for i, a in enumerate(pending)}
    vetoed: set = set()

    print(f"\n{'=' * 62}")
    print(f"[L3 VETO WINDOW]  score={verdict.score:.2f}  payload={verdict.payload}")
    for r in verdict.reasons:
        print(f"  ! {r}")
    print(f"  System fires in {timeout_s:.0f}s — intervene now or stand by.\n")
    _print_menu(pending, veto_map, vetoed)
    print()

    deadline = time.time() + timeout_s

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            print("\n[L3] Window closed — executing.")
            break

        print(f"\r  {remaining:4.1f}s  > ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], min(0.25, remaining))

        if not ready:
            continue

        line = sys.stdin.readline().strip().lower()

        if line == "":
            print("[operator] approved — executing now")
            break

        if line == "a":
            vetoed = set(pending)
            print("[operator] ALL VETOED — standing down")
            break

        if line in veto_map:
            action = veto_map[line]
            if action in vetoed:
                vetoed.discard(action)
                print(f"[operator] un-vetoed '{action}'")
            else:
                vetoed.add(action)
                print(f"[operator] vetoed '{action}'")
            if vetoed == set(pending):
                print("[operator] all actions vetoed — standing down")
                break
            print()
            _print_menu(pending, veto_map, vetoed)
            print()
        else:
            valid = list(veto_map) + ["a", "<Enter>"]
            print(f"[operator] unknown input '{line}' — valid: {valid}")

    print(f"{'=' * 62}\n")
    return vetoed


# ---------------------------------------------------------------------------
# L3-aware subclass
# ---------------------------------------------------------------------------

class L3TripleD(TripleD):
    """TripleD with a VETO_WINDOW pseudo-state injected after AUTHORIZING.

    step() intercepts exactly two cases:
      - AUTHORIZING  : pre-approve the full plan, transition to _VETO sentinel
      - _VETO        : run the veto window, filter authorized list, go to DEFEATING
    Everything else (IDLE, DECIDING, DEFEATING, COOLDOWN) falls through to super().
    """

    def step(self, telem, contact):
        if self.state is _VETO:
            self._handle_veto_window()
        elif self.state is State.AUTHORIZING:
            self.authorized = list(RESPONSE_PLAN)
            print(f"[L3] pre-approved: {self.authorized} — opening veto window")
            self.state = _VETO
        else:
            super().step(telem, contact)

    def _handle_veto_window(self):
        vetoed = collect_vetos(self.authorized, self.verdict, _VETO_WINDOW_S)
        if vetoed:
            print(f"[operator] vetoed: {', '.join(sorted(vetoed))}")
        self.authorized = [a for a in self.authorized if a not in vetoed]
        self.state = State.DEFEATING


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if config.AUTONOMY_LEVEL != 3:
        print(f"[autonomy] WARNING: AUTONOMY_LEVEL={config.AUTONOMY_LEVEL} in config.py "
              f"(expected 3). Set it to 3 for human-on-the-loop behaviour.")
<<<<<<< HEAD
    L3TripleD().run()
=======
    L3TripleD().run()
>>>>>>> 38886bff9f8b3bd993679991d5d2097ff321428a
