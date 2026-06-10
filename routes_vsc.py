"""
routes_vsc.py — VSC 500 / 700 / 700D / 900 / 900D Video Scan Converter control pages.

Part numbers (from N command):
  VSC 500:  60-476-01
  VSC 700:  60-477-01
  VSC 700D: 60-477-02
  VSC 900:  60-478-01
  VSC 900D: 60-478-02

Serial: 9600 baud, 1 stop bit, no parity, no flow control.
Queries go via TCP direct (IPCP port 2000+COM).
Set commands containing * go via HTTP passthrough (IPCP intercepts * on TCP).
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
import os, socket, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from shared import log

router = APIRouter()

IPCP_IP = os.getenv("IPCP505_IP", "10.0.0.5")
SOCK_TO = int(os.getenv("SOCKET_TIMEOUT_SECONDS", "4"))


# ─── transport ───────────────────────────────────────────────────────────────

def _tcp(com: int, cmd: str) -> str | None:
    """Send SIS command via TCP direct (IPCP port 2000+com). Returns stripped response."""
    try:
        with socket.create_connection((IPCP_IP, 2000 + com), timeout=SOCK_TO) as s:
            s.sendall((cmd + "\r").encode())
            s.settimeout(1.5)
            buf = b""
            while True:
                try:
                    chunk = s.recv(512)
                    if not chunk:
                        break
                    buf += chunk
                    if b"\r" in buf or b"\n" in buf:
                        break
                except socket.timeout:
                    break
        return buf.decode(errors="replace").strip()
    except Exception as e:
        log(f"VSC tcp com{com} {cmd!r}: {e}")
        return None


def _http_send(com: int, cmd: str) -> bool:
    """Send via IPCP HTTP passthrough (required when command contains *)."""
    try:
        enc = urllib.parse.quote(cmd + "\r", safe="")
        url = f"http://{IPCP_IP}/?cmd=W{com:02d}RS%7C{enc}"
        with urllib.request.urlopen(url, timeout=SOCK_TO) as r:
            return r.status == 200
    except Exception as e:
        log(f"VSC http com{com} {cmd!r}: {e}")
        return False


def _send(com: int, cmd: str) -> bool:
    """Route to HTTP if * or ESC present (IPCP intercepts *), else TCP."""
    if "*" in cmd or "\x1b" in cmd:
        return _http_send(com, cmd)
    r = _tcp(com, cmd)
    return r is not None


def _strip(resp: str | None, prefix: str) -> str:
    """Remove known response prefix and return the value."""
    if not resp:
        return ""
    s = resp.strip()
    if s.lower().startswith(prefix.lower()):
        return s[len(prefix):].strip()
    return s


# ─── status query ─────────────────────────────────────────────────────────────

def _status(com: int, model: str) -> dict:
    """Query all VSC status fields. model: '500', '700', or '900'."""
    def q(c):
        return _tcp(com, c) or ""

    st: dict = {
        "info": q("I"),
        "hph":  _strip(q("H"),    "Hph "),
        "vph":  _strip(q("/"),    "Vph "),
        "hsz":  _strip(q(":"),    "Hsz "),
        "vsz":  _strip(q(";"),    "Vsz "),
        "frz":  _strip(q("F"),    "Frz "),
        "exe":  _strip(q("X"),    "Exe "),
        "tpo":  _strip(q("6#"),   "Tpo "),
        "rte":  _strip(q("14#"),  "Rte "),
        "nop":  _strip(q("13#"),  "Out "),
    }
    if model in ("700", "900"):
        st["dhz"] = _strip(q("D"),    "Dhz ")
        st["dvz"] = _strip(q("d"),    "Dvz ")
        st["enc"] = _strip(q("10#"),  "Enc ")
    if model == "900":
        st["typ"] = _strip(q("\\"),   "Typ ")
        st["tst"] = _strip(q("J"),    "Tst ")
        st["atn"] = _strip(q("15#"),  "Attn ")
    return st


# ─── API ──────────────────────────────────────────────────────────────────────

@router.get("/api/vsc/{port}/status")
def api_vsc_status(port: int, model: str = "700"):
    if not 1 <= port <= 8:
        return JSONResponse({"error": "port out of range"}, status_code=400)
    return JSONResponse(_status(port, model))


@router.post("/api/vsc/{port}/cmd")
def api_vsc_cmd(port: int, cmd: str):
    if not 1 <= port <= 8:
        return JSONResponse({"error": "port out of range"}, status_code=400)
    ok = _send(port, cmd)
    log(f"VSC com{port} cmd={cmd!r} ok={ok}")
    return JSONResponse({"ok": ok})


# ─── page routes ──────────────────────────────────────────────────────────────

@router.get("/control/ipcp505/vsc500", response_class=HTMLResponse)
def page_vsc500():
    return HTMLResponse(VSC_HTML)


@router.get("/control/ipcp505/vsc700", response_class=HTMLResponse)
def page_vsc700():
    return HTMLResponse(VSC_HTML)


@router.get("/control/ipcp505/vsc900", response_class=HTMLResponse)
def page_vsc900():
    return HTMLResponse(VSC_HTML)


# ─── HTML ─────────────────────────────────────────────────────────────────────
# Model is detected in JS from location.pathname.
# Port is read from ?port= query param.
# All state is loaded via /api/vsc/{port}/status?model=MODEL fetch calls.

VSC_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VSC Control</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
a{color:#a78bfa;text-decoration:none}
/* header */
.hdr{background:#1e293b;border-bottom:3px solid #7c3aed;padding:14px 24px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.hdr h1{font-size:1.2rem;font-weight:700}
.badge{background:#7c3aed;color:#fff;padding:2px 11px;border-radius:99px;font-size:.75rem;font-weight:700}
.sig{margin-left:auto;font-size:.78rem;color:#64748b;font-family:monospace}
/* body */
.body{max-width:820px;margin:24px auto;padding:0 16px}
/* port picker */
.port-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px}
.pbt{padding:6px 14px;border-radius:6px;border:1px solid #334155;background:#1e293b;color:#94a3b8;cursor:pointer;font-size:.85rem;transition:border-color .15s}
.pbt:hover{border-color:#7c3aed}
.pbt.on{background:#7c3aed;color:#fff;border-color:#7c3aed}
/* wrong device */
#wrong-dev{background:#7f1d1d;border:1px solid #ef4444;border-radius:10px;padding:14px 18px;margin-bottom:16px;display:none;font-size:.88rem}
/* tabs */
.tabs{display:flex;border-bottom:1px solid #334155;margin-bottom:18px}
.tbt{padding:10px 20px;background:none;border:none;border-bottom:2px solid transparent;color:#64748b;cursor:pointer;font-size:.9rem;margin-bottom:-1px;transition:color .15s}
.tbt.on{color:#7c3aed;border-bottom-color:#7c3aed}
.tp{display:none}
.tp.on{display:block}
/* card */
.card{background:#1e293b;border-radius:12px;padding:20px;margin-bottom:14px}
.ch{font-size:.78rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.06em;margin-bottom:16px}
/* stepper row */
.srow{display:grid;grid-template-columns:170px 90px 36px 36px;align-items:center;gap:8px;margin-bottom:12px}
.slbl{font-size:.85rem;color:#cbd5e1}
.sval{background:#0f172a;color:#e2e8f0;font-family:monospace;font-size:.98rem;padding:6px 10px;border-radius:6px;text-align:center}
.sbt{width:36px;height:36px;border-radius:8px;border:none;background:#1e3a5f;color:#93c5fd;cursor:pointer;font-size:1.1rem;display:flex;align-items:center;justify-content:center;transition:background .15s}
.sbt:hover{background:#1d4ed8;color:#fff}
/* toggle row */
.trow{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid #0f172a}
.trow:last-child{border-bottom:none}
.tlbl{font-size:.88rem;color:#cbd5e1}
.tgl{padding:5px 18px;border:none;border-radius:6px;cursor:pointer;font-weight:700;font-size:.82rem}
.tgl-on{background:#16a34a;color:#fff}
.tgl-off{background:#334155;color:#94a3b8}
/* select row */
.serow{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.serow label{min-width:180px;font-size:.85rem;color:#cbd5e1}
.serow select{flex:1;background:#0f172a;color:#e2e8f0;border:1px solid #334155;padding:7px 10px;border-radius:7px;font-size:.85rem}
/* slider row */
.slrow{display:grid;grid-template-columns:180px 1fr 44px;align-items:center;gap:10px;margin-bottom:14px}
.slrow label{font-size:.85rem;color:#cbd5e1}
.slrow input[type=range]{accent-color:#7c3aed;width:100%}
.sv{font-family:monospace;font-size:.9rem;color:#f8fafc;text-align:right}
/* input selector */
.inp-row{display:flex;gap:10px;margin-bottom:18px}
.ibt{padding:10px 28px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#94a3b8;cursor:pointer;font-size:1rem;font-weight:700;transition:.15s}
.ibt.on{background:#7c3aed;color:#fff;border-color:#7c3aed}
/* type radio */
.rrow{display:flex;gap:24px;margin-bottom:18px}
.rrow label{display:flex;align-items:center;gap:7px;font-size:.88rem;cursor:pointer;color:#cbd5e1}
.rrow input[type=radio]{accent-color:#7c3aed;width:16px;height:16px}
/* preset grid */
.pgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.prbt{padding:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#94a3b8;cursor:pointer;font-size:.9rem;font-weight:700;transition:.15s}
.prbt:hover{border-color:#7c3aed;color:#7c3aed}
/* action buttons */
.abt{padding:9px 20px;border-radius:8px;border:none;cursor:pointer;font-size:.87rem;font-weight:700;transition:opacity .15s}
.abt:hover{opacity:.85}
.abt-v{background:#7c3aed;color:#fff}
.abt-r{background:#991b1b;color:#fff}
/* toast */
#toast{position:fixed;bottom:20px;right:20px;background:#1e293b;border:1px solid #7c3aed;color:#e2e8f0;padding:10px 16px;border-radius:10px;font-size:.85rem;display:none;z-index:999;box-shadow:0 4px 20px #0009}
</style>
</head>
<body>

<div class="hdr">
  <h1 id="hdr-title">VSC Control</h1>
  <span class="badge" id="hdr-com">COM —</span>
  <span class="sig" id="hdr-sig">—</span>
  <a href="/control/ipcp505" style="margin-left:12px;font-size:.8rem;color:#64748b">← IPCP 505</a>
</div>

<div class="body">

  <div id="wrong-dev">
    ⚠ Device on this port doesn't match expected model — check connections or re-run COM scan.
  </div>

  <!-- Port picker -->
  <div class="port-row" id="port-row"></div>

  <!-- Tab bar (populated by JS) -->
  <div class="tabs" id="tab-bar"></div>

  <!-- ═══ INPUT tab (VSC 900 only) ═══════════════════════════════════════════ -->
  <div class="tp" id="tp-inp">
    <div class="card">
      <div class="ch">Input Select</div>
      <div class="inp-row">
        <button class="ibt" id="ibt-1" onclick="selInput(1)">Input 1</button>
        <button class="ibt" id="ibt-2" onclick="selInput(2)">Input 2</button>
      </div>
      <div class="ch">Input Video Type</div>
      <div class="rrow">
        <label><input type="radio" name="vtyp" value="0" onchange="setInputType(0)"> RGB</label>
        <label><input type="radio" name="vtyp" value="1" onchange="setInputType(1)"> YUV (Component)</label>
      </div>
      <div class="ch">Memory Presets (1–8)</div>
      <p style="font-size:.78rem;color:#64748b;margin-bottom:10px">Click to recall &nbsp;·&nbsp; Shift+click to save</p>
      <div class="pgrid" id="preset-grid"></div>
    </div>
  </div>

  <!-- ═══ POSITION / SIZE tab ════════════════════════════════════════════════ -->
  <div class="tp" id="tp-pos">
    <div class="card">
      <div class="ch">Centering (Shift)</div>
      <div class="srow">
        <span class="slbl">Horizontal Shift</span>
        <span class="sval" id="v-hph">—</span>
        <button class="sbt" onclick="step('H','-')">−</button>
        <button class="sbt" onclick="step('H','+')">+</button>
      </div>
      <div class="srow">
        <span class="slbl">Vertical Shift</span>
        <span class="sval" id="v-vph">—</span>
        <button class="sbt" onclick="step('/','−')">−</button>
        <button class="sbt" onclick="step('/','+')" >+</button>
      </div>
    </div>
    <div class="card">
      <div class="ch">Size</div>
      <div class="srow">
        <span class="slbl">Horizontal Size</span>
        <span class="sval" id="v-hsz">—</span>
        <button class="sbt" onclick="step(':','-')">−</button>
        <button class="sbt" onclick="step(':','+')">+</button>
      </div>
      <div class="srow">
        <span class="slbl">Vertical Size</span>
        <span class="sval" id="v-vsz">—</span>
        <button class="sbt" onclick="step(';','-')">−</button>
        <button class="sbt" onclick="step(';','+')">+</button>
      </div>
      <div class="srow">
        <span class="slbl">Zoom</span>
        <span class="sval">—</span>
        <button class="sbt" onclick="sendCmd('-{')">−</button>
        <button class="sbt" onclick="sendCmd('+{')">+</button>
      </div>
    </div>
    <div style="margin-bottom:16px">
      <button class="abt abt-v" onclick="autoImage()">⚡ Auto Image</button>
    </div>
  </div>

  <!-- ═══ FILTERS tab (VSC 700 and 900 only) ════════════════════════════════ -->
  <div class="tp" id="tp-flt">
    <div class="card">
      <div class="ch">Horizontal Filter (Detail)</div>
      <div class="slrow">
        <label id="dhz-label">H Filter (0–3)</label>
        <input type="range" min="0" max="3" step="1" id="sl-dhz"
               oninput="slPreview('dhz',this.value)"
               onchange="sendCmd(this.value+'D')">
        <span class="sv" id="sv-dhz">—</span>
      </div>
    </div>
    <div class="card">
      <div class="ch">Flicker Filter (Vertical)</div>
      <div class="slrow">
        <label>Flicker (0–3)</label>
        <input type="range" min="0" max="3" step="1" id="sl-dvz"
               oninput="slPreview('dvz',this.value)"
               onchange="sendCmd(this.value+'d')">
        <span class="sv" id="sv-dvz">—</span>
      </div>
    </div>
    <div class="card">
      <div class="ch">Encoder Filter (Sharpness)</div>
      <div class="slrow">
        <label>Encoder (0–3)</label>
        <input type="range" min="0" max="3" step="1" id="sl-enc"
               oninput="slPreview('enc',this.value)"
               onchange="sendCmd(this.value+'*10#')">
        <span class="sv" id="sv-enc">—</span>
      </div>
    </div>
  </div>

  <!-- ═══ CONFIG tab ═════════════════════════════════════════════════════════ -->
  <div class="tp" id="tp-cfg">
    <div class="card">
      <div class="ch">Output Configuration</div>
      <div class="serow">
        <label>Output Video Type</label>
        <select id="sel-tpo" onchange="sendCmd(this.value+'*6#')">
          <option value="0">RGBHV (default)</option>
          <option value="1">RGBS</option>
          <option value="2">RGsB</option>
          <option value="3">YUV (Component)</option>
        </select>
      </div>
      <div class="serow">
        <label>Video Standard</label>
        <select id="sel-rte" onchange="sendCmd(this.value+'*14#')">
          <option value="0">NTSC (default)</option>
          <option value="1">PAL</option>
        </select>
      </div>
      <div class="serow">
        <label>No-Signal Output</label>
        <select id="sel-nop" onchange="sendCmd(this.value+'*13#')">
          <option value="0">Black Screen (default)</option>
          <option value="1">Color Bars</option>
        </select>
      </div>
    </div>

    <!-- VSC 900 only: test pattern + attenuation -->
    <div class="card" id="card-900" style="display:none">
      <div class="ch">Test Pattern</div>
      <div class="serow">
        <label>Pattern</label>
        <select id="sel-tst" onchange="sendCmd(this.value+'J')">
          <option value="0">Off</option>
          <option value="1">Color Bars</option>
          <option value="2">Crosshatch</option>
          <option value="3">Grayscale</option>
        </select>
      </div>
      <div class="ch" style="margin-top:14px">Input Attenuation</div>
      <div class="slrow">
        <label>Attenuation (0–64)</label>
        <input type="range" min="0" max="64" step="1" id="sl-atn"
               oninput="slPreview('atn',this.value)"
               onchange="sendCmd(this.value+'*15#')">
        <span class="sv" id="sv-atn">—</span>
      </div>
    </div>

    <div class="card">
      <div class="ch">Device Controls</div>
      <div class="trow">
        <span class="tlbl">Freeze (lock output to current frame)</span>
        <button class="tgl tgl-off" id="tgl-frz" onclick="toggleFreeze()">OFF</button>
      </div>
      <div class="trow">
        <span class="tlbl">Executive Mode (front panel lockout)</span>
        <button class="tgl tgl-off" id="tgl-exe" onclick="toggleExec()">OFF</button>
      </div>
      <div class="trow">
        <span class="tlbl">Auto Image (auto center + size)</span>
        <button class="abt abt-v" onclick="autoImage()">Run</button>
      </div>
      <div class="trow">
        <span class="tlbl">Factory Reset</span>
        <button class="abt abt-r" onclick="zapReset()">ZAP Reset</button>
      </div>
    </div>
  </div>

</div><!-- /body -->

<div id="toast"></div>

<script>
// ── Model + port from URL ──────────────────────────────────────────────────
const path = location.pathname;
const MODEL = path.includes('vsc500') ? '500' : path.includes('vsc900') ? '900' : '700';
const MODEL_NAME = {'500':'VSC 500','700':'VSC 700 / 700D','900':'VSC 900 / 900D'}[MODEL];
let COM = parseInt(new URLSearchParams(location.search).get('port') || '1');
let STATE = {};

// ── Boot ──────────────────────────────────────────────────────────────────
function setup() {
  document.title = MODEL_NAME + ' Control';
  document.getElementById('hdr-title').textContent = MODEL_NAME;
  document.getElementById('hdr-com').textContent = 'COM ' + COM;

  // port buttons
  const pr = document.getElementById('port-row');
  for (let i = 1; i <= 8; i++) {
    const b = document.createElement('button');
    b.className = 'pbt' + (i === COM ? ' on' : '');
    b.textContent = 'COM ' + i;
    b.id = 'pb-' + i;
    b.onclick = () => setPort(i);
    pr.appendChild(b);
  }

  // build tab list
  const tabs = [];
  if (MODEL === '900') tabs.push({id:'inp', label:'Input'});
  tabs.push({id:'pos', label:'Position / Size'});
  if (MODEL !== '500') tabs.push({id:'flt', label:'Filters'});
  tabs.push({id:'cfg', label:'Config'});

  const tb = document.getElementById('tab-bar');
  tabs.forEach((t, i) => {
    const b = document.createElement('button');
    b.className = 'tbt' + (i === 0 ? ' on' : '');
    b.textContent = t.label;
    b.id = 'tb-' + t.id;
    b.onclick = () => showTab(t.id);
    tb.appendChild(b);
  });
  showTab(tabs[0].id);

  // model-specific tweaks
  if (MODEL === '900') {
    document.getElementById('card-900').style.display = '';
    document.getElementById('sl-dhz').max = '7';
    document.getElementById('dhz-label').textContent = 'H Filter (0–7)';
    // preset grid
    const pg = document.getElementById('preset-grid');
    for (let i = 1; i <= 8; i++) {
      const b = document.createElement('button');
      b.className = 'prbt';
      b.textContent = 'P' + i;
      b.title = 'Click = recall P' + i + ' / Shift+Click = save P' + i;
      b.onclick = (e) => e.shiftKey ? savePreset(i) : recallPreset(i);
      pg.appendChild(b);
    }
  }

  poll();
}

function showTab(id) {
  document.querySelectorAll('.tp').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tbt').forEach(b => b.classList.remove('on'));
  const p = document.getElementById('tp-' + id);
  const b = document.getElementById('tb-' + id);
  if (p) p.classList.add('on');
  if (b) b.classList.add('on');
}

function setPort(p) {
  COM = p;
  document.querySelectorAll('.pbt').forEach(b => b.classList.remove('on'));
  document.getElementById('pb-' + p).classList.add('on');
  document.getElementById('hdr-com').textContent = 'COM ' + p;
  history.replaceState(null, '', '?port=' + p);
  poll();
}

// ── API ───────────────────────────────────────────────────────────────────
let _pollTimer = null;

async function poll() {
  clearTimeout(_pollTimer);
  try {
    const r = await fetch(`/api/vsc/${COM}/status?model=${MODEL}`);
    if (r.ok) { STATE = await r.json(); updateUI(); }
  } catch(e) { console.warn('poll error', e); }
  _pollTimer = setTimeout(poll, 10000);
}

async function sendCmd(cmd) {
  try {
    const r = await fetch(`/api/vsc/${COM}/cmd?cmd=${encodeURIComponent(cmd)}`, {method:'POST'});
    const j = await r.json();
    toast(j.ok ? 'Sent: ' + cmd : 'Error sending: ' + cmd);
    clearTimeout(_pollTimer);
    _pollTimer = setTimeout(poll, 900);
  } catch(e) { toast('Network error'); }
}

// ── Update UI from STATE ──────────────────────────────────────────────────
function updateUI() {
  document.getElementById('hdr-sig').textContent = STATE.info || '—';

  setTxt('hph', 'v-hph');
  setTxt('vph', 'v-vph');
  setTxt('hsz', 'v-hsz');
  setTxt('vsz', 'v-vsz');

  setToggle('frz', 'tgl-frz');
  setToggle('exe', 'tgl-exe');

  setSel('tpo', 'sel-tpo');
  setSel('rte', 'sel-rte');
  setSel('nop', 'sel-nop');

  if (MODEL !== '500') {
    setSl('dhz', 'sl-dhz', 'sv-dhz');
    setSl('dvz', 'sl-dvz', 'sv-dvz');
    setSl('enc', 'sl-enc', 'sv-enc');
  }

  if (MODEL === '900') {
    setSel('tst', 'sel-tst');
    setSl('atn', 'sl-atn', 'sv-atn');
    if (STATE.typ !== undefined && STATE.typ !== '') {
      document.querySelectorAll('input[name="vtyp"]').forEach(r => {
        r.checked = (r.value === String(STATE.typ));
      });
    }
  }
}

function setTxt(key, id) {
  const el = document.getElementById(id);
  if (el) el.textContent = (STATE[key] !== undefined && STATE[key] !== '') ? STATE[key] : '—';
}

function setToggle(key, id) {
  const el = document.getElementById(id);
  if (!el) return;
  const on = STATE[key] === '1';
  el.textContent = on ? 'ON' : 'OFF';
  el.className = 'tgl ' + (on ? 'tgl-on' : 'tgl-off');
}

function setSel(key, id) {
  const el = document.getElementById(id);
  if (!el || STATE[key] === undefined || STATE[key] === '') return;
  el.value = String(STATE[key]);
}

function setSl(key, slId, svId) {
  const v = STATE[key];
  if (v === undefined || v === '') return;
  const sl = document.getElementById(slId);
  const sv = document.getElementById(svId);
  if (sl) sl.value = v;
  if (sv) sv.textContent = v;
}

function slPreview(key, val) {
  const sv = document.getElementById('sv-' + key);
  if (sv) sv.textContent = val;
}

// ── Commands ──────────────────────────────────────────────────────────────
function step(axis, dir) {
  sendCmd((dir === '+' ? '+' : '-') + axis);
}

function toggleFreeze() {
  sendCmd(STATE.frz === '1' ? '0F' : '1F');
}

function toggleExec() {
  sendCmd(STATE.exe === '1' ? '0X' : '1X');
}

function autoImage() { sendCmd('55#'); }

function zapReset() {
  if (confirm('ZAP: reset ' + MODEL_NAME + ' on COM ' + COM + ' to factory defaults?')) {
    sendCmd('\\x1bzXXX');  // ESC + zXXX — handled by backend as HTTP passthrough
  }
}

// VSC 900 only
function selInput(n) {
  sendCmd(n + '!');
  document.querySelectorAll('.ibt').forEach(b => b.classList.remove('on'));
  const el = document.getElementById('ibt-' + n);
  if (el) el.classList.add('on');
}

function setInputType(v) { sendCmd(v + '\\\\'); }

function recallPreset(n) { sendCmd(n + '.'); }
function savePreset(n)   { sendCmd(n + ','); }

// ── Toast ─────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.style.display = 'none', 2500);
}

setup();
</script>
</body>
</html>"""


