from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import config_store
import dms_control
import dms_names
from shared import log

router = APIRouter()

def _dms_device():
    """Find the DMS device config from config_store."""
    for d in config_store.get_devices():
        if d.get("kind") == "dms3600":
            return d
    return None


@router.get("/control/dms", response_class=HTMLResponse)
def control_dms():
    return HTMLResponse(DMS_HTML)


@router.get("/api/control/dms/state")
def dms_state():
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS device not found in config"}, status_code=404)
    ties, signals, err = dms_control.poll_state(dev["ip"], dev.get("port", 23))
    return JSONResponse({
        "ties": ties,
        "signals": signals,
        "error": err,
        "connected": err is None,
    })


@router.post("/api/control/dms/tie")
async def dms_tie(request: Request):
    body = await request.json()
    inp = int(body.get("input", -1))
    out = int(body.get("output", 0))
    if inp < 0 or out < 1:
        return JSONResponse({"error": "input (>=0) and output (>=1) required"}, status_code=400)
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS not found"}, status_code=404)
    ok, resp, err = dms_control.send_tie(dev["ip"], dev.get("port", 23), inp, out)
    log(f"DMS tie: in{inp}→out{out}  {'OK' if ok else 'FAIL: '+str(err)}")
    return JSONResponse({"ok": ok, "response": resp, "error": err})


@router.post("/api/control/dms/ties-batch")
async def dms_ties_batch(request: Request):
    body = await request.json()
    inp = int(body.get("input", 0))
    outputs = [int(x) for x in body.get("outputs", [])]
    if inp < 1 or not outputs:
        return JSONResponse({"error": "input and outputs required"}, status_code=400)
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS not found"}, status_code=404)
    ok, errors = dms_control.send_ties_batch(dev["ip"], dev.get("port", 23), inp, outputs)
    log(f"DMS batch tie: in{inp}→{outputs}  {'OK' if ok else 'FAIL'}")
    return JSONResponse({"ok": ok, "errors": errors})


