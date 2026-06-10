"""
routes_mtpx.py — MTPX Plus RGB skew and output peaking control.

Protocol: Telnet (TCP port 23), UTF-8, CRLF-terminated.
Commands (MTPXCommand from companion Swift app):
  Set skew:    W{input}*{r}*{g}*{b}Iseq   (values 0-31)
  Reset input: W{input}*0*0*0Iseq
  Reset all:   ESC ZK  (\x1bZK)
  Peaking:     W{output}*{0|1}Opek

Devices:
  MTPX 1 @ 10.0.0.15 — 8  inputs (16x8)
  MTPX 2 @ 10.0.0.16 — 16 inputs (16x16)
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
import os, socket, random
from shared import log

router = APIRouter()

SOCK_TO = int(os.getenv("SOCKET_TIMEOUT_SECONDS", "4"))

MTPX_DEVICES = [
    {"name": "MTPX 1", "host": "10.0.0.15", "inputs": 8},
    {"name": "MTPX 2", "host": "10.0.0.16", "inputs": 16},
]


# ─── transport ────────────────────────────────────────────────────────────────

def _send(host: str, cmd: str) -> bool:
    try:
        with socket.create_connection((host, 23), timeout=SOCK_TO) as s:
            s.sendall((cmd + "\r\n").encode("utf-8"))
        return True
    except Exception as e:
        log(f"MTPX {host} cmd={cmd!r}: {e}")
        return False


def _send_batch(host: str, cmds: list[str]) -> bool:
    """Send multiple commands over a single TCP connection."""
    try:
        with socket.create_connection((host, 23), timeout=SOCK_TO) as s:
            for cmd in cmds:
                s.sendall((cmd + "\r\n").encode("utf-8"))
        return True
    except Exception as e:
        log(f"MTPX {host} batch({len(cmds)} cmds): {e}")
        return False


def _clamp(v: int) -> int:
    return max(0, min(31, v))


def _preset_rgb(preset: str) -> tuple[int, int, int]:
    p = preset.lower().replace(" ", "").replace("_", "")
    if p == "mild":
        return random.randint(0, 10), random.randint(0, 10), random.randint(0, 10)
    if p == "medium":
        return random.randint(10, 20), random.randint(10, 20), random.randint(10, 20)
    if p == "extreme":
        return random.randint(20, 31), random.randint(20, 31), random.randint(20, 31)
    if p == "symmetrical":
        return 31, 0, 31
    if p == "redblast":
        return 31, 0, 0
    if p == "greenblast":
        return 0, 31, 0
    if p == "blueblast":
        return 0, 0, 31
    return 0, 0, 0


# ─── API ──────────────────────────────────────────────────────────────────────

@router.post("/api/mtpx/skew")
def api_mtpx_skew(host: str, input: int, r: int = 0, g: int = 0, b: int = 0):
    if not 1 <= input <= 16:
        return JSONResponse({"error": "input out of range"}, status_code=400)
    r, g, b = _clamp(r), _clamp(g), _clamp(b)
    cmd = f"W{input}*{r}*{g}*{b}Iseq"
    ok = _send(host, cmd)
    log(f"MTPX {host} input={input} R{r}/G{g}/B{b} ok={ok}")
    return JSONResponse({"ok": ok})


@router.post("/api/mtpx/reset")
def api_mtpx_reset(host: str, input: int = 0):
    if input == 0:
        ok = _send(host, "\x1bZK")
        log(f"MTPX {host} reset-all ok={ok}")
    else:
        ok = _send(host, f"W{input}*0*0*0Iseq")
        log(f"MTPX {host} reset input={input} ok={ok}")
    return JSONResponse({"ok": ok})


@router.post("/api/mtpx/preset")
def api_mtpx_preset(host: str, preset: str, inputs: int = 16):
    inputs = max(1, min(16, inputs))
    p = preset.lower().replace(" ", "").replace("_", "")

    if p == "resetall":
        ok = _send(host, "\x1bZK")
        state = {str(i): {"r": 0, "g": 0, "b": 0} for i in range(1, inputs + 1)}
        log(f"MTPX {host} preset=resetall ok={ok}")
        return JSONResponse({"ok": ok, "state": state})

    cmds, state = [], {}
    for inp in range(1, inputs + 1):
        r, g, b = _preset_rgb(p)
        cmds.append(f"W{inp}*{r}*{g}*{b}Iseq")
        state[str(inp)] = {"r": r, "g": g, "b": b}

    ok = _send_batch(host, cmds)
    log(f"MTPX {host} preset={preset} inputs={inputs} ok={ok}")
    return JSONResponse({"ok": ok, "state": state})


@router.post("/api/mtpx/peaking")
def api_mtpx_peaking(host: str, output: int, enabled: int = 1):
    cmd = f"W{output}*{1 if enabled else 0}Opek"
    ok = _send(host, cmd)
    log(f"MTPX {host} peaking output={output} enabled={bool(enabled)} ok={ok}")
    return JSONResponse({"ok": ok})


# ─── page ─────────────────────────────────────────────────────────────────────

@router.get("/control/mtpx", response_class=HTMLResponse)
def page_mtpx():
    return HTMLResponse(MTPX_HTML)


# ─── HTML ─────────────────────────────────────────────────────────────────────

MTPX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MTPX Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;user-select:none}
a{color:#fbbf24;text-decoration:none}
/* ── header ──────────────────────── */
.hdr{background:#1e293b;border-bottom:3px solid #d97706;padding:12px 18px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
.hdr-titles h1{font-size:1.15rem;font-weight:700}
.hdr-sub{font-size:.7rem;color:#64748b;margin-top:1px}
.dtabs{display:flex;gap:6px}
.dtab{padding:5px 14px;border-radius:20px;border:1px solid #334155;background:#0f172a;color:#64748b;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}
.dtab:hover{border-color:#d97706;color:#fbbf24}
.dtab.on{background:#d97706;border-color:#d97706;color:#0a0e1a;font-weight:700}
.link-wrap{display:flex;align-items:center;gap:7px;font-size:.82rem;color:#94a3b8;cursor:pointer}
.link-wrap input{accent-color:#d97706;width:15px;height:15px;cursor:pointer}
.link-badge{background:#d97706;color:#0a0e1a;border-radius:4px;padding:2px 9px;font-size:.7rem;font-weight:700;display:none}
.link-badge.on{display:inline}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:14px}
.hdr-back{font-size:.78rem;color:#475569}
.hdr-back:hover{color:#fbbf24}
/* ── preset bar ──────────────────── */
.pbar{display:flex;gap:7px;align-items:center;padding:9px 18px;background:#0f172a;border-bottom:1px solid #1e293b;flex-wrap:wrap}
.pbt{padding:5px 13px;border-radius:7px;border:1px solid #334155;background:transparent;color:#94a3b8;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}
.pbt:hover{border-color:#d97706;color:#fbbf24}
.pbt.rst{color:#64748b}
.pbt.rst:hover{border-color:#ef4444;color:#fca5a5}
.pbt.sym{border-color:#4c1d95;color:#c4b5fd}
.pbt.sym:hover{background:#4c1d95;color:#fff}
.pbt.red{border-color:#7f1d1d;color:#fca5a5}
.pbt.red:hover{background:#7f1d1d;color:#fff}
.pbt.grn{border-color:#14532d;color:#86efac}
.pbt.grn:hover{background:#14532d;color:#fff}
.pbt.blu{border-color:#1e3a8a;color:#93c5fd}
.pbt.blu:hover{background:#1e3a8a;color:#fff}
.pbt.brst{border-color:#92400e;color:#fcd34d}
.pbt.brst:hover{background:#92400e;color:#fff}
/* ── status line ─────────────────── */
.sbar{padding:4px 18px;font-size:.72rem;color:#334155;font-family:monospace;background:#0a0e1a;border-bottom:1px solid #0f172a;min-height:20px}
/* ── input grid ──────────────────── */
.grid{display:grid;grid-template-columns:repeat(8,1fr);gap:8px;padding:12px 16px 20px}
@media(max-width:1100px){.grid{grid-template-columns:repeat(4,1fr)}}
@media(max-width:580px){.grid{grid-template-columns:repeat(2,1fr)}}
/* ── input card ──────────────────── */
.card{background:#1e293b;border:1px solid #334155;border-radius:9px;padding:9px 10px;transition:border-color .15s}
.card:hover{border-color:#d9770622}
.card-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.inp-lbl{font-size:.8rem;font-weight:700;color:#e2e8f0}
.rst-btn{background:none;border:none;color:#334155;cursor:pointer;font-size:.9rem;padding:0 2px;transition:color .15s;line-height:1}
.rst-btn:hover{color:#fca5a5}
/* ── RGB bars ────────────────────── */
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.bar-row:last-child{margin-bottom:0}
.ch{font-size:.68rem;font-weight:700;font-family:monospace;width:10px;flex-shrink:0;line-height:1}
.ch.r{color:#ef4444}.ch.g{color:#22c55e}.ch.b{color:#3b82f6}
.bar-track{flex:1;height:13px;background:rgba(255,255,255,0.07);border-radius:3px;cursor:crosshair;position:relative;overflow:hidden;transition:background .1s}
.bar-track:hover{background:rgba(255,255,255,0.12)}
.bar-fill{height:100%;border-radius:3px;pointer-events:none;transition:width .05s}
.bar-val{font-size:.68rem;font-family:monospace;color:#64748b;width:20px;text-align:right;flex-shrink:0}
/* ── toast ───────────────────────── */
#toast{position:fixed;bottom:18px;right:18px;background:#1e293b;border:1px solid #d97706;color:#e2e8f0;padding:9px 15px;border-radius:9px;font-size:.82rem;display:none;z-index:999;box-shadow:0 4px 20px #0009;max-width:320px}
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<div class="hdr">
  <div class="hdr-titles">
    <h1>MTPX Plus</h1>
    <div class="hdr-sub">RGB skew &amp; output peaking</div>
  </div>
  <div class="dtabs" id="dtabs"></div>
  <label class="link-wrap">
    <input type="checkbox" id="linked" onchange="setLinked(this.checked)">
    Linked
    <span class="link-badge" id="link-badge">LINKED</span>
  </label>
  <div class="hdr-right">
    <a class="hdr-back" href="/">← Dashboard</a>
  </div>
</div>

<!-- ── Preset bar ───────────────────────────────────────────────────────────── -->
<div class="pbar">
  <button class="pbt rst" onclick="applyPreset('resetall')">↺ Reset All</button>
  <button class="pbt"     onclick="applyPreset('mild')">Mild</button>
  <button class="pbt"     onclick="applyPreset('medium')">Medium</button>
  <button class="pbt"     onclick="applyPreset('extreme')">Extreme</button>
  <button class="pbt sym" onclick="applyPreset('symmetrical')">Symmetrical</button>
  <button class="pbt red" onclick="applyPreset('redblast')">Red Blast</button>
  <button class="pbt grn" onclick="applyPreset('greenblast')">Green Blast</button>
  <button class="pbt blu" onclick="applyPreset('blueblast')">Blue Blast</button>
  <button class="pbt brst" onclick="applyPreset('blueblast');applyPreset('resetall')" style="margin-left:auto" title="Send all-blue then immediately reset — delay stress test">⚡ Burst 0/0/31</button>
</div>

<!-- ── Status line ─────────────────────────────────────────────────────────── -->
<div class="sbar" id="sbar">Ready</div>

<!-- ── Input grid ──────────────────────────────────────────────────────────── -->
<div class="grid" id="grid"></div>

<div id="toast"></div>

<script>
// ── Config ────────────────────────────────────────────────────────────────────
const DEVICES = [
  {name:'MTPX 1', host:'10.0.0.15', inputs:8},
  {name:'MTPX 2', host:'10.0.0.16', inputs:16},
];

// ── State ─────────────────────────────────────────────────────────────────────
const STATE = {};
DEVICES.forEach(d => {
  STATE[d.host] = {};
  for (let i = 1; i <= 16; i++) STATE[d.host][i] = {r:0, g:0, b:0};
});

let activeIdx  = 0;
let linked     = false;
let _drag      = null;   // {ch, inp}
let _lastSend  = {};
let _toastTmr;

// ── Boot ──────────────────────────────────────────────────────────────────────
function setup() {
  const tabs = document.getElementById('dtabs');
  DEVICES.forEach((d, i) => {
    const b = document.createElement('button');
    b.className = 'dtab' + (i === 0 ? ' on' : '');
    b.textContent = d.name + ' (' + d.inputs + ' in)';
    b.onclick = () => selectDevice(i);
    tabs.appendChild(b);
  });
  document.addEventListener('mousemove', e => { if (_drag) applyDrag(e.clientX); });
  document.addEventListener('mouseup',   () => { _drag = null; });
  buildGrid();
}

// ── Device / link ─────────────────────────────────────────────────────────────
function selectDevice(idx) {
  activeIdx = idx;
  document.querySelectorAll('.dtab').forEach((b, i) => b.classList.toggle('on', i === idx));
  buildGrid();
}

function setLinked(val) {
  linked = val;
  document.getElementById('link-badge').classList.toggle('on', val);
  setStatus(val ? 'Linked — commands broadcast to both devices' : 'Unlinked');
}

function getHosts() {
  return linked ? DEVICES.map(d => d.host) : [DEVICES[activeIdx].host];
}

function activeDev() { return DEVICES[activeIdx]; }

// ── Grid ──────────────────────────────────────────────────────────────────────
function buildGrid() {
  const dev  = activeDev();
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (let i = 1; i <= dev.inputs; i++) grid.appendChild(makeCard(dev.host, i));
}

function makeCard(host, inp) {
  const st = STATE[host][inp];
  const el = document.createElement('div');
  el.className = 'card';
  el.id = 'card-' + inp;
  el.innerHTML =
    '<div class="card-hdr">' +
      '<span class="inp-lbl">Input ' + inp + '</span>' +
      '<button class="rst-btn" onclick="resetInput(' + inp + ')" title="Reset input ' + inp + '">↺</button>' +
    '</div>' +
    mkBar('r', inp, st.r) +
    mkBar('g', inp, st.g) +
    mkBar('b', inp, st.b);
  return el;
}

function mkBar(ch, inp, val) {
  const pct  = (val / 31 * 100).toFixed(1);
  const grad = {
    r:'linear-gradient(90deg,#ef444433,#ef4444)',
    g:'linear-gradient(90deg,#22c55e33,#22c55e)',
    b:'linear-gradient(90deg,#3b82f633,#3b82f6)'
  }[ch];
  return '<div class="bar-row">' +
    '<span class="ch ' + ch + '">' + ch.toUpperCase() + '</span>' +
    '<div class="bar-track" id="trk-' + ch + '-' + inp +
        '" onmousedown="barDown(event,\'' + ch + '\',' + inp + ')">' +
      '<div class="bar-fill" id="fill-' + ch + '-' + inp +
          '" style="width:' + pct + '%;background:' + grad + '"></div>' +
    '</div>' +
    '<span class="bar-val" id="bv-' + ch + '-' + inp + '">' + val + '</span>' +
  '</div>';
}

// ── Bar interaction ───────────────────────────────────────────────────────────
function barDown(e, ch, inp) {
  e.preventDefault();
  _drag = {ch, inp};
  applyDrag(e.clientX);
}

function applyDrag(clientX) {
  const {ch, inp} = _drag;
  const trk = document.getElementById('trk-' + ch + '-' + inp);
  if (!trk) return;
  const rect = trk.getBoundingClientRect();
  const val  = Math.max(0, Math.min(31, Math.round((clientX - rect.left) / rect.width * 31)));
  setChannel(inp, ch, val);
}

// ── Channel logic ─────────────────────────────────────────────────────────────
function setChannel(inp, ch, val) {
  val = Math.max(0, Math.min(31, val));
  const hosts = getHosts();

  // Update local state + UI immediately (feels instant)
  hosts.forEach(host => { STATE[host][inp][ch] = val; });
  updateBar(ch, inp, val);

  // Throttle network sends to ~80 ms (matching Swift app drag rate)
  const key = ch + '-' + inp;
  const now = Date.now();
  if (now - (_lastSend[key] || 0) < 80) return;
  _lastSend[key] = now;

  hosts.forEach(host => {
    const st = STATE[host][inp];
    sendSkew(host, inp, st.r, st.g, st.b);
  });
}

function updateBar(ch, inp, val) {
  const fill = document.getElementById('fill-' + ch + '-' + inp);
  const lbl  = document.getElementById('bv-'   + ch + '-' + inp);
  if (fill) fill.style.width = (val / 31 * 100).toFixed(1) + '%';
  if (lbl)  lbl.textContent  = val;
}

// ── API ───────────────────────────────────────────────────────────────────────
function sendSkew(host, inp, r, g, b) {
  const url = '/api/mtpx/skew?host=' + host + '&input=' + inp +
              '&r=' + r + '&g=' + g + '&b=' + b;
  fetch(url, {method:'POST'})
    .then(r => r.json())
    .then(j => {
      if (!j.ok) setStatus('⚠ ' + host + ' not responding — is it connected?');
      else setStatus('W' + inp + '*' + r + '*' + g + '*' + b + 'Iseq → ' + host);
    })
    .catch(() => setStatus('Network error'));
}

async function resetInput(inp) {
  const hosts = getHosts();
  hosts.forEach(host => { STATE[host][inp] = {r:0, g:0, b:0}; });
  ['r','g','b'].forEach(ch => updateBar(ch, inp, 0));
  for (const host of hosts) {
    fetch('/api/mtpx/reset?host=' + host + '&input=' + inp, {method:'POST'});
  }
  toast('Reset Input ' + inp);
  setStatus('Reset input ' + inp + ' → ' + hosts.join(', '));
}

async function applyPreset(preset) {
  const hosts = getHosts();
  setStatus('Applying ' + preset + '…');
  for (const host of hosts) {
    const dev = DEVICES.find(d => d.host === host) || DEVICES[0];
    const url = '/api/mtpx/preset?host=' + host + '&preset=' + encodeURIComponent(preset) +
                '&inputs=' + dev.inputs;
    try {
      const res = await fetch(url, {method:'POST'});
      const j   = await res.json();
      if (j.state) {
        Object.entries(j.state).forEach(([inp, vals]) => {
          STATE[host][parseInt(inp)] = vals;
        });
      }
      if (!j.ok) toast('⚠ ' + host + ' not responding');
    } catch(e) { toast('Network error'); }
  }
  buildGrid();
  toast('Applied: ' + preset);
  setStatus('Applied preset "' + preset + '" → ' + hosts.join(', '));
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function setStatus(msg) {
  document.getElementById('sbar').textContent = msg;
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(_toastTmr);
  _toastTmr = setTimeout(() => el.style.display = 'none', 2600);
}

setup();
</script>
</body>
</html>"""
