"""autonomy.py — L3 (human-on-the-loop) controller for Triple D.

WHAT IS L3?
===========
L3 is the responsible-autonomy level where the system acts on a pre-approved
plan and the human's job is to watch and intervene if something is wrong.

This is the opposite of L2 (human-IN-the-loop), where the human must
explicitly approve every action before it fires. Under time pressure — a
closing threat at 10 m/s leaves ~8 seconds from detection to impact at
tabletop-arena scale — that prompt IS the bottleneck. L3 flips the default:
the system fires the pre-approved plan unless you stop it. The operator's
cognitive load drops from "approve everything" to "watch and stop if wrong."

This matches how effective defense operators actually work in doctrine. NATO
STANAG 4586 and UK JCNSS guidance on autonomous weapon systems both describe
this pattern: automated sensing + recommendation, with a human who retains
meaningful override within a defined intervention window.


L3 STATE FLOW
=============

                         main.py states
                         ──────────────
    IDLE ──(contact)──▶ DECIDING
                             │
                             │ (HOSTILE verdict)
                             ▼
                        AUTHORIZING  ◀── TripleD normally gates each action
                             │           here via human.authorize(). L3TripleD
                             │           intercepts this state instead.
                             │
                       [pre-approval]  ◀── system approves full RESPONSE_PLAN
                             │             instantly, no human required yet
                             ▼
                ┌─────── _VETO ───────────────────────────────────────────┐
                │                                                          │
                │   Operator sees:                                         │
                │     • verdict score and all DECIDE reasons               │
                │     • full plan colour-coded by risk level               │
                │     • live countdown bar                                 │
                │     • per-action veto toggles [1][2][3]                 │
                │     • "veto all" shortcut [a]                           │
                │     • "fire now" shortcut [Enter]                       │
                │                                                          │
                │   Window closes on: timeout | Enter | all vetoed        │
                └─────────────────────────────────────────────────────────┘
                             │
                             │ (filtered plan)
                             ▼
                        DEFEATING  ──▶  COOLDOWN  ──▶  IDLE


ETHICAL DESIGN
==============
DISTRACT is HARMLESS: luring a drone to empty space hurts no one. It fires
first in RESPONSE_PLAN and is highlighted green. It can be safely automated
even further at L4.

DAZZLE and DEFEND are HARMFUL: defeating a sensor or tracking a vehicle is
a use-of-force decision. They are highlighted red in the veto window. Even
at L3, the operator has clear visual differentiation so they know exactly
what they are (or are not) allowing.

A minimum hold time (MIN_VETO_S in config.py, default 1.5 s) is enforced
even if the operator presses Enter, so no accidental fire is possible from
a stray keystroke at the moment the window opens.

Every operator decision — veto, un-veto, early fire, full timeout — is
written to a tamper-evident JSONL audit log. In a real deployment this log
would be cryptographically signed and shipped to a chain-of-custody store.
For the hackathon it is the artefact that proves responsible-autonomy design
to judges and Bow Capital alike.


JOYSTICK SUPPORT
================
Set USE_JOYSTICK = True in config.py. The ModulinoJoystick class talks to
the Arduino Modulino Joystick over Qwiic/I2C using smbus2. If smbus2 is
absent or the device is not found, the class raises _JoystickUnavailable
and the system falls back to keyboard silently — no crash, no reconfigure.

    Joystick click          → approve (same as Enter)
    Joystick tilt down      → next action (highlight for veto)  [stretch]
    Joystick tilt up        → previous action                   [stretch]
    Long-hold (>1 s)        → veto all (same as 'a')            [stretch]

The click-to-approve mapping works today with zero extra code. The
navigation stretch goals are marked in _read_joystick().


AUDIT LOG FORMAT
================
JSONL — one JSON object per line, timezone-aware ISO-8601 timestamps.

    {"ts": "2026-05-30T...", "event": "session_start",   "autonomy_level": 3, ...}
    {"ts": "...",            "event": "veto_window_open", "score": 0.82, ...}
    {"ts": "...",            "event": "operator_vetoed",  "action": "DAZZLE", ...}
    {"ts": "...",            "event": "defeat_plan_final","authorized": [...], ...}
    {"ts": "...",            "event": "session_end"}

Events: session_start · plan_pre_approved · veto_window_open ·
        operator_vetoed · operator_unvetoed · operator_vetoed_all ·
        operator_vetoed_all_via_toggles · operator_approved_early ·
        veto_window_timeout · defeat_plan_final · session_end


USAGE
=====
    # Set in config.py: AUTONOMY_LEVEL = 3
    python autonomy.py

    # Replay the audit log after a session:
    python autonomy.py --replay

    # Import directly:
    from autonomy import L3TripleD
    L3TripleD().run()


DEPENDENCIES
============
Standard library only for keyboard + mock mode. Optional:
    smbus2          — Modulino Joystick over I2C
    (already in requirements.txt: pyserial — for live car)
"""