# ─── Quad / multi-unit API ────────────────────────────────────────────────────

def _parse_ports(ports: str) -> list[int]:
    result = []
    for p in ports.split(","):
        p = p.strip()
        if p.isdigit():
            n = int(p)
            if 1 <= n <= 8:
                result.append(n)
    return result


@router.get("/api/vscquad/status")
def api_vsc_quad_status(ports: str = "1,2,3,4", model: str = "700"):
    """Query status for multiple VSC ports in parallel."""
    port_list = _parse_ports(ports)
    if not port_list:
        return JSONResponse({})
    with ThreadPoolExecutor(max_workers=min(len(port_list), 8)) as ex:
        futures = {p: ex.submit(_status, p, model) for p in port_list}
    result = {}
    for p, fut in futures.items():
        try:
            result[str(p)] = fut.result()
        except Exception as e:
            log(f"VSC quad status com{p}: {e}")
            result[str(p)] = {}
    return JSONResponse(result)


@router.post("/api/vscquad/cmd")
def api_vsc_quad_cmd(ports: str, cmd: str):
    """Send SIS command to multiple VSC ports in parallel."""
    port_list = _parse_ports(ports)
    if not port_list:
        return JSONResponse({})
    with ThreadPoolExecutor(max_workers=min(len(port_list), 8)) as ex:
        futures = {p: ex.submit(_send, p, cmd) for p in port_list}
    result = {}
    for p, fut in futures.items():
        try:
            result[str(p)] = fut.result()
        except Exception as e:
            log(f"VSC quad cmd com{p} {cmd!r}: {e}")
            result[str(p)] = False
    log(f"VSC quad cmd={cmd!r} ports={port_list}")
    return JSONResponse(result)


