"""iff.py - autonomous Identify-Friend-or-Foe (the AUTONOMOUS decision).

After the vision stage confirms a drone, Triple D no longer asks a human what to
do. It decides defend-vs-stand-down on its own with a challenge/response over a
side-channel:

    Triple D  --challenge-->  drone
    drone     --shared key-->  Triple D      (only a friendly drone can)

A FRIENDLY drone is a second Arduino Uno preloaded with the same predefined
secret (config.IFF_SHARED_SECRET). When challenged it presents that key, so
Triple D stands down and does nothing. Anything that cannot present the key
-- wrong key, or silence past IFF_CHALLENGE_TIMEOUT_S -- is a FOE, and the
autonomous DEFEAT stage engages.

The comparison is constant-time (hmac.compare_digest) so the channel can't be
probed by timing. HARDENING PATH: swap the plain key presentation for a nonce +
HMAC(secret, nonce) handshake so the secret never crosses the wire; the friendly
Uno would then compute the HMAC instead of echoing the key.
"""
import hmac
import time
from dataclasses import dataclass

import config

try:
    import serial  # pyserial
    _HAVE_SERIAL = True
except ImportError:
    _HAVE_SERIAL = False


@dataclass
class IFFResult:
    friendly: bool
    detail: str


class IFFChallenger:
    """Challenges a detected drone for the shared key and rules friend or foe.

    In MOCK_IFF mode there is no second Uno on the wire: the reply is synthesized
    from config.MOCK_IFF_FRIENDLY so the whole decision path runs on a laptop.
    """

    def __init__(self):
        self.mock = config.MOCK_IFF or not _HAVE_SERIAL
        self.ser = None
        if self.mock:
            why = "MOCK_IFF" if config.MOCK_IFF else "pyserial not installed"
            stance = "FRIENDLY" if config.MOCK_IFF_FRIENDLY else "FOE"
            print(f"[iff] running in MOCK mode ({why}) - "
                  f"simulated contact will answer as {stance}")
        else:
            self.ser = serial.Serial(config.IFF_PORT, config.IFF_BAUD, timeout=0)
            time.sleep(2.0)  # let the IFF Uno reset after the port opens
            print(f"[iff] transceiver on {config.IFF_PORT} @ {config.IFF_BAUD}")

    def challenge(self) -> IFFResult:
        """Run one friend-or-foe handshake. Blocks up to IFF_CHALLENGE_TIMEOUT_S."""
        print("[iff] >> CHALLENGE: requesting shared key from contact")
        reply = self._exchange()
        if reply is None:
            return IFFResult(
                False,
                f"no reply in {config.IFF_CHALLENGE_TIMEOUT_S:.1f}s "
                f"(does not hold the shared key)",
            )
        if hmac.compare_digest(reply.strip(), config.IFF_SHARED_SECRET):
            return IFFResult(True, "presented the shared key")
        return IFFResult(False, "presented a wrong key")

    def _exchange(self):
        """Return the contact's raw key string, or None on timeout/silence."""
        if self.mock:
            if config.MOCK_IFF_FRIENDLY:
                # A friendly Uno holds the same secret and answers with it.
                return config.IFF_SHARED_SECRET
            # A foe drone has no shared key -> stays silent.
            return None

        # Real transceiver: ask, then wait for "IFF,KEY,<secret>" within timeout.
        self.ser.reset_input_buffer()
        self.ser.write(b"IFF,WHO\n")
        deadline = time.time() + config.IFF_CHALLENGE_TIMEOUT_S
        buf = ""
        while time.time() < deadline:
            if self.ser.in_waiting:
                buf += self.ser.read(self.ser.in_waiting).decode(errors="ignore")
                if "\n" in buf:
                    line, _, buf = buf.partition("\n")
                    parts = line.strip().split(",")
                    if len(parts) == 3 and parts[0] == "IFF" and parts[1] == "KEY":
                        return parts[2]
            time.sleep(0.01)
        return None

    def close(self):
        if self.ser is not None:
            self.ser.close()