from __future__ import annotations

import json
import logging
import select
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config
from main import TripleD, State, RESPONSE_PLAN


# ---------------------------------------------------------------------------
# Module-level tunables — all overridable from config.py
# ---------------------------------------------------------------------------
_VETO_WINDOW_S: float = getattr(config, "VETO_WINDOW_S",    5.0)
_MIN_VETO_S:    float = getattr(config, "MIN_VETO_S",        1.5)
_AUDIT_LOG:     Path  = Path(getattr(config, "AUDIT_LOG_PATH", "triple_d_audit.jsonl"))
_USE_COLOR:     bool  = getattr(config, "USE_COLOR",         sys.stdout.isatty())
_JOYSTICK_BUS:  int   = getattr(config, "JOYSTICK_I2C_BUS",  1)

# Sentinel: the VETO_WINDOW pseudo-state.
# Plain object (not a State member) so main.py's `is State.X` checks are
# completely unaffected — main.py stays byte-for-byte unchanged.
_VETO = object()


# ---------------------------------------------------------------------------
# Risk classification — mirrors human.py's HARMLESS/HARMFUL split
# ---------------------------------------------------------------------------
RISK: dict[str, str] = {
    "DISTRACT": "HARMLESS",   # luring a drone to empty space hurts no one
    "DAZZLE":   "HARMFUL",    # blinding a seeker is a use-of-force decision
    "DEFEND":   "HARMFUL",    # tracking/intercepting is a use-of-force decision
}


# ---------------------------------------------------------------------------
# ANSI colour helpers — zero third-party dependencies
# ---------------------------------------------------------------------------
_ANSI: dict[str, str] = {
    "reset":  "\033[0m",
    "bold":   "\033[1m",
    "dim":    "\033[2m",
    "red":    "\033[31m",
    "green":  "\033[32m",
    "yellow": "\033[33m",
    "cyan":   "\033[36m",
    "white":  "\033[37m",
    "bred":   "\033[91m",
}


def _c(text: str, *names: str) -> str:
    """Wrap text in ANSI codes. No-op if colour is disabled or not a TTY."""
    if not _USE_COLOR:
        return text
    codes = "".join(_ANSI.get(n, "") for n in names)
    return codes + text + _ANSI["reset"]


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def _audit(event_type: str, **payload) -> None:
    """Append one JSONL event record to the audit log.

    Silently warns on I/O failure — the pipeline must never crash because
    the audit log is unavailable. In production, swap for a proper logging
    backend (syslog, signed append-only store, etc.).
    """
    record = {
        "ts":    datetime.now(timezone.utc).isoformat(),
        "event": event_type,
    }
    record.update({k: v for k, v in payload.items() if v is not None})
    try:
        with _AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        logging.warning("[L3 audit] write failed — %s", exc)


# ---------------------------------------------------------------------------
# Modulino Joystick (optional hardware; graceful fallback if absent)
# ---------------------------------------------------------------------------

class _JoystickUnavailable(Exception):
    pass