@router.get("/control/ipcp505/vscquad", response_class=HTMLResponse)
def page_vsc_quad():
    return HTMLResponse(VSC_QUAD_HTML)


# ─── Quad HTML ────────────────────────────────────────────────────────────────

VSC_QUAD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VSC Quad</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0e1a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
a{color:#a78bfa;text-decoration:none}
/* ── Header ─────────────────────────────── */
.hdr{background:#1e293b;border-bottom:3px solid #7c3aed;padding:11px 18px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
.hdr h1{font-size:1.15rem;font-weight:700;white-space:nowrap}
.hdr-back{font-size:.8rem;color:#64748b;margin-right:4px}
.stx{font-size:.78rem;color:#a5b4fc;margin-left:auto;white-space:nowrap}
.mbtn{padding:7px 18px;border-radius:20px;border:none;font-size:.88rem;font-weight:700;cursor:pointer;transition:.15s;letter-spacing:.03em}
.mbtn.gang{background:#059669;color:#fff}
.mbtn.gang:hover{background:#047857}
.mbtn.solo{background:#d97706;color:#fff}
.mbtn.solo:hover{background:#b45309}
/* ── Chip bar ────────────────────────────── */
.cpbar{display:flex;align-items:center;gap:7px;padding:9px 18px;background:#0f172a;border-bottom:1px solid #1e293b;flex-wrap:wrap}
.cplbl{font-size:.78rem;color:#475569;margin-right:2px;white-space:nowrap}
.chip{padding:4px 12px;border-radius:20px;border:1px solid #334155;background:#0f172a;color:#64748b;cursor:pointer;font-size:.78rem;transition:.15s}
.chip:hover{border-color:#7c3aed;color:#c4b5fd}
.chip.on{background:#7c3aed;border-color:#7c3aed;color:#fff;font-weight:700}
/* ── Master control box ──────────────────── */
.master{background:#1e293b;border:1px solid #334155;border-radius:12px;margin:12px 16px 8px;overflow:hidden}
.tbar{display:flex;border-bottom:1px solid #334155}
.tbt{flex:1;padding:10px 14px;border:none;border-bottom:2px solid transparent;background:transparent;color:#64748b;cursor:pointer;font-size:.85rem;font-weight:600;margin-bottom:-1px;transition:.15s}
.tbt.on{color:#c4b5fd;border-bottom-color:#7c3aed}
.tp{display:none;padding:14px 16px}
.tp.on{display:block}
/* position/size */
.crow{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.cgrp{display:flex;flex-direction:column;gap:7px}
.ci{display:grid;grid-template-columns:88px 34px 34px;align-items:center;gap:7px}
.ci label{font-size:.82rem;color:#94a3b8}
.sbt{width:34px;height:32px;border-radius:7px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:1rem;font-weight:700;cursor:pointer;transition:.12s}
.sbt:hover{border-color:#7c3aed;color:#c4b5fd;background:#1a1040}
.cacts{display:flex;flex-direction:column;gap:8px;padding-top:1px}
.abt{padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:.84rem;font-weight:700;transition:opacity .15s;white-space:nowrap}
.abt:hover{opacity:.85}
.abt-v{background:#7c3aed;color:#fff}
.abt-t{background:#1e40af;color:#fff}
.abt-x{background:#0f766e;color:#fff}
.abt-r{background:#991b1b;color:#fff}
/* filter sliders */
.slrow{display:grid;grid-template-columns:150px 1fr 40px;align-items:center;gap:10px;margin-bottom:10px}
.slrow label{font-size:.82rem;color:#94a3b8}
.slrow input[type=range]{accent-color:#7c3aed}
.sv{font-family:monospace;font-size:.88rem;color:#f8fafc;text-align:right}
/* config */
.serow{display:flex;align-items:center;gap:12px;margin-bottom:9px;flex-wrap:wrap}
.serow label{color:#94a3b8;font-size:.82rem;min-width:130px}
.serow select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;padding:5px 9px;border-radius:7px;font-size:.82rem}
.cfg-acts{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
/* ── Status grid ─────────────────────────── */
.sgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 16px 20px}
@media(max-width:580px){.sgrid{grid-template-columns:1fr}}
/* ── Status card ─────────────────────────── */
.scard{background:#1e293b;border:1px solid #334155;border-radius:10px;overflow:hidden;transition:border-color .2s,box-shadow .2s}
.scard.ginGang{border-color:#7c3aed44}
.scard.soloed{border:2px solid #7c3aed;box-shadow:0 0 16px #7c3aed33}
.scard-hdr{display:flex;align-items:center;gap:9px;padding:9px 13px;background:#0f172a;cursor:pointer;user-select:none;transition:background .12s}
.scard-hdr:hover{background:#111827}
.sport{font-weight:700;font-size:.92rem;color:#a78bfa;white-space:nowrap}
.ssig{font-size:.73rem;color:#64748b;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:monospace}
.solo-btn{padding:3px 9px;border-radius:5px;border:1px solid #334155;background:#0a0e1a;color:#64748b;cursor:pointer;font-size:.72rem;white-space:nowrap;transition:.12s}
.solo-btn:hover{border-color:#d97706;color:#fbbf24}
.solo-badge{background:#d97706;color:#fff;border-radius:4px;padding:2px 8px;font-size:.7rem;font-weight:700;white-space:nowrap}
.scard-body{padding:9px 13px 11px;display:grid;grid-template-columns:1fr 1fr;gap:3px 10px}
.sv2{display:flex;justify-content:space-between;align-items:baseline;padding:2px 0;border-bottom:1px solid #1e293b}
.sk2{font-size:.72rem;color:#475569}
.vv2{font-family:monospace;font-size:.84rem;color:#cbd5e1}
.vv2.vv-on{color:#a78bfa;font-weight:700}
.open-link{grid-column:span 2;display:block;text-align:right;color:#334155;font-size:.72rem;padding-top:7px;transition:color .12s}
.open-link:hover{color:#7c3aed}
/* ── Toast ───────────────────────────────── */
#toast{position:fixed;bottom:20px;right:20px;background:#1e293b;border:1px solid #7c3aed;color:#e2e8f0;padding:10px 16px;border-radius:10px;font-size:.83rem;display:none;z-index:999;box-shadow:0 4px 20px #0009;max-width:320px}
</style>
</head>
<body>

<!-- ── Header ──────────────────────────────────────────────────────────────── -->
<div class="hdr">
  <a class="hdr-back" href="/control/ipcp505">← IPCP 505</a>
  <h1>VSC <span id="mdl">700D</span>&nbsp;Quad</h1>
  <span id="sendingTo" class="stx">Sending to: all active</span>
  <button id="modeBtn" class="mbtn gang" onclick="toggleMode()">⊕ GANG</button>
</div>

<!-- ── Port chips ──────────────────────────────────────────────────────────── -->
<div class="cpbar">
  <span class="cplbl">Active ports:</span>
  <div id="chips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
</div>

<!-- ── Master controls ─────────────────────────────────────────────────────── -->
<div class="master">
  <div class="tbar" id="tbar"></div>

  <!-- Position / Size -->
  <div class="tp" id="tp-pos">
    <div class="crow">
      <div class="cgrp">
        <div class="ci"><label>H Shift</label><button class="sbt" onclick="sq('-H')">−</button><button class="sbt" onclick="sq('+H')">+</button></div>
        <div class="ci"><label>V Shift</label><button class="sbt" onclick="sq('-/')">−</button><button class="sbt" onclick="sq('+/')">+</button></div>
        <div class="ci"><label>H Size</label> <button class="sbt" onclick="sq('-:')">−</button><button class="sbt" onclick="sq('+:')">+</button></div>
        <div class="ci"><label>V Size</label>  <button class="sbt" onclick="sq('-;')">−</button><button class="sbt" onclick="sq('+;')">+</button></div>
        <div class="ci"><label>Zoom</label>    <button class="sbt" onclick="sq('-{')">−</button><button class="sbt" onclick="sq('+{')">+</button></div>
      </div>
      <div class="cacts">
        <button class="abt abt-v" onclick="sq('55#')">⚡ Auto Image</button>
        <button class="abt abt-t" id="btn-frz" onclick="sqFreeze()">Freeze</button>
        <button class="abt abt-x" id="btn-exe" onclick="sqExec()">Exec Mode</button>
      </div>
    </div>
  </div>

  <!-- Filters (700 / 900) -->
  <div class="tp" id="tp-flt">
    <div class="slrow">
      <label id="hflt-lbl">H Filter (0–3)</label>
      <input type="range" min="0" max="3" step="1" id="sl-dhz"
             oninput="slPr('dhz',this.value)" onchange="sq(this.value+'D')">
      <span class="sv" id="sv-dhz">—</span>
    </div>
    <div class="slrow">
      <label>Flicker (0–3)</label>
      <input type="range" min="0" max="3" step="1" id="sl-dvz"
             oninput="slPr('dvz',this.value)" onchange="sq(this.value+'d')">
      <span class="sv" id="sv-dvz">—</span>
    </div>
    <div class="slrow">
      <label>Encoder (0–3)</label>
      <input type="range" min="0" max="3" step="1" id="sl-enc"
             oninput="slPr('enc',this.value)" onchange="sq(this.value+'*10#')">
      <span class="sv" id="sv-enc">—</span>
    </div>
  </div>

  <!-- Config -->
  <div class="tp" id="tp-cfg">
    <div class="serow">
      <label>Output Video Type</label>
      <select onchange="sq(this.value+'*6#')">
        <option value="0">RGBHV (default)</option>
        <option value="1">RGBS</option>
        <option value="2">RGsB</option>
        <option value="3">YUV (Component)</option>
      </select>
    </div>
    <div class="serow">
      <label>Video Standard</label>
      <select onchange="sq(this.value+'*14#')">
        <option value="0">NTSC (default)</option>
        <option value="1">PAL</option>
      </select>
    </div>
    <div class="serow">
      <label>No-Signal Output</label>
      <select onchange="sq(this.value+'*13#')">
        <option value="0">Black Screen (default)</option>
        <option value="1">Color Bars</option>
      </select>
    </div>
    <div class="cfg-acts">
      <button class="abt abt-v" onclick="sq('55#')">⚡ Auto Image All</button>
      <button class="abt abt-r" onclick="zapAll()">ZAP Reset All</button>
    </div>
  </div>
</div>

<!-- ── Status grid ─────────────────────────────────────────────────────────── -->
<div class="sgrid" id="sgrid"></div>

<div id="toast"></div>

<script>
// ── State ────────────────────────────────────────────────────────────────────
const _params = new URLSearchParams(location.search);
const MODEL = _params.get('model') || '700';
let ACTIVE_PORTS = new Set(
  (_params.get('ports') || '1,2,3,4').split(',')
    .map(Number).filter(p => p >= 1 && p <= 8)
);
let GANG_MODE = true;
let SOLO_PORT = null;
let STATUS = {};
let _pollTimer = null;

// ── Setup ────────────────────────────────────────────────────────────────────
function setup() {
  const names = {'500':'VSC 500','700':'VSC 700D','900':'VSC 900D'};
  document.getElementById('mdl').textContent = (names[MODEL] || 'VSC').replace('VSC ','');
  document.title = (names[MODEL] || 'VSC') + ' Quad';

  // Tabs
  const tabDefs = [{id:'pos',lbl:'Position / Size'}];
  if (MODEL !== '500') tabDefs.push({id:'flt',lbl:'Filters'});
  tabDefs.push({id:'cfg',lbl:'Config'});
  const tbar = document.getElementById('tbar');
  tabDefs.forEach((t, i) => {
    const b = document.createElement('button');
    b.className = 'tbt' + (i === 0 ? ' on' : '');
    b.id = 'tbt-' + t.id;
    b.textContent = t.lbl;
    b.onclick = () => showTab(t.id);
    tbar.appendChild(b);
  });
  showTab(tabDefs[0].id);

  // 900: wider H filter
  if (MODEL === '900') {
    document.getElementById('sl-dhz').max = '7';
    document.getElementById('hflt-lbl').textContent = 'H Filter (0–7)';
  }

  // Port chips
  const chips = document.getElementById('chips');
  for (let p = 1; p <= 8; p++) {
    const b = document.createElement('button');
    b.className = 'chip' + (ACTIVE_PORTS.has(p) ? ' on' : '');
    b.id = 'chip-' + p;
    b.textContent = 'COM ' + p;
    b.onclick = () => togglePort(p);
    chips.appendChild(b);
  }

  updateMode();
  buildGrid();
  poll();
}

function showTab(id) {
  document.querySelectorAll('.tp').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tbt').forEach(b => b.classList.remove('on'));
  const tp = document.getElementById('tp-' + id);
  const tb = document.getElementById('tbt-' + id);
  if (tp) tp.classList.add('on');
  if (tb) tb.classList.add('on');
}

// ── Port / mode management ───────────────────────────────────────────────────
function togglePort(p) {
  if (ACTIVE_PORTS.has(p)) {
    ACTIVE_PORTS.delete(p);
    if (SOLO_PORT === p) { SOLO_PORT = null; GANG_MODE = true; }
  } else {
    ACTIVE_PORTS.add(p);
  }
  document.getElementById('chip-' + p).className = 'chip' + (ACTIVE_PORTS.has(p) ? ' on' : '');
  updateMode();
  buildGrid();
}

function toggleMode() {
  if (GANG_MODE) {
    GANG_MODE = false;
    SOLO_PORT = [...ACTIVE_PORTS].sort((a,b) => a-b)[0] || null;
  } else {
    GANG_MODE = true;
    SOLO_PORT = null;
  }
  updateMode();
  buildGrid();
}

function soloPort(p) {
  if (!ACTIVE_PORTS.has(p)) {
    ACTIVE_PORTS.add(p);
    document.getElementById('chip-' + p).className = 'chip on';
  }
  GANG_MODE = false;
  SOLO_PORT = p;
  updateMode();
  buildGrid();
}

function updateMode() {
  const btn = document.getElementById('modeBtn');
  const stx = document.getElementById('sendingTo');
  const sorted = [...ACTIVE_PORTS].sort((a,b) => a-b);
  if (GANG_MODE) {
    btn.textContent = '⊕ GANG';
    btn.className = 'mbtn gang';
    stx.textContent = sorted.length
      ? 'Sending to: COM ' + sorted.join(', ')
      : 'No ports active';
  } else {
    btn.textContent = '◎ SOLO: COM ' + (SOLO_PORT || '—');
    btn.className = 'mbtn solo';
    stx.textContent = SOLO_PORT
      ? 'Sending to: COM ' + SOLO_PORT + ' only'
      : 'Click a card to solo it';
  }
}

function getTargetPorts() {
  return GANG_MODE
    ? [...ACTIVE_PORTS].sort((a,b) => a-b)
    : (SOLO_PORT ? [SOLO_PORT] : []);
}

// ── Commands ─────────────────────────────────────────────────────────────────
async function sq(cmd) {
  const ports = getTargetPorts();
  if (!ports.length) { toast('No port targeted — select a port'); return; }
  try {
    const r = await fetch(
      '/api/vscquad/cmd?ports=' + ports.join(',') + '&cmd=' + encodeURIComponent(cmd),
      {method:'POST'}
    );
    const j = await r.json();
    const ok = Object.values(j).every(v => v);
    toast((ok ? '✓ ' : '⚠ ') + '"' + cmd + '" → COM ' + ports.join(', '));
    schedPoll(900);
  } catch(e) { toast('Network error'); }
}

function sqFreeze() {
  const ports = getTargetPorts();
  if (!ports.length) return;
  const anyFrozen = ports.some(p => STATUS[String(p)] && STATUS[String(p)].frz === '1');
  sq(anyFrozen ? '0F' : '1F');
}

function sqExec() {
  const ports = getTargetPorts();
  if (!ports.length) return;
  const anyExec = ports.some(p => STATUS[String(p)] && STATUS[String(p)].exe === '1');
  sq(anyExec ? '0X' : '1X');
}

function zapAll() {
  const ports = getTargetPorts();
  if (!ports.length) return;
  if (!confirm('ZAP factory reset COM ' + ports.join(', ') + '?\\nThis cannot be undone!')) return;
  sq('\\x1bzXXX');
}

function slPr(key, val) {
  const el = document.getElementById('sv-' + key);
  if (el) el.textContent = val;
}

// ── Polling ──────────────────────────────────────────────────────────────────
function schedPoll(ms) { clearTimeout(_pollTimer); _pollTimer = setTimeout(poll, ms); }

async function poll() {
  clearTimeout(_pollTimer);
  const ports = [...ACTIVE_PORTS].sort((a,b) => a-b);
  if (!ports.length) { _pollTimer = setTimeout(poll, 10000); return; }
  try {
    const r = await fetch('/api/vscquad/status?ports=' + ports.join(',') + '&model=' + MODEL);
    if (r.ok) { STATUS = await r.json(); buildGrid(); }
  } catch(e) { console.warn('quad poll:', e); }
  _pollTimer = setTimeout(poll, 10000);
}

// ── Status grid ───────────────────────────────────────────────────────────────
function buildGrid() {
  const grid = document.getElementById('sgrid');
  grid.innerHTML = '';
  const sorted = [...ACTIVE_PORTS].sort((a,b) => a-b);
  if (!sorted.length) {
    grid.innerHTML = '<div style="color:#475569;font-size:.9rem;padding:20px 0">No ports active — toggle ports above</div>';
    return;
  }
  const modelPage = '/control/ipcp505/vsc' + (MODEL === '900' ? '900' : MODEL === '500' ? '500' : '700');
  sorted.forEach(p => {
    const st = STATUS[String(p)] || {};
    const soloed = !GANG_MODE && SOLO_PORT === p;
    const inGang = GANG_MODE && ACTIVE_PORTS.has(p);
    const card = document.createElement('div');
    card.className = 'scard' + (soloed ? ' soloed' : inGang ? ' ginGang' : '');

    const headerExtra = soloed
      ? '<span class="solo-badge">● SOLO</span>'
      : '<button class="solo-btn" onclick="soloPort(' + p + ');event.stopPropagation()">Solo →</button>';

    const frzCls = st.frz === '1' ? ' vv-on' : '';
    const exeCls = st.exe === '1' ? ' vv-on' : '';

    card.innerHTML =
      '<div class="scard-hdr" onclick="soloPort(' + p + ')">' +
        '<span class="sport">COM ' + p + '</span>' +
        '<span class="ssig">' + (st.info || 'No signal / offline') + '</span>' +
        headerExtra +
      '</div>' +
      '<div class="scard-body">' +
        '<div class="sv2"><span class="sk2">H Shift</span><span class="vv2">' + (st.hph || '—') + '</span></div>' +
        '<div class="sv2"><span class="sk2">V Shift</span><span class="vv2">' + (st.vph || '—') + '</span></div>' +
        '<div class="sv2"><span class="sk2">H Size</span><span class="vv2">'  + (st.hsz || '—') + '</span></div>' +
        '<div class="sv2"><span class="sk2">V Size</span><span class="vv2">'  + (st.vsz || '—') + '</span></div>' +
        '<div class="sv2"><span class="sk2">Freeze</span><span class="vv2' + frzCls + '">' + (st.frz === '1' ? 'ON' : 'OFF') + '</span></div>' +
        '<div class="sv2"><span class="sk2">Exec</span><span class="vv2'   + exeCls + '">' + (st.exe === '1' ? 'ON' : 'OFF') + '</span></div>' +
        '<a class="open-link" href="' + modelPage + '?port=' + p + '" target="_blank">Open full page ↗</a>' +
      '</div>';
    grid.appendChild(card);
  });
}

// ── Toast ────────────────────────────────────────────────────────────────────
let _toastTimer2;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(_toastTimer2);
  _toastTimer2 = setTimeout(() => el.style.display = 'none', 2800);
}

setup();
</script>
</body>
</html>"""
