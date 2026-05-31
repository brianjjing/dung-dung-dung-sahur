"""dashboard.py — Flask + WebSocket dashboard for FiberTrace.

Serves http://localhost:5000:
  /            live video panel + operator-bearing fan + status
  /video_feed  MJPEG stream of the annotated camera frame
  /ws          WebSocket pushing {state, detection, operator} ~10 Hz
               (falls back to polling /state if flask-sock isn't installed)

The control loop (trace_main) runs in a background thread and feeds this via
update_frame()/update_state(); the server only reads the latest snapshot.

draw_overlay() annotates the frame with the detected fiber, the extrapolation
ray, and the predicted operator marker.
"""
from __future__ import annotations

import json
import threading
import time

import config

try:
    import cv2
    _HAVE_CV = True
except ImportError:  # pragma: no cover
    _HAVE_CV = False

try:
    from flask import Flask, Response, jsonify
    _HAVE_FLASK = True
except ImportError:  # pragma: no cover
    _HAVE_FLASK = False

try:
    from flask_sock import Sock
    _HAVE_SOCK = True
except ImportError:  # pragma: no cover
    _HAVE_SOCK = False


_STATE_BGR = {
    "SEARCHING": (0, 215, 255),    # amber
    "TRACKING":  (90, 220, 90),    # green
}


def _label(img, text, org, color=(255, 255, 255), scale=0.55, thick=1):
    x, y = org
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
    cv2.rectangle(img, (x - 3, y - th - 5), (x + tw + 3, y + 4), (0, 0, 0), -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                thick, cv2.LINE_AA)


