"""defeat.py - DEFEAT stage (autonomous).

Once IFF (iff.py) judges a detected drone a FOE, Triple D engages with NO human
in the loop. Two coordinated effects fire together:

  LASER_CUTOFF : a laser severs the drone's fiber-optic control tether. The aim
                 solution (decide.TrajectoryTracker.aim) places the cut BEHIND
                 the drone and sweeps PERPENDICULAR to its flight path -- that is
                 where the trailing fiber runs.
  DECOY        : a predefined decoy is triggered at a separate location to pull
                 any follow-on threat away from the protected asset.

Both effects here are PLACEHOLDERS. The real laser gimbal / decoy drive is part
of the hardware output defined elsewhere; this module only emits the command and
records intent so the rest of the pipeline can be exercised end to end.
"""


class Responder:
    def __init__(self, car):
        self.car = car
        self.active = set()

    # --- placeholders: real hardware drive is wired elsewhere ----------------

    def laser_cutoff(self, aim, on: bool = True):
        """PLACEHOLDER: fire/stop the fiber-cut laser at the aim solution."""
        if on:
            if aim is not None and aim.available:
                print(f"[defeat] LASER_CUTOFF -> cut behind drone at "
                      f"({aim.cutoff_xy[0]:.0f},{aim.cutoff_xy[1]:.0f}) px, "
                      f"sweep {aim.sweep_deg:.0f}deg perpendicular to "
                      f"heading {aim.heading_deg:.0f}deg")
            else:
                note = aim.note if aim is not None else "no aim solution"
                print(f"[defeat] LASER_CUTOFF -> firing on last-known bearing "
                      f"({note})")
            self.car.send("LASER_CUTOFF_ON")     # placeholder hardware command
            self.active.add("LASER_CUTOFF")
        else:
            self.car.send("LASER_CUTOFF_OFF")
            self.active.discard("LASER_CUTOFF")

    def decoy(self, on: bool = True):
        """PLACEHOLDER: trigger/clear the predefined decoy at its own location."""
        if on:
            print("[defeat] DECOY -> triggering predefined decoy (offset location)")
            self.car.send("DECOY_ON")            # placeholder hardware command
            self.active.add("DECOY")
        else:
            self.car.send("DECOY_OFF")
            self.active.discard("DECOY")

    # --- dispatch ------------------------------------------------------------

    def fire(self, action: str, aim=None):
        if action == "LASER_CUTOFF":
            self.laser_cutoff(aim, True)
        elif action == "DECOY":
            self.decoy(True)

    def all_off(self):
        self.car.send("ALL_OFF")
        self.laser_cutoff(None, False)
        self.decoy(False)
        self.active.clear()