@router.post("/api/control/dms/preset")
async def dms_preset(request: Request):
    body = await request.json()
    preset = int(body.get("preset", 0))
    if preset < 1:
        return JSONResponse({"error": "preset number required"}, status_code=400)
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS not found"}, status_code=404)
    ok, err = dms_control.recall_preset(dev["ip"], dev.get("port", 23), preset)
    log(f"DMS preset recall: {preset}  {'OK' if ok else 'FAIL: '+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.get("/api/control/dms/names")
def dms_names_get():
    return JSONResponse(dms_names.load())


@router.put("/api/control/dms/names")
async def dms_names_put(request: Request):
    body = await request.json()
    data = dms_names.load()
    for section in ("inputs", "outputs", "presets"):
        if section in body:
            for k, v in body[section].items():
                data[section][str(k)] = str(v)[:32].strip()
    dms_names.save(data)
    return JSONResponse({"ok": True})


@router.post("/api/control/dms/rename")
async def dms_rename(request: Request):
    """Rename an input, output, or preset on the switcher AND in local JSON."""
    body = await request.json()
    kind   = body.get("kind", "")      # "input" | "output" | "preset"
    number = int(body.get("number", 0))
    name   = str(body.get("name", "")).strip()
    if kind not in ("input", "output", "preset") or number < 1 or not name:
        return JSONResponse({"error": "kind, number, name required"}, status_code=400)
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS not found"}, status_code=404)
    # Send rename to switcher
    ok, err = dms_control.rename_io(dev["ip"], dev.get("port", 23), kind, number, name)
    # Always persist locally too (even if switcher errors)
    section = kind + "s"  # "inputs" / "outputs" / "presets"
    dms_names.update_names(section, {str(number): name})
    log(f"DMS rename {kind} {number} → '{name}'  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.post("/api/control/dms/poll-names")
def dms_poll_names():
    """Pull all I/O and preset names from the switcher and persist them."""
    dev = _dms_device()
    if not dev:
        return JSONResponse({"error": "DMS not found"}, status_code=404)
    polled, err = dms_control.poll_names(dev["ip"], dev.get("port", 23))
    # Merge: polled names override stored defaults
    existing = dms_names.load()
    for section in ("inputs", "outputs", "presets"):
        existing[section].update(polled.get(section, {}))
    dms_names.save(existing)
    log(f"DMS names synced: {sum(len(v) for v in polled.values())} names polled")
    return JSONResponse({"ok": True, "names": existing, "polled": polled, "error": err})


DMS_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<title>Joebot Lab · DMS 3600</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
  /* base tokens come from lab.css */
  :root{ --staged:#e0a040; }
  *{-webkit-tap-highlight-color:transparent}
  body{font-size:14px;line-height:1.4;
    height:100dvh;display:flex;flex-direction:column;overflow:hidden}

  /* ── header ── */
  header{display:flex;align-items:center;gap:12px;flex-wrap:nowrap;
    padding:10px 16px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(224,160,64,.06),transparent);
    flex-shrink:0}
  .brand{font-size:18px;font-weight:700;letter-spacing:.1em;color:var(--accent);white-space:nowrap}
  nav a{color:var(--muted);text-decoration:none;font-size:12px;padding:4px 9px;
    border-radius:6px;border:1px solid transparent;white-space:nowrap}
  nav a:hover{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  .hdr-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
  .conn-dot{width:8px;height:8px;border-radius:50%;background:var(--gray);flex-shrink:0}
  .conn-dot.ok{background:var(--ok);box-shadow:0 0 6px rgba(52,211,153,.6)}
  .conn-dot.bad{background:var(--bad)}

  /* ── mode toggle ── */
  .mode-wrap{display:flex;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;overflow:hidden;flex-shrink:0}
  .mode-btn{background:transparent;border:none;color:var(--muted);
    font-family:var(--mono);font-size:12px;padding:6px 12px;cursor:pointer;
    transition:background .15s,color .15s}
  .mode-btn.active{background:var(--accent);color:#0c0e12;font-weight:700}
  .mode-btn:hover:not(.active){color:var(--ink)}

  /* ── take bar ── */
  #take-bar{display:none;align-items:center;gap:10px;
    padding:8px 16px;background:rgba(224,160,64,.1);
    border-bottom:1px solid rgba(224,160,64,.3);flex-shrink:0}
  #take-bar.show{display:flex}
  #take-bar .info{color:var(--accent);font-size:13px;flex:1}
  .btn-take{background:var(--accent);color:#0c0e12;border:none;
    font-family:var(--mono);font-weight:700;font-size:14px;
    padding:8px 24px;border-radius:8px;cursor:pointer;letter-spacing:.05em}
  .btn-take:hover{background:#f0b050}
  .btn-clear{background:transparent;color:var(--muted);border:1px solid var(--line);
    font-family:var(--mono);font-size:12px;padding:6px 12px;border-radius:6px;cursor:pointer}
  .btn-clear:hover{color:var(--ink);border-color:var(--accent)}

  /* ── main layout ── */
  main{display:flex;flex:1;overflow:hidden;gap:0}
  .pane{display:flex;flex-direction:column;flex:1;overflow:hidden;
    border-right:1px solid var(--line)}
  .pane:last-child{border-right:none}
  .pane-hdr{display:flex;align-items:center;gap:8px;
    padding:8px 14px;border-bottom:1px solid var(--line);
    background:var(--panel);flex-shrink:0}
  .pane-title{font-size:12px;font-weight:600;letter-spacing:.08em;
    text-transform:uppercase;color:var(--muted)}
  .pane-count{font-size:11px;color:var(--gray)}
  .pane-search{margin-left:auto;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:6px;padding:4px 8px;
    font-family:var(--mono);font-size:12px;width:120px}
  .pane-search:focus{outline:none;border-color:var(--accent);width:160px;transition:width .2s}
  .pane-body{flex:1;overflow-y:auto;padding:8px}

  /* ── shared card grid — columns set dynamically by JS ── */
  .card-grid{display:grid;gap:6px;grid-auto-rows:72px}

  /* ── input cards ── */
  .in-item{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:9px 11px;cursor:pointer;transition:background .12s,border-color .12s,box-shadow .12s;
    position:relative;user-select:none;display:flex;flex-direction:column;justify-content:space-between;
    height:100%;overflow:hidden}
  .in-item:hover:not(.selected){background:var(--panel2);border-color:var(--muted)}
  .in-item.selected{background:rgba(224,160,64,.16);border-color:rgba(224,160,64,.65);
    box-shadow:0 0 0 1px rgba(224,160,64,.25)}
  .in-top{display:flex;align-items:center;gap:5px}
  .in-num{font-size:10px;color:var(--muted);flex:1}
  .in-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--gray)}
  .in-dot.active{background:var(--ok);box-shadow:0 0 5px rgba(52,211,153,.55)}
  .in-name{font-size:12.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    margin-top:4px}
  .in-item.selected .in-name{color:var(--accent);font-weight:600}
  .in-edit{position:absolute;top:5px;right:7px;background:none;border:none;
    color:var(--muted);cursor:pointer;padding:2px;font-size:11px;opacity:0}
  .in-item:hover .in-edit,.in-item.selected .in-edit{opacity:.6}

  /* ── output items ── */
  .out-grid{display:grid;gap:6px;grid-auto-rows:72px}
  .out-item{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:9px 11px;cursor:pointer;transition:background .1s,border-color .1s,box-shadow .1s;
    position:relative;user-select:none;display:flex;flex-direction:column;justify-content:space-between;
    height:100%;overflow:hidden}
  .out-item:hover{background:var(--panel2);border-color:var(--muted)}
  .out-item.tied-selected{background:rgba(52,211,153,.1);border-color:rgba(52,211,153,.5)}
  .out-item.tied-other{border-color:rgba(255,255,255,.15)}
  .out-item.staged{border-color:var(--staged);box-shadow:0 0 0 1px rgba(224,160,64,.3);
    animation:stagepulse 2s ease-in-out infinite}
  @keyframes stagepulse{0%,100%{box-shadow:0 0 0 1px rgba(224,160,64,.2)}
    50%{box-shadow:0 0 0 3px rgba(224,160,64,.4)}}
  .out-num{font-size:10px;color:var(--muted);margin-bottom:2px}
  .out-name{font-size:13px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .out-tie{font-size:11px;margin-top:4px;min-height:16px}
  .out-tie .tie-inp{color:var(--ok)} .tied-selected .out-tie .tie-inp{color:var(--ok)}
  .out-tie .tie-none{color:var(--gray)}
  .out-tie .tie-other{color:var(--muted)}
  .out-item.staged .out-tie{color:var(--accent);font-weight:600}
  .out-edit{position:absolute;top:6px;right:7px;background:none;border:none;
    color:var(--muted);cursor:pointer;padding:2px;font-size:12px;opacity:0}
  .out-item:hover .out-edit{opacity:.5}

  /* ── presets panel ── */
  #presets-pane{width:200px;border-left:1px solid var(--line);
    display:flex;flex-direction:column;flex-shrink:0;overflow:hidden;
    transition:width .2s;background:var(--panel)}
  #presets-pane.hidden{width:0;border-left:none;overflow:hidden}
  .pst-hdr{padding:8px 12px;border-bottom:1px solid var(--line);
    font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;
    color:var(--muted);flex-shrink:0}
  .pst-body{flex:1;overflow-y:auto;padding:6px}
  .pst-item{padding:7px 10px;border-radius:7px;cursor:pointer;
    border:1px solid transparent;margin-bottom:2px;display:flex;align-items:center;gap:6px}
  .pst-item:hover{background:var(--panel2);border-color:var(--line)}
  .pst-item.confirming{background:rgba(224,160,64,.15);border-color:rgba(224,160,64,.4)}
  .pst-num{font-size:10px;color:var(--muted);min-width:20px}
  .pst-name{font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .pst-item.confirming .pst-name{color:var(--accent)}
  .pst-confirm-lbl{font-size:10px;color:var(--accent)}

  /* ── portrait layout ── */
  @media (orientation:portrait){
    main{flex-direction:column}
    .pane{flex:1;border-right:none;border-bottom:1px solid var(--line)}
    #presets-pane{width:100%;height:180px;border-left:none;border-top:1px solid var(--line);
      flex-direction:row;flex-shrink:0}
    #presets-pane.hidden{height:0;border-top:none;width:100%}
    .pst-hdr{writing-mode:horizontal-tb;border-bottom:1px solid var(--line);border-right:none;
      width:100%;height:auto}
    .card-grid{grid-auto-rows:64px}
  }

  /* ── edit overlay ── */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
    z-index:100;align-items:center;justify-content:center}
  .overlay.open{display:flex}
  .edit-modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:20px;width:300px;max-width:90vw}
  .edit-modal h3{margin:0 0 14px;font-size:14px;font-weight:600;color:var(--accent)}
  .edit-modal input{width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:8px 10px;
    font-family:var(--mono);font-size:14px;margin-bottom:12px}
  .edit-modal input:focus{outline:none;border-color:var(--accent)}
  .edit-foot{display:flex;gap:8px;justify-content:flex-end}
  .btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:7px 14px;font-family:var(--mono);cursor:pointer;font-size:13px}
  .btn:hover{border-color:var(--accent);color:var(--accent)}
  .btn-acc{background:rgba(224,160,64,.15);color:var(--accent);
    border-color:rgba(224,160,64,.4)}
  .btn-acc:hover{background:rgba(224,160,64,.25)}

  /* ── toast ── */
  .toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
    background:var(--panel2);border:1px solid var(--line);border-radius:8px;
    padding:8px 18px;font-size:13px;opacity:0;transition:opacity .25s;
    pointer-events:none;z-index:200;white-space:nowrap}
  .toast.show{opacity:1}

  /* ── scrollbar ── */
  ::-webkit-scrollbar{width:5px;height:5px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
</style></head>
<body>

<header>
  <div class="brand">🦖 DMS 3600</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/config">Config</a>
  </nav>
  <div class="spacer"></div>
  <div class="hdr-right">
    <span class="conn-dot" id="conn-dot"></span>
    <span style="font-size:11px;color:var(--muted)" id="status-lbl">connecting…</span>
    <div class="mode-wrap">
      <button class="mode-btn active" id="btn-quick" onclick="setMode('quick')">⚡ Quick</button>
      <button class="mode-btn" id="btn-take" onclick="setMode('take')">✦ Take</button>
    </div>
    <button class="btn" id="btn-sync" onclick="syncNames()" style="font-size:12px;padding:5px 11px" title="Pull names from switcher">⟳ Sync Names</button>
    <button class="btn" id="btn-presets" onclick="togglePresets()" style="font-size:12px;padding:5px 11px">Presets</button>
  </div>
</header>

<div id="take-bar">
  <span class="info" id="take-info"></span>
  <button class="btn-clear" onclick="clearPending()">✕ Clear</button>
  <button class="btn-take" onclick="commitTake()">TAKE</button>
</div>

<main id="main-layout">
  <!-- Inputs pane -->
  <div class="pane" id="inputs-pane">
    <div class="pane-hdr">
      <span class="pane-title">Inputs</span>
      <span class="pane-count" id="sig-count"></span>
      <input class="pane-search" id="in-search" placeholder="filter…" oninput="renderInputs()"/>
    </div>
    <div class="pane-body"><div class="card-grid" id="inputs-body"></div></div>
  </div>

  <!-- Outputs pane -->
  <div class="pane" id="outputs-pane">
    <div class="pane-hdr">
      <span class="pane-title">Outputs</span>
      <span class="pane-count" id="out-count"></span>
      <input class="pane-search" id="out-search" placeholder="filter…" oninput="renderOutputs()"/>
    </div>
    <div class="pane-body">
      <div class="out-grid card-grid" id="outputs-body"></div>
    </div>
  </div>

  <!-- Presets panel -->
  <div id="presets-pane" class="hidden">
    <div class="pst-hdr">Presets</div>
    <div class="pst-body" id="presets-body"></div>
  </div>
</main>

<!-- Name edit modal -->
<div class="overlay" id="edit-overlay">
  <div class="edit-modal">
    <h3 id="edit-title">Edit Name</h3>
    <input id="edit-input" maxlength="32" placeholder="Name…"/>
    <div class="edit-foot">
      <button class="btn" onclick="closeEdit()">Cancel</button>
      <button class="btn btn-acc" onclick="saveEditName()">Save</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
const N_INPUTS  = 36;
const N_OUTPUTS = 24;
const N_PRESETS = 32;

let state = { ties: {}, signals: {}, connected: false };
let names = { inputs: {}, outputs: {}, presets: {} };
let selectedInput = 1;
let pendingOutputs = new Set();
let mode = 'quick';          // 'quick' | 'take'
let presetsVisible = false;
let confirmingPreset = null;
let editTarget = null;       // {section, number}

// ── grid column fitting ───────────────────────────────────────────────────────
// Find the largest divisor of `count` that still fits within `pxWidth`
// using a minimum card width of `minCard` px (+ gap between cards).
function bestColumns(count, pxWidth, minCard=140, gap=6) {
  const maxCols = Math.max(1, Math.floor((pxWidth + gap) / (minCard + gap)));
  // Walk divisors of count descending and pick the first that fits
  const divs = [];
  for (let d = 1; d <= count; d++) if (count % d === 0) divs.push(d);
  // largest divisor <= maxCols
  let cols = divs.filter(d => d <= maxCols).pop() || 1;
  return cols;
}

function applyGridColumns() {
  const inEl  = document.getElementById('inputs-body');
  const outEl = document.getElementById('outputs-body');
  if (inEl)  inEl.style.gridTemplateColumns  = `repeat(${bestColumns(N_INPUTS,  inEl.parentElement.clientWidth)}, 1fr)`;
  if (outEl) outEl.style.gridTemplateColumns = `repeat(${bestColumns(N_OUTPUTS, outEl.parentElement.clientWidth)}, 1fr)`;
}

// Re-fit whenever panes change size (orientation flip, sidebar toggle, etc.)
const _ro = new ResizeObserver(applyGridColumns);
['inputs-pane','outputs-pane'].forEach(id => {
  const el = document.getElementById(id);
  if (el) _ro.observe(el);
});

// ── init ─────────────────────────────────────────────────────────────────────
async function init() {
  await loadNames();
  renderInputs();
  renderOutputs();
  renderPresets();
  applyGridColumns();
  await refresh();
  setInterval(refresh, 5000);
  // Auto-sync names from switcher on first load (non-blocking)
  syncNames(true);
}

// ── sync names from switcher ──────────────────────────────────────────────────
async function syncNames(silent=false) {
  const btn = document.getElementById('btn-sync');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Syncing…'; }
  try {
    const r = await fetch('/api/control/dms/poll-names', { method: 'POST' });
    const d = await r.json();
    if (d.names) {
      names = d.names;
      renderInputs(); renderOutputs(); renderPresets();
      if (!silent) {
        const count = Object.values(d.polled || {}).reduce((a,v)=>a+Object.keys(v).length,0);
        toast(`Synced ${count} names from switcher`);
      }
    } else if (!silent) {
      toast('Sync failed: ' + (d.error || 'unknown'));
    }
  } catch(e) {
    if (!silent) toast('Sync error: ' + e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ Sync Names'; }
  }
}

// ── data loading ─────────────────────────────────────────────────────────────
async function loadNames() {
  try {
    const r = await fetch('/api/control/dms/names');
    names = await r.json();
  } catch(e) {}
}

function inName(n)  { return names.inputs?.[String(n)]  || `Input ${n}`; }
function outName(n) { return names.outputs?.[String(n)] || `Output ${n}`; }
function pstName(n) { return names.presets?.[String(n)] || `Preset ${n}`; }

// ── polling ──────────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const r = await fetch('/api/control/dms/state');
    const d = await r.json();
    state = d;
    updateStatus(d.connected, d.error);
    renderInputs();
    renderOutputs();
  } catch(e) {
    updateStatus(false, String(e));
  }
}