class ModulinoJoystick:
    """Arduino Modulino Joystick over Qwiic / I2C (smbus2).

    I2C address: 0x20 (default Modulino Joystick address).
    Register map (Modulino firmware v1.x):
        0x00 : X axis  (int8, centre = 0, right = +)
        0x01 : Y axis  (int8, centre = 0, up = +)
        0x02 : button  (uint8, bit 0 = pressed)

    Raises _JoystickUnavailable on __init__ if smbus2 is missing or the
    device does not respond — caller falls back to keyboard automatically.
    """
    I2C_ADDR = 0x20
    REG_X    = 0x00
    REG_Y    = 0x01
    REG_BTN  = 0x02
    _LONG_HOLD_S = 1.0   # seconds of continuous press → "veto all" gesture

    def __init__(self, bus_num: int = 1):
        try:
            import smbus2  # noqa: PLC0415
            self._bus = smbus2.SMBus(bus_num)
            self._bus.read_byte_data(self.I2C_ADDR, self.REG_BTN)  # ping
        except ImportError:
            raise _JoystickUnavailable("smbus2 not installed")
        except OSError as exc:
            raise _JoystickUnavailable(
                f"Modulino not found on I2C bus {bus_num}: {exc}")
        self._press_since: Optional[float] = None

    def read_raw(self) -> tuple[int, int, bool]:
        """Return (x, y, button_pressed).  x/y in [-127, 127]."""
        x   = self._bus.read_byte_data(self.I2C_ADDR, self.REG_X)
        y   = self._bus.read_byte_data(self.I2C_ADDR, self.REG_Y)
        btn = self._bus.read_byte_data(self.I2C_ADDR, self.REG_BTN)
        x = x if x < 128 else x - 256
        y = y if y < 128 else y - 256
        return x, y, bool(btn & 0x01)

    def gesture(self) -> Optional[str]:
        """Return a high-level gesture string, or None if nothing actionable.

        Gestures:
            "click"     short button press and release
            "long"      button held for > LONG_HOLD_S seconds
            "down"      joystick tilted significantly downward (Y < -48)
            "up"        joystick tilted significantly upward   (Y >  48)
        """
        try:
            _, y, pressed = self.read_raw()
        except OSError:
            return None

        now = time.time()
        if pressed:
            if self._press_since is None:
                self._press_since = now
            elif now - self._press_since >= self._LONG_HOLD_S:
                self._press_since = None
                return "long"
        else:
            if self._press_since is not None:
                self._press_since = None
                return "click"

        if y < -48:
            return "down"
        if y > 48:
            return "up"
        return None

    def close(self):
        try:
            self._bus.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Terminal UI helpers
# ---------------------------------------------------------------------------

def _divider(char: str = "─", width: int = 64) -> str:
    return _c(char * width, "dim")


def _progress_bar(frac: float, width: int = 24) -> str:
    """Unicode progress bar that turns from green → yellow → red as it drains."""
    frac   = max(0.0, min(1.0, frac))
    filled = int(width * frac)
    bar    = "█" * filled + "░" * (width - filled)
    color  = "green" if frac > 0.55 else "yellow" if frac > 0.22 else "bred"
    return _c(bar, color)


def _render_header(verdict, timeout_s: float) -> None:
    score_col = "bred" if verdict.score >= 0.8 else "yellow" if verdict.score >= 0.6 else "white"
    print()
    print(_divider("═"))
    print(f"  {_c('L3 VETO WINDOW', 'bold', 'cyan')}   "
          f"score {_c(f'{verdict.score:.2f}', 'bold', score_col)}   "
          f"payload: {_c(verdict.payload, 'bold')}")
    print(_divider())
    for r in verdict.reasons:
        print(f"  {_c('▸', 'yellow')}  {r}")
    print(_divider())
    print(f"  {_c('System fires in', 'dim')} "
          f"{_c(f'{timeout_s:.0f}s', 'bold', 'yellow')}"
          f"{_c(' — intervene now or stand by.', 'dim')}")


def _render_actions(pending: list[str],
                    veto_map: dict[str, str],
                    vetoed: set[str],
                    highlight: Optional[str] = None) -> None:
    """Render the action rows.  highlight marks the cursor row for joystick nav."""
    print()
    print(_c("  Pending actions:", "dim"))
    for key, action in veto_map.items():
        risk    = RISK.get(action, "UNKNOWN")
        is_veto = action in vetoed
        is_hi   = action == highlight

        cursor = _c("▶", "cyan", "bold") if is_hi else " "

        if is_veto:
            a_str  = _c(f"  {action:<10}", "dim")
            r_str  = _c(f"[{risk:<8}]", "dim")
            st_str = _c("VETOED   ", "dim")
        elif risk == "HARMFUL":
            a_str  = _c(f"  {action:<10}", "bred")
            r_str  = _c(f"[{risk:<8}]", "bred")
            st_str = _c("WILL FIRE", "bold", "bred")
        else:
            a_str  = _c(f"  {action:<10}", "green")
            r_str  = _c(f"[{risk:<8}]", "green")
            st_str = _c("WILL FIRE", "bold", "green")

        print(f"  {cursor} {_c(f'[{key}]', 'bold')}  {a_str}  {r_str}  {st_str}")

    live = [a for a in pending if a not in vetoed]
    print()
    if live:
        print(f"  {_c('Auto-fires:', 'dim')} "
              f"{_c(', '.join(live), 'bold')}")
    else:
        print(f"  {_c('STANDING DOWN', 'bold', 'yellow')}"
              f"{_c(' — all actions vetoed.', 'dim')}")


