from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import config_store
import matrix12800_control
import matrix12800_names
from shared import log

router = APIRouter()

def _mtx12800_device():
    for d in config_store.get_devices():
        if d.get("kind") == "matrix12800":
            return d
    return None


@router.get("/control/matrix12800", response_class=HTMLResponse)
def control_matrix12800():
    return HTMLResponse(MTX12800_HTML)


@router.get("/api/control/matrix12800/info")
def mtx12800_info():
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    info, n_in, n_out, err = matrix12800_control.poll_info(dev["ip"], dev.get("port", 23))
    return JSONResponse({"ok": err is None, "info": info,
                         "n_inputs": n_in, "n_outputs": n_out, "error": err})


@router.get("/api/control/matrix12800/ties")
def mtx12800_ties():
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    names = matrix12800_names.load()
    n_out = int(names.get("_n_outputs", 128))
    ties, err = matrix12800_control.poll_ties(dev["ip"], dev.get("port", 23), n_outputs=n_out)
    return JSONResponse({"ok": err is None, "ties": {str(k): v for k, v in ties.items()}, "error": err})


@router.post("/api/control/matrix12800/tie")
async def mtx12800_tie(request: Request):
    body = await request.json()
    inp = int(body.get("input", -1))
    out = int(body.get("output", 0))
    if inp < 0 or out < 1:
        return JSONResponse({"error": "input (>=0) and output (>=1) required"}, status_code=400)
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    ok, resp, err = matrix12800_control.send_tie(dev["ip"], dev.get("port", 23), inp, out)
    log(f"MTX12800 tie: in{inp}→out{out}  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "response": resp, "error": err})


@router.post("/api/control/matrix12800/ties-batch")
async def mtx12800_ties_batch(request: Request):
    body = await request.json()
    inp = int(body.get("input", 0))
    outputs = [int(x) for x in body.get("outputs", [])]
    if inp < 1 or not outputs:
        return JSONResponse({"error": "input and outputs required"}, status_code=400)
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    ok, errors = matrix12800_control.send_ties_batch(dev["ip"], dev.get("port", 23), inp, outputs)
    log(f"MTX12800 batch tie: in{inp}→{outputs}  {'OK' if ok else 'FAIL'}")
    return JSONResponse({"ok": ok, "errors": errors})


@router.post("/api/control/matrix12800/preset")
async def mtx12800_preset(request: Request):
    body = await request.json()
    preset = int(body.get("preset", 0))
    if preset < 1:
        return JSONResponse({"error": "preset number required"}, status_code=400)
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    ok, err = matrix12800_control.recall_preset(dev["ip"], dev.get("port", 23), preset)
    log(f"MTX12800 preset recall: {preset}  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.get("/api/control/matrix12800/names")
def mtx12800_names_get():
    return JSONResponse(matrix12800_names.load())


@router.put("/api/control/matrix12800/names")
async def mtx12800_names_put(request: Request):
    body = await request.json()
    data = matrix12800_names.load()
    for section in ("inputs", "outputs", "presets"):
        if section in body:
            for k, v in body[section].items():
                data[section][str(k)] = str(v)[:32].strip()
    if "_n_inputs" in body:  data["_n_inputs"] = body["_n_inputs"]
    if "_n_outputs" in body: data["_n_outputs"] = body["_n_outputs"]
    matrix12800_names.save(data)
    return JSONResponse({"ok": True})


@router.post("/api/control/matrix12800/rename")
async def mtx12800_rename(request: Request):
    body = await request.json()
    kind   = body.get("kind", "")
    number = int(body.get("number", 0))
    name   = str(body.get("name", "")).strip()
    if kind not in ("input", "output", "preset") or number < 1 or not name:
        return JSONResponse({"error": "kind, number, name required"}, status_code=400)
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    ok, err = matrix12800_control.rename_io(dev["ip"], dev.get("port", 23), kind, number, name)
    section = kind + "s"
    matrix12800_names.update_names(section, {str(number): name})
    log(f"MTX12800 rename {kind} {number} → '{name}'  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.post("/api/control/matrix12800/poll-bank")
async def mtx12800_poll_bank(request: Request):
    """Poll names+metadata for one bank (start..start+count-1) of inputs or outputs."""
    body = await request.json()
    kind  = body.get("kind", "input")
    start = int(body.get("start", 1))
    count = int(body.get("count", 32))
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    meta, err = matrix12800_control.poll_bank_metadata(
        dev["ip"], dev.get("port", 23), kind, start, count)
    # merge names into stored JSON
    existing = matrix12800_names.load()
    section = kind + "s"
    for n, m in meta.items():
        if m.get("name"):
            existing[section][str(n)] = m["name"]
    matrix12800_names.save(existing)
    log(f"MTX12800 bank poll: {kind} {start}-{start+count-1}  {len(meta)} names")
    return JSONResponse({"ok": True, "meta": {str(k): v for k, v in meta.items()},
                         "names": existing, "error": err})


@router.post("/api/control/matrix12800/poll-presets")
async def mtx12800_poll_presets(request: Request):
    body = await request.json()
    count = int(body.get("count", 64))
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    names_polled, err = matrix12800_control.poll_bank_names(
        dev["ip"], dev.get("port", 23), "preset", 1, count)
    existing = matrix12800_names.load()
    for n, name in names_polled.items():
        existing["presets"][str(n)] = name
    matrix12800_names.save(existing)
    return JSONResponse({"ok": True, "presets": existing["presets"], "error": err})



MTX12800_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>Joebot Lab · Matrix 12800</title>
<style>
  :root{
    --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
    --ink:#e8ebf0;--muted:#8b93a3;--accent:#e0a040;
    --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--gray:#454b58;
    --staged:#e0a040;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:var(--mono);font-size:14px;
    height:100dvh;display:flex;flex-direction:column;overflow:hidden}

  /* header */
  header{display:flex;align-items:center;gap:10px;flex-wrap:nowrap;
    padding:9px 14px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(52,211,153,.05),transparent);flex-shrink:0}
  .brand{font-size:17px;font-weight:700;letter-spacing:.1em;color:var(--ok);white-space:nowrap}
  nav a{color:var(--muted);text-decoration:none;font-size:11.5px;padding:4px 9px;
    border-radius:6px;border:1px solid transparent}
  nav a:hover{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  .hdr-right{display:flex;align-items:center;gap:7px;flex-shrink:0}
  .conn-dot{width:8px;height:8px;border-radius:50%;background:var(--gray)}
  .conn-dot.ok{background:var(--ok);box-shadow:0 0 6px rgba(52,211,153,.6)}
  .conn-dot.bad{background:var(--bad)}
  .conn-dot.warn{background:var(--warn)}
  #conn-label{font-size:11.5px;color:var(--muted)}
  button{font-family:var(--mono);cursor:pointer;font-size:12.5px}
  .btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:5px 11px}
  .btn:hover{border-color:var(--ok);color:var(--ok)}
  .btn-ok{background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.35)}
  .btn-ok:hover{background:rgba(52,211,153,.2)}
  .btn-warn{background:rgba(245,185,66,.1);color:var(--warn);border-color:rgba(245,185,66,.35)}
  .btn-warn:hover{background:rgba(245,185,66,.2)}
  .btn-bad{background:rgba(255,84,112,.1);color:var(--bad);border-color:rgba(255,84,112,.3)}

  /* mode toggle */
  .mode-toggle{display:flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
  .mode-btn{background:transparent;color:var(--muted);border:none;padding:5px 13px;
    font-family:var(--mono);font-size:12px;cursor:pointer}
  .mode-btn.active{background:rgba(52,211,153,.12);color:var(--ok)}

  /* layout */
  .layout{display:flex;flex:1;overflow:hidden;gap:0}
  .col{display:flex;flex-direction:column;overflow:hidden}
  .col-inputs{flex:0 0 280px;border-right:1px solid var(--line)}
  .col-outputs{flex:1}
  .col-presets{flex:0 0 220px;border-left:1px solid var(--line)}

  /* col headers */
  .col-hdr{padding:8px 10px;border-bottom:1px solid var(--line);flex-shrink:0;
    display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .col-title{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
  .col-sub{font-size:11px;color:var(--accent)}

  /* bank bar */
  .bank-bar{display:flex;gap:3px;flex-wrap:wrap;padding:5px 8px;
    border-bottom:1px solid var(--line);flex-shrink:0;background:var(--panel)}
  .bank-btn{background:transparent;color:var(--muted);border:1px solid var(--line);
    border-radius:5px;padding:3px 8px;font-family:var(--mono);font-size:11px;cursor:pointer}
  .bank-btn:hover{color:var(--ink);border-color:var(--accent)}
  .bank-btn.active{background:rgba(52,211,153,.12);color:var(--ok);border-color:rgba(52,211,153,.4)}

  /* scrollable card areas */
  .card-scroll{flex:1;overflow-y:auto;padding:6px;
    display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
    gap:5px;align-content:start}
  .card-scroll.outputs-grid{grid-template-columns:repeat(auto-fill,minmax(130px,1fr))}

  /* input/output cards */
  .icard,.ocard{border-radius:8px;padding:8px 10px;cursor:pointer;
    border:2px solid transparent;transition:all .12s;position:relative}
  .icard{background:var(--panel);border-color:var(--line)}
  .icard:hover{border-color:var(--ok);background:rgba(52,211,153,.06)}
  .icard.selected{background:rgba(52,211,153,.15);border-color:var(--ok)}
  .ocard{background:var(--panel2);border-color:var(--line)}
  .ocard:hover{border-color:var(--muted)}
  .ocard.tied{border-color:var(--ok);background:rgba(52,211,153,.08)}
  .ocard.tied-to-selected{border-color:var(--ok);background:rgba(52,211,153,.2)}
  .ocard.staged{border-color:var(--staged);background:rgba(224,160,64,.12);
    animation:pulse-staged 1.4s ease-in-out infinite}
  @keyframes pulse-staged{0%,100%{background:rgba(224,160,64,.12)}50%{background:rgba(224,160,64,.22)}}
  .card-num{font-size:10px;color:var(--muted);margin-bottom:2px}
  .card-name{font-size:11.5px;font-weight:600;white-space:nowrap;overflow:hidden;
    text-overflow:ellipsis}
  .card-tied{font-size:10px;color:var(--ok);margin-top:2px}
  .card-type{font-size:9.5px;color:var(--muted);margin-top:1px}
  .edit-btn{position:absolute;top:4px;right:5px;background:transparent;border:none;
    color:var(--muted);font-size:11px;cursor:pointer;padding:2px 4px;
    opacity:0;transition:opacity .1s}
  .icard:hover .edit-btn,.ocard:hover .edit-btn{opacity:1}
  .edit-btn:hover{color:var(--accent)}

  /* take bar */
  .take-bar{flex-shrink:0;padding:7px 10px;border-top:1px solid var(--line);
    display:flex;align-items:center;gap:8px;background:var(--panel)}
  .take-bar.hidden{display:none}
  .take-info{font-size:12px;color:var(--muted);flex:1}
  .take-count{color:var(--staged);font-weight:600}

  /* presets */
  .preset-scroll{flex:1;overflow-y:auto;padding:6px;
    display:grid;grid-template-columns:1fr 1fr;gap:4px;align-content:start}
  .pcard{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    padding:6px 8px;cursor:pointer;transition:all .12s}
  .pcard:hover{border-color:var(--accent);background:rgba(224,160,64,.06)}
  .pcard.confirming{border-color:var(--warn);background:rgba(245,185,66,.12);
    animation:pulse-pre 1s ease-in-out infinite}
  @keyframes pulse-pre{0%,100%{background:rgba(245,185,66,.12)}50%{background:rgba(245,185,66,.22)}}
  .pcard-num{font-size:9px;color:var(--muted);margin-bottom:1px}
  .pcard-name{font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .pcard-edit{display:block;font-size:9px;color:var(--muted);margin-top:1px;cursor:pointer}
  .pcard-edit:hover{color:var(--accent)}

  /* status bar */
  .status-bar{flex-shrink:0;padding:5px 14px;border-top:1px solid var(--line);
    font-size:11.5px;color:var(--muted);display:flex;gap:14px;background:var(--panel)}
  .status-bar .sv{color:var(--ink)}

  /* edit modal */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
    z-index:100;align-items:center;justify-content:center;padding:20px}
  .overlay.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:22px;width:100%;max-width:420px}
  .modal h3{margin:0 0 14px;font-size:15px;font-weight:600}
  .modal input{width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:8px 10px;
    font-family:var(--mono);font-size:13px;margin-bottom:12px}
  .modal input:focus{outline:none;border-color:var(--ok)}
  .modal-foot{display:flex;gap:8px;justify-content:flex-end}
  .toast{position:fixed;bottom:22px;right:22px;background:var(--panel2);
    border:1px solid var(--line);border-radius:8px;padding:9px 15px;
    font-size:12.5px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}

  /* loading overlay */
  .loading-overlay{position:absolute;inset:0;background:rgba(12,14,18,.7);
    display:flex;align-items:center;justify-content:center;
    font-size:13px;color:var(--muted);z-index:10;display:none}
  .col-outputs{position:relative}

  @media(max-width:700px){
    .col-inputs{flex:0 0 160px}
    .col-presets{display:none}
  }
</style></head>
<body>
<header>
  <div class="brand">⚡ MATRIX 12800</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/config/mtx">MTX Editor</a>
  </nav>
  <div class="spacer"></div>
  <div class="hdr-right">
    <span class="conn-dot" id="conn-dot"></span>
    <span id="conn-label">Connecting…</span>
    <div class="mode-toggle">
      <button class="mode-btn active" id="btn-quick" onclick="setMode('quick')">Quick</button>
      <button class="mode-btn" id="btn-take" onclick="setMode('take')">Take</button>
    </div>
    <button class="btn" onclick="doConnect()" id="btn-connect">⟳ Connect</button>
    <button class="btn" onclick="pollBank('input')" title="Reload input names for current bank">↻ In names</button>
    <button class="btn" onclick="pollBank('output')" title="Reload output names for current bank">↻ Out names</button>
  </div>
</header>

<div class="layout">
  <!-- INPUTS column -->
  <div class="col col-inputs">
    <div class="col-hdr">
      <span class="col-title">Inputs</span>
      <span class="col-sub" id="in-bank-label">Bank 1–32</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)" id="selected-label">none selected</span>
    </div>
    <div class="bank-bar" id="in-bank-bar"></div>
    <div class="card-scroll" id="in-cards"></div>
  </div>

  <!-- OUTPUTS column -->
  <div class="col col-outputs" style="flex:1">
    <div class="loading-overlay" id="loading">Loading…</div>
    <div class="col-hdr">
      <span class="col-title">Outputs</span>
      <span class="col-sub" id="out-bank-label">Bank 1–32</span>
      <span style="margin-left:auto;font-size:11px;color:var(--muted)" id="out-filter-label"></span>
    </div>
    <div class="bank-bar" id="out-bank-bar"></div>
    <div class="card-scroll outputs-grid" id="out-cards"></div>
    <div class="take-bar hidden" id="take-bar">
      <span class="take-info">Staged: <span class="take-count" id="take-count">0</span> output(s)</span>
      <button class="btn btn-ok" onclick="doTake()">✓ TAKE</button>
      <button class="btn" onclick="clearStaged()">✕ Clear</button>
    </div>
  </div>

  <!-- PRESETS column -->
  <div class="col col-presets">
    <div class="col-hdr">
      <span class="col-title">Presets</span>
      <button class="btn" style="margin-left:auto;font-size:11px;padding:3px 8px" onclick="pollPresets()">↻ Names</button>
    </div>
    <div class="preset-scroll" id="preset-cards"></div>
  </div>
</div>

<div class="status-bar">
  <span>ties <span class="sv" id="sb-ties">—</span></span>
  <span>selected <span class="sv" id="sb-sel">—</span></span>
  <span>staged <span class="sv" id="sb-staged">—</span></span>
  <span id="sb-status" style="margin-left:auto;color:var(--muted)">Ready</span>
</div>

<!-- Edit name modal -->
<div class="overlay" id="edit-overlay">
  <div class="modal">
    <h3 id="edit-title">Rename</h3>
    <input id="edit-input" maxlength="12" placeholder="Max 12 chars"/>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">
      Invalid chars stripped: + ~ , @ = ` [ ] { } &lt; &gt; ' " ; : | \ ?
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeEdit()">Cancel</button>
      <button class="btn btn-ok" onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
// ── state ─────────────────────────────────────────────────────────────────────
const BANK_SIZE = 32;
let quickMode   = true;
let connected   = false;
let nInputs     = 128, nOutputs = 128;
let inBankStart = 1, outBankStart = 1;
let selectedInput = 0;          // 0 = none
let ties = {};                  // out_num → in_num
let staged = new Set();         // output numbers staged for take
let names = {inputs:{},outputs:{},presets:{}};
let metaCache = {};             // number → {code, name, phys}
let confirmingPreset = null;
let confirmTimer = null;
let editTarget = null;          // {kind, number, currentName}

// ── helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function inName(n){ return names.inputs[String(n)] || `Input ${n}`; }
function outName(n){ return names.outputs[String(n)] || `Output ${n}`; }
function preName(n){ return names.presets[String(n)] || `Preset ${n}`; }

function signalLabel(code){
  const c = parseInt(code) % 10;
  return {1:'RGB',2:'RGBS',3:'RGBHV',4:'Composite',5:'S-Video',6:'Component',7:'Audio'}[c] || '';
}

let _toastT;
function toast(msg,dur=2200){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  clearTimeout(_toastT);_toastT=setTimeout(()=>el.classList.remove('show'),dur);
}

function setStatus(msg){ document.getElementById('sb-status').textContent=msg; }
function setConnected(ok, label=''){
  connected=ok;
  const dot=document.getElementById('conn-dot');
  dot.className='conn-dot '+(ok?'ok':connected===null?'warn':'');
  document.getElementById('conn-label').textContent = label||(ok?'Live':'Offline');
}

// ── mode ──────────────────────────────────────────────────────────────────────
function setMode(m){
  quickMode = m==='quick';
  document.getElementById('btn-quick').classList.toggle('active', quickMode);
  document.getElementById('btn-take').classList.toggle('active', !quickMode);
  if(quickMode){ clearStaged(); }
}

// ── bank system ───────────────────────────────────────────────────────────────
function numBanks(total){ return Math.ceil(total/BANK_SIZE); }
function bankEnd(start, total){ return Math.min(start+BANK_SIZE-1, total); }

function renderBankBars(){
  const inBar = document.getElementById('in-bank-bar');
  const outBar = document.getElementById('out-bank-bar');
  inBar.innerHTML=''; outBar.innerHTML='';
  for(let s=1; s<=nInputs; s+=BANK_SIZE){
    const e=bankEnd(s,nInputs);
    const btn=document.createElement('button');
    btn.className='bank-btn'+(s===inBankStart?' active':'');
    btn.textContent=`${s}–${e}`;
    btn.onclick=()=>switchInputBank(s);
    inBar.appendChild(btn);
  }
  for(let s=1; s<=nOutputs; s+=BANK_SIZE){
    const e=bankEnd(s,nOutputs);
    const btn=document.createElement('button');
    btn.className='bank-btn'+(s===outBankStart?' active':'');
    btn.textContent=`${s}–${e}`;
    btn.onclick=()=>switchOutputBank(s);
    outBar.appendChild(btn);
  }
}

function switchInputBank(start){
  inBankStart=start;
  document.querySelectorAll('#in-bank-bar .bank-btn').forEach(b=>{
    b.classList.toggle('active', b.textContent.startsWith(String(start)+'–')||b.textContent===`${start}–${bankEnd(start,nInputs)}`);
  });
  document.getElementById('in-bank-label').textContent=`Bank ${start}–${bankEnd(start,nInputs)}`;
  renderInputCards();
}

function switchOutputBank(start){
  outBankStart=start;
  document.querySelectorAll('#out-bank-bar .bank-btn').forEach(b=>{
    b.classList.toggle('active', b.textContent===`${start}–${bankEnd(start,nOutputs)}`);
  });
  document.getElementById('out-bank-label').textContent=`Bank ${start}–${bankEnd(start,nOutputs)}`;
  renderOutputCards();
}

// ── card rendering ────────────────────────────────────────────────────────────
function renderInputCards(){
  const box=document.getElementById('in-cards');
  const end=bankEnd(inBankStart,nInputs);
  box.innerHTML='';
  for(let n=inBankStart; n<=end; n++){
    const nm=esc(inName(n));
    const meta=metaCache['i'+n];
    const typeStr=meta?signalLabel(meta.code):'';
    const div=document.createElement('div');
    div.className='icard'+(n===selectedInput?' selected':'');
    div.dataset.n=n;
    div.innerHTML=`<div class="card-num">#${n}</div>
      <div class="card-name">${nm}</div>
      ${typeStr?`<div class="card-type">${esc(typeStr)}</div>`:''}
      <button class="edit-btn" onclick="openEdit(event,'input',${n},'${nm}')">✎</button>`;
    div.addEventListener('click', ()=>selectInput(n));
    box.appendChild(div);
  }
  updateStatusBar();
}

function renderOutputCards(){
  const box=document.getElementById('out-cards');
  const end=bankEnd(outBankStart,nOutputs);
  box.innerHTML='';
  for(let n=outBankStart; n<=end; n++){
    const nm=esc(outName(n));
    const tied=ties[n]||0;
    const meta=metaCache['o'+n];
    const typeStr=meta?signalLabel(meta.code):'';
    const isStaged=staged.has(n);
    const isTied=tied>0;
    const isTiedToSel=tied===selectedInput&&selectedInput>0;
    let cls='ocard';
    if(isStaged) cls+=' staged';
    else if(isTiedToSel) cls+=' tied-to-selected';
    else if(isTied) cls+=' tied';

    const tiedLabel=isTied?`<div class="card-tied">← In ${tied}: ${esc(inName(tied))}</div>`:'';
    const div=document.createElement('div');
    div.className=cls;
    div.dataset.n=n;
    div.innerHTML=`<div class="card-num">#${n}</div>
      <div class="card-name">${nm}</div>
      ${typeStr?`<div class="card-type">${esc(typeStr)}</div>`:''}
      ${tiedLabel}
      <button class="edit-btn" onclick="openEdit(event,'output',${n},'${nm}')">✎</button>`;
    div.addEventListener('click', ()=>handleOutputClick(n));
    box.appendChild(div);
  }
  updateStatusBar();
}

function renderPresetCards(){
  const box=document.getElementById('preset-cards');
  box.innerHTML='';
  for(let n=1; n<=64; n++){
    const nm=esc(preName(n));
    const isConf=confirmingPreset===n;
    const div=document.createElement('div');
    div.className='pcard'+(isConf?' confirming':'');
    div.innerHTML=`<div class="pcard-num">Preset ${n}</div>
      <div class="pcard-name">${nm}</div>
      <span class="pcard-edit" onclick="openEdit(event,'preset',${n},'${nm}')">✎ rename</span>`;
    div.addEventListener('click', e=>{ if(!e.target.classList.contains('pcard-edit')) handlePreset(n); });
    box.appendChild(div);
  }
}

// ── input selection ───────────────────────────────────────────────────────────
function selectInput(n){
  selectedInput=n;
  document.querySelectorAll('.icard').forEach(c=>{
    c.classList.toggle('selected', parseInt(c.dataset.n)===n);
  });
  document.getElementById('selected-label').textContent=`In ${n}: ${inName(n)}`;
  document.getElementById('sb-sel').textContent=`In ${n}`;
  renderOutputCards();   // refresh tied highlights
}

// ── output click (tie / stage) ────────────────────────────────────────────────
function handleOutputClick(out){
  if(selectedInput===0){ toast('Select an input first'); return; }
  if(quickMode){
    const currentTie=ties[out]||0;
    const newIn = currentTie===selectedInput ? 0 : selectedInput;
    optimisticTie(out, newIn);
    sendTie(selectedInput===currentTie?0:selectedInput, out);
  } else {
    if(staged.has(out)){ staged.delete(out); }
    else               { staged.add(out); }
    renderOutputCards();
    const tb=document.getElementById('take-bar');
    tb.classList.toggle('hidden', staged.size===0);
    document.getElementById('take-count').textContent=staged.size;
    document.getElementById('sb-staged').textContent=staged.size||'—';
  }
}

function optimisticTie(out, inp){
  ties[out]=inp;
  renderOutputCards();
}

async function sendTie(inp, out){
  setStatus(`Routing In ${inp} → Out ${out}…`);
  try{
    const r=await fetch('/api/control/matrix12800/tie',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:inp,output:out})});
    const j=await r.json();
    if(j.ok){
      setStatus(inp===0?`Untied Out ${out}`:`Tied In ${inp} → Out ${out}`);
    } else {
      toast('Route failed: '+(j.error||'unknown'));
      setStatus('Route failed');
    }
  }catch(e){ toast('Network error: '+e.message); setStatus('Error'); }
}

// ── take ──────────────────────────────────────────────────────────────────────
async function doTake(){
  if(!staged.size){ return; }
  if(selectedInput===0){ toast('Select an input first'); return; }
  const outputs=[...staged];
  // Optimistic
  outputs.forEach(o=>ties[o]=selectedInput);
  staged.clear();
  renderOutputCards();
  document.getElementById('take-bar').classList.add('hidden');
  setStatus(`Taking ${outputs.length} route(s)…`);
  try{
    const r=await fetch('/api/control/matrix12800/ties-batch',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({input:selectedInput,outputs})});
    const j=await r.json();
    setStatus(j.ok?`Took ${outputs.length} route(s) (unverified)`:'Batch take failed: '+(j.error||''));
    if(!j.ok) toast('Batch take error: '+(j.error||'unknown'));
  }catch(e){ toast('Network error: '+e.message); setStatus('Error'); }
  document.getElementById('sb-staged').textContent='—';
}

function clearStaged(){
  staged.clear();
  document.getElementById('take-bar').classList.add('hidden');
  document.getElementById('sb-staged').textContent='—';
  renderOutputCards();
}

// ── presets ───────────────────────────────────────────────────────────────────
function handlePreset(n){
  if(confirmingPreset===n){
    clearTimeout(confirmTimer);
    confirmingPreset=null;
    recallPreset(n);
    renderPresetCards();
  } else {
    confirmingPreset=n;
    renderPresetCards();
    clearTimeout(confirmTimer);
    confirmTimer=setTimeout(()=>{ confirmingPreset=null; renderPresetCards(); },3000);
    toast(`Tap again to recall Preset ${n}: ${preName(n)}`, 2800);
  }
}

async function recallPreset(n){
  setStatus(`Recalling Preset ${n}: ${preName(n)}…`);
  try{
    const r=await fetch('/api/control/matrix12800/preset',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({preset:n})});
    const j=await r.json();
    setStatus(j.ok?`Recalled Preset ${n}`:'Preset failed: '+(j.error||''));
    if(j.ok) toast(`Preset ${n} recalled`);
    else toast('Preset error: '+(j.error||'unknown'));
  }catch(e){ toast('Error: '+e.message); }
}

// ── name edit modal ───────────────────────────────────────────────────────────
function openEdit(e, kind, num, currentName){
  e.stopPropagation();
  editTarget={kind,number:num,currentName};
  document.getElementById('edit-title').textContent=`Rename ${kind} ${num}`;
  document.getElementById('edit-input').value=currentName;
  document.getElementById('edit-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('edit-input').focus(),50);
}
function closeEdit(){ document.getElementById('edit-overlay').classList.remove('open'); editTarget=null; }
document.getElementById('edit-overlay').addEventListener('click',e=>{
  if(e.target===document.getElementById('edit-overlay')) closeEdit();
});
document.getElementById('edit-input').addEventListener('keydown',e=>{
  if(e.key==='Enter') saveEdit();
  if(e.key==='Escape') closeEdit();
});

async function saveEdit(){
  if(!editTarget) return;
  const name=document.getElementById('edit-input').value.trim();
  if(!name){ toast('Name cannot be empty'); return; }
  const {kind,number}=editTarget;
  closeEdit();
  try{
    const r=await fetch('/api/control/matrix12800/rename',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind,number,name})});
    const j=await r.json();
    if(j.ok){
      const section=kind+'s';
      names[section][String(number)]=name;
      if(kind==='input') renderInputCards();
      else if(kind==='output') renderOutputCards();
      else renderPresetCards();
      toast(`Renamed ${kind} ${number} → "${name}"`);
    } else {
      toast('Rename failed: '+(j.error||'unknown'));
    }
  }catch(e){ toast('Error: '+e.message); }
}

// ── connect + poll ────────────────────────────────────────────────────────────
async function doConnect(){
  setStatus('Connecting…'); setConnected(null,'Connecting…');
  try{
    const r=await fetch('/api/control/matrix12800/info');
    const j=await r.json();
    if(!j.ok){ setConnected(false,'Error'); setStatus('Connect failed: '+(j.error||'')); toast('Connect failed'); return; }
    nInputs=j.n_inputs||128; nOutputs=j.n_outputs||128;
    setConnected(true, `${nInputs}×${nOutputs}`);
    setStatus(`Connected — ${nInputs} in / ${nOutputs} out`);
    // Save sizes for backend reference
    await fetch('/api/control/matrix12800/names',{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({_n_inputs:nInputs,_n_outputs:nOutputs})});
    renderBankBars();
    // Load stored names
    const nr=await fetch('/api/control/matrix12800/names');
    names=await nr.json();
    renderInputCards(); renderOutputCards(); renderPresetCards();
    // Lazy-load ties
    pollTies();
    // Lazy-load current bank metadata
    pollBank('input'); pollBank('output');
  }catch(e){ setConnected(false,'Error'); setStatus('Error: '+e.message); toast('Connect error: '+e.message); }
}

async function pollTies(){
  setStatus('Polling ties…');
  try{
    const r=await fetch('/api/control/matrix12800/ties');
    const j=await r.json();
    if(j.ok){
      ties={}; for(const[k,v] of Object.entries(j.ties||{})) ties[parseInt(k)]=v;
      const tiedCount=Object.values(ties).filter(v=>v>0).length;
      document.getElementById('sb-ties').textContent=tiedCount;
      setStatus(`${tiedCount} active tie(s)`); setConnected(true,'Live');
      renderOutputCards();
    } else { toast('Poll error: '+(j.error||'')); }
  }catch(e){ toast('Poll failed: '+e.message); }
}

async function pollBank(kind){
  const start=kind==='input'?inBankStart:outBankStart;
  setStatus(`Loading ${kind} names ${start}–${bankEnd(start,kind==='input'?nInputs:nOutputs)}…`);
  try{
    const r=await fetch('/api/control/matrix12800/poll-bank',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({kind,start,count:BANK_SIZE})});
    const j=await r.json();
    if(j.ok){
      // Merge names
      const section=kind+'s';
      for(const[k,v] of Object.entries(j.names[section]||{})) names[section][k]=v;
      // Cache metadata
      for(const[k,v] of Object.entries(j.meta||{})){
        metaCache[(kind==='input'?'i':'o')+k]=v;
      }
      if(kind==='input') renderInputCards(); else renderOutputCards();
      setStatus(`${kind} bank ${start} names loaded`);
    }
  }catch(e){ toast('Poll error: '+e.message); }
}

async function pollPresets(){
  try{
    const r=await fetch('/api/control/matrix12800/poll-presets',{method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({count:64})});
    const j=await r.json();
    if(j.ok){
      names.presets=j.presets; renderPresetCards(); toast('Preset names loaded');
    }
  }catch(e){ toast('Error: '+e.message); }
}

function updateStatusBar(){
  const tiedCount=Object.values(ties).filter(v=>v>0).length;
  document.getElementById('sb-ties').textContent=tiedCount||'—';
}

// ── init ──────────────────────────────────────────────────────────────────────
(async ()=>{
  // Load stored names + sizes
  try{
    const r=await fetch('/api/control/matrix12800/names');
    names=await r.json();
    if(names._n_inputs)  nInputs=parseInt(names._n_inputs);
    if(names._n_outputs) nOutputs=parseInt(names._n_outputs);
  }catch(e){}
  renderBankBars();
  renderInputCards(); renderOutputCards(); renderPresetCards();
  doConnect();
})();
</script>
</body></html>"""

