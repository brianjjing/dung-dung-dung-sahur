#!/usr/bin/env python3
"""mock_server.py — offline demo server for the TRIPLE D V11 Converged Console.

Python standard library ONLY. Serves the dashboard and a fully synthetic data
feed so the operator console can be built, demoed, and screenshotted with no
hardware and zero external network access:

    GET /              -> index.html (the dashboard, served from this folder)
    GET /state.json    -> the live state contract (scripted FSM scenario)
    GET /cam.mjpeg     -> a synthetic MJPEG-style stream (pure-Python PNG frames,
                          multipart/x-mixed-replace); 503 when the camera is off

It scripts the full pipeline on a loop:

    IDLE -> noise -> DECIDING (vote climbs) -> IDENTIFYING -> branch:
            FOE  -> DEFEATING (laser + decoy) -> COOLDOWN -> IDLE
            FRIEND (alternate loop) -> stand down -> COOLDOWN -> IDLE

Force a single path with the query string (works on /state.json and /cam.mjpeg,
and the dashboard forwards its own ?scenario= to both):

    ?scenario=foe        always resolve the contact to a FOE (laser + decoy)
    ?scenario=friend     always resolve the contact to a FRIEND (no laser/decoy)
    ?scenario=linkdown   link.serial = "down"  -> dashboard enters LINK DOWN
    ?scenario=degraded   health.camera / vision_model false -> OFFLINE widgets

Run it:

    python3 mock_server.py            # http://127.0.0.1:8077
    python3 mock_server.py --port 9000

This is a stand-in only. In production the real device (main.py + frontend/ui.py)
serves the identical /state.json + /cam.mjpeg contract from live sensors.
"""

import argparse
import json
import math
import os
import struct
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")

HOST = "127.0.0.1"
PORT = 8077
START = time.time()

CYCLE = 20.0          # length of one full scripted pipeline loop (seconds)
THRESHOLD = 0.60      # threat threshold (constant)
VOTE_WINDOW = 6

# scripted decision-log events: (offset_in_cycle, text, level, branch)
# branch: None = always, "foe" / "friend" = only on that branch
LOG_SCRIPT = [
    (0.2,  "SYS boot · self-test ok",        "info", None),
    (0.6,  "MODELS loaded vis+aud",          "info", None),
    (1.0,  "LINK serial up 115200",          "info", None),
    (1.3,  "STATE → idle · listening",       "ok",   None),
    (3.0,  "SWEEP ambient · no contact",     "info", None),
    (4.1,  "NOISE amp 0.62",                 "warn", None),
    (4.7,  "DETECT contact brg +50°",        "info", None),
    (6.2,  "VOTE 4/6 confirming",            "cyan", None),
    (7.2,  "IFF challenge issued",           "cyan", None),
    (7.5,  "STATE → identifying",            "cyan", None),
    (9.8,  "VOTE 6/6 confirmed",             "info", None),
    (10.1, "IFF no reply · FOE",             "foe",  "foe"),
    (10.4, "AUTH human approved · OP-7",     "foe",  "foe"),
    (10.7, "DISTRACT decoy active",          "foe",  "foe"),
    (11.0, "DISABLE laser tracking",         "foe",  "foe"),
    (11.2, "STATE → defeating",              "foe",  "foe"),
    (10.1, "IFF key valid · FRIEND",         "ok",   "friend"),
    (10.4, "STAND DOWN · no action",         "ok",   "friend"),
    (10.7, "STATE → cooldown",               "info", "friend"),
    (15.2, "COOLDOWN · re-arming",           "info", None),
]


def clamp(v, a, b):
    return max(a, min(b, v))


def waveform(amp, t, n=128):
    """A lively trace whose envelope tracks amplitude; always animated so the
    oscilloscope proves liveness even at IDLE."""
    out = []
    for i in range(n):
        env = math.sin(math.pi * i / (n - 1))
        v = env * (0.05 + amp * 0.95) * math.sin(i * 0.55 + t * 2.1) \
            * math.sin(i * 0.21 - t * 1.3)
        out.append(round(v, 4))
    return out


