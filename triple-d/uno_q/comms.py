"""comms.py - the one wire between the two brains.

CarLink wraps the serial port to the UNO R3. It parses telemetry lines into
a dict and sends one-word ACTION commands back. In MOCK_SERIAL mode it
synthesizes a scripted threat so the whole pipeline runs with no hardware.
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
    can watch DETECT -> DECIDE -> DEFEAT fire end to end with no car."""
    def __init__(self):
        self.t0 = time.time()

    def telemetry(self):
        t = time.time() - self.t0
        if t < 3.0:                      # quiet room
            return {"amp": 30, "pitch": 300, "dist": 200, "line": 0}
        elif t < 9.0:                    # threat approaches, whine rises
            prog = (t - 3.0) / 6.0
            return {
                "amp":   int(40 + prog * 400),
                "pitch": int(800 + prog * 2600),
                "dist":  int(180 - prog * 150),
                "line":  0,
            }
        else:                            # threat has passed / been handled
            return {"amp": 30, "pitch": 300, "dist": 200, "line": 0}


class CarLink:
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

    def send(self, action: str):
        """Send one ACTION command (e.g. 'DISTRACT_ON')."""
        if self.mock:
            print(f"[comms:MOCK] -> CMD,{action}")
            return
        self.ser.write(f"CMD,{action}\n".encode())

    def close(self):
        if not self.mock:
            self.ser.close()