function updateStatus(connected, error) {
  const dot = document.getElementById('conn-dot');
  const lbl = document.getElementById('status-lbl');
  dot.className = 'conn-dot ' + (connected ? 'ok' : 'bad');
  if (connected) {
    const tieCount = Object.values(state.ties).filter(v => v > 0).length;
    lbl.textContent = `live · ${tieCount} tie${tieCount !== 1 ? 's' : ''}`;
  } else {
    lbl.textContent = error ? 'offline' : 'connecting…';
  }
}

// ── mode ─────────────────────────────────────────────────────────────────────
function setMode(m) {
  mode = m;
  clearPending();
  document.getElementById('btn-quick').classList.toggle('active', m === 'quick');
  document.getElementById('btn-take').classList.toggle('active', m === 'take');
  renderOutputs();
}

// ── presets ──────────────────────────────────────────────────────────────────
function togglePresets() {
  presetsVisible = !presetsVisible;
  document.getElementById('presets-pane').classList.toggle('hidden', !presetsVisible);
}

// ── take mode ────────────────────────────────────────────────────────────────
function updateTakeBar() {
  const bar = document.getElementById('take-bar');
  const info = document.getElementById('take-info');
  if (mode === 'take' && pendingOutputs.size > 0) {
    bar.classList.add('show');
    const outList = [...pendingOutputs].sort((a,b)=>a-b)
      .slice(0,6).map(o=>outName(o)).join(', ');
    const more = pendingOutputs.size > 6 ? ` +${pendingOutputs.size-6} more` : '';
    info.textContent = `Route In ${selectedInput} (${inName(selectedInput)}) → ${outList}${more}`;
  } else {
    bar.classList.remove('show');
  }
}