def _render_controls(has_joystick: bool = False) -> None:
    kb = (f"  {_c('[number]', 'bold')} toggle veto  "
          f"{_c('[a]', 'bold')} veto ALL  "
          f"{_c('[Enter]', 'bold')} fire now  "
          f"{_c('[?]', 'dim')} redraw")
    jy = (f"  {_c('joystick:', 'dim')} "
          f"{_c('click', 'bold')}=approve  "
          f"{_c('long-hold', 'bold')}=veto all")
    print()
    print(kb)
    if has_joystick:
        print(jy)
    print()


# ---------------------------------------------------------------------------
# Input reading — keyboard and joystick unified
# ---------------------------------------------------------------------------

def _read_input(joystick: Optional[ModulinoJoystick],
                timeout: float) -> Optional[str]:
    """Non-blocking read from keyboard (and joystick if present).

    Returns a stripped lowercase string, or None on timeout.
    Joystick gestures are mapped to equivalent keyboard strings:
        click → "" (approve / fire now)
        long  → "a" (veto all)
        down  → "<down>" (navigate — for future highlight logic)
        up    → "<up>"
    """
    if joystick is not None:
        gesture = joystick.gesture()
        if gesture == "click":
            return ""
        if gesture == "long":
            return "a"
        if gesture in ("down", "up"):
            return f"<{gesture}>"

    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    return sys.stdin.readline().strip().lower()


# ---------------------------------------------------------------------------
# Veto window — the ethical core of L3
# ---------------------------------------------------------------------------

