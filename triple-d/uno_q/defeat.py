"""defeat.py - DEFEAT stage (autonomous).

Once IFF (iff.py) judges a detected drone a FOE, Triple D engages with NO human
in the loop. Two coordinated effects fire together:

  LASER_CUTOFF : a laser severs the drone's fiber-optic control tether. The aim
                 solution (decide.TrajectoryTracker.aim) places the cut BEHIND
                 the drone and sweeps PERPENDICULAR to its flight path -- that is
                 where the trailing fiber runs.
  DECOY        : a predefined decoy is triggered at a separate location to pull
                 any follow-on threat away from the protected asset.

The DECOY head is driven on the real car: it triggers the UNO R4 WiFi board's
DISTRACT routine over Wi-Fi (car_client.deploy_decoy). LASER_CUTOFF stays a
PLACEHOLDER -- there is no R4 command for the laser gimbal yet, so this module
only records intent so the rest of the pipeline can be exercised end to end.
"""
import car_client


def _safe(fn, *args):
    """Run a car_client call; warn (don't crash) if the R4 is unreachable, so
    the pipeline still runs end to end with no car on the network."""
    try:
        fn(*args)
    except OSError as e:
        print(f"[defeat] R4 unreachable ({e}); car command dropped")


class Responder:
    def __init__(self):
        self.active = set()

    # --- effects -------------------------------------------------------------

    def laser_cutoff(self, aim, on: bool = True):
        """PLACEHOLDER: no R4 command for the laser gimbal yet -- record intent."""
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
            self.active.add("LASER_CUTOFF")
        else:
            self.active.discard("LASER_CUTOFF")

    def decoy(self, on: bool = True):
        """Trigger/clear the DISTRACT head on the R4 car over Wi-Fi."""
        if on:
            print("[defeat] DECOY -> R4 DISTRACT routine (Wi-Fi)")
            _safe(car_client.deploy_decoy)
            self.active.add("DECOY")
        else:
            _safe(car_client.stop_car)
            self.active.discard("DECOY")

    # --- dispatch ------------------------------------------------------------

    def fire(self, action: str, aim=None):
        if action == "LASER_CUTOFF":
            self.laser_cutoff(aim, True)
        elif action == "DECOY":
            self.decoy(True)

    def all_off(self):
        _safe(car_client.stop_car)
        self.laser_cutoff(None, False)
        self.decoy(False)
        self.active.clear()