function clearPending() {
  pendingOutputs = new Set();
  updateTakeBar();
  renderOutputs();
}

async function commitTake() {
  if (pendingOutputs.size === 0) return;
  const outputs = [...pendingOutputs];
  clearPending();
  try {
    const r = await fetch('/api/control/dms/ties-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ input: selectedInput, outputs })
    });
    const d = await r.json();
    if (d.ok) {
      toast(`Tied In ${selectedInput} → ${outputs.length} output${outputs.length!==1?'s':''}`);
      setTimeout(refresh, 600);
    } else {
      toast('Take failed: ' + (d.errors?.[0] || 'unknown error'));
    }
  } catch(e) { toast('Take error: ' + e); }
}

// ── routing ──────────────────────────────────────────────────────────────────
async function handleOutput(outputNum) {
  if (mode === 'quick') {
    const currentTie = parseInt(state.ties?.[String(outputNum)] || 0);
    // Re-clicking an output already tied to the selected input → UNTIE (input 0)
    const targetInput = (currentTie === selectedInput) ? 0 : selectedInput;

    // Optimistic update immediately
    state.ties[String(outputNum)] = targetInput;
    renderOutputs();

    try {
      const r = await fetch('/api/control/dms/tie', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ input: targetInput, output: outputNum })
      });
      const d = await r.json();
      if (d.ok) {
        if (targetInput === 0) {
          toast(`Out ${outputNum} untied`);
        } else {
          toast(`In ${selectedInput} → Out ${outputNum}`);
        }
        setTimeout(refresh, 1200);
      } else {
        // Roll back optimistic update
        state.ties[String(outputNum)] = currentTie;
        renderOutputs();
        toast('Tie failed: ' + (d.error || 'unknown'));
      }
    } catch(e) {
      state.ties[String(outputNum)] = currentTie;
      renderOutputs();
      toast('Error: ' + e);
    }
  } else {
    // Take mode: toggle pending
    if (pendingOutputs.has(outputNum)) pendingOutputs.delete(outputNum);
    else pendingOutputs.add(outputNum);
    updateTakeBar();
    renderOutputs();
  }
}

