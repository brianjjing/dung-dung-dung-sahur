"""operator.py - the human in the loop.

This is the ethical core: harmful actions are gated behind a human, and the
autonomy level decides which actions (if any) the system may take on its own.

  authorize(action, verdict) -> True/False

Default input is the keyboard (works today). Set USE_JOYSTICK=True to wire the
Modulino Joystick instead (stub provided).
"""
import select
import sys
import time
import config

# Actions split by whether they can harm anyone.
HARMLESS = {"DISTRACT"}            # luring a drone to empty space hurts no one
HARMFUL  = {"DAZZLE", "DEFEND"}    # actively defeating the threat's sensors


def _policy_auto_ok(action: str) -> bool:
    """Per the autonomy dial: may the system take THIS action without a human?"""
    lvl = config.AUTONOMY_LEVEL
    if lvl <= 2:
        return False                       # gate everything (recommend only)
    if lvl == 3:
        return True                        # acts; human may veto in the window
    if lvl == 4:
        return action in HARMLESS          # auto only the safe action
    if lvl >= 5:
        return True                        # swarm autonomy (demo carefully!)
    return False


def _keyboard_confirm(prompt: str, timeout: float) -> bool:
    print(prompt + f"  [y/N, {timeout:.0f}s] ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        print("\n[operator] timeout -> DENIED")
        return False
    ans = sys.stdin.readline().strip().lower()
    return ans == "y"


def _joystick_confirm(timeout: float) -> bool:
    # TODO: read Modulino Joystick over Qwiic/I2C (smbus2 or the Modulino lib).
    # Push-to-confirm; release/timeout -> deny. Falls back to keyboard for now.
    print("[operator] joystick not wired yet; using keyboard")
    return _keyboard_confirm("  press to AUTHORIZE", timeout)


def authorize(action: str, verdict) -> bool:
    """Return True if this action is cleared to fire."""
    if _policy_auto_ok(action):
        print(f"[operator] L{config.AUTONOMY_LEVEL}: '{action}' auto-authorized")
        if config.AUTONOMY_LEVEL == 3:     # human-on-the-loop veto window
            vetoed = _keyboard_confirm(
                f"  VETO '{action}'? (acts unless you confirm veto)",
                min(3.0, config.AUTH_TIMEOUT_S))
            return not vetoed
        return True

    # Otherwise a human must explicitly authorize.
    prompt = (f"[operator] AUTHORIZE '{action}'? "
              f"verdict=HOSTILE score={verdict.score} ({verdict.payload})")
    if config.USE_JOYSTICK:
        return _joystick_confirm(config.AUTH_TIMEOUT_S)
    return _keyboard_confirm(prompt, config.AUTH_TIMEOUT_S)
