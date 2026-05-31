"""frontend/ui.py - live operator dashboard server for the Triple D brain.

This is the PRODUCTION side of the V11 Converged Console. The brain (main.py)
builds a state snapshot in the published contract shape and hands it to
``Dashboard.publish(...)``; a tiny stdlib HTTP server (background daemon thread)
exposes it — the SAME contract the offline ``mock_server.py`` serves:

    GET /              -> index.html (the single-file dashboard, served from disk)
    GET /state.json    -> the live state contract (snapshot under a lock)
    GET /cam.mjpeg     -> live camera as multipart/x-mixed-replace JPEG frames
                          (503 while the camera is dark / vision asleep)

Contract (all fields tolerated null by the dashboard):
    { ts, mode, state, link{serial,baud}, health{vision_model,acoustic_model,mic,camera},
      sensors{mic{amplitude,confidence,waveform[]}, camera{awake,stream}, distance_cm},
      threat{score,threshold,closing,components{sound,camera,closing}},
      contact{iff,grid{x,y},heading_deg,trail[],vote{hits,window}} | null,
      defeat{active,decoy,laser{active,aim,sweep_deg}}, log[] }

The dashboard is MONITORING-ONLY: it has no control affordances and never
actuates anything. The server only ever reads a snapshot under a lock and
encodes camera frames in its own thread, so the control loop is never blocked.
Best-effort: if the port is taken or the browser can't open, the pipeline runs
exactly as before and just prints a note.
"""
import json
import os
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")
LOG_MAX = 60


def _empty_contract():
    return {
        "ts": time.time(),
        "mode": "mock" if getattr(config, "MOCK_SERIAL", False) else "live",
        "state": "IDLE",
        "link": {"serial": "up", "baud": getattr(config, "BAUD", 115200)},
        "health": {"vision_model": True, "acoustic_model": True,
                   "mic": True, "camera": True},
        "sensors": {"mic": {"amplitude": 0.0, "confidence": 0.0, "waveform": None},
                    "camera": {"awake": False, "stream": "/cam.mjpeg"},
                    "distance_cm": None},
        "threat": {"score": 0.0, "threshold": getattr(config, "SCORE_HOSTILE", 0.60),
                   "closing": False,
                   "components": {"sound": 0.0, "camera": 0.0, "closing": 0.0}},
        "contact": None,
        "defeat": {"active": False, "decoy": False,
                   "laser": {"active": False, "aim": None, "sweep_deg": 0.0}},
        "log": [],
    }


