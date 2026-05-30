"""detect.py - DETECT stage (acoustic).

Consumes the AMP/PITCH features the UNO R3 already extracted from its mic and
decides whether a high prop-whine signature is sustained long enough to count
as a contact. Threshold + debounce on purpose: a hackathon-robust baseline you
can later swap for a trained classifier without touching the rest of the code.

UPGRADE PATH: to classify off the Uno Q's own webcam mic instead, read audio
with `sounddevice`, compute a mel-spectrogram, run a TFLite model, and have
update() consume that score. The interface below stays identical.
"""
import time
import config


class AcousticDetector:
    def __init__(self):
        self._since = None       # when the signature first appeared
        self.detected = False
        self.confidence = 0.0

    def update(self, telem: dict) -> bool:
        """Feed one telemetry frame; returns True while a contact is held."""
        if telem is None:
            return self.detected

        amp   = telem.get("amp", 0)
        pitch = telem.get("pitch", 0)
        lo, hi = config.PITCH_BAND

        loud_enough = amp >= config.AMP_FLOOR
        right_pitch = lo <= pitch <= hi
        signature   = loud_enough and right_pitch

        now = time.time()
        if signature:
            if self._since is None:
                self._since = now
            held = now - self._since
            self.detected = held >= config.DETECT_HOLD
            # crude confidence: how far above the floor + held duration
            amp_margin = min(1.0, (amp - config.AMP_FLOOR) / 400.0)
            self.confidence = round(min(1.0, 0.5 * amp_margin + 0.5 * min(1.0, held)), 2)
        else:
            self._since = None
            self.detected = False
            self.confidence = 0.0

        return self.detected
