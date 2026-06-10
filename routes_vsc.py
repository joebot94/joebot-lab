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