def collect_vetos(pending: list[str],
                  verdict,
                  timeout_s: float,
                  joystick: Optional[ModulinoJoystick] = None) -> set[str]:
    """Present the veto window; return the set of action names operator vetoed.

    The window enforces a minimum open time of MIN_VETO_S seconds even if
    the operator presses Enter immediately, preventing accidental fires from
    a keystroke that lands the instant the window opens.

    Args:
        pending:   ordered list of action names to consider (e.g. RESPONSE_PLAN)
        verdict:   the Verdict from decide.assess()
        timeout_s: how long the window stays open before auto-firing
        joystick:  optional ModulinoJoystick; keyboard used when None

    Returns:
        set of action names the operator blocked; caller filters them out
    """
    veto_map:     dict[str, str]   = {str(i + 1): a for i, a in enumerate(pending)}
    vetoed:       set[str]         = set()
    open_time:    float            = time.time()
    deadline:     float            = open_time + timeout_s
    highlight:    Optional[str]    = None   # joystick cursor (stretch goal)

    _render_header(verdict, timeout_s)
    _render_actions(pending, veto_map, vetoed, highlight)
    _render_controls(has_joystick=joystick is not None)

    _audit("veto_window_open",
           score=verdict.score,
           payload=verdict.payload,
           pending=pending,
           timeout_s=timeout_s)

    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            print(f"\n  {_c('[L3]', 'bold', 'cyan')} Window closed — executing.")
            _audit("veto_window_timeout", vetoed=sorted(vetoed))
            break

        bar = _progress_bar(remaining / timeout_s)
        print(f"\r  {_c(f'{remaining:4.1f}s', 'bold', 'yellow')} [{bar}]  > ",
              end="", flush=True)

        line = _read_input(joystick, min(0.10, remaining))
        if line is None:
            continue

        # ── [Enter] — approve / fire now ────────────────────────────────────
        if line == "":
            elapsed = time.time() - open_time
            if elapsed < _MIN_VETO_S:
                gap = _MIN_VETO_S - elapsed
                print(f"\n  {_c('[L3]', 'cyan')} Minimum hold "
                      f"({_MIN_VETO_S:.1f}s) — auto-fires in "
                      f"{_c(f'{gap:.1f}s', 'yellow')} …")
                time.sleep(gap)
            print(f"\n  {_c('[operator]', 'bold')} approved — executing.")
            _audit("operator_approved_early",
                   elapsed_s=round(time.time() - open_time, 2),
                   vetoed=sorted(vetoed))
            break

        # ── [a] — veto everything ────────────────────────────────────────────
        if line == "a":
            vetoed = set(pending)
            print(f"\n  {_c('[operator]', 'bold')} "
                  f"{_c('ALL VETOED', 'bold', 'yellow')} — standing down.")
            _audit("operator_vetoed_all",
                   elapsed_s=round(time.time() - open_time, 2))
            break

        # ── [?] — redraw the menu ────────────────────────────────────────────
        if line == "?":
            print()
            _render_actions(pending, veto_map, vetoed, highlight)
            _render_controls(has_joystick=joystick is not None)
            continue

        # ── joystick navigation (stretch) ────────────────────────────────────
        if line in ("<down>", "<up>"):
            keys = list(veto_map)
            if highlight is None:
                highlight = veto_map[keys[0]]
            else:
                cur_key = next((k for k, v in veto_map.items() if v == highlight), keys[0])
                idx = keys.index(cur_key)
                idx = (idx + (1 if line == "<down>" else -1)) % len(keys)
                highlight = veto_map[keys[idx]]
            print()
            _render_actions(pending, veto_map, vetoed, highlight)
            _render_controls(has_joystick=True)
            continue

        # ── [1/2/3] — toggle veto on a specific action ──────────────────────
        if line in veto_map:
            action = veto_map[line]
            if action in vetoed:
                vetoed.discard(action)
                print(f"\n  {_c('[operator]', 'bold')} "
                      f"un-vetoed {_c(action, 'green', 'bold')}")
                _audit("operator_unvetoed",
                       action=action,
                       elapsed_s=round(time.time() - open_time, 2))
            else:
                vetoed.add(action)
                print(f"\n  {_c('[operator]', 'bold')} "
                      f"vetoed {_c(action, 'bred', 'bold')}")
                _audit("operator_vetoed",
                       action=action,
                       risk=RISK.get(action),
                       elapsed_s=round(time.time() - open_time, 2))

            if vetoed == set(pending):
                print(f"  {_c('[L3]', 'cyan')} All actions vetoed — standing down.")
                _audit("operator_vetoed_all_via_toggles",
                       elapsed_s=round(time.time() - open_time, 2))
                break

            _render_actions(pending, veto_map, vetoed, highlight)
            _render_controls(has_joystick=joystick is not None)
            continue

        # ── Unknown ──────────────────────────────────────────────────────────
        valid = sorted(veto_map) + ["a", "Enter", "?"]
        print(f"\n  {_c('[?]', 'yellow')} Unknown: {line!r}   valid: {valid}")

    print(_divider("═"))
    print()
    return vetoed


# ---------------------------------------------------------------------------
# L3-aware subclass of TripleD
# ---------------------------------------------------------------------------

