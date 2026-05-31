"""ui.py - live operator dashboard for the Triple D brain.

A dependency-free visual front end for the state machine in main.py. A tiny
stdlib HTTP server runs in a background daemon thread and exposes the brain's
current state as JSON at /state; the single-page canvas app served at / polls
that ~20x/s and renders one persistent view that mirrors the pipeline:

  The bird's-eye DETECTION GRID is on screen the whole time. While nothing is
  detected the grid is empty -- no drone, no laser, no track -- except for the
  decoy, which sits at its fixed position in a DEACTIVATED (standby) state. The
  microphone listening indicator lives in the bottom-right corner permanently
  and reacts to the live acoustic amplitude.

  When acoustic noise wakes the vision model, a CAMERA PLAYBACK window opens in
  the top-right corner showing the live webcam feed (served at /frame.jpg). It
  closes again when the ~1.5s watch window expires with nothing found, or once
  the contact is no longer being tracked.

  Once a drone is confirmed it appears on the grid (green = friendly, red =
  foe) with a fading track. On a FOE verdict the laser cut point + beam are
  drawn and the decoy flips to its ACTIVE flashing state.

The brain pushes state with Dashboard.update(**fields) and registers a webcam
frame provider with Dashboard.set_frame_provider(fn); the server only ever reads
a snapshot under a lock and encodes frames in its own thread, so the control loop
is never blocked. The whole thing is best-effort: if the port is taken or the
browser can't open, the pipeline runs exactly as before and just prints a note.
"""
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config