def draw_overlay(frame, est, state_name, operator):
    """Return an annotated copy of frame for the live video panel."""
    if not _HAVE_CV:
        return frame
    img = frame.copy()
    h, w = img.shape[:2]
    accent = _STATE_BGR.get(state_name, (255, 255, 255))

    # detected fiber segments
    for (x1, y1, x2, y2) in getattr(est, "raw_lines", []):
        cv2.line(img, (x1, y1), (x2, y2), (255, 220, 0), 2, cv2.LINE_AA)

    # extrapolation ray toward the ground + operator marker
    if operator is not None:
        (rx1, ry1), (rx2, ry2) = operator["ray"]
        cv2.line(img, (int(rx1), int(ry1)), (int(rx2), int(ry2)),
                 (60, 80, 255), 2, cv2.LINE_AA)
        gx = int(max(6, min(w - 6, operator["ground_x"])))
        cv2.drawMarker(img, (gx, h - 10), (60, 80, 255),
                       cv2.MARKER_TRIANGLE_UP, 22, 3)
        txt = (f"OPERATOR EST: {operator['side']} "
               f"{abs(operator['bearing_deg']):.0f}deg  "
               f"conf {operator['confidence']:.2f}")
        _label(img, txt, (10, h - 16), (60, 80, 255), 0.62, 2)

    # status block
    _label(img, f"STATE: {state_name}", (10, 26), accent, 0.7, 2)
    _label(img, f"fiber conf: {est.confidence:.2f}   segments: {est.num_lines}",
           (10, 50))
    return img


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>FiberTrace</title><style>
 body{background:#0d1117;color:#e6edf3;font-family:ui-monospace,monospace;margin:0}
 header{padding:10px 16px;border-bottom:1px solid #21262d}
 header b{color:#58a6ff} header span{color:#8b949e}
 .wrap{display:flex;gap:14px;padding:14px;flex-wrap:wrap}
 .panel{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:10px}
 img{width:560px;max-width:90vw;border-radius:4px;display:block}
 canvas{background:#0b0f14;border-radius:4px}
 #bar{padding:10px 16px;border-top:1px solid #21262d;font-size:14px}
 .k{color:#8b949e} .v{color:#e6edf3;font-weight:bold} .op{color:#ff7b72;font-weight:bold}
</style></head><body>
<header><b>FiberTrace</b> &nbsp;<span>Detect the fiber from a drone &rarr; guesstimate the operator (offline)</span></header>
<div class="wrap">
 <div class="panel"><div class="k">LIVE CAMERA + FIBER</div><img src="/video_feed"></div>
 <div class="panel"><div class="k">OPERATOR BEARING (from the car)</div>
   <canvas id="fan" width="420" height="430"></canvas></div>
</div>
<div id="bar">
 <span class="k">state</span> <span class="v" id="st">-</span> &nbsp;|&nbsp;
 <span class="k">fiber conf</span> <span class="v" id="cf">-</span> &nbsp;|&nbsp;
 <span class="k">operator</span> <span class="op" id="op">-</span>
</div>
<script>
const $=id=>document.getElementById(id), cv=$("fan"), ctx=cv.getContext("2d");
function fan(op){
 ctx.clearRect(0,0,cv.width,cv.height);
 const cx=cv.width/2, cy=cv.height-26, R=cv.height-70;
 // compass arcs + ticks
 ctx.strokeStyle="#21262d";ctx.lineWidth=1;
 for(let rr=R*0.4; rr<=R; rr+=R*0.3){ctx.beginPath();ctx.arc(cx,cy,rr,Math.PI,2*Math.PI);ctx.stroke();}
 ctx.fillStyle="#8b949e";ctx.font="11px monospace";
 [-30,-15,0,15,30].forEach(d=>{let a=d*Math.PI/180;
   let x=cx+Math.sin(a)*R, y=cy-Math.cos(a)*R;
   ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(x,y);ctx.strokeStyle="#1b2230";ctx.stroke();
   ctx.fillText((d>0?"+":"")+d+"°", x-10, y-4);});
 // car
 ctx.fillStyle="#9db4d0";ctx.fillRect(cx-7,cy-3,14,10);
 ctx.fillStyle="#8b949e";ctx.fillText("car", cx-10, cy+20);
 if(!op){return;}
 // operator ray + marker
 let a=op.bearing_deg*Math.PI/180;
 let x=cx+Math.sin(a)*R, y=cy-Math.cos(a)*R;
 ctx.strokeStyle="#ff5a5f";ctx.lineWidth=3;ctx.globalAlpha=0.4+0.6*op.confidence;
 ctx.beginPath();ctx.moveTo(cx,cy);ctx.lineTo(x,y);ctx.stroke();ctx.globalAlpha=1;
 ctx.fillStyle="#ff5a5f";ctx.font="22px monospace";ctx.fillText("★",x-9,y+8);
 ctx.font="13px monospace";
 ctx.fillText(op.side+" "+Math.abs(op.bearing_deg).toFixed(0)+"°", x-18, y+26);
}
function apply(d){
 $("st").textContent=d.state||"-";
 $("cf").textContent=(d.detection&&d.detection.confidence!=null)?d.detection.confidence.toFixed(2):"-";
 let op=d.operator;
 $("op").textContent=op?(op.side+" "+Math.abs(op.bearing_deg).toFixed(0)+"° · conf "+op.confidence.toFixed(2)):"(estimating…)";
 fan(op);
}
function poll(){fetch("/state").then(r=>r.json()).then(apply).catch(()=>{});}
let ws;
try{
 ws=new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws");
 ws.onmessage=e=>apply(JSON.parse(e.data));
 ws.onclose=()=>setInterval(poll,200);
 ws.onerror=()=>{try{ws.close()}catch(_){ }};
}catch(_){setInterval(poll,200);}
setInterval(poll,1000);
</script></body></html>"""


class Dashboard:
    """Thread-safe latest-snapshot holder + Flask/WebSocket server."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jpeg = None
        self._state = {"state": "INIT", "detection": {}, "operator": None}
        self.enabled = _HAVE_FLASK and _HAVE_CV

    def update_frame(self, annotated_bgr):
        ok, buf = cv2.imencode(".jpg", annotated_bgr,
                               [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            with self._lock:
                self._jpeg = buf.tobytes()

    def update_state(self, state: dict):
        with self._lock:
            self._state = state

    def _snapshot_state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _snapshot_jpeg(self):
        with self._lock:
            return self._jpeg

    def run(self):
        if not self.enabled:
            missing = []
            if not _HAVE_FLASK:
                missing.append("flask")
            if not _HAVE_CV:
                missing.append("opencv-python")
            print(f"[dashboard] disabled — missing: {', '.join(missing)}. "
                  f"Install: pip install -r fibertrace/requirements.txt")
            return

        app = Flask(__name__)
        app.logger.disabled = True

        @app.route("/")
        def index():
            return Response(_PAGE, mimetype="text/html")

        @app.route("/state")
        def state():
            return jsonify(self._snapshot_state())

        @app.route("/video_feed")
        def video_feed():
            def gen():
                period = 1.0 / max(1, config.FT_TARGET_FPS)
                while True:
                    jpeg = self._snapshot_jpeg()
                    if jpeg is not None:
                        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                               + jpeg + b"\r\n")
                    time.sleep(period)
            return Response(gen(),
                            mimetype="multipart/x-mixed-replace; boundary=frame")

        if _HAVE_SOCK:
            sock = Sock(app)

            @sock.route("/ws")
            def ws(ws):                       # noqa: ANN001
                try:
                    while True:
                        ws.send(json.dumps(self._snapshot_state()))
                        time.sleep(0.1)
                except Exception:
                    return
        else:
            print("[dashboard] flask-sock not installed — browser will poll "
                  "/state instead of using WebSocket")

        port = config.FT_DASH_PORT
        print(f"[dashboard] http://localhost:{port}  "
              f"(WebSocket {'on' if _HAVE_SOCK else 'off -> polling'})")
        app.run(host=config.FT_DASH_HOST, port=port, threaded=True,
                debug=False, use_reloader=False)