def contact_pos(progress):
    """Closing spiral: comes in from the upper-right, radius shrinks toward the
    unit at the center. Returns normalized grid coords (-1..1, y down)."""
    progress = clamp(progress, 0.0, 1.0)
    r = 0.82 - 0.62 * progress
    th = math.radians(42 + 16 * progress)      # compass-ish bearing from north
    gx = r * math.sin(th)
    gy = -r * math.cos(th)
    return gx, gy, th


def ramp(t, t0, t1, v0, v1):
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def build_state(scenario, at=None):
    now = time.time()
    if at is not None:
        # deterministic phase override (verification/screenshot aid): pin the
        # cycle-time so a specific FSM phase can be captured reliably.
        elapsed = at
        tc = at % CYCLE
        cycle_idx = int(at // CYCLE)
    else:
        elapsed = now - START
        tc = elapsed % CYCLE
        cycle_idx = int(elapsed // CYCLE)

    # which branch does IDENTIFYING resolve to?
    if scenario == "foe":
        foe = True
    elif scenario == "friend":
        foe = False
    else:
        foe = (cycle_idx % 2 == 0)

    degraded = (scenario == "degraded")
    linkdown = (scenario == "linkdown")

    # --- phase + FSM state -------------------------------------------------
    has_contact = (4.0 <= tc < 18.5) and not degraded
    if tc < 4.0:
        state = "IDLE"
    elif tc < 7.0:
        state = "DECIDING"
    elif tc < 10.0:
        state = "IDENTIFYING"
    elif tc < 15.0:
        state = "DEFEATING" if foe else "COOLDOWN"
    elif tc < 18.5:
        state = "COOLDOWN"
    else:
        state = "IDLE"
    if degraded:
        state = "DECIDING" if (2.0 <= tc < 6.0) else "IDLE"

    defeating = (state == "DEFEATING") and foe

    # --- amplitude / confidence -------------------------------------------
    base_amp = 0.05 + 0.05 * (0.5 + 0.5 * math.sin(elapsed * 1.7))   # ambient
    if has_contact or degraded:
        amp = ramp(tc, 4.0, 5.5, 0.18, 0.71) if tc < 15 else ramp(tc, 15, 18.5, 0.71, 0.2)
        amp = max(amp, base_amp)
    else:
        amp = base_amp
    conf = clamp(0.04 + (0.8 if has_contact else 0.0) * ramp(tc, 4.0, 6.0, 0.05, 1.0), 0, 0.95)

    # --- threat score / components ----------------------------------------
    if state == "IDLE":
        score = 0.05 + 0.04 * (0.5 + 0.5 * math.sin(elapsed * 2.3))
    elif state == "DECIDING":
        score = ramp(tc, 4.0, 7.0, 0.14, 0.55)
    elif state == "IDENTIFYING":
        score = ramp(tc, 7.0, 10.0, 0.55, 0.58)         # stays just under threshold
    elif defeating:
        score = ramp(tc, 10.0, 12.0, 0.61, 0.84)        # crosses threshold on escalation
    elif state == "COOLDOWN" and foe:
        score = ramp(tc, 15.0, 18.5, 0.8, 0.18)
    else:  # friend cooldown / stand-down
        score = ramp(tc, 10.0, 14.0, 0.55, 0.12)
    if degraded:
        score = 0.05 + 0.03 * (0.5 + 0.5 * math.sin(elapsed * 2.0))

    comp_sound = clamp(amp, 0, 1)
    comp_cam = clamp(conf, 0, 1) if not degraded else 0.0
    comp_close = clamp(ramp(tc, 5.0, 12.0, 0.0, 0.9) if has_contact else 0.0, 0, 1)
    closing_bool = bool(defeating or (has_contact and tc >= 11.0 and foe))

    # --- vote --------------------------------------------------------------
    if not has_contact:
        hits = 0
    elif state == "DECIDING":
        hits = int(round(ramp(tc, 4.2, 7.0, 0.0, 4.0)))
    elif state == "IDENTIFYING":
        hits = int(round(ramp(tc, 7.0, 9.8, 4.0, 6.0)))
    else:
        hits = 6

    # --- contact geometry --------------------------------------------------
    contact = None
    distance_cm = None
    if has_contact:
        progress = clamp((tc - 4.0) / 13.0, 0, 1)
        gx, gy, th = contact_pos(progress)
        # trail: recent samples behind the current position
        trail = []
        for i in range(12, 0, -1):
            pgx, pgy, _ = contact_pos(progress - i * 0.045)
            trail.append({"x": round(pgx, 4), "y": round(pgy, 4)})
        heading_deg = round((math.degrees(th) + 180) % 360, 1)   # moving inward
        iff = "unknown"
        if state in ("DEFEATING", "COOLDOWN"):
            iff = "foe" if foe else "friend"
        elif state == "IDENTIFYING" and tc >= 9.6:
            iff = "foe" if foe else "friend"
        distance_cm = round(clamp(0.82 - 0.62 * progress, 0.05, 1.0) * 5000, 0)
        contact = {
            "iff": iff,
            "grid": {"x": round(gx, 4), "y": round(gy, 4)},
            "heading_deg": heading_deg,
            "trail": trail,
            "vote": {"hits": hits, "window": VOTE_WINDOW},
        }

    # --- defeat ------------------------------------------------------------
    defeat = {"active": False, "decoy": False,
              "laser": {"active": False, "aim": None, "sweep_deg": 0.0}}
    if defeating and contact:
        gx, gy = contact["grid"]["x"], contact["grid"]["y"]
        m = math.hypot(gx, gy) or 1.0
        aim = [round(gx + gx / m * 0.20, 4), round(gy + gy / m * 0.20, 4)]  # behind drone
        defeat = {
            "active": True,
            "decoy": True,
            "laser": {"active": True, "aim": aim,
                      "sweep_deg": round((elapsed * 90) % 360, 1)},
        }

    # --- camera ------------------------------------------------------------
    cam_healthy = not degraded
    cam_awake = bool(has_contact) and cam_healthy
    vision_ok = not degraded

    # --- decision log ------------------------------------------------------
    branch = "foe" if foe else "friend"
    log = []
    for off, text, level, only in LOG_SCRIPT:
        if only is not None and only != branch:
            continue
        if off <= tc:
            ev_wall = now - (tc - off)
            lt = time.strftime("%H:%M:%S", time.localtime(ev_wall))
            log.append({"t": lt, "text": text, "level": level})
    log.sort(key=lambda e: e["t"])
    if degraded:
        log = [
            {"t": time.strftime("%H:%M:%S"), "text": "SYS boot · self-test", "level": "info"},
            {"t": time.strftime("%H:%M:%S"), "text": "CAMERA health FAULT", "level": "foe"},
            {"t": time.strftime("%H:%M:%S"), "text": "VISION MODEL not loaded", "level": "foe"},
            {"t": time.strftime("%H:%M:%S"), "text": "DEGRADED · acoustic only", "level": "warn"},
        ]

    return {
        "ts": now,
        "mode": "mock",
        "state": state,
        "link": {"serial": "down" if linkdown else "up", "baud": 115200},
        "health": {
            "vision_model": vision_ok,
            "acoustic_model": True,
            "mic": True,
            "camera": cam_healthy,
        },
        "sensors": {
            "mic": {
                "amplitude": round(amp, 3),
                "confidence": round(conf, 3),
                "waveform": waveform(amp, elapsed),
            },
            "camera": {"awake": cam_awake, "stream": "/cam.mjpeg"},
            "distance_cm": distance_cm,
        },
        "threat": {
            "score": round(score, 3),
            "threshold": THRESHOLD,
            "closing": closing_bool,
            "components": {
                "sound": round(comp_sound, 3),
                "camera": round(comp_cam, 3),
                "closing": round(comp_close, 3),
            },
        },
        "contact": contact,
        "defeat": defeat,
        "log": log,
    }


# ---------------------------------------------------------------------------
# Synthetic camera: pure-Python PNG frames over multipart/x-mixed-replace.
# (No JPEG encoder in the stdlib, but browsers render PNG parts in an MJPEG
# multipart stream just the same — fully offline, no dependencies.)
# ---------------------------------------------------------------------------
CAM_W, CAM_H = 256, 150


def _png(width, height, rgb):
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)                      # filter type 0 (none)
        raw += rgb[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 6)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


def _cam_frame(f):
    """A dark sensor view with a faint drifting blob + scanline — enough motion
    to read as a live optical feed behind the dashboard's reticle overlay."""
    buf = bytearray(CAM_W * CAM_H * 3)
    bx = CAM_W / 2 + math.sin(f / 9.0) * 46
    by = CAM_H / 2 + math.cos(f / 13.0) * 26
    scan = int((f * 5) % CAM_H)
    for y in range(CAM_H):
        base = 12 + (y * 10) // CAM_H
        row = y * CAM_W
        near_scan = abs(y - scan) < 2
        for x in range(CAM_W):
            i = (row + x) * 3
            r, g, b = 6, base, base + 6
            d2 = (x - bx) * (x - bx) + (y - by) * (y - by)
            if d2 < 1100:
                v = int((1 - d2 / 1100) * 70)
                r += v // 3
                g += v
                b += v // 2
            if near_scan:
                g += 40
                b += 28
            buf[i] = r if r < 255 else 255
            buf[i + 1] = g if g < 255 else 255
            buf[i + 2] = b if b < 255 else 255
    return _png(CAM_W, CAM_H, buf)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _scenario(self):
        q = parse_qs(urlparse(self.path).query)
        return (q.get("scenario", [None])[0] or "").lower()

    def _at(self):
        q = parse_qs(urlparse(self.path).query)
        v = q.get("at", [None])[0]
        try:
            return float(v) if v is not None else None
        except ValueError:
            return None

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(b"index.html not found", "text/plain", 404)
        elif path == "/state.json":
            body = json.dumps(build_state(self._scenario(), self._at())).encode("utf-8")
            self._send(body, "application/json")
        elif path == "/cam.mjpeg":
            self._stream_cam()
        elif path == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
        else:
            self._send(b"not found", "text/plain", 404)

    def _stream_cam(self):
        # Camera off (degraded) -> 503 so the dashboard shows OFFLINE/STANDBY.
        if self._scenario() == "degraded":
            self._send(b"camera offline", "text/plain", 503)
            return
        boundary = "ddframe"
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=" + boundary)
        self.end_headers()
        f = 0
        try:
            while True:
                png = _cam_frame(f)
                head = ("--%s\r\nContent-Type: image/png\r\n"
                        "Content-Length: %d\r\n\r\n" % (boundary, len(png)))
                self.wfile.write(head.encode("ascii"))
                self.wfile.write(png)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                f += 1
                time.sleep(1 / 6.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main():
    ap = argparse.ArgumentParser(description="TRIPLE D V11 mock dashboard server")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = "http://%s:%d" % (args.host, args.port)
    print("TRIPLE D — V11 Converged Console (MOCK)")
    print("  dashboard : %s" % url)
    print("  state     : %s/state.json" % url)
    print("  camera    : %s/cam.mjpeg" % url)
    print("  scenarios : %s/?scenario=foe|friend|linkdown|degraded" % url)
    print("  (Ctrl+C to stop)  — zero external network requests")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        srv.shutdown()


if __name__ == "__main__":
    main()