class Dashboard:
    """Background HTTP server + thread-safe state the brain publishes into."""

    def __init__(self):
        self.enabled = getattr(config, "UI_ENABLED", False)
        self._lock = threading.Lock()
        self._frame_provider = None          # callable -> JPEG bytes or None
        self._state = {
            "mode": "listening",     # listening | searching | tracking
            "raw_state": "IDLE",
            "amp": 0,
            "pitch": 0,
            "dist": -1,
            "acoustic_conf": 0.0,
            "search_remaining": 0.0,
            "search_total": getattr(config, "VISION_DECIDE_TIMEOUT_S", 1.5),
            "iff": None,             # None | "friendly" | "foe"
            "overlay": "",
            "engaging": False,
            "drone_xy": None,        # [x,y] normalised 0..1, or None (UI sims it)
            "laser_xy": None,
            "decoy_xy": None,
            "camera_on": False,      # live webcam window shown top-right while True
            "ts": time.time(),
        }
        self._server = None
        self._thread = None
        if self.enabled:
            self._start()

    def _start(self):
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # silence per-request logging
                pass

            def _send(self, body: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/state"):
                    self._send(json.dumps(dash.snapshot()).encode(),
                               "application/json")
                elif self.path.startswith("/frame.jpg"):
                    jpeg = dash.latest_frame()
                    if jpeg:
                        self._send(jpeg, "image/jpeg")
                    else:
                        self.send_response(204)      # no live frame -> UI sims it
                        self.end_headers()
                elif self.path == "/" or self.path.startswith("/index"):
                    self._send(INDEX_HTML.encode("utf-8"),
                               "text/html; charset=utf-8")
                else:
                    self.send_response(404)
                    self.end_headers()

        host = getattr(config, "UI_HOST", "127.0.0.1")
        port = getattr(config, "UI_PORT", 8077)
        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
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

    def set_frame_provider(self, fn):
        """Register a callable returning JPEG bytes (or None) for the camera
        window. Invoked from the HTTP thread, so it must be cheap/non-blocking."""
        self._frame_provider = fn

    def latest_frame(self):
        """Latest camera JPEG bytes for the /frame.jpg endpoint, or None."""
        fn = self._frame_provider
        if fn is None:
            return None
        try:
            return fn()
        except Exception:                                    # noqa: BLE001
            return None

    def update(self, **fields):
        """Merge fields into the published state. No-op if the UI is off."""
        if not self.enabled:
            return
        with self._lock:
            self._state.update(fields)
            self._state["ts"] = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server = None


# ----------------------------------------------------------------------------
# Single-page front end. Pure canvas; no external assets so it works offline on
# the Uno Q. Polls /state and animates with requestAnimationFrame. The detection
# grid is always the view; the camera window and mic indicator float over it.
# ----------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Triple D - Operator Dashboard</title>
<style>
  html,body{margin:0;height:100%;background:#05070a;overflow:hidden;
    font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace;color:#cfe;}
  #c{display:block;width:100vw;height:100vh;}
</style>
</head>
<body>
<canvas id="c"></canvas>
<script>
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');
let W, H, DPR;
function resize(){
  DPR = window.devicePixelRatio || 1;
  W = cv.width  = Math.floor(innerWidth  * DPR);
  H = cv.height = Math.floor(innerHeight * DPR);
  cv.style.width = innerWidth + 'px';
  cv.style.height = innerHeight + 'px';
}
addEventListener('resize', resize); resize();

let state = {
  mode:'listening', raw_state:'IDLE', amp:0, pitch:0, dist:-1, acoustic_conf:0,
  search_remaining:0, search_total:1.5, iff:null, overlay:'', engaging:false,
  drone_xy:null, laser_xy:null, decoy_xy:null, camera_on:false, online:false
};

async function poll(){
  try{
    const r = await fetch('/state', {cache:'no-store'});
    const s = await r.json();
    s.online = true;
    state = s;
  }catch(e){ state.online = false; }
}
poll(); setInterval(poll, 50);

// ---- live webcam frames for the camera window -----------------------------
// Reload /frame.jpg ~10x/s while the camera is on. A 204 (no live frame, e.g.
// mock mode) trips onerror, so we fall back to a synthetic feed.
let camImg = new Image();
let camHasFrame = false;
camImg.onload  = ()=>{ camHasFrame = true; };
camImg.onerror = ()=>{ camHasFrame = false; };
setInterval(()=>{
  if(state.camera_on){ camImg.src = '/frame.jpg?t=' + Date.now(); }
  else { camHasFrame = false; }
}, 100);

// ---- smoothing + local clocks ---------------------------------------------
let ampS = 0;                       // smoothed amplitude 0..1
let trail = [];                     // recent drone positions (normalised)
let lastHeading = -Math.PI/2;
let wasTracking = false;            // to reset the track when tracking begins
let trackStart = 0;
let wasEngaging = false;            // freeze the laser cut on the first engage frame
let cutPoint = null;                // frozen {x,y} cut position (does NOT chase the drone)
let cutHeading = 0;                 // frozen heading at the cut, orients the saw line
let activeDecoyIdx = -1;            // which decoy the drone's position cued (frozen at engage)

const C = {
  bg:'#05070a', grid:'#0c2a24', gridHot:'#103a31',
  green:'#39ff8b', greenDim:'#0c5',
  red:'#ff3b4e', yellow:'#ffe14d', cyan:'#39d6ff', dim:'#5a7a86',
  grey:'#3a4a52'
};

function lerp(a,b,t){ return a + (b-a)*t; }
function clamp(v,a,b){ return Math.max(a, Math.min(b, v)); }

// map normalised 0..1 -> detection grid rect
let gridRect = {x:0,y:0,w:0,h:0};
function nToScreen(nx, ny){
  return [gridRect.x + nx*gridRect.w, gridRect.y + ny*gridRect.h];
}

// Fixed decoy emitters scattered across the ground (bird's-eye). The one
// nearest the tracked drone is the node that fires.
const DECOYS = [
  [0.15,0.20],[0.84,0.16],[0.27,0.83],
  [0.73,0.80],[0.63,0.34],[0.11,0.60]
];
function decoyXY(i){ return nToScreen(DECOYS[i][0], DECOYS[i][1]); }
function nearestDecoy(nx, ny){
  let best = -1, bd = Infinity;
  for(let i=0;i<DECOYS.length;i++){
    const d = Math.hypot(nx-DECOYS[i][0], ny-DECOYS[i][1]);
    if(d < bd){ bd = d; best = i; }
  }
  return best;
}

// Right-hand column reserved for the camera (top) + mic (bottom) so they sit
// beside the grid instead of on top of it. The grid shrinks to its left.
function rightPanel(){
  const pad = Math.min(W,H)*0.035;
  // square side for the camera + mic boxes; narrower than before so the grid
  // reclaims the freed width
  const w = clamp(W*0.155, 200*DPR, 270*DPR);
  return {pad, w, x: W - w - pad, left: W - w - pad*2};
}

function glowDot(x, y, r, color, blur){
  ctx.save();
  ctx.shadowColor = color; ctx.shadowBlur = blur||r*3;
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI*2); ctx.fill();
  ctx.restore();
}

function textC(str, x, y, size, color, align){
  ctx.fillStyle = color; ctx.textAlign = align||'center';
  ctx.textBaseline = 'middle';
  ctx.font = (size*DPR) + 'px "SF Mono",ui-monospace,Menlo,monospace';
  ctx.fillText(str, x, y);
}

// ===========================================================================
// DETECTION GRID : always on screen. Empty while nothing is detected, except
// the decoy (fixed position, deactivated). A confirmed drone appears here.
// ===========================================================================
function simulateDrone(now){
  // smooth wandering path used when no real vision coordinate is available
  const t = (now - trackStart)/1000;
  return {
    x: 0.5 + 0.34*Math.cos(t*0.55) * Math.cos(t*0.17),
    y: 0.5 + 0.30*Math.sin(t*0.80)
  };
}

function drawDecoys(now, activeIdx){
  // the whole scattered field of decoy nodes in STANDBY; the active one is
  // drawn separately as a big flash, so skip it here.
  for(let i=0;i<DECOYS.length;i++){
    if(i === activeIdx) continue;
    const [cxp,cyp] = decoyXY(i);
    ctx.save();
    ctx.strokeStyle = 'rgba(90,122,134,0.55)'; ctx.lineWidth = 1.3*DPR;
    ctx.beginPath(); ctx.arc(cxp, cyp, 5*DPR, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = 'rgba(90,122,134,0.6)';
    ctx.beginPath(); ctx.arc(cxp, cyp, 2*DPR, 0, Math.PI*2); ctx.fill();
    textC('DECOY', cxp, cyp-13*DPR, 9, C.dim);
    ctx.restore();
  }
}

function drawDecoyFlash(now, i){
  // the cued decoy goes off with a big bloom: expanding shockwave rings plus a
  // white-hot core, much larger than the standby marker.
  const [cxp,cyp] = decoyXY(i);
  ctx.save();
  for(let k=0;k<3;k++){
    const ph = ((now/650) + k/3) % 1;
    const rr = (12 + ph*64)*DPR;
    ctx.strokeStyle = `rgba(255,225,77,${(1-ph)*0.7})`;
    ctx.lineWidth = 3*DPR;
    ctx.beginPath(); ctx.arc(cxp, cyp, rr, 0, Math.PI*2); ctx.stroke();
  }
  ctx.restore();
  const fl = 0.55 + 0.45*Math.sin(now/110);
  glowDot(cxp, cyp, (15+fl*9)*DPR, C.yellow, 52);   // big yellow bloom
  glowDot(cxp, cyp, 5*DPR, '#fffdf0', 30);          // white-hot center
  textC('DECOY ACTIVATED', cxp, cyp-30*DPR, 13, C.yellow);
}

function drawGrid(now){
  const m = Math.min(W,H)*0.10;
  const p = rightPanel();
  // grid spans from the left margin up to (but not under) the right-hand panel
  gridRect = {x:m, y:m+H*0.06, w:(p.left - m*0.4) - m, h:H-2*m-H*0.06};

  // ---- graph landscape -----------------------------------------------------
  ctx.fillStyle = '#02110d';
  ctx.fillRect(gridRect.x, gridRect.y, gridRect.w, gridRect.h);

  const N = 16;
  // fine lattice
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1*DPR;
  for(let i=0;i<=N;i++){
    const gxp = gridRect.x + gridRect.w*i/N;
    const gyp = gridRect.y + gridRect.h*i/N;
    ctx.beginPath(); ctx.moveTo(gxp, gridRect.y); ctx.lineTo(gxp, gridRect.y+gridRect.h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(gridRect.x, gyp); ctx.lineTo(gridRect.x+gridRect.w, gyp); ctx.stroke();
  }
  // major lattice every 4 cells, a touch brighter
  ctx.strokeStyle = C.gridHot; ctx.lineWidth = 1*DPR;
  for(let i=0;i<=N;i+=4){
    const gxp = gridRect.x + gridRect.w*i/N;
    const gyp = gridRect.y + gridRect.h*i/N;
    ctx.beginPath(); ctx.moveTo(gxp, gridRect.y); ctx.lineTo(gxp, gridRect.y+gridRect.h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(gridRect.x, gyp); ctx.lineTo(gridRect.x+gridRect.w, gyp); ctx.stroke();
  }
  // graph NODES at every major intersection -- the techy lattice landscape
  ctx.fillStyle = 'rgba(57,214,255,0.22)';
  for(let i=0;i<=N;i+=4){
    for(let j=0;j<=N;j+=4){
      const nx = gridRect.x + gridRect.w*i/N;
      const ny = gridRect.y + gridRect.h*j/N;
      ctx.beginPath(); ctx.arc(nx, ny, 1.7*DPR, 0, Math.PI*2); ctx.fill();
    }
  }

  // the protected asset / emitter sits at the centre of the grid
  const [ex, ey] = nToScreen(0.5, 0.5);
  // range rings around the asset: a distance reference radiating outward
  ctx.save();
  ctx.setLineDash([2*DPR, 6*DPR]);
  ctx.strokeStyle = 'rgba(57,214,255,0.13)'; ctx.lineWidth = 1*DPR;
  const ringMax = Math.min(gridRect.w, gridRect.h)*0.5;
  for(let k=1;k<=3;k++){
    ctx.beginPath(); ctx.arc(ex, ey, ringMax*k/3.2, 0, Math.PI*2); ctx.stroke();
  }
  ctx.restore();

  // hot border framing the field
  ctx.strokeStyle = C.gridHot; ctx.lineWidth = 1.5*DPR;
  ctx.strokeRect(gridRect.x, gridRect.y, gridRect.w, gridRect.h);

  // asset rendered as a clear ringed node
  ctx.save();
  ctx.strokeStyle = 'rgba(57,214,255,0.5)'; ctx.lineWidth = 1.2*DPR;
  ctx.beginPath(); ctx.arc(ex, ey, 10*DPR, 0, Math.PI*2); ctx.stroke();
  ctx.restore();
  glowDot(ex, ey, 5*DPR, C.cyan, 16);
  textC('TRIPLE-D', ex, ey-18*DPR, 11, C.cyan);

  const tracking = state.mode === 'tracking';
  const friendly = state.iff === 'friendly';
  const engaging = state.engaging && !friendly;

  // the scattered decoy field is always present; the cued one flashes later
  drawDecoys(now, engaging ? activeDecoyIdx : -1);

  if(!tracking){
    // nothing detected -> empty grid (decoy field already drawn, all standby)
    trail = [];
    cutPoint = null; wasEngaging = false; activeDecoyIdx = -1;   // clear frozen state
    textC('Nothing detected yet', gridRect.x+gridRect.w*0.5, gridRect.y+gridRect.h*0.72,
          16, 'rgba(90,122,134,0.5)');
    return;
  }

  // --- a drone is being tracked ---------------------------------------------
  const droneColor = friendly ? C.green : C.red;
  let dn = state.drone_xy ? {x:state.drone_xy[0], y:state.drone_xy[1]}
                          : simulateDrone(now);
  // Keep the ENTIRE flown path on screen (no cap, nothing shifted off). Only
  // record a point once the drone has actually moved, so a near-stationary
  // contact doesn't pile up thousands of duplicate points.
  const lastPt = trail[trail.length-1];
  if(!lastPt || Math.hypot(dn.x-lastPt.x, dn.y-lastPt.y) > 0.004) trail.push(dn);

  // GENERAL DIRECTION: heading fitted over the whole track (first -> last),
  // so it reflects the straight-line path rather than per-frame jitter.
  if(trail.length>3){
    const a = trail[0], b = trail[trail.length-1];
    if(Math.hypot(b.x-a.x, b.y-a.y) > 1e-3)
      lastHeading = Math.atan2(b.y-a.y, b.x-a.x);
  }

  // FIBER-OPTIC TETHER: the thin fiber the drone trails along its flown path.
  // The recorded track is drawn as one connected filament -- bright at the
  // drone, fading down its length -- then continued faintly past the oldest
  // sample toward the grid edge, implying the cable runs back to the launcher.
  // It is what the laser cut point (placed behind the drone) severs.
  if(trail.length >= 2){
    const fib = '255,255,255';                        // the whole tether is white
    ctx.save();
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.shadowColor = `rgba(${fib},0.5)`; ctx.shadowBlur = 5*DPR;
    // One continuous filament: the WHOLE recorded path stays solid white (no
    // fade-out along its length), thickening only slightly toward the drone so
    // the direction of travel still reads.
    for(let i=1;i<trail.length;i++){
      const [ax,ay] = nToScreen(trail[i-1].x, trail[i-1].y);
      const [bx,by] = nToScreen(trail[i].x,   trail[i].y);
      const a = i/trail.length;                       // 0 at tail -> 1 at drone
      ctx.strokeStyle = `rgba(${fib},0.9)`;
      ctx.lineWidth = (1.2 + a*1.2)*DPR;
      ctx.beginPath(); ctx.moveTo(ax,ay); ctx.lineTo(bx,by); ctx.stroke();
    }
    // The bulk of the tether: a straight run-back from the oldest sample to the
    // grid edge, opposite the flight heading -- the cable laid down on the way
    // in, continuing off-grid to the launcher. Drawn solid (just dimmer than the
    // recorded curve) so the fiber reads as one continuous line behind the drone.
    const t0 = trail[0];
    const back = lastHeading + Math.PI;                 // opposite the travel direction
    const bx = Math.cos(back), by = Math.sin(back);
    // distance from t0 along (bx,by) until it leaves the [0,1] grid
    let s = Infinity;
    if(bx >  1e-6) s = Math.min(s, (1 - t0.x) / bx);
    else if(bx < -1e-6) s = Math.min(s, (0 - t0.x) / bx);
    if(by >  1e-6) s = Math.min(s, (1 - t0.y) / by);
    else if(by < -1e-6) s = Math.min(s, (0 - t0.y) / by);
    s = isFinite(s) ? Math.max(0, s) : 0;
    const ex = t0.x + bx*s, ey = t0.y + by*s;
    const [t0x,t0y] = nToScreen(t0.x, t0.y);
    const [exx,eyy] = nToScreen(ex, ey);
    const g = ctx.createLinearGradient(t0x,t0y, exx,eyy);
    g.addColorStop(0, `rgba(${fib},0.42)`);             // continues the curve's tail
    g.addColorStop(1, `rgba(${fib},0.14)`);             // fading off toward the edge
    ctx.strokeStyle = g; ctx.lineWidth = 1.1*DPR;
    ctx.beginPath(); ctx.moveTo(t0x,t0y); ctx.lineTo(exx,eyy); ctx.stroke();
    ctx.restore();
    // label the run-back, set off the line so it stays clear of the drone marker
    if(s > 0.18){
      const mx = t0.x + bx*s*0.45, my = t0.y + by*s*0.45;
      const [lx,ly] = nToScreen(mx, my);
      textC('FIBER-OPTIC TETHER', lx, ly - 13*DPR, 9, `rgba(${fib},0.7)`);
    }
  }

  const [dx,dy] = nToScreen(dn.x, dn.y);
  const rgb = friendly ? '57,255,139' : '255,59,78';

  // HEADING: one short, bold arrow off the nose of the drone marking the
  // direction of travel -- not a full-grid projection lane.
  {
    const len = 30*DPR;                              // short, fixed length
    const sx = dx + Math.cos(lastHeading)*11*DPR;    // start just off the marker
    const sy = dy + Math.sin(lastHeading)*11*DPR;
    const hxp = dx + Math.cos(lastHeading)*len;
    const hyp = dy + Math.sin(lastHeading)*len;
    ctx.save();
    ctx.strokeStyle = `rgba(${rgb},0.95)`; ctx.lineCap = 'round';
    ctx.lineWidth = 3.5*DPR;
    ctx.shadowColor = `rgba(${rgb},0.85)`; ctx.shadowBlur = 6*DPR;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(hxp, hyp); ctx.stroke();
    // bold filled arrowhead
    const ah = 10*DPR;
    ctx.fillStyle = `rgba(${rgb},0.95)`;
    ctx.translate(hxp, hyp); ctx.rotate(lastHeading);
    ctx.beginPath(); ctx.moveTo(3*DPR, 0);
    ctx.lineTo(-ah, -ah*0.62); ctx.lineTo(-ah, ah*0.62);
    ctx.closePath(); ctx.fill();
    ctx.restore();
  }

  if(engaging){
    // LASER CUT: locked to where the firing solution was FIRST generated. The
    // beam runs from the Triple-D emitter to that fixed point and stays there
    // instead of chasing the drone -- the cut point and its orientation are
    // frozen on the first engage frame.
    if(!wasEngaging || !cutPoint){
      // base cut point: the backend solution if present, else just behind the drone
      let bx, by;
      if(state.laser_xy){ bx = state.laser_xy[0]; by = state.laser_xy[1]; }
      else { bx = dn.x - Math.cos(lastHeading)*0.10;
             by = dn.y - Math.sin(lastHeading)*0.10; }
      // nudge it a little FORWARD along the heading so the cut sits just inside
      // where the trailing fiber begins, instead of behind it
      const fwd = 0.06;
      cutPoint = { x: clamp(bx + Math.cos(lastHeading)*fwd, 0, 1),
                   y: clamp(by + Math.sin(lastHeading)*fwd, 0, 1) };
      cutHeading = lastHeading;
      // the drone's position picks which decoy fires (frozen so it doesn't
      // flicker between decoys as the drone moves)
      activeDecoyIdx = nearestDecoy(dn.x, dn.y);
    }
    const [ex2, ey2] = nToScreen(0.5, 0.5);                  // Triple-D emitter
    const [lx,ly]    = nToScreen(cutPoint.x, cutPoint.y);    // fixed cut point
    const perp = cutHeading + Math.PI/2;                     // across the fiber tether
    const sl = Math.min(gridRect.w, gridRect.h)*0.06;

    const flick = 0.5 + 0.5*Math.sin(now/40);
    ctx.save();
    ctx.strokeStyle = C.red; ctx.shadowColor = C.red; ctx.shadowBlur = 18*DPR;
    // anchored beam: emitter -> fixed cut point (does not move with the drone)
    ctx.globalAlpha = 0.4 + 0.6*flick;
    ctx.lineWidth = 2.5*DPR;
    ctx.beginPath(); ctx.moveTo(ex2,ey2); ctx.lineTo(lx,ly); ctx.stroke();
    // the cut span: a faint static line perpendicular to the fiber, marking the
    // stroke the laser works across.
    ctx.globalAlpha = 0.22;
    ctx.lineWidth = 1.5*DPR;
    ctx.beginPath();
    ctx.moveTo(lx + Math.cos(perp)*sl, ly + Math.sin(perp)*sl);
    ctx.lineTo(lx - Math.cos(perp)*sl, ly - Math.sin(perp)*sl);
    ctx.stroke();
    ctx.restore();

    // cutting head: a bright blade sawing BACK AND FORTH along that perpendicular
    // line -- the in-place motion that signifies the laser severing the fiber.
    const saw = Math.sin(now/90);                            // -1..1, back and forth
    const hx = lx + Math.cos(perp)*sl*saw;
    const hy = ly + Math.sin(perp)*sl*saw;
    const blade = sl*0.30;
    ctx.save();
    ctx.strokeStyle = C.red; ctx.shadowColor = C.red; ctx.shadowBlur = 22*DPR;
    ctx.lineWidth = 3.5*DPR; ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(hx + Math.cos(perp)*blade, hy + Math.sin(perp)*blade);
    ctx.lineTo(hx - Math.cos(perp)*blade, hy - Math.sin(perp)*blade);
    ctx.stroke();
    ctx.restore();
    glowDot(hx, hy, 4*DPR, C.red, 20);                       // hot point at the cut

    glowDot(lx, ly, 6*DPR, C.red, 20);
    textC('LASER · CUT', lx, ly-16*DPR, 11, C.red);

    // tasking link: which decoy the drone's position has cued, then its flash
    if(activeDecoyIdx >= 0){
      const [adx,ady] = decoyXY(activeDecoyIdx);
      ctx.save();
      ctx.setLineDash([4*DPR, 5*DPR]);
      ctx.strokeStyle = 'rgba(255,225,77,0.45)'; ctx.lineWidth = 1.3*DPR;
      ctx.beginPath(); ctx.moveTo(dx, dy); ctx.lineTo(adx, ady); ctx.stroke();
      ctx.restore();
      drawDecoyFlash(now, activeDecoyIdx);
    }
  }

  // the drone itself
  glowDot(dx, dy, 8*DPR, droneColor, 26);
  textC(friendly ? 'FRIENDLY' : 'ENEMY', dx, dy-18*DPR, 12, droneColor);
  // live grid reference of the target, reinforcing the GPS read
  const gref = 'E' + String(Math.round(dn.x*100)).padStart(2,'0')
             + ' · N' + String(Math.round((1-dn.y)*100)).padStart(2,'0');
  textC(gref, dx, dy+18*DPR, 9, C.dim);

  // overlay banner (top-centre, clear of the top-right camera window)
  const txt = state.overlay ||
    (friendly ? 'Friendly drone — returning to listening'
              : 'Tracking enemy drone — neutralizing threat');
  const col = friendly ? C.green : C.red;
  ctx.save();
  ctx.globalAlpha = friendly ? 1 : (0.7 + 0.3*Math.sin(now/240));
  // keep the banner clear of the (now taller) top bar on short viewports
  textC(txt, W*0.5, Math.max(H*0.085, 104*DPR), 24, col);
  ctx.restore();

  // drop the frozen cut solution + cued decoy once we're no longer engaging a foe
  if(!engaging){ cutPoint = null; activeDecoyIdx = -1; }
  wasEngaging = engaging;
}

// ===========================================================================
// CAMERA WINDOW : top-right, on screen the whole time. Live webcam frames when
// available, else a synthetic feed. While nothing is picked up it sits in a
// quiet STANDBY look; once acoustic noise wakes the vision model a blinking
// "NOISE DETECTED…" banner appears above it and the frame goes hot.
// ===========================================================================
function drawSyntheticFeed(x, y, w, h, now, active){
  // dark "sensor" backdrop
  const g = ctx.createLinearGradient(x, y, x, y+h);
  g.addColorStop(0, '#06121a'); g.addColorStop(1, '#03090d');
  ctx.fillStyle = g; ctx.fillRect(x, y, w, h);
  // faint scan grid
  ctx.strokeStyle = 'rgba(57,214,255,0.08)'; ctx.lineWidth = 1*DPR;
  for(let i=1;i<6;i++){
    ctx.beginPath(); ctx.moveTo(x, y+h*i/6); ctx.lineTo(x+w, y+h*i/6); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x+w*i/6, y); ctx.lineTo(x+w*i/6, y+h); ctx.stroke();
  }
  // sweeping scanline -- only while the camera is actually active, so a STANDBY
  // window doesn't show a moving green line
  if(active){
    const sy = y + ((now/16) % h);
    ctx.strokeStyle = 'rgba(57,255,139,0.35)'; ctx.lineWidth = 2*DPR;
    ctx.beginPath(); ctx.moveTo(x, sy); ctx.lineTo(x+w, sy); ctx.stroke();
  }
}

function drawCamera(now){
  const p = rightPanel();
  const w = p.w;
  const h = w*0.60;
  // frame top (drawn at y-22) sits flush with the detection grid's top edge,
  // so the camera window starts within the grid's height range.
  const x = p.x, y = gridRect.y + 22*DPR;

  // picked up = vision woke on acoustic noise (searching) or a confirmed track
  const picked = state.camera_on || state.mode !== 'listening';
  const tracking = state.mode === 'tracking';
  const frameCol = picked ? (tracking ? (state.iff==='friendly'?C.green:C.red)
                                      : C.yellow)
                          : C.cyan;

  // blinking "NOISE DETECTED…" banner above the window, only once picked up
  if(picked){
    const bl = 0.55 + 0.45*Math.sin(now/180);
    ctx.save(); ctx.globalAlpha = bl;
    textC('▲ NOISE DETECTED…', x + w/2, y - 34*DPR, 15, C.yellow);
    ctx.restore();
  }

  // window frame (tinted by state)
  ctx.save();
  ctx.fillStyle = 'rgba(4,10,14,0.92)';
  ctx.fillRect(x-6*DPR, y-22*DPR, w+12*DPR, h+30*DPR);
  ctx.strokeStyle = frameCol; ctx.lineWidth = (picked?2:1.5)*DPR;
  if(picked){ ctx.shadowColor = frameCol; ctx.shadowBlur = 14*DPR; }
  ctx.strokeRect(x-6*DPR, y-22*DPR, w+12*DPR, h+30*DPR);
  ctx.restore();

  // header bar: REC light blinks only when actually picked up
  const rec = 0.4 + 0.6*Math.abs(Math.sin(now/350));
  ctx.save(); ctx.globalAlpha = picked ? rec : 0.25;
  glowDot(x+6*DPR, y-11*DPR, 4*DPR, picked?C.red:C.grey, 10); ctx.restore();
  textC(picked?'REC':'STANDBY', x+18*DPR, y-11*DPR, 11, picked?C.red:C.dim, 'left');
  textC(picked?'CAM-01 · LIVE':'CAM-01 · IDLE', x+w, y-11*DPR, 11, frameCol, 'right');

  // feed
  ctx.save();
  ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip();
  if(camHasFrame && camImg.naturalWidth){
    // cover-fit the frame
    const ir = camImg.naturalWidth/camImg.naturalHeight, rr = w/h;
    let dw, dh;
    if(ir > rr){ dh = h; dw = h*ir; } else { dw = w; dh = w/ir; }
    ctx.drawImage(camImg, x+(w-dw)/2, y+(h-dh)/2, dw, dh);
  } else {
    drawSyntheticFeed(x, y, w, h, now, picked);
  }
  // centre reticle
  const cx = x+w/2, cy = y+h/2, rl = Math.min(w,h)*0.12;
  const rc = frameCol;
  ctx.save(); ctx.globalAlpha = picked ? 1 : 0.35;
  ctx.strokeStyle = rc; ctx.lineWidth = 1.5*DPR;
  ctx.beginPath(); ctx.moveTo(cx-rl, cy); ctx.lineTo(cx-rl*0.4, cy);
  ctx.moveTo(cx+rl*0.4, cy); ctx.lineTo(cx+rl, cy);
  ctx.moveTo(cx, cy-rl); ctx.lineTo(cx, cy-rl*0.4);
  ctx.moveTo(cx, cy+rl*0.4); ctx.lineTo(cx, cy+rl); ctx.stroke();
  if(tracking){
    ctx.strokeRect(cx-rl, cy-rl, rl*2, rl*2);
    textC('LOCK', cx, cy+rl+12*DPR, 11, rc);
  } else if(picked){
    textC('ACQUIRING…', cx, cy+rl+12*DPR, 11, rc);
  } else {
    textC('STANDBY', cx, cy+rl+12*DPR, 11, rc);
    textC('ACTIVATES WHEN DRONE IS HEARD', cx, cy+rl+26*DPR, 9, C.dim);
  }
  ctx.restore();
  ctx.restore();
}

// ===========================================================================
// MIC INDICATOR : bottom-right, on screen the whole time.
// ===========================================================================
function drawMic(now){
  const p = rightPanel();
  const bw = p.w, bh = bw*0.80;
  // frame bottom (y+bh) sits flush with the detection grid's bottom edge, so
  // the mic panel ends within the grid's height range.
  const x = p.x, y = gridRect.y + gridRect.h - bh;

  const ampN = clamp(state.amp/450, 0, 1);
  ampS = lerp(ampS, ampN, 0.22);

  // once noise is picked up the mic flips from calm cyan to a hot yellow
  const picked = state.camera_on || state.mode !== 'listening';
  const micCol = picked ? C.yellow : C.cyan;
  const micRGB = picked ? '255,225,77' : '57,214,255';

  // panel
  ctx.save();
  ctx.fillStyle = 'rgba(4,10,14,0.82)';
  ctx.fillRect(x, y, bw, bh);
  ctx.strokeStyle = picked ? micCol : '#13343a';
  ctx.lineWidth = (picked?1.5:1)*DPR;
  ctx.strokeRect(x, y, bw, bh);
  ctx.restore();

  const cx = x + bw*0.5, cy = y + bh*0.36;

  // 1) label ABOVE the microphone
  textC(picked ? 'NOISE DETECTED' : 'LISTENING', cx, y + bh*0.12, 14, micCol);

  // pulsing rings radiating from the mic, scaled by live amplitude
  for(let i=0;i<3;i++){
    const ph = ((now/900) + i/3) % 1;
    const rr = (14 + ph*48*(0.5+ampS)) * DPR;
    ctx.beginPath(); ctx.arc(cx, cy, rr, 0, Math.PI*2);
    ctx.strokeStyle = `rgba(${micRGB},${(1-ph)*0.4*(0.3+ampS)})`;
    ctx.lineWidth = 1.5*DPR; ctx.stroke();
  }

  // 2) microphone glyph — POPS in size with the incoming sound level
  ctx.save();
  ctx.translate(cx, cy);
  const s = DPR*0.36*(1 + ampS*0.5);   // base size grows with amplitude
  ctx.shadowColor = micCol; ctx.shadowBlur = (6+ampS*32)*DPR;
  ctx.strokeStyle = micCol; ctx.fillStyle = `rgba(${micRGB},0.14)`;
  ctx.lineWidth = 3*s;
  const cw = 46*s, ch = 96*s, r = cw/2;
  ctx.beginPath();
  ctx.moveTo(-r, -ch/2 + r);
  ctx.arc(0, -ch/2 + r, r, Math.PI, 0);
  ctx.lineTo(r, ch/2 - r);
  ctx.arc(0, ch/2 - r, r, 0, Math.PI);
  ctx.closePath(); ctx.fill(); ctx.stroke();
  ctx.beginPath(); ctx.arc(0, ch/2-8*s, r+22*s, 0, Math.PI, false); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(0, ch/2 + r+14*s); ctx.lineTo(0, ch/2 + r+30*s); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(-22*s, ch/2 + r+30*s); ctx.lineTo(22*s, ch/2 + r+30*s); ctx.stroke();
  ctx.restore();

  // 3) live sound wave under the mic — an oscilloscope strip driven by amplitude
  const wy = y + bh*0.78, ww = bw*0.82, wx = cx - ww/2, bars = 30;
  ctx.save();
  ctx.strokeStyle = micCol; ctx.lineWidth = 2*DPR; ctx.lineCap = 'round';
  for(let i=0;i<bars;i++){
    const fx = wx + ww*i/(bars-1);
    const env = Math.sin(Math.PI*i/(bars-1));               // taper toward edges
    const wob = Math.sin(i*0.7 + now/110)*Math.sin(i*0.33 - now/190);
    const hh = (1.5 + ampS*22*env*Math.abs(wob)) * DPR;
    ctx.beginPath(); ctx.moveTo(fx, wy-hh); ctx.lineTo(fx, wy+hh); ctx.stroke();
  }
  ctx.restore();

  // 4) confidence UNDER the microphone
  textC('conf ' + (state.acoustic_conf||0).toFixed(2)
        + '   ·   amp ' + Math.round(state.amp), cx, y + bh*0.93, 11, C.dim);
}

// ===========================================================================
function statusBar(){
  ctx.fillStyle = 'rgba(8,14,18,0.85)';
  ctx.fillRect(0, H-26*DPR, W, 26*DPR);
  ctx.strokeStyle = '#13343a'; ctx.lineWidth=1*DPR;
  ctx.beginPath(); ctx.moveTo(0,H-26*DPR); ctx.lineTo(W,H-26*DPR); ctx.stroke();
  const dot = state.online ? C.green : C.red;
  glowDot(16*DPR, H-13*DPR, 4*DPR, dot, 8);
  textC((state.online?'LINK':'NO LINK') + '  │  STATE ' + state.raw_state
        + '  │  ' + (state.iff?('IFF '+state.iff.toUpperCase()):'IFF —')
        + '  │  CAM ' + (state.camera_on?'ON':'OFF'),
        30*DPR, H-13*DPR, 12, C.dim, 'left');
  textC('TRIPLE-D  ·  D-D-D : Detect · Decide · Defeat',
        W-16*DPR, H-13*DPR, 12, C.dim, 'right');
}

// ===========================================================================
// LOGO + TOP BAR : the brand line across the top, same tactical language as the
// rest of the page -- a corner-bracket targeting reticle around a three-dot
// triad (Detect - Decide - Defeat), beside a tracked TRIPLE-D wordmark.
// ===========================================================================
function drawLogo(cx, cy, s, now){
  const half = s/2;
  const bl = s*0.30;                 // bracket leg length
  ctx.save();
  // breathing glow keeps the mark alive without drawing attention
  const pulse = 0.78 + 0.22*Math.sin(now/1400);
  ctx.strokeStyle = C.cyan; ctx.lineWidth = 1.5*DPR; ctx.lineCap = 'round';
  ctx.shadowColor = C.cyan; ctx.shadowBlur = 6*DPR;
  ctx.globalAlpha = pulse;
  const corners = [[-1,-1],[1,-1],[-1,1],[1,1]];
  for(const [sx, sy] of corners){
    const ox = sx*half, oy = sy*half;
    ctx.beginPath();
    ctx.moveTo(cx+ox, cy+oy - sy*bl);
    ctx.lineTo(cx+ox, cy+oy);
    ctx.lineTo(cx+ox - sx*bl, cy+oy);
    ctx.stroke();
  }
  // three dots in a triad -> Detect - Decide - Defeat
  const r = s*0.28;
  const triad = [[0, -r], [-r*0.92, r*0.58], [r*0.92, r*0.58]];
  ctx.globalAlpha = 1;
  for(const [ox, oy] of triad) glowDot(cx+ox, cy+oy, 2.3*DPR, C.cyan, 9);
  ctx.restore();
}

function topBar(now){
  // taller bar so the wordmark has real breathing room above and below it,
  // instead of being pinned against the hairline.
  const h = 75*DPR, pad = 22*DPR, cy = h/2;
  // backing + hairline divider, same tactical language as the bottom status bar
  ctx.fillStyle = 'rgba(8,14,18,0.85)';
  ctx.fillRect(0, 0, W, h);
  ctx.strokeStyle = '#13343a'; ctx.lineWidth = 1*DPR;
  ctx.beginPath(); ctx.moveTo(0, h); ctx.lineTo(W, h); ctx.stroke();

  // logo mark: left edge, vertically centred in the padded bar
  drawLogo(pad + 10*DPR, cy, 20*DPR, now);

  // TRIPLE-D wordmark, tracked, centred in the bar with a soft cyan glow.
  // Nudged left by half the tracking so the trailing letter-space doesn't
  // push the word optically right of dead centre.
  const track = 4*DPR;
  ctx.save();
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.font = '600 ' + (17*DPR) + 'px "SF Mono",ui-monospace,Menlo,monospace';
  ctx.letterSpacing = track + 'px';
  ctx.shadowColor = C.cyan; ctx.shadowBlur = 8*DPR;
  ctx.fillStyle = C.cyan;
  ctx.fillText('TRIPLE-D', W/2 - track/2, cy);
  ctx.letterSpacing = '0px';
  ctx.restore();

  // right side: quiet subtitle, balances the bar without repeating the footer
  ctx.save();
  ctx.letterSpacing = (2*DPR) + 'px';
  textC('OPERATOR DASHBOARD', W - pad, cy, 11, C.dim, 'right');
  ctx.letterSpacing = '0px';
  ctx.restore();
}

function frame(now){
  const tracking = state.mode === 'tracking';
  if(tracking && !wasTracking){ trail = []; trackStart = now; }
  wasTracking = tracking;

  ctx.fillStyle = C.bg; ctx.fillRect(0,0,W,H);
  drawGrid(now);                     // detection grid: always the view
  drawCamera(now);                   // camera window: on screen the whole time
  drawMic(now);                      // mic indicator: bottom-right, always on
  statusBar();
  topBar(now);                       // brand line: top, always on
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script>
</body>
</html>
"""
