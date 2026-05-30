"""defeat.py - DEFEAT stage (the three heads).

Translates an authorized verdict into ACTION commands on the wire. Holds the
current effect state so we can cleanly turn everything off afterwards.

  DISTRACT : decoy beacon + warm element on, car repositions to lure  (harmless)
  DAZZLE   : IR LED array floods the threat's camera                   (DISABLE)
  DEFEND   : track the tether with the laser gimbal -- TRACKING ONLY.
             We do NOT claim a fiber cut; severing is the roadmap. The honest
             demo is precision lock-and-track. (No command is sent that would
             imply otherwise.)
"""


class Responder:
    def __init__(self, car):
        self.car = car
        self.active = set()

    def distract(self, on: bool):
        self.car.send("DISTRACT_ON" if on else "DISTRACT_OFF")
        if on:
            # nudge the decoy away from the protected asset to pull the seeker
            self.car.send("DRIVE_L")
            self.active.add("DISTRACT")
        else:
            self.car.send("DRIVE_S")
            self.active.discard("DISTRACT")

    def dazzle(self, on: bool):
        self.car.send("DAZZLE_ON" if on else "DAZZLE_OFF")
        self.active.add("DAZZLE") if on else self.active.discard("DAZZLE")

    def defend_track(self, on: bool):
        # Tracking only: the gimbal aim loop lives on the Uno Q vision side.
        # Here we just log intent; no "cut" command exists by design.
        print(f"[defeat] DEFEND (tracking-only) {'engaged' if on else 'released'}")
        self.active.add("DEFEND") if on else self.active.discard("DEFEND")

    def fire(self, action: str):
        if action == "DISTRACT":
            self.distract(True)
        elif action == "DAZZLE":
            self.dazzle(True)
        elif action == "DEFEND":
            self.defend_track(True)

    def all_off(self):
        self.car.send("ALL_OFF")
        self.defend_track(False)
        self.active.clear()