class L3TripleD(TripleD):
    """TripleD with a VETO_WINDOW pseudo-state injected after AUTHORIZING.

    Overrides step() to intercept exactly two state transitions:
      State.AUTHORIZING → pre-approves the full plan, moves to _VETO
      _VETO             → runs the veto window, filters plan, moves to DEFEATING

    Every other state (IDLE, DECIDING, COOLDOWN, DEFEATING) falls through to
    super().step() unchanged. main.py's state machine is never modified.
    """

    def __init__(self):
        super().__init__()
        self._joystick: Optional[ModulinoJoystick] = None

        if getattr(config, "USE_JOYSTICK", False):
            try:
                self._joystick = ModulinoJoystick(bus_num=_JOYSTICK_BUS)
                print(f"{_c('[L3]', 'cyan', 'bold')} Modulino Joystick online "
                      f"(I2C bus {_JOYSTICK_BUS})")
            except _JoystickUnavailable as exc:
                print(f"{_c('[L3]', 'yellow')} Joystick unavailable ({exc}) — "
                      f"falling back to keyboard")

        _audit("session_start",
               autonomy_level=config.AUTONOMY_LEVEL,
               veto_window_s=_VETO_WINDOW_S,
               min_veto_s=_MIN_VETO_S,
               mock_serial=config.MOCK_SERIAL,
               mock_vision=config.MOCK_VISION,
               joystick=self._joystick is not None,
               audit_log=str(_AUDIT_LOG.resolve()))

    # ------------------------------------------------------------------ run()

    def run(self):
        """Delegate to parent; clean up joystick and write session_end on exit."""
        try:
            super().run()
        finally:
            if self._joystick is not None:
                self._joystick.close()
            _audit("session_end")

    # ------------------------------------------------------------------ step()

    def step(self, telem, contact):
        """Intercept AUTHORIZING and _VETO; delegate everything else to super."""
        if self.state is State.AUTHORIZING:
            self._enter_veto_phase()
        elif self.state is _VETO:
            self._run_veto_window()
        else:
            super().step(telem, contact)

    # ---------------------------------------------------------- private helpers

    def _enter_veto_phase(self) -> None:
        """Pre-approve the full RESPONSE_PLAN and open the veto window.

        Pre-approval is the defining feature of L3. The system trusts its
        own verdict enough to commit to a plan; the operator's role is to
        narrow that plan within the window, not to rubber-stamp each line.
        If the plan is wrong, the operator can veto everything — but the
        default is to act, not to wait.
        """
        self.authorized = list(RESPONSE_PLAN)

        harmless = [a for a in self.authorized if RISK.get(a) == "HARMLESS"]
        harmful  = [a for a in self.authorized if RISK.get(a) == "HARMFUL"]

        print(f"\n{_c('[L3]', 'bold', 'cyan')} Pre-approved full response plan:")
        if harmless:
            print(f"  {_c('HARMLESS', 'green', 'bold')} — {', '.join(harmless)}")
        if harmful:
            print(f"  {_c('HARMFUL ', 'bred', 'bold')}  — {', '.join(harmful)}")
        print(f"{_c('[L3]', 'bold', 'cyan')} Opening veto window …")

        _audit("plan_pre_approved",
               plan=self.authorized,
               verdict_score=self.verdict.score,
               verdict_payload=self.verdict.payload)

        self.state = _VETO

    def _run_veto_window(self) -> None:
        """Run the veto window, filter the authorized list, go to DEFEATING."""
        vetoed = collect_vetos(
            self.authorized,
            self.verdict,
            _VETO_WINDOW_S,
            joystick=self._joystick,
        )

        if vetoed:
            print(f"  {_c('[operator]', 'bold')} final vetoes: "
                  f"{_c(', '.join(sorted(vetoed)), 'bred')}")

        self.authorized = [a for a in self.authorized if a not in vetoed]

        if self.authorized:
            print(f"  {_c('[L3]', 'bold', 'cyan')} executing: "
                  f"{_c(', '.join(self.authorized), 'green', 'bold')}")
        else:
            print(f"  {_c('[L3]', 'bold', 'cyan')} standing down — all actions vetoed.")

        _audit("defeat_plan_final",
               authorized=self.authorized,
               vetoed=sorted(vetoed))

        self.state = State.DEFEATING


# ---------------------------------------------------------------------------
# Audit log replay — print a human-readable session summary
# ---------------------------------------------------------------------------