// ── rendering ────────────────────────────────────────────────────────────────
function esc(s) {
  return (s == null ? '' : String(s))
    .replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function renderInputs() {
  const q = (document.getElementById('in-search').value || '').toLowerCase();
  const active = Object.values(state.signals || {}).filter(v=>v).length;
  document.getElementById('sig-count').textContent = `${active}/${N_INPUTS} active`;

  let h = '';
  for (let i = 1; i <= N_INPUTS; i++) {
    const nm = inName(i);
    if (q && !nm.toLowerCase().includes(q) && !String(i).includes(q)) continue;
    const sig = state.signals?.[String(i)] || state.signals?.[i] || false;
    const sel = i === selectedInput;
    const nmEsc = esc(nm).replace(/'/g, '&#39;');
    h += `<div class="in-item${sel?' selected':''}" onclick="selectInput(${i})">
      <button class="in-edit" onclick="event.stopPropagation();openEdit('inputs',${i},'${nmEsc}')" title="Rename">✏</button>
      <div class="in-top">
        <span class="in-num">${i}</span>
        <span class="in-dot${sig?' active':''}"></span>
      </div>
      <div class="in-name">${esc(nm)}</div>
    </div>`;
  }
  document.getElementById('inputs-body').innerHTML = h;
  applyGridColumns();
}

function selectInput(n) {
  selectedInput = n;
  pendingOutputs = new Set();
  updateTakeBar();
  renderInputs();
  renderOutputs();
}

function renderOutputs() {
  const q = (document.getElementById('out-search').value || '').toLowerCase();
  const tied = Object.values(state.ties || {}).filter(v => v > 0).length;
  document.getElementById('out-count').textContent = `${tied}/${N_OUTPUTS} tied`;

  let h = '';
  for (let o = 1; o <= N_OUTPUTS; o++) {
    const nm = outName(o);
    if (q && !nm.toLowerCase().includes(q) && !String(o).includes(q)) continue;
    const tiedInput = parseInt(state.ties?.[String(o)] || state.ties?.[o] || 0);
    const staged = pendingOutputs.has(o);
    const tiedToSelected = tiedInput === selectedInput && tiedInput > 0;
    const tiedToOther = tiedInput > 0 && tiedInput !== selectedInput;

    let cls = 'out-item';
    if (staged) cls += ' staged';
    else if (tiedToSelected) cls += ' tied-selected';
    else if (tiedToOther) cls += ' tied-other';

    let tieHtml = '';
    if (staged) {
      tieHtml = `<span class="out-tie">→ In ${selectedInput}</span>`;
    } else if (tiedInput > 0) {
      const tiecls = tiedToSelected ? 'tie-inp' : 'tie-other';
      tieHtml = `<span class="out-tie"><span class="${tiecls}">← In ${tiedInput}${tiedToSelected ? ' ✓' : ''}</span></span>`;
    } else {
      tieHtml = `<span class="out-tie"><span class="tie-none">untied</span></span>`;
    }

    const nmEscOut = esc(nm).replace(/'/g, '&#39;');
    h += `<div class="${cls}" onclick="handleOutput(${o})">
      <button class="out-edit" onclick="event.stopPropagation();openEdit('outputs',${o},'${nmEscOut}')" title="Rename">✏</button>
      <div class="out-num">${o}</div>
      <div class="out-name">${esc(nm)}</div>
      ${tieHtml}
    </div>`;
  }
  document.getElementById('outputs-body').innerHTML = h;
  applyGridColumns();
}

function renderPresets() {
  let h = '';
  for (let p = 1; p <= N_PRESETS; p++) {
    const nm = pstName(p);
    const conf = p === confirmingPreset;
    h += `<div class="pst-item${conf?' confirming':''}" onclick="handlePreset(${p})">
      <span class="pst-num">${p}</span>
      <span class="pst-name">${esc(nm)}</span>
      ${conf ? '<span class="pst-confirm-lbl">tap to confirm</span>' : ''}
    </div>`;
  }
  document.getElementById('presets-body').innerHTML = h;
}

async function handlePreset(n) {
  if (confirmingPreset === n) {
    // Second tap → recall
    confirmingPreset = null;
    renderPresets();
    try {
      const r = await fetch('/api/control/dms/preset', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ preset: n })
      });
      const d = await r.json();
      if (d.ok) { toast(`Preset ${n}: ${pstName(n)} recalled`); setTimeout(refresh, 800); }
      else toast('Preset failed: ' + (d.error || 'unknown'));
    } catch(e) { toast('Error: ' + e); }
  } else {
    confirmingPreset = n;
    renderPresets();
    setTimeout(() => { if (confirmingPreset === n) { confirmingPreset = null; renderPresets(); } }, 3000);
  }
}

