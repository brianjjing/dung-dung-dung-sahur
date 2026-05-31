# Product

## Register

product

## Users

Hackathon judges and demo audiences watching a live counter-drone defense
demonstration, plus the operator running it. The judge is the primary viewer:
they stand a few feet back, watch for seconds at a time, and have to grasp what
the system is doing without narration. The operator runs `python main.py` and
watches the same screen to confirm the pipeline is alive and behaving.

The job to be done: in one glance, read where the system is in its
**DETECT → DECIDE → DEFEAT** pipeline, whether a contact is friend or foe, and
what action the system is taking, with enough confidence cues (amplitude, IFF,
link state) to trust the verdict.

## Product Purpose

Triple-D is a layered, non-kinetic defense against RF-silent (fiber-optic)
suicide drones. The dashboard (`frontend/ui.py`) is the single live operator
view: a full-screen canvas HUD served from a stdlib HTTP server on the Mac,
polling brain state ~20×/s. It renders one persistent scene — a bird's-eye detection
grid that is empty until a contact is confirmed, a camera window that wakes on
acoustic noise, and a permanent microphone listening indicator — and overlays
tracks, headings, IFF verdicts, the laser cut point, and the decoy state as the
pipeline advances.

Success: a judge watching the demo understands the threat story end to end
(noise heard → camera wakes → drone confirmed → friend/foe decided → effect
engaged) without anyone explaining it, and trusts that the on-screen state is
the real system state, not a scripted animation.

## Brand Personality

Calm authority. Controlled, precise, decisive. The system is in command; a foe
verdict reads as resolve, not panic. Three words: **composed, exact,
trustworthy.** The voice is instrument-grade and literal — it states what is
happening (TRACKING ENEMY DRONE, NOISE DETECTED, LASER · CUT) rather than
dramatizing it. Confidence comes from precision and restraint, never from
visual noise or alarm theater.

## Anti-references

- **Cluttered legacy C2 software.** No wall of tiny gray text, no 40-panel
  command-and-control density, no Windows-chrome boxes. The screen stays
  legible from across a room; every element earns its place.
- **Fictional / movie UI.** No Hollywood "hacker" decoration: no spinning
  hexagons, code rain, fake glyphs, or motion that means nothing. Every moving
  element maps to a real signal (live amplitude, an actual track, an active
  effect). Decoration that doesn't represent state is banned.

## Design Principles

- **Every pixel maps to state.** Nothing on screen is decorative-only. Glows,
  pulses, sweeps, and colors all encode a real value the brain published.
- **One persistent scene.** The detection grid is always the view; the camera
  and mic float over it. State changes reveal and recolor elements in place
  rather than swapping screens, so the operator never loses orientation.
- **Glanceable truth.** The core verdict (friend/foe, what state, link alive)
  reads in under a second from a distance. Detail (conf, amp, heading) is
  available but secondary.
- **Calm under threat.** Escalation is communicated through decisive color and
  measured pulse, not flashing chaos. The interface never panics.
- **Honest instrument.** The UI shows what the hardware actually does (track
  and lock, IR dazzle, decoy) and never implies capability it lacks. State on
  screen is the real pipeline state.

## Accessibility & Inclusion

No formal WCAG target for this demo build. Maintain strong legibility on the
near-black tactical background: bright foreground colors against `#05070a`,
text large enough to read from a few feet back, and the existing high-contrast
green/red/cyan/yellow signal palette. Friend/foe currently relies on hue plus a
text label (FRIENDLY / ENEMY), which keeps the verdict legible without color
alone; preserve that label-plus-color pairing in any future change.