def replay_audit_log(path: Path) -> None:
    """Read the JSONL audit log and print a human-readable summary.

    Groups events by session (session_start … session_end pairs) and prints
    each operator decision with elapsed time and risk level.
    """
    if not path.exists():
        print(f"No audit log found at {path}")
        return

    events: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    if not events:
        print("Audit log is empty.")
        return

    # Split into sessions
    sessions: list[list[dict]] = []
    current:  list[dict]       = []
    for ev in events:
        current.append(ev)
        if ev["event"] == "session_end":
            sessions.append(current)
            current = []
    if current:
        sessions.append(current)

    print(_divider("═"))
    print(f"  {_c('AUDIT LOG REPLAY', 'bold', 'cyan')}  "
          f"{path.resolve()}")
    print(_divider("═"))

    for s_idx, session in enumerate(sessions, 1):
        start_ev = next((e for e in session if e["event"] == "session_start"), {})
        print(f"\n  {_c(f'Session {s_idx}', 'bold')}  "
              f"started {start_ev.get('ts', '?')}  "
              f"L{start_ev.get('autonomy_level', '?')}")
        print(_divider())

        for ev in session:
            etype = ev["event"]

            if etype == "veto_window_open":
                print(f"  {_c('▸ window opened', 'cyan')}  "
                      f"score={ev.get('score')}  "
                      f"payload={ev.get('payload')}  "
                      f"timeout={ev.get('timeout_s')}s")
                print(f"    pending: {ev.get('pending')}")

            elif etype == "operator_vetoed":
                risk = RISK.get(ev.get("action", ""), "?")
                rc   = "bred" if risk == "HARMFUL" else "green"
                print(f"  {_c('✗ vetoed', 'bred')}       "
                      f"{_c(ev.get('action', '?'), 'bold', rc)}  "
                      f"[{risk}]  "
                      f"+{ev.get('elapsed_s', '?')}s")

            elif etype == "operator_unvetoed":
                print(f"  {_c('↩ un-vetoed', 'green')}    "
                      f"{_c(ev.get('action', '?'), 'bold', 'green')}  "
                      f"+{ev.get('elapsed_s', '?')}s")

            elif etype in ("operator_vetoed_all",
                           "operator_vetoed_all_via_toggles"):
                print(f"  {_c('✗ VETO ALL', 'bold', 'yellow')}      "
                      f"standing down  "
                      f"+{ev.get('elapsed_s', '?')}s")

            elif etype == "operator_approved_early":
                print(f"  {_c('✓ approved early', 'green')}  "
                      f"vetoed={ev.get('vetoed')}  "
                      f"+{ev.get('elapsed_s', '?')}s")

            elif etype == "veto_window_timeout":
                print(f"  {_c('⌛ timeout', 'yellow')}        "
                      f"auto-fired  "
                      f"vetoed={ev.get('vetoed')}")

            elif etype == "defeat_plan_final":
                auth = ev.get("authorized", [])
                veto = ev.get("vetoed", [])
                print(f"  {_c('→ fired', 'bold')}          "
                      f"{_c(', '.join(auth) or 'nothing', 'bold', 'green' if auth else 'yellow')}")
                if veto:
                    print(f"    blocked: {_c(', '.join(veto), 'dim')}")

        print()

    print(_divider("═"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_BANNER = r"""
  ╔══════════════════════════════════════════════════════════════╗
  ║          Triple D  ·  L3 Human-on-the-Loop                  ║
  ║                                                              ║
  ║  The system acts on a pre-approved plan.                     ║
  ║  You have a countdown window to veto what you don't want.    ║
  ║  Harmful actions are shown in red.                           ║
  ║  Every decision is written to the audit log.                 ║
  ╚══════════════════════════════════════════════════════════════╝
"""

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Triple D — L3 human-on-the-loop controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--replay", action="store_true",
        help="Print a human-readable summary of the audit log and exit.",
    )
    args = parser.parse_args()

    if args.replay:
        replay_audit_log(_AUDIT_LOG)
        sys.exit(0)

    # --- startup banner ---
    if _USE_COLOR:
        print(_c(_BANNER, "cyan"))
    else:
        print(_BANNER)

    if config.AUTONOMY_LEVEL != 3:
        print(
            _c(f"[WARNING] AUTONOMY_LEVEL={config.AUTONOMY_LEVEL} in config.py "
               f"(expected 3). L3 behaviour is active regardless, but the "
               f"dial and the runner are inconsistent.", "yellow"),
            file=sys.stderr,
        )
        print(file=sys.stderr)

    print(f"  {_c('Veto window:', 'dim')}  "
          f"{_VETO_WINDOW_S:.0f}s  "
          f"(min hold {_MIN_VETO_S:.1f}s enforced)")
    print(f"  {_c('Joystick:',   'dim')}  "
          f"{'enabled — USE_JOYSTICK=True in config.py' if getattr(config, 'USE_JOYSTICK', False) else 'disabled — keyboard only'}")
    print(f"  {_c('Audit log:',  'dim')}  {_AUDIT_LOG.resolve()}")
    print(f"  {_c('Colour:',     'dim')}  {'on' if _USE_COLOR else 'off (not a TTY)'}")
    print(f"  {_c('Mock:',       'dim')}  "
          f"serial={'yes' if config.MOCK_SERIAL else 'no'}  "
          f"vision={'yes' if config.MOCK_VISION else 'no'}")
    print()

    L3TripleD().run()
