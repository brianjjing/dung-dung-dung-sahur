# Triple D — Brand Guide

Defense-tech that's **calm, precise, and accountable** — command-console aesthetics,
dark-first, one decisive accent. The visual system mirrors the product ethic:
*the safe action automates; the harmful one always asks a human.*

---

## Logo

| File | Use |
|------|-----|
| `logo.svg` | Primary horizontal lockup (gradient mark) — slides, web, light/dark surfaces |
| `logo-mono.svg` | Single-color — laser-etch on the enclosure, stamps, watermarks, faxable docs |
| `banner.svg` | 1280×320 README / repo social banner |

**The mark** = three chevrons `〉〉〉` reading left-to-right as the pipeline
**D**etect → **D**ecide → **D**efeat, colored cyan → amber → red. The logo *is* the
system diagram. Don't recolor the chevrons; their order encodes the pipeline.

**Clear space:** keep at least the height of one chevron clear on all sides.
**Min width:** 120px (primary), 90px (mark only).

---

## Color

### Core
| Role | Name | Hex |
|------|------|-----|
| Base | Command Black | `#0B0F14` |
| Surface | Slate Panel | `#161C24` |
| Border / grid | Radar Line | `#2A3441` |
| Primary text | Signal White | `#E8EDF2` |
| **Accent** | **Defeat Amber** | `#FF9E2C` |
| Active / lock | Lock Cyan | `#27E0C8` |

### State semantics (map these to the `main.py` state machine)
| State | Name | Hex |
|-------|------|-----|
| Idle / safe | Standby Green | `#3FB950` |
| Detecting / deciding | Lock Cyan | `#27E0C8` |
| Authorizing (human gate) | Caution Amber | `#FF9E2C` |
| Hostile / defeating | Threat Red | `#F0503A` |
| Cooldown | Muted Blue-grey | `#5A6B7D` |

> **Rule:** Threat Red is reserved for confirmed-hostile / firing only — never for
> buttons or decoration. When the only red on screen means "a human is about to
> authorize," that restraint *is* the responsible-autonomy story.

---

## Typography
- **Display / wordmark:** Rajdhani (HUD feel) — alts: Chakra Petch, Space Grotesk
- **UI / body:** Inter — alt: IBM Plex Sans
- **Telemetry / data / serial logs:** JetBrains Mono — alt: IBM Plex Mono
  (you literally print `TEL,AMP:512,PITCH:2200...` — keep data monospace)

---

## Voice & taglines
- **Detect. Decide. Defeat.** — owns the pipeline
- **The safe action is automatic. The hard one asks you.** — leads with the ethics (best for judges)
- **Non-kinetic defense, human-accountable.**

Tone: understated, technical, honest. The README's "Honesty notes" section *is* the
brand voice — never oversell.

---

## Quick reference (copy/paste tokens)
```
--ddd-black:   #0B0F14;
--ddd-panel:   #161C24;
--ddd-line:    #2A3441;
--ddd-white:   #E8EDF2;
--ddd-amber:   #FF9E2C;   /* accent */
--ddd-cyan:    #27E0C8;   /* active/lock */
--ddd-green:   #3FB950;   /* idle/safe */
--ddd-red:     #F0503A;   /* hostile/firing only */
--ddd-muted:   #5A6B7D;   /* cooldown / secondary text */
```
