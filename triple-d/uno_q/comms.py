"""comms.py - the R3 sensor telemetry link.

SensorLink wraps the USB serial port to the UNO R3 sensor board on the car and
parses its telemetry lines into a dict. It is read-only: actuation (motors +
effects) goes to the UNO R4 WiFi board over Wi-Fi via car_client.py. In
MOCK_SERIAL mode it synthesizes a scripted threat so the whole pipeline runs
with no hardware.
"""
import time
import config

try:
    import serial  # pyserial
    _HAVE_SERIAL = True
except ImportError:
    _HAVE_SERIAL = False


def parse_telemetry(line: str):
    """'TEL,AMP:512,PITCH:2200,DIST:84,LINE:0' -> dict, or None if malformed."""
    line = line.strip()
    if not line.startswith("TEL,"):
        return None
    out = {}
    for field in line[4:].split(","):
        if ":" not in field:
            continue
        key, _, val = field.partition(":")
        try:
            out[key.lower()] = int(val)
        except ValueError:
            pass
    # require the core fields
    if {"amp", "pitch", "dist"} <= out.keys():
        return out
    return None


class _MockScenario:
    """Quietly idles, then ~3s in, ramps a closing high-pitched threat so you
    can watch DETECT -> DECIDE -> DEFEAT fire end to end with no car.

    The scenario REPEATS on a fixed cycle so the pipeline can be exercised over
    and over: detect a threat, re-arm, then detect the next one. The trailing
    quiet stretch is long enough for COOLDOWN_S to expire and the brain to
    return to IDLE before the next threat ramps in."""

    QUIET_S  = 3.0       # quiet room before a threat appears
    THREAT_S = 6.0       # threat approaches; whine rises and it closes in
    REARM_S  = 6.0       # quiet stretch after the threat: lets COOLDOWN clear
    CYCLE_S  = QUIET_S + THREAT_S + REARM_S

    def __init__(self):
        self.t0 = time.time()

    def telemetry(self):
        # phase within the current cycle, so the whole quiet->threat->quiet
        # sequence loops indefinitely instead of firing exactly once.
        phase = (time.time() - self.t0) % self.CYCLE_S
        if phase < self.QUIET_S:                         # quiet room
            return {"amp": 30, "pitch": 300, "dist": 200, "line": 0}
        elif phase < self.QUIET_S + self.THREAT_S:       # threat approaches
            prog = (phase - self.QUIET_S) / self.THREAT_S
            return {
                "amp":   int(40 + prog * 400),
                "pitch": int(800 + prog * 2600),
                "dist":  int(180 - prog * 150),
                "line":  0,
            }
        else:                                            # threat handled; re-arm
            return {"amp": 30, "pitch": 300, "dist": 200, "line": 0}


class SensorLink:
    """Serial telemetry from the R3 sensor board. Read-only: the R3 streams
    mic + ultrasonic telemetry up and receives no commands (actuation lives on
    the R4 WiFi board -- see car_client.py)."""
    def __init__(self):
        self.mock = config.MOCK_SERIAL or not _HAVE_SERIAL
        self._last = None
        if self.mock:
            self._scn = _MockScenario()
            why = "MOCK_SERIAL" if config.MOCK_SERIAL else "pyserial not installed"
            print(f"[comms] running in MOCK mode ({why}) - no car attached")
        else:
            self.ser = serial.Serial(config.SERIAL_PORT, config.BAUD, timeout=0)
            time.sleep(2.0)  # let the UNO reset after the port opens
            print(f"[comms] connected to {config.SERIAL_PORT} @ {config.BAUD}")

    def read_telemetry(self):
        """Return the most recent telemetry dict, or None if nothing new."""
        if self.mock:
            self._last = self._scn.telemetry()
            return self._last
        latest = None
        while self.ser.in_waiting:
            raw = self.ser.readline().decode(errors="ignore")
            parsed = parse_telemetry(raw)
            if parsed:
                latest = parsed
        if latest:
            self._last = latest
        return latest

    def close(self):
        if not self.mock:
            self.ser.close()