// ── name editing ─────────────────────────────────────────────────────────────
function openEdit(section, number, currentName) {
  editTarget = { section, number };
  document.getElementById('edit-title').textContent =
    `Rename ${section.slice(0,-1)} ${number}`;
  document.getElementById('edit-input').value = currentName;
  document.getElementById('edit-overlay').classList.add('open');
  setTimeout(() => {
    const inp = document.getElementById('edit-input');
    inp.focus(); inp.select();
  }, 50);
}

function closeEdit() {
  document.getElementById('edit-overlay').classList.remove('open');
  editTarget = null;
}

async function saveEditName() {
  if (!editTarget) return;
  const { section, number } = editTarget;
  const newName = document.getElementById('edit-input').value.trim();
  if (!newName) { closeEdit(); return; }

  // Optimistic local update
  names[section] = names[section] || {};
  names[section][String(number)] = newName;
  closeEdit();
  renderInputs(); renderOutputs(); renderPresets();

  // section is "inputs"|"outputs"|"presets" → kind is "input"|"output"|"preset"
  const kind = section.replace(/s$/, '');
  try {
    const r = await fetch('/api/control/dms/rename', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ kind, number: parseInt(number), name: newName })
    });
    const d = await r.json();
    if (d.ok) {
      toast(`Renamed ${kind} ${number} → "${newName}" on switcher`);
    } else {
      // Rename saved locally but switcher rejected it
      toast(`Saved locally (switcher: ${d.error || 'error'})`);
    }
  } catch(e) { toast('Save error: ' + e); }
}

document.getElementById('edit-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveEditName();
  if (e.key === 'Escape') closeEdit();
});
document.getElementById('edit-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('edit-overlay')) closeEdit();
});

// ── toast ────────────────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

init();
</script>
</body></html>"""