class Dashboard:
    """Background HTTP server + thread-safe state the brain publishes into."""

    def __init__(self):
        self.enabled = getattr(config, "UI_ENABLED", False)
        self._lock = threading.Lock()
        self._frame_provider = None          # callable -> JPEG bytes or None
        self._state = _empty_contract()
        self._log = deque(maxlen=LOG_MAX)    # derived audit trail (state transitions)
        self._seen = {"state": None, "iff": None, "engaging": None, "awake": None}
        self._server = None
        self._thread = None
        if self.enabled:
            self._start()

    # ---- public API used by the brain -------------------------------------

    def publish(self, contract: dict):
        """Store a full state-contract snapshot (built by main.py). No-op if the
        UI is off. Also derives the decision-log audit trail from transitions."""
        if not self.enabled:
            return
        with self._lock:
            contract = dict(contract)
            contract["ts"] = time.time()
            self._derive_log(contract)
            contract["log"] = list(self._log)
            self._state = contract

    def update(self, **fields):
        """Back-compat shallow merge (kept for any caller using the old API)."""
        if not self.enabled:
            return
        with self._lock:
            self._state.update(fields)
            self._state["ts"] = time.time()

    def event(self, text: str, level: str = "info"):
        """Append an explicit audit-log line (e.g. a DETECT/IFF/DEFEAT event)."""
        if not self.enabled:
            return
        with self._lock:
            self._append_log(text, level)
            self._state["log"] = list(self._log)

    def set_frame_provider(self, fn):
        """Register a callable returning JPEG bytes (or None) for the camera
        stream. Invoked from the HTTP thread, so it must be cheap/non-blocking."""
        self._frame_provider = fn

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # ---- internals --------------------------------------------------------

    def _append_log(self, text, level):
        self._log.append({"t": time.strftime("%H:%M:%S"), "text": text, "level": level})

    def _derive_log(self, contract):
        state = contract.get("state")
        contact = contract.get("contact") or {}
        iff = contact.get("iff")
        engaging = bool((contract.get("defeat") or {}).get("active"))
        awake = bool(((contract.get("sensors") or {}).get("camera") or {}).get("awake"))

        if awake and not self._seen["awake"]:
            self._append_log("VISION activated · acoustic threat", "cyan")
        if state != self._seen["state"] and state is not None:
            lvl = {"DEFEATING": "foe", "IDENTIFYING": "cyan",
                   "COOLDOWN": "info", "IDLE": "ok"}.get(state, "info")
            self._append_log("STATE → " + str(state).lower(), lvl)
        if iff != self._seen["iff"] and iff:
            if iff == "foe":
                self._append_log("IFF no reply · FOE", "foe")
            elif iff in ("friend", "friendly"):
                self._append_log("IFF key valid · FRIEND", "ok")
        if engaging and not self._seen["engaging"]:
            self._append_log("AUTH human approved · OP-7", "foe")
            self._append_log("DISTRACT decoy active", "foe")
            self._append_log("DISABLE laser tracking", "foe")

        self._seen.update(state=state, iff=iff, engaging=engaging, awake=awake)

    # ---- server -----------------------------------------------------------

    def _start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, body, ctype, code=200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    try:
                        with open(INDEX_PATH, "rb") as fh:
                            self._send(fh.read(), "text/html; charset=utf-8")
                    except OSError:
                        self._send(b"index.html missing", "text/plain", 404)
                elif path == "/state.json":
                    self._send(json.dumps(dash.snapshot()).encode("utf-8"),
                               "application/json")
                elif path == "/cam.mjpeg":
                    self._stream_cam()
                elif path == "/favicon.ico":
                    self._send(b"", "image/x-icon", 204)
                else:
                    self.send_response(404)
                    self.end_headers()

            def _stream_cam(self):
                # Wait briefly for a live frame; if the camera is dark/asleep,
                # 503 so the dashboard shows the STANDBY/OFFLINE panel.
                first = None
                for _ in range(8):
                    first = dash.latest_frame()
                    if first:
                        break
                    time.sleep(0.05)
                if not first:
                    self._send(b"camera asleep", "text/plain", 503)
                    return
                boundary = "ddframe"
                self.send_response(200)
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=" + boundary)
                self.end_headers()
                last = first
                try:
                    while True:
                        jpeg = dash.latest_frame() or last
                        if jpeg is None:
                            time.sleep(0.08)
                            continue
                        last = jpeg
                        head = ("--%s\r\nContent-Type: image/jpeg\r\n"
                                "Content-Length: %d\r\n\r\n" % (boundary, len(jpeg)))
                        self.wfile.write(head.encode("ascii"))
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                        time.sleep(1 / 12.0)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

        host = getattr(config, "UI_HOST", "127.0.0.1")
        port = getattr(config, "UI_PORT", 8077)
        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
            self._server.daemon_threads = True
        except OSError as e:                                     # noqa: BLE001
            print(f"[ui] dashboard could not start ({e}); UI disabled")
            self.enabled = False
            return
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        url = f"http://{host}:{port}/"
        print(f"[ui] dashboard live at {url}")
        if getattr(config, "UI_OPEN_BROWSER", False):
            threading.Thread(target=lambda: webbrowser.open(url),
                             daemon=True).start()

    def latest_frame(self):
        """Latest camera JPEG bytes for the /cam.mjpeg stream, or None."""
        fn = self._frame_provider
        if fn is None:
            return None
        try:
            return fn()
        except Exception:                                    # noqa: BLE001
            return None
