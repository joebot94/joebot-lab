"""
routes_mtpx.py — MTPX Plus RGB skew, output skew, peaking, and auto-mode control.

Protocol: Telnet (TCP port 23), UTF-8, CRLF-terminated.
Commands (MTPXCommand from companion Swift app MTPXControl-claude-code):
  Input skew:   W{input}*{r}*{g}*{b}Iseq    (values 0-31)
  Output skew:  W{output}*{r}*{g}*{b}Oseq   (values 0-31)
  Reset input:  W{input}*0*0*0Iseq
  Reset all:    ESC ZK  (\x1bZK)
  Peaking:      W{output}*{0|1}Opek
  Verbose:      W{0|1|3}CV

Devices:
  MTPX 1 @ 10.0.0.15 — 8  inputs / 8  outputs (16x8)
  MTPX 2 @ 10.0.0.16 — 16 inputs / 16 outputs (16x16)

Auto modes run client-side (setInterval in the page JS) so they stop when the
page closes; multi-command ticks go through /api/mtpx/batch which chains all
commands over a single TCP connection.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import os, socket, random
from shared import log

router = APIRouter()

SOCK_TO = int(os.getenv("SOCKET_TIMEOUT_SECONDS", "4"))

MTPX_DEVICES = [
    {"name": "MTPX 1", "host": "10.0.0.15", "inputs": 8,  "outputs": 8},
    {"name": "MTPX 2", "host": "10.0.0.16", "inputs": 16, "outputs": 16},
]

_ALLOWED_HOSTS = {d["host"] for d in MTPX_DEVICES}


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


@router.post("/api/mtpx/oskew")
def api_mtpx_oskew(host: str, output: int, r: int = 0, g: int = 0, b: int = 0):
    if not 1 <= output <= 16:
        return JSONResponse({"error": "output out of range"}, status_code=400)
    r, g, b = _clamp(r), _clamp(g), _clamp(b)
    cmd = f"W{output}*{r}*{g}*{b}Oseq"
    ok = _send(host, cmd)
    log(f"MTPX {host} output={output} skew R{r}/G{g}/B{b} ok={ok}")
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


@router.post("/api/mtpx/oreset")
def api_mtpx_oreset(host: str, output: int = 0, outputs: int = 16):
    if output == 0:
        cmds = [f"W{o}*0*0*0Oseq" for o in range(1, max(1, min(16, outputs)) + 1)]
        ok = _send_batch(host, cmds)
        log(f"MTPX {host} reset all output skew ok={ok}")
    else:
        ok = _send(host, f"W{output}*0*0*0Oseq")
        log(f"MTPX {host} reset output={output} skew ok={ok}")
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


@router.post("/api/mtpx/route")
def api_mtpx_route(host: str, input: int, output: int):
    """Tie input to output (standard Extron SIS crosspoint). input=0 unties."""
    if not (0 <= input <= 16 and 1 <= output <= 16):
        return JSONResponse({"error": "out of range"}, status_code=400)
    cmd = f"{input}*{output}!"
    ok = _send(host, cmd)
    log(f"MTPX {host} route {input}*{output}! ok={ok}")
    return JSONResponse({"ok": ok})


@router.post("/api/mtpx/peaking")
def api_mtpx_peaking(host: str, output: int, enabled: int = 1):
    cmd = f"W{output}*{1 if enabled else 0}Opek"
    ok = _send(host, cmd)
    log(f"MTPX {host} peaking output={output} enabled={bool(enabled)} ok={ok}")
    return JSONResponse({"ok": ok})


@router.post("/api/mtpx/batch")
async def api_mtpx_batch(request: Request):
    """Chain many commands over one TCP connection — used by auto-mode ticks.

    Body: {"host": "...", "cmds": [
        {"kind": "iseq", "n": 1, "r": 0, "g": 0, "b": 31},
        {"kind": "oseq", "n": 2, "r": 5, "g": 0, "b": 0},
        {"kind": "opek", "n": 3, "en": 0},
        {"kind": "zk"}
    ]}
    Commands are built server-side from structured fields — raw strings are
    never accepted from the client.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    host = body.get("host", "")
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"error": "unknown host"}, status_code=400)

    cmds: list[str] = []
    for c in body.get("cmds", [])[:64]:
        kind = c.get("kind")
        if kind == "zk":
            cmds.append("\x1bZK")
            continue
        if kind == "tie":
            i, o = int(c.get("i", -1)), int(c.get("o", 0))
            if 0 <= i <= 16 and 1 <= o <= 16:
                cmds.append(f"{i}*{o}!")
            continue
        n = int(c.get("n", 0))
        if not 1 <= n <= 16:
            continue
        if kind == "iseq":
            cmds.append(f"W{n}*{_clamp(int(c.get('r',0)))}*{_clamp(int(c.get('g',0)))}*{_clamp(int(c.get('b',0)))}Iseq")
        elif kind == "oseq":
            cmds.append(f"W{n}*{_clamp(int(c.get('r',0)))}*{_clamp(int(c.get('g',0)))}*{_clamp(int(c.get('b',0)))}Oseq")
        elif kind == "opek":
            cmds.append(f"W{n}*{1 if c.get('en') else 0}Opek")

    if not cmds:
        return JSONResponse({"error": "no valid commands"}, status_code=400)

    ok = _send_batch(host, cmds)
    return JSONResponse({"ok": ok, "sent": len(cmds)})


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
/* ── auto bar ────────────────────── */
.abar{display:flex;gap:10px;align-items:center;padding:9px 18px;background:#0d1322;border-bottom:1px solid #1e293b;flex-wrap:wrap}
.abar label{font-size:.72rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
.abar select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:7px;padding:4px 8px;font-size:.8rem}
.abar input[type=range]{accent-color:#d97706;width:110px}
.aval{font-size:.75rem;font-family:monospace;color:#94a3b8;min-width:46px}
.abt{padding:6px 18px;border-radius:7px;border:1px solid #14532d;background:transparent;color:#86efac;cursor:pointer;font-size:.82rem;font-weight:700;transition:.15s}
.abt:hover{background:#14532d;color:#fff}
.abt.stop{border-color:#7f1d1d;color:#fca5a5;animation:pulse 1.2s infinite}
.abt.stop:hover{background:#7f1d1d;color:#fff}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 #ef444455}50%{box-shadow:0 0 0 6px #ef444400}}
.flk-wrap{display:flex;align-items:center;gap:6px;font-size:.78rem;color:#94a3b8;cursor:pointer}
.flk-wrap input{accent-color:#d97706;cursor:pointer}
.tickct{font-size:.72rem;font-family:monospace;color:#475569;margin-left:auto}
/* ── status line ─────────────────── */
.sbar{padding:4px 18px;font-size:.72rem;color:#334155;font-family:monospace;background:#0a0e1a;border-bottom:1px solid #0f172a;min-height:20px}
/* ── section headers ─────────────── */
.sect{display:flex;align-items:center;gap:10px;padding:14px 18px 2px;cursor:pointer}
.sect h2{font-size:.85rem;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.08em}
.sect .chev{color:#475569;font-size:.75rem;transition:transform .15s}
.sect.closed .chev{transform:rotate(-90deg)}
.sect .sect-note{font-size:.7rem;color:#475569;font-family:monospace}
.sect-body.closed{display:none}
/* ── grids ───────────────────────── */
.grid{display:grid;grid-template-columns:repeat(8,1fr);gap:8px;padding:12px 16px 20px}
@media(max-width:1100px){.grid{grid-template-columns:repeat(4,1fr)}}
@media(max-width:580px){.grid{grid-template-columns:repeat(2,1fr)}}
/* ── cards ───────────────────────── */
.card{background:#1e293b;border:1px solid #334155;border-radius:9px;padding:9px 10px;transition:border-color .15s}
.card:hover{border-color:#d9770622}
.card.ocard{background:#181f31;border-color:#2b3650}
.card-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:4px}
.inp-lbl{font-size:.8rem;font-weight:700;color:#e2e8f0}
.rst-btn{background:none;border:none;color:#334155;cursor:pointer;font-size:.9rem;padding:0 2px;transition:color .15s;line-height:1}
.rst-btn:hover{color:#fca5a5}
.peak-btn{font-size:.62rem;font-weight:700;font-family:monospace;border:1px solid #334155;background:transparent;color:#475569;border-radius:5px;padding:2px 7px;cursor:pointer;transition:.15s;letter-spacing:.04em}
.peak-btn.on{border-color:#d97706;background:#d9770622;color:#fbbf24}
/* ── RGB bars ────────────────────── */
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:5px}
.bar-row:last-child{margin-bottom:0}
.ch{font-size:.68rem;font-weight:700;font-family:monospace;width:10px;flex-shrink:0;line-height:1}
.ch.r{color:#ef4444}.ch.g{color:#22c55e}.ch.b{color:#3b82f6}
.bar-track{flex:1;height:13px;background:rgba(255,255,255,0.07);border-radius:3px;cursor:crosshair;position:relative;overflow:hidden;transition:background .1s}
.bar-track:hover{background:rgba(255,255,255,0.12)}
.bar-fill{height:100%;border-radius:3px;pointer-events:none;transition:width .05s}
.bar-val{font-size:.68rem;font-family:monospace;color:#64748b;width:20px;text-align:right;flex-shrink:0}
/* ── crosspoint grid ─────────────── */
.xp-wrap{padding:12px 16px 20px;overflow-x:auto}
.xp{border-collapse:collapse}
.xp th{font-size:.62rem;color:#64748b;font-family:monospace;padding:3px;font-weight:600;min-width:24px}
.xp td{padding:2px}
.xc{width:22px;height:22px;border-radius:5px;border:1px solid #263145;background:#131a2b;cursor:pointer;transition:.1s;display:block}
.xc:hover{border-color:#d97706}
.xc.on{background:#d97706;border-color:#fbbf24;box-shadow:0 0 6px #d9770688}
.rowlbl{font-size:.66rem;color:#94a3b8;font-family:monospace;padding-right:8px;text-align:right;white-space:nowrap}
/* ── toast ───────────────────────── */
#toast{position:fixed;bottom:18px;right:18px;background:#1e293b;border:1px solid #d97706;color:#e2e8f0;padding:9px 15px;border-radius:9px;font-size:.82rem;display:none;z-index:999;box-shadow:0 4px 20px #0009;max-width:320px}
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<div class="hdr">
  <div class="hdr-titles">
    <h1>MTPX Plus</h1>
    <div class="hdr-sub">RGB skew · output skew · peaking · auto</div>
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
  <button class="pbt rst" onclick="panic()" title="Stop auto + reset everything">🛑 Panic</button>
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

<!-- ── Auto bar ─────────────────────────────────────────────────────────────── -->
<div class="abar">
  <label>Auto</label>
  <select id="auto-pattern">
    <option value="random">Fully Random</option>
    <option value="sweep">Sweep</option>
    <option value="chaos">Chaos</option>
    <option value="bluepulse">Blue / Reset Pulse</option>
    <option value="blast">Full Board Blast</option>
  </select>
  <label>Rate</label>
  <input type="range" id="auto-rate" min="1" max="20" value="6" oninput="autoRateChanged(this.value)">
  <span class="aval" id="auto-rate-val">6/s</span>
  <label class="flk-wrap" title="Randomly drops output peaking for a moment, then restores it">
    <input type="checkbox" id="flk-on"> Peak flicker
  </label>
  <input type="range" id="flk-chance" min="5" max="80" value="20" oninput="document.getElementById('flk-chance-val').textContent=this.value+'%'">
  <span class="aval" id="flk-chance-val">20%</span>
  <button class="abt" id="auto-btn" onclick="toggleAuto()">▶ Start</button>
  <span class="tickct" id="tickct"></span>
</div>

<!-- ── Status line ─────────────────────────────────────────────────────────── -->
<div class="sbar" id="sbar">Ready</div>

<!-- ── Routing ─────────────────────────────────────────────────────────────── -->
<div class="sect" onclick="toggleSect(this,'xp-wrap')">
  <h2>Routing</h2><span class="chev">▼</span>
  <span class="sect-note">{in}*{out}! · click a lit cell to untie · state is last-sent (no live query yet)</span>
</div>
<div class="xp-wrap sect-body" id="xp-wrap"></div>

<!-- ── Input skew ──────────────────────────────────────────────────────────── -->
<div class="sect" onclick="toggleSect(this,'grid')">
  <h2>Input Skew</h2><span class="chev">▼</span>
  <span class="sect-note">W{in}*{r}*{g}*{b}Iseq</span>
</div>
<div class="grid sect-body" id="grid"></div>

<!-- ── Output skew + peaking ───────────────────────────────────────────────── -->
<div class="sect" onclick="toggleSect(this,'ogrid')">
  <h2>Output Skew &amp; Peaking</h2><span class="chev">▼</span>
  <span class="sect-note">W{out}*{r}*{g}*{b}Oseq · W{out}*{0|1}Opek</span>
  <button class="pbt rst" style="margin-left:auto;font-size:.7rem;padding:3px 10px"
          onclick="event.stopPropagation();resetAllOutputs()">↺ Reset output skew</button>
</div>
<div class="grid sect-body" id="ogrid"></div>

<div id="toast"></div>

<script>
// ── Config ────────────────────────────────────────────────────────────────────
const DEVICES = [
  {name:'MTPX 1', host:'10.0.0.15', inputs:8,  outputs:8},
  {name:'MTPX 2', host:'10.0.0.16', inputs:16, outputs:16},
];

// ── State ─────────────────────────────────────────────────────────────────────
// STATE[host].i[n] = input skew, STATE[host].o[n] = output skew, .peak[n] = bool
const STATE = {};
DEVICES.forEach(d => {
  STATE[d.host] = {i:{}, o:{}, peak:{}, tie:{}};
  for (let n = 1; n <= 16; n++) {
    STATE[d.host].i[n] = {r:0, g:0, b:0};
    STATE[d.host].o[n] = {r:0, g:0, b:0};
    STATE[d.host].peak[n] = true;
    STATE[d.host].tie[n] = 0;   // input currently tied to output n (0 = unknown/untied)
  }
});

let activeIdx  = 0;
let linked     = false;
let _drag      = null;   // {kind:'i'|'o', ch, n}
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
  buildGrids();
}

function toggleSect(hdr, bodyId) {
  hdr.classList.toggle('closed');
  document.getElementById(bodyId).classList.toggle('closed');
}

// ── Device / link ─────────────────────────────────────────────────────────────
function selectDevice(idx) {
  activeIdx = idx;
  document.querySelectorAll('.dtab').forEach((b, i) => b.classList.toggle('on', i === idx));
  buildGrids();
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

// ── Grids ─────────────────────────────────────────────────────────────────────
function buildGrids() {
  const dev = activeDev();
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (let n = 1; n <= dev.inputs; n++) grid.appendChild(makeCard('i', dev.host, n));
  const ogrid = document.getElementById('ogrid');
  ogrid.innerHTML = '';
  for (let n = 1; n <= dev.outputs; n++) ogrid.appendChild(makeCard('o', dev.host, n));
  buildXp();
}

// ── crosspoint routing grid ───────────────────────────────────────────────────
function buildXp() {
  const dev = activeDev();
  const ties = STATE[dev.host].tie;
  let h = '<table class="xp"><tr><th></th>';
  for (let i = 1; i <= dev.inputs; i++) h += '<th>' + i + '</th>';
  h += '<th style="color:#334155;padding-left:8px">in →</th></tr>';
  for (let o = 1; o <= dev.outputs; o++) {
    h += '<tr><td class="rowlbl">out ' + o + '</td>';
    for (let i = 1; i <= dev.inputs; i++) {
      h += '<td><span class="xc' + (ties[o] === i ? ' on' : '') + '" id="xc-' + i + '-' + o +
           '" onclick="tie(' + i + ',' + o + ')" title="' + i + '*' + o + '!"></span></td>';
    }
    h += '</tr>';
  }
  h += '</table>';
  document.getElementById('xp-wrap').innerHTML = h;
}

function tie(i, o) {
  const hosts = getHosts();
  const untying = STATE[activeDev().host].tie[o] === i;
  const inp = untying ? 0 : i;
  hosts.forEach(host => {
    STATE[host].tie[o] = inp;
    fetch('/api/mtpx/route?host=' + host + '&input=' + inp + '&output=' + o, {method:'POST'})
      .then(r => r.json())
      .then(j => {
        if (!j.ok) setStatus('⚠ ' + host + ' not responding — is it connected?');
        else setStatus(inp + '*' + o + '! → ' + host);
      })
      .catch(() => setStatus('Network error'));
  });
  buildXp();
}

function makeCard(kind, host, n) {
  const st = STATE[host][kind][n];
  const el = document.createElement('div');
  el.className = 'card' + (kind === 'o' ? ' ocard' : '');
  const lbl = (kind === 'i' ? 'Input ' : 'Out ') + n;
  const peak = kind === 'o'
    ? '<button class="peak-btn' + (STATE[host].peak[n] ? ' on' : '') + '" id="peak-' + n +
      '" onclick="togglePeak(' + n + ')" title="Output peaking">PEAK</button>'
    : '';
  el.innerHTML =
    '<div class="card-hdr">' +
      '<span class="inp-lbl">' + lbl + '</span>' + peak +
      '<button class="rst-btn" onclick="resetOne(\\'' + kind + '\\',' + n + ')" title="Reset ' + lbl + '">↺</button>' +
    '</div>' +
    mkBar(kind, 'r', n, st.r) +
    mkBar(kind, 'g', n, st.g) +
    mkBar(kind, 'b', n, st.b);
  return el;
}

function mkBar(kind, ch, n, val) {
  const pct  = (val / 31 * 100).toFixed(1);
  const grad = {
    r:'linear-gradient(90deg,#ef444433,#ef4444)',
    g:'linear-gradient(90deg,#22c55e33,#22c55e)',
    b:'linear-gradient(90deg,#3b82f633,#3b82f6)'
  }[ch];
  const id = kind + '-' + ch + '-' + n;
  return '<div class="bar-row">' +
    '<span class="ch ' + ch + '">' + ch.toUpperCase() + '</span>' +
    '<div class="bar-track" id="trk-' + id +
        '" onmousedown="barDown(event,\\'' + kind + '\\',\\'' + ch + '\\',' + n + ')">' +
      '<div class="bar-fill" id="fill-' + id +
          '" style="width:' + pct + '%;background:' + grad + '"></div>' +
    '</div>' +
    '<span class="bar-val" id="bv-' + id + '">' + val + '</span>' +
  '</div>';
}

// ── Bar interaction ───────────────────────────────────────────────────────────
function barDown(e, kind, ch, n) {
  e.preventDefault();
  _drag = {kind, ch, n};
  applyDrag(e.clientX);
}

function applyDrag(clientX) {
  const {kind, ch, n} = _drag;
  const trk = document.getElementById('trk-' + kind + '-' + ch + '-' + n);
  if (!trk) return;
  const rect = trk.getBoundingClientRect();
  const val  = Math.max(0, Math.min(31, Math.round((clientX - rect.left) / rect.width * 31)));
  setChannel(kind, n, ch, val);
}

// ── Channel logic ─────────────────────────────────────────────────────────────
function setChannel(kind, n, ch, val) {
  val = Math.max(0, Math.min(31, val));
  const hosts = getHosts();

  hosts.forEach(host => { STATE[host][kind][n][ch] = val; });
  updateBar(kind, ch, n, val);

  // Throttle network sends to ~80 ms (matching Swift app drag rate)
  const key = kind + ch + '-' + n;
  const now = Date.now();
  if (now - (_lastSend[key] || 0) < 80) return;
  _lastSend[key] = now;

  hosts.forEach(host => {
    const st = STATE[host][kind][n];
    if (kind === 'i') sendSkew(host, n, st.r, st.g, st.b);
    else              sendOSkew(host, n, st.r, st.g, st.b);
  });
}

function updateBar(kind, ch, n, val) {
  const id = kind + '-' + ch + '-' + n;
  const fill = document.getElementById('fill-' + id);
  const lbl  = document.getElementById('bv-' + id);
  if (fill) fill.style.width = (val / 31 * 100).toFixed(1) + '%';
  if (lbl)  lbl.textContent  = val;
}

// ── API ───────────────────────────────────────────────────────────────────────
function sendSkew(host, inp, r, g, b) {
  fetch('/api/mtpx/skew?host=' + host + '&input=' + inp + '&r=' + r + '&g=' + g + '&b=' + b,
        {method:'POST'})
    .then(r => r.json())
    .then(j => {
      if (!j.ok) setStatus('⚠ ' + host + ' not responding — is it connected?');
      else setStatus('W' + inp + '*' + r + '*' + g + '*' + b + 'Iseq → ' + host);
    })
    .catch(() => setStatus('Network error'));
}

function sendOSkew(host, out, r, g, b) {
  fetch('/api/mtpx/oskew?host=' + host + '&output=' + out + '&r=' + r + '&g=' + g + '&b=' + b,
        {method:'POST'})
    .then(r => r.json())
    .then(j => {
      if (!j.ok) setStatus('⚠ ' + host + ' not responding — is it connected?');
      else setStatus('W' + out + '*' + r + '*' + g + '*' + b + 'Oseq → ' + host);
    })
    .catch(() => setStatus('Network error'));
}

function sendBatch(host, cmds) {
  return fetch('/api/mtpx/batch', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({host, cmds})
  }).then(r => r.json()).catch(() => ({ok:false}));
}

function togglePeak(n) {
  const hosts = getHosts();
  const newVal = !STATE[activeDev().host].peak[n];
  hosts.forEach(host => {
    STATE[host].peak[n] = newVal;
    fetch('/api/mtpx/peaking?host=' + host + '&output=' + n + '&enabled=' + (newVal?1:0), {method:'POST'});
  });
  const btn = document.getElementById('peak-' + n);
  if (btn) btn.classList.toggle('on', newVal);
  setStatus('W' + n + '*' + (newVal?1:0) + 'Opek → ' + hosts.join(', '));
}

function resetOne(kind, n) {
  const hosts = getHosts();
  hosts.forEach(host => { STATE[host][kind][n] = {r:0, g:0, b:0}; });
  ['r','g','b'].forEach(ch => updateBar(kind, ch, n, 0));
  for (const host of hosts) {
    const ep = kind === 'i' ? '/api/mtpx/reset?host=' + host + '&input=' + n
                            : '/api/mtpx/oreset?host=' + host + '&output=' + n;
    fetch(ep, {method:'POST'});
  }
  toast('Reset ' + (kind === 'i' ? 'Input ' : 'Output ') + n);
}

function resetAllOutputs() {
  const hosts = getHosts();
  hosts.forEach(host => {
    const dev = DEVICES.find(d => d.host === host) || DEVICES[0];
    for (let n = 1; n <= 16; n++) STATE[host].o[n] = {r:0, g:0, b:0};
    fetch('/api/mtpx/oreset?host=' + host + '&output=0&outputs=' + dev.outputs, {method:'POST'});
  });
  buildGrids();
  toast('All output skew reset');
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
          STATE[host].i[parseInt(inp)] = vals;
        });
      }
      if (!j.ok) toast('⚠ ' + host + ' not responding');
    } catch(e) { toast('Network error'); }
  }
  buildGrids();
  toast('Applied: ' + preset);
  setStatus('Applied preset "' + preset + '" → ' + hosts.join(', '));
}

// ── Auto mode ─────────────────────────────────────────────────────────────────
const AUTO = {
  running:false, timer:null, ticks:0,
  sweepInput:1, sweepChIdx:0, chaosFlip:false, bluePhase:true,
  flickers:0,
};

function autoRateChanged(v) {
  document.getElementById('auto-rate-val').textContent = v + '/s';
  if (AUTO.running) { stopAutoTimer(); startAutoTimer(); }
}

function toggleAuto() {
  AUTO.running ? stopAuto() : startAuto();
}

function startAuto() {
  AUTO.running = true;
  AUTO.ticks = 0;
  const btn = document.getElementById('auto-btn');
  btn.textContent = '■ Stop';
  btn.classList.add('stop');
  startAutoTimer();
  setStatus('Auto: ' + document.getElementById('auto-pattern').value + ' started');
}

function stopAuto() {
  AUTO.running = false;
  stopAutoTimer();
  const btn = document.getElementById('auto-btn');
  btn.textContent = '▶ Start';
  btn.classList.remove('stop');
  setStatus('Auto stopped after ' + AUTO.ticks + ' ticks');
}

function startAutoTimer() {
  const rate = parseInt(document.getElementById('auto-rate').value) || 6;
  AUTO.timer = setInterval(autoTick, Math.round(1000 / rate));
}

function stopAutoTimer() {
  clearInterval(AUTO.timer);
  AUTO.timer = null;
}

function panic() {
  if (AUTO.running) stopAuto();
  applyPreset('resetall');
  resetAllOutputs();
  // Restore peaking everywhere
  getHosts().forEach(host => {
    const dev = DEVICES.find(d => d.host === host) || DEVICES[0];
    const cmds = [];
    for (let n = 1; n <= dev.outputs; n++) { STATE[host].peak[n] = true; cmds.push({kind:'opek', n, en:1}); }
    sendBatch(host, cmds);
  });
  buildGrids();
  toast('🛑 Panic: auto stopped, everything reset');
}

function rnd(lo, hi) { return lo + Math.floor(Math.random() * (hi - lo + 1)); }

function autoTick() {
  const hosts = getHosts();
  const pattern = document.getElementById('auto-pattern').value;
  AUTO.ticks++;
  document.getElementById('tickct').textContent = 'tick ' + AUTO.ticks;

  maybeFlicker(hosts);

  hosts.forEach(host => {
    const dev = DEVICES.find(d => d.host === host) || DEVICES[0];
    const nIn = dev.inputs;

    if (pattern === 'random') {
      const n = rnd(1, nIn);
      const r = rnd(0,31), g = rnd(0,31), b = rnd(0,31);
      STATE[host].i[n] = {r, g, b};
      syncBars(host, n);
      sendBatch(host, [{kind:'iseq', n, r, g, b}]);

    } else if (pattern === 'sweep') {
      const n  = ((AUTO.sweepInput - 1) % nIn) + 1;
      const ch = ['r','g','b'][AUTO.sweepChIdx % 3];
      const st = STATE[host].i[n];
      st[ch] = (st[ch] + 1) % 32;
      syncBars(host, n);
      sendBatch(host, [{kind:'iseq', n, r:st.r, g:st.g, b:st.b}]);

    } else if (pattern === 'chaos') {
      const n = rnd(1, nIn);
      const high = AUTO.chaosFlip ? 31 : rnd(20,31);
      const low  = AUTO.chaosFlip ? 0  : rnd(0,8);
      const r = Math.random() < .5 ? high : low;
      const g = Math.random() < .5 ? high : low;
      const b = Math.random() < .5 ? high : low;
      STATE[host].i[n] = {r, g, b};
      syncBars(host, n);
      sendBatch(host, [{kind:'iseq', n, r, g, b}]);

    } else if (pattern === 'bluepulse') {
      const cmds = [];
      for (let n = 1; n <= nIn; n++) {
        const v = AUTO.bluePhase ? {r:0, g:0, b:31} : {r:0, g:0, b:0};
        STATE[host].i[n] = {...v};
        cmds.push({kind:'iseq', n, ...v});
      }
      for (let n = 1; n <= nIn; n++) syncBars(host, n);
      sendBatch(host, cmds);

    } else if (pattern === 'blast') {
      const cmds = [];
      for (let n = 1; n <= nIn; n++) {
        const v = {r:rnd(0,31), g:rnd(0,31), b:rnd(0,31)};
        STATE[host].i[n] = {...v};
        cmds.push({kind:'iseq', n, ...v});
      }
      for (let n = 1; n <= nIn; n++) syncBars(host, n);
      sendBatch(host, cmds);
    }
  });

  if (pattern === 'bluepulse') AUTO.bluePhase = !AUTO.bluePhase;
  if (pattern === 'chaos')     AUTO.chaosFlip = !AUTO.chaosFlip;
  if (pattern === 'sweep') {
    AUTO.sweepChIdx = (AUTO.sweepChIdx + 1) % 3;
    if (AUTO.sweepChIdx === 0) AUTO.sweepInput++;
  }
}

// Peak flicker: with chance% per tick, drop a random output's peaking for a
// moment, then restore it. Max 3 concurrent flickers.
function maybeFlicker(hosts) {
  if (!document.getElementById('flk-on').checked) return;
  if (AUTO.flickers >= 3) return;
  const chance = parseInt(document.getElementById('flk-chance').value) / 100;
  if (Math.random() > chance) return;

  hosts.forEach(host => {
    const dev = DEVICES.find(d => d.host === host) || DEVICES[0];
    const n = rnd(1, dev.outputs);
    AUTO.flickers++;
    sendBatch(host, [{kind:'opek', n, en:0}]);
    setTimeout(() => {
      sendBatch(host, [{kind:'opek', n, en:1}]);
      AUTO.flickers = Math.max(0, AUTO.flickers - 1);
    }, rnd(300, 1500));
  });
}

function syncBars(host, n) {
  if (host !== activeDev().host) return;
  const st = STATE[host].i[n];
  ['r','g','b'].forEach(ch => updateBar('i', ch, n, st[ch]));
}

// Stop auto if the tab is closed / navigated away
window.addEventListener('beforeunload', () => { if (AUTO.running) stopAutoTimer(); });

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
