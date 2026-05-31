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

// Right-hand column reserved for the camera (top) + mic (bottom) so they sit
// beside the grid instead of on top of it. The grid shrinks to its left.
function rightPanel(){
  const pad = Math.min(W,H)*0.035;
  const w = clamp(W*0.22, 230*DPR, 340*DPR);
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

function drawDecoy(now, active){
  // decoy lives at a fixed position every time; deactivated unless engaging
  const dc = state.decoy_xy ? {x:state.decoy_xy[0], y:state.decoy_xy[1]}
                            : {x:0.80, y:0.22};
  const [cxp,cyp] = nToScreen(dc.x, dc.y);
  if(active){
    const db = 0.5 + 0.5*Math.sin(now/160);
    ctx.save(); ctx.globalAlpha = db;
    glowDot(cxp, cyp, 7*DPR, C.yellow, 22);
    textC('DECOY', cxp, cyp-18*DPR, 12, C.yellow);
    ctx.restore();
  } else {
    // deactivated: dim hollow marker, no glow
    ctx.save();
    ctx.strokeStyle = C.grey; ctx.lineWidth = 1.5*DPR;
    ctx.beginPath(); ctx.arc(cxp, cyp, 6*DPR, 0, Math.PI*2); ctx.stroke();
    ctx.fillStyle = 'rgba(58,74,82,0.5)';
    ctx.beginPath(); ctx.arc(cxp, cyp, 2.5*DPR, 0, Math.PI*2); ctx.fill();
    textC('DECOY · STANDBY', cxp, cyp-16*DPR, 10, C.grey);
    ctx.restore();
  }
}

function drawGrid(now){
  const m = Math.min(W,H)*0.10;
  const p = rightPanel();
  // grid spans from the left margin up to (but not under) the right-hand panel
  gridRect = {x:m, y:m+H*0.06, w:(p.left - m*0.4) - m, h:H-2*m-H*0.06};

  // flat black grid
  ctx.fillStyle = '#02110d';
  ctx.fillRect(gridRect.x, gridRect.y, gridRect.w, gridRect.h);
  ctx.strokeStyle = C.grid; ctx.lineWidth = 1*DPR;
  const N = 16;
  for(let i=0;i<=N;i++){
    const gxp = gridRect.x + gridRect.w*i/N;
    const gyp = gridRect.y + gridRect.h*i/N;
    ctx.beginPath(); ctx.moveTo(gxp, gridRect.y); ctx.lineTo(gxp, gridRect.y+gridRect.h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(gridRect.x, gyp); ctx.lineTo(gridRect.x+gridRect.w, gyp); ctx.stroke();
  }
  ctx.strokeStyle = C.gridHot; ctx.lineWidth = 1.5*DPR;
  ctx.strokeRect(gridRect.x, gridRect.y, gridRect.w, gridRect.h);

  // the protected asset / emitter sits at the centre of the grid
  const [ex, ey] = nToScreen(0.5, 0.5);
  glowDot(ex, ey, 5*DPR, C.cyan, 16);
  textC('TRIPLE-D', ex, ey-16*DPR, 11, C.dim);

  const tracking = state.mode === 'tracking';
  const friendly = state.iff === 'friendly';
  const engaging = state.engaging && !friendly;

  // decoy is always present; only ACTIVE while engaging a foe
  drawDecoy(now, engaging);

  if(!tracking){
    // nothing detected -> empty grid (decoy already drawn, deactivated)
    trail = [];
    textC('NO CONTACT', gridRect.x+gridRect.w*0.5, gridRect.y+gridRect.h*0.72,
          16, 'rgba(90,122,134,0.5)');
    return;
  }

  // --- a drone is being tracked ---------------------------------------------
  const droneColor = friendly ? C.green : C.red;
  let dn = state.drone_xy ? {x:state.drone_xy[0], y:state.drone_xy[1]}
                          : simulateDrone(now);
  trail.push(dn); if(trail.length>60) trail.shift();

  // GENERAL DIRECTION: heading fitted over the whole track (first -> last),
  // so it reflects the straight-line path rather than per-frame jitter.
  if(trail.length>3){
    const a = trail[0], b = trail[trail.length-1];
    if(Math.hypot(b.x-a.x, b.y-a.y) > 1e-3)
      lastHeading = Math.atan2(b.y-a.y, b.x-a.x);
  }

  // fading trail
  for(let i=0;i<trail.length;i++){
    const p = trail[i], [px,py] = nToScreen(p.x,p.y);
    const a = i/trail.length;
    ctx.fillStyle = `rgba(${friendly?'57,255,139':'255,59,78'},${a*0.4})`;
    ctx.beginPath(); ctx.arc(px,py, 2*DPR + a*3*DPR, 0, Math.PI*2); ctx.fill();
  }

  const [dx,dy] = nToScreen(dn.x, dn.y);
  const rgb = friendly ? '57,255,139' : '255,59,78';

  // projected straight-line path: where the drone is generally headed, mapped
  // forward across the grid as a dashed lane with an arrowhead.
  {
    const proj = 0.55;     // normalised look-ahead distance
    const hx = clamp(dn.x + Math.cos(lastHeading)*proj, 0, 1);
    const hy = clamp(dn.y + Math.sin(lastHeading)*proj, 0, 1);
    const [hxp, hyp] = nToScreen(hx, hy);
    ctx.save();
    ctx.strokeStyle = `rgba(${rgb},0.30)`; ctx.lineWidth = 1.5*DPR;
    ctx.setLineDash([6*DPR, 7*DPR]);
    ctx.beginPath(); ctx.moveTo(dx, dy); ctx.lineTo(hxp, hyp); ctx.stroke();
    ctx.setLineDash([]);
    // arrowhead at the projected end
    const ah = 8*DPR;
    ctx.fillStyle = `rgba(${rgb},0.45)`;
    ctx.translate(hxp, hyp); ctx.rotate(lastHeading);
    ctx.beginPath(); ctx.moveTo(0,0);
    ctx.lineTo(-ah, -ah*0.5); ctx.lineTo(-ah, ah*0.5);
    ctx.closePath(); ctx.fill();
    ctx.restore();
    textC('HEADING', (dx+hxp)/2, (dy+hyp)/2 - 12*DPR, 10, C.dim);
  }

  if(engaging){
    // laser CUT POINT: behind the drone along its heading (where the trailing
    // fiber-optic tether runs), swept PERPENDICULAR to the flight path.
    const [ex2, ey2] = nToScreen(0.5, 0.5);
    let lp;
    if(state.laser_xy){ lp = {x:state.laser_xy[0], y:state.laser_xy[1]}; }
    else { lp = { x: clamp(dn.x - Math.cos(lastHeading)*0.10,0,1),
                  y: clamp(dn.y - Math.sin(lastHeading)*0.10,0,1) }; }
    const [lx,ly] = nToScreen(lp.x, lp.y);
    const flick = 0.5 + 0.5*Math.sin(now/40);
    ctx.save();
    ctx.globalAlpha = 0.4 + 0.6*flick;
    ctx.strokeStyle = C.red; ctx.shadowColor = C.red; ctx.shadowBlur = 18*DPR;
    // beam from the emitter to the cut point
    ctx.lineWidth = 2.5*DPR;
    ctx.beginPath(); ctx.moveTo(ex2,ey2); ctx.lineTo(lx,ly); ctx.stroke();
    // perpendicular sweep across the flight path at the cut point
    const perp = lastHeading + Math.PI/2;
    const sl = Math.min(gridRect.w, gridRect.h)*0.06;
    ctx.lineWidth = 3.5*DPR;
    ctx.beginPath();
    ctx.moveTo(lx + Math.cos(perp)*sl, ly + Math.sin(perp)*sl);
    ctx.lineTo(lx - Math.cos(perp)*sl, ly - Math.sin(perp)*sl);
    ctx.stroke();
    ctx.restore();
    glowDot(lx, ly, 6*DPR, C.red, 20);
    textC('LASER · CUT', lx, ly-16*DPR, 11, C.red);
  }

  // the drone itself
  glowDot(dx, dy, 8*DPR, droneColor, 26);
  textC(friendly ? 'FRIENDLY' : 'ENEMY', dx, dy-18*DPR, 12, droneColor);

  // overlay banner (top-centre, clear of the top-right camera window)
  const txt = state.overlay ||
    (friendly ? 'Friendly drone — returning to listening'
              : 'Tracking enemy drone — neutralizing threat');
  const col = friendly ? C.green : C.red;
  ctx.save();
  ctx.globalAlpha = friendly ? 1 : (0.7 + 0.3*Math.sin(now/240));
  textC(txt, W*0.5, H*0.07, 24, col);
  ctx.restore();
}

// ===========================================================================
// CAMERA WINDOW : top-right, on screen the whole time. Live webcam frames when
// available, else a synthetic feed. While nothing is picked up it sits in a
// quiet STANDBY look; once acoustic noise wakes the vision model a blinking
// "NOISE DETECTED…" banner appears above it and the frame goes hot.
// ===========================================================================
function drawSyntheticFeed(x, y, w, h, now){
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
  // sweeping scanline
  const sy = y + ((now/16) % h);
  ctx.strokeStyle = 'rgba(57,255,139,0.35)'; ctx.lineWidth = 2*DPR;
  ctx.beginPath(); ctx.moveTo(x, sy); ctx.lineTo(x+w, sy); ctx.stroke();
}

function drawCamera(now){
  const p = rightPanel();
  const w = p.w;
  const h = w*0.60;
  const x = p.x, y = p.pad + 30*DPR;   // leave room for the alert banner

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
    drawSyntheticFeed(x, y, w, h, now);
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
  } else {
    textC(picked ? 'ACQUIRING…' : 'STANDBY', cx, cy+rl+12*DPR, 11, rc);
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
  const x = p.x, y = H - bh - p.pad - 30*DPR;  // sit above the status bar

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

  const cx = x + bw*0.5, cy = y + bh*0.46;

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
  const s = DPR*0.40*(1 + ampS*0.7);   // base size grows with amplitude
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

function frame(now){
  const tracking = state.mode === 'tracking';
  if(tracking && !wasTracking){ trail = []; trackStart = now; }
  wasTracking = tracking;

  ctx.fillStyle = C.bg; ctx.fillRect(0,0,W,H);
  drawGrid(now);                     // detection grid: always the view
  drawCamera(now);                   // camera window: on screen the whole time
  drawMic(now);                      // mic indicator: bottom-right, always on
  statusBar();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
</script>
</body>
</html>
"""
