from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import config_store
import smx_control
import smx_names
from shared import log

router = APIRouter()

def _smx_device():
    for d in config_store.get_devices():
        if d.get("kind") == "smx":
            return d
    return None

@router.get("/control/smx", response_class=HTMLResponse)
def control_smx():
    return HTMLResponse(SMX_HTML)


@router.get("/api/control/smx/info")
def smx_info():
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    info, err = smx_control.poll_info(dev["ip"], dev.get("port", 23))
    return JSONResponse({"ok": err is None, "info": info,
                         "planes": smx_control.PLANES,
                         "plane_order": smx_control.PLANE_ORDER,
                         "n_inputs": smx_control.N_INPUTS,
                         "n_outputs": smx_control.N_OUTPUTS,
                         "error": err})


@router.get("/api/control/smx/ties")
def smx_ties(plane: str = "00"):
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    ties, err = smx_control.poll_ties_plane(dev["ip"], dev.get("port", 23), plane)
    return JSONResponse({"ok": True, "plane": plane,
                         "ties": {str(k): v for k, v in ties.items()}, "error": err})


@router.get("/api/control/smx/ties-all")
def smx_ties_all():
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    result, err = smx_control.poll_ties_all_planes(dev["ip"], dev.get("port", 23))
    # Convert int keys to str for JSON
    out = {plane: {str(k): v for k, v in ties.items()} for plane, ties in result.items()}
    return JSONResponse({"ok": True, "planes": out, "error": err})


@router.post("/api/control/smx/tie")
async def smx_tie(request: Request):
    body = await request.json()
    plane  = str(body.get("plane", "00"))
    inp    = int(body.get("input", -1))
    out    = int(body.get("output", 0))
    global_mode = bool(body.get("global", False))
    if inp < 0 or out < 1:
        return JSONResponse({"error": "input (>=0) and output (>=1) required"}, status_code=400)
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    if global_mode:
        ok, errors = smx_control.send_tie_global(dev["ip"], dev.get("port", 23), inp, out)
        log(f"SMX global tie: in{inp}→out{out}  {'OK' if ok else 'FAIL'}")
        return JSONResponse({"ok": ok, "errors": errors})
    else:
        ok, resp, err = smx_control.send_tie(dev["ip"], dev.get("port", 23), plane, inp, out)
        log(f"SMX tie plane={plane}: in{inp}→out{out}  {'OK' if ok else 'FAIL:'+str(err)}")
        return JSONResponse({"ok": ok, "response": resp, "error": err})


@router.post("/api/control/smx/ties-batch")
async def smx_ties_batch(request: Request):
    body = await request.json()
    plane   = str(body.get("plane", "00"))
    inp     = int(body.get("input", 0))
    outputs = [int(x) for x in body.get("outputs", [])]
    global_mode = bool(body.get("global", False))
    if inp < 1 or not outputs:
        return JSONResponse({"error": "input and outputs required"}, status_code=400)
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    if global_mode:
        ok, errors = smx_control.send_ties_batch_global(dev["ip"], dev.get("port", 23), inp, outputs)
    else:
        ok, errors = smx_control.send_ties_batch(dev["ip"], dev.get("port", 23), plane, inp, outputs)
    log(f"SMX batch tie {'global' if global_mode else 'plane='+plane}: in{inp}→{outputs}")
    return JSONResponse({"ok": ok, "errors": errors})


@router.post("/api/control/smx/preset/recall")
async def smx_preset_recall(request: Request):
    body = await request.json()
    preset = int(body.get("preset", 0))
    if preset < 1:
        return JSONResponse({"error": "preset number required"}, status_code=400)
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    ok, err = smx_control.recall_preset(dev["ip"], dev.get("port", 23), preset)
    log(f"SMX preset recall: {preset}  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.post("/api/control/smx/preset/save")
async def smx_preset_save(request: Request):
    body = await request.json()
    preset = int(body.get("preset", 0))
    if preset < 1:
        return JSONResponse({"error": "preset number required"}, status_code=400)
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    ok, err = smx_control.save_preset(dev["ip"], dev.get("port", 23), preset)
    log(f"SMX preset save: {preset}  {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.get("/api/control/smx/names")
def smx_names_get():
    return JSONResponse(smx_names.load())


@router.put("/api/control/smx/names")
async def smx_names_put(request: Request):
    body = await request.json()
    data = smx_names.load()
    for section in ("inputs", "outputs", "presets"):
        if section in body:
            for k, v in body[section].items():
                data[section][str(k)] = str(v)[:32].strip()
    smx_names.save(data)
    return JSONResponse({"ok": True})


@router.post("/api/control/smx/rename")
async def smx_rename(request: Request):
    body = await request.json()
    kind   = body.get("kind", "")
    number = int(body.get("number", 0))
    name   = str(body.get("name", "")).strip()
    if kind not in ("input", "output", "preset") or number < 1 or not name:
        return JSONResponse({"error": "kind, number, name required"}, status_code=400)
    data = smx_names.load()
    section = kind + "s"
    data[section][str(number)] = name
    smx_names.save(data)
    # Presets are local-only; inputs/outputs also push to switcher
    if kind in ("input", "output"):
        dev = _smx_device()
        if dev:
            ok, err = smx_control.rename_io(dev["ip"], dev.get("port", 23), kind, number, name)
            log(f"SMX rename {kind} {number} → '{name}'  {'OK' if ok else 'FAIL:'+str(err)}")
            return JSONResponse({"ok": ok, "error": err})
    return JSONResponse({"ok": True})


@router.post("/api/control/smx/poll-names")
async def smx_poll_names():
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    polled, err = smx_control.poll_names(dev["ip"], dev.get("port", 23))
    existing = smx_names.load()
    for section in ("inputs", "outputs"):
        for k, v in polled.get(section, {}).items():
            existing[section][str(k)] = v
    smx_names.save(existing)
    log(f"SMX poll names: {len(polled.get('inputs',{}))} in / {len(polled.get('outputs',{}))} out")
    return JSONResponse({"ok": True, "names": existing, "error": err})



SMX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>Joebot Lab · SMX</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
  /* base tokens + toast come from lab.css; page adds plane accent colors */
  :root{
    --accent:#a78bfa;
    --c-vga:#4fa3e0;--c-svid:#e06b4f;--c-vid:#4fe08a;--c-aud:#c46fe0;
    --c-active:var(--c-vga);
  }
  *{-webkit-tap-highlight-color:transparent}
  body{font-size:14px;height:100dvh;display:flex;flex-direction:column;overflow:hidden}

  header{display:flex;align-items:center;gap:10px;padding:8px 14px;flex-shrink:0;
    border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(255,255,255,.02),transparent)}
  .brand{font-size:16px;font-weight:700;letter-spacing:.12em;
    color:var(--c-active);white-space:nowrap;transition:color .2s}
  nav a{color:var(--muted);text-decoration:none;font-size:11.5px;padding:3px 8px;
    border-radius:5px;border:1px solid transparent}
  nav a:hover{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  .hdr-r{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
  .conn-dot{width:8px;height:8px;border-radius:50%;background:var(--gray);flex-shrink:0}
  .conn-dot.ok{background:var(--ok);box-shadow:0 0 6px rgba(52,211,153,.55)}
  #conn-lbl{font-size:11px;color:var(--muted)}
  .hbtn{font-family:var(--mono);cursor:pointer;font-size:11.5px;
    background:var(--panel2);color:var(--muted);border:1px solid var(--line);
    border-radius:6px;padding:4px 10px}
  .hbtn:hover{color:var(--ink);border-color:var(--c-active)}
  .mode-toggle{display:flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .mbtn{background:transparent;color:var(--muted);border:none;
    padding:4px 11px;font-family:var(--mono);font-size:11.5px;cursor:pointer}
  .mbtn.active{background:rgba(52,211,153,.12);color:var(--ok)}

  /* plane selector */
  .plane-row{display:flex;gap:6px;padding:10px 14px;flex-shrink:0;
    border-bottom:1px solid var(--line);align-items:center;overflow-x:auto}
  .plane-row-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--muted);white-space:nowrap;margin-right:4px;flex-shrink:0}
  .pbtn{flex:1;min-width:80px;max-width:160px;
    font-family:var(--mono);font-size:12px;font-weight:700;letter-spacing:.06em;
    cursor:pointer;border-radius:9px;padding:9px 6px;border:2px solid var(--line);
    background:var(--panel);color:var(--muted);transition:all .15s;white-space:nowrap}
  .pbtn:hover{color:var(--ink)}
  .pbtn.active-vga {background:rgba(79,163,224,.12);color:var(--c-vga);
    border-color:var(--c-vga);box-shadow:0 0 14px rgba(79,163,224,.2)}
  .pbtn.active-svid{background:rgba(224,107,79,.12);color:var(--c-svid);
    border-color:var(--c-svid);box-shadow:0 0 14px rgba(224,107,79,.2)}
  .pbtn.active-vid {background:rgba(79,224,138,.12);color:var(--c-vid);
    border-color:var(--c-vid);box-shadow:0 0 14px rgba(79,224,138,.2)}
  .pbtn.active-aud {background:rgba(196,111,224,.12);color:var(--c-aud);
    border-color:var(--c-aud);box-shadow:0 0 14px rgba(196,111,224,.2)}

  /* all-planes toggle */
  .all-toggle{flex-shrink:0;display:flex;align-items:center;gap:7px;
    margin-left:10px;padding-left:12px;border-left:1px solid var(--line)}
  .all-toggle-label{font-size:11px;color:var(--muted);white-space:nowrap}
  .all-check{
    appearance:none;-webkit-appearance:none;
    width:36px;height:20px;border-radius:10px;
    background:var(--panel2);border:1px solid var(--line);
    cursor:pointer;position:relative;transition:background .2s;flex-shrink:0}
  .all-check::after{content:'';position:absolute;top:3px;left:3px;
    width:12px;height:12px;border-radius:50%;background:var(--muted);transition:all .2s}
  .all-check:checked{background:rgba(245,185,66,.2);border-color:var(--warn)}
  .all-check:checked::after{left:19px;background:var(--warn)}
  .all-badge{font-size:10px;font-weight:700;letter-spacing:.06em;
    color:var(--warn);opacity:0;transition:opacity .2s;white-space:nowrap}
  .all-badge.show{opacity:1}

  /* info strip */
  .info-strip{flex-shrink:0;padding:5px 14px;border-bottom:1px solid var(--line);
    display:flex;align-items:center;gap:14px;font-size:11px;color:var(--muted);
    background:rgba(255,255,255,.015)}
  .info-strip .sv{color:var(--ink);font-weight:600}
  .info-strip .accent{color:var(--c-active);transition:color .2s}

  /* main io area */
  .io-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;
    padding:10px 12px;gap:12px}
  .sec-label{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--muted);margin-bottom:5px;display:flex;align-items:center;gap:8px}
  .sec-label::after{content:'';flex:1;height:1px;background:var(--line)}

  /* io buttons */
  .btn-grid{display:flex;flex-wrap:wrap;gap:5px}
  .io-btn{font-family:var(--mono);cursor:pointer;border-radius:8px;
    border:2px solid var(--line);background:var(--panel);color:var(--ink);
    display:flex;flex-direction:column;align-items:center;justify-content:center;
    padding:6px 4px;min-width:58px;flex:1 1 58px;max-width:90px;
    transition:all .12s;position:relative;min-height:54px}
  .io-btn:hover{border-color:var(--muted)}
  .io-btn .bnum{font-size:9.5px;color:var(--muted);font-weight:700;letter-spacing:.04em}
  .io-btn .bname{font-size:10.5px;font-weight:600;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    width:100%;text-align:center;padding:0 2px}
  .io-btn .btied{font-size:9px;color:var(--muted);margin-top:2px;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    width:100%;text-align:center}
  .io-btn .edit-dot{position:absolute;top:3px;right:4px;font-size:9px;
    color:var(--muted);opacity:0;cursor:pointer;padding:1px 2px}
  .io-btn:hover .edit-dot{opacity:1}
  .io-btn .edit-dot:hover{color:var(--c-active)}

  /* signal presence dot on inputs */
  .sig-dot{width:6px;height:6px;border-radius:50%;background:var(--gray);
    position:absolute;top:4px;left:5px;transition:background .3s}
  .sig-dot.live{background:var(--ok);box-shadow:0 0 5px rgba(52,211,153,.6)}

  /* input states */
  .in-btn.selected{background:rgba(52,211,153,.18);border-color:var(--ok);
    box-shadow:0 0 10px rgba(52,211,153,.25);color:var(--ok)}
  .in-btn.selected .bnum{color:var(--ok)}

  /* output states */
  .out-btn.tied{border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.05)}
  .out-btn.tied-sel{background:rgba(52,211,153,.2);border-color:var(--ok);
    box-shadow:0 0 10px rgba(52,211,153,.2)}
  .out-btn.tied-sel .bnum,.out-btn.tied-sel .btied{color:var(--ok)}
  .out-btn.staged{border-color:var(--warn);background:rgba(245,185,66,.14);
    animation:pulse-s 1.4s ease-in-out infinite}
  @keyframes pulse-s{0%,100%{background:rgba(245,185,66,.14)}50%{background:rgba(245,185,66,.26)}}

  /* take bar */
  .take-bar{flex-shrink:0;padding:7px 12px;border-top:1px solid var(--line);
    display:none;align-items:center;gap:8px;background:var(--panel)}
  .take-bar.show{display:flex}
  .take-info{font-size:12px;color:var(--muted);flex:1}
  .take-count{color:var(--warn);font-weight:700}
  .tbtn{font-family:var(--mono);cursor:pointer;font-size:12.5px;
    border-radius:7px;padding:6px 14px}
  .tbtn-ok{background:rgba(52,211,153,.1);color:var(--ok);
    border:1px solid rgba(52,211,153,.35)}
  .tbtn-ok:hover{background:rgba(52,211,153,.2)}
  .tbtn-cl{background:var(--panel2);color:var(--muted);border:1px solid var(--line)}

  /* presets */
  .preset-strip{flex-shrink:0;border-top:1px solid var(--line);
    background:var(--panel);padding:8px 12px}
  .preset-hdr{display:flex;align-items:center;gap:8px;margin-bottom:7px}
  .preset-hdr-lbl{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .preset-scroll{display:flex;gap:5px;overflow-x:auto;padding-bottom:2px}
  .pcard{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    padding:5px 8px;min-width:82px;flex-shrink:0}
  .pcard-num{font-size:9px;color:var(--muted)}
  .pcard-name{font-size:10.5px;font-weight:600;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis;max-width:110px;cursor:pointer}
  .pcard-name:hover{color:var(--c-active)}
  .pcard-actions{display:flex;gap:3px;margin-top:4px}
  .pab{font-family:var(--mono);font-size:9px;border-radius:4px;padding:2px 5px;
    cursor:pointer;flex:1;text-align:center}
  .pab-recall{background:rgba(245,185,66,.08);color:var(--warn);
    border:1px solid rgba(245,185,66,.3)}
  .pab-recall:hover,.pab-recall.arm{background:rgba(245,185,66,.22)}
  .pab-save{background:rgba(124,106,245,.08);color:#a78bfa;
    border:1px solid rgba(124,106,245,.3)}
  .pab-save:hover,.pab-save.arm{background:rgba(124,106,245,.22)}

  /* modal */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
    z-index:100;align-items:center;justify-content:center;padding:20px}
  .overlay.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    padding:22px;width:100%;max-width:380px}
  .modal h3{margin:0 0 12px;font-size:14px}
  .modal input{width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:8px 10px;
    font-family:var(--mono);font-size:13px;margin-bottom:10px}
  .modal input:focus{outline:none;border-color:var(--c-active)}
  .modal-note{font-size:10.5px;color:var(--muted);margin-bottom:12px}
  .modal-foot{display:flex;gap:8px;justify-content:flex-end}
  .mfbtn{font-family:var(--mono);cursor:pointer;font-size:12px;
    border-radius:6px;padding:5px 12px}
  .mfbtn-ok{background:rgba(52,211,153,.1);color:var(--ok);
    border:1px solid rgba(52,211,153,.35)}
  .mfbtn-cl{background:var(--panel2);color:var(--muted);border:1px solid var(--line)}

  @media(max-width:600px){
    .pbtn{min-width:60px;font-size:11px;padding:7px 4px}
    .io-btn{min-width:48px;max-width:72px;min-height:46px}
    .all-toggle-label{display:none}
  }
</style></head>
<body>

<header>
  <div class="brand" id="brand">⚡ SMX</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/control/matrix12800">Matrix 12800</a>
  </nav>
  <div class="spacer"></div>
  <div class="hdr-r">
    <span class="conn-dot" id="conn-dot"></span>
    <span id="conn-lbl">…</span>
    <div class="mode-toggle">
      <button class="mbtn active" id="mbtn-quick" onclick="setMode('quick')">Quick</button>
      <button class="mbtn" id="mbtn-take"  onclick="setMode('take')">Take</button>
    </div>
    <button class="hbtn" onclick="doConnect()">⟳ Connect</button>
    <button class="hbtn" onclick="pollTies()">↻ Ties</button>
    <button class="hbtn" onclick="doPollNames()">↻ Names</button>
  </div>
</header>

<!-- plane tabs -->
<div class="plane-row">
  <span class="plane-row-label">PLANE</span>
  <button class="pbtn" id="pb-00" onclick="switchPlane('00')">VGA</button>
  <button class="pbtn" id="pb-01" onclick="switchPlane('01')">S-VIDEO</button>
  <button class="pbtn" id="pb-02" onclick="switchPlane('02')">VIDEO</button>
  <button class="pbtn" id="pb-04" onclick="switchPlane('04')">AUDIO</button>
  <div class="all-toggle">
    <label class="all-toggle-label" for="all-check">All planes</label>
    <input type="checkbox" class="all-check" id="all-check" onchange="toggleAllPlanes(this.checked)"/>
    <span class="all-badge" id="all-badge">ALL PLANES</span>
  </div>
</div>

<!-- info strip -->
<div class="info-strip">
  <span>plane <span class="accent" id="si-plane">VGA</span></span>
  <span>size <span class="sv" id="si-size">16×16</span></span>
  <span>selected <span class="sv" id="si-sel">—</span></span>
  <span>staged <span class="sv" id="si-staged">—</span></span>
  <span style="margin-left:auto" id="si-status">Ready</span>
</div>

<!-- io buttons -->
<div class="io-wrap">
  <div>
    <div class="sec-label">INPUTS</div>
    <div class="btn-grid" id="in-grid"></div>
  </div>
  <div>
    <div class="sec-label">OUTPUTS</div>
    <div class="btn-grid" id="out-grid"></div>
  </div>
</div>

<!-- take bar -->
<div class="take-bar" id="take-bar">
  <span class="take-info">
    <span class="take-count" id="take-count">0</span> output(s) staged
    — <span id="take-desc">select input then TAKE</span>
  </span>
  <button class="tbtn tbtn-ok" onclick="doTake()">✓ TAKE</button>
  <button class="tbtn tbtn-cl" onclick="clearStaged()">✕ Clear</button>
</div>

<!-- presets -->
<div class="preset-strip">
  <div class="preset-hdr">
    <span class="preset-hdr-lbl">GLOBAL PRESETS</span>
    <span style="font-size:10.5px;color:var(--muted);margin-left:6px">tap once to arm · tap again to confirm</span>
  </div>
  <div class="preset-scroll" id="preset-scroll"></div>
</div>

<div class="overlay" id="edit-overlay">
  <div class="modal">
    <h3 id="edit-title">Rename</h3>
    <input id="edit-input" maxlength="12" placeholder="Up to 12 chars"/>
    <div class="modal-note">Invalid chars stripped: + ~ , @ = ` [ ] { } &lt; &gt; ' " ; : | \ ?</div>
    <div class="modal-foot">
      <button class="mfbtn mfbtn-cl" onclick="closeEdit()">Cancel</button>
      <button class="mfbtn mfbtn-ok" onclick="saveEdit()">Save</button>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const PLANES = {
  "00":{label:"VGA",    css:"vga",  nIn:16,nOut:16},
  "01":{label:"S-VIDEO",css:"svid", nIn:16,nOut:16},
  "02":{label:"VIDEO",  css:"vid",  nIn:16,nOut:16},
  "04":{label:"AUDIO",  css:"aud",  nIn:16,nOut:16},
};
const PLANE_ORDER = ["00","01","02","04"];
const N_PRE = 32;

let quickMode     = true;
let activePlane   = "00";
let allPlanesMode = false;   // route to all planes at once
let selectedInput = 0;
let allTies       = {"00":{},"01":{},"02":{},"04":{}};
let staged        = new Set();
let names         = {inputs:{},outputs:{},presets:{}};
let signals       = {};  // input# -> true/false from dashboard poll
let confirmState  = {};
let editTarget    = null;

// ── helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function inName(n){ return names.inputs[String(n)]  || `In ${n}`; }
function outName(n){ return names.outputs[String(n)] || `Out ${n}`; }
function preName(n){ return names.presets[String(n)] || `Preset ${n}`; }

let _toastT;
function toast(msg,dur=2200){
  const el=document.getElementById("toast");
  el.textContent=msg; el.classList.add("show");
  clearTimeout(_toastT); _toastT=setTimeout(()=>el.classList.remove("show"),dur);
}
function setStatus(msg){ document.getElementById("si-status").textContent=msg; }
function setConnected(ok){
  document.getElementById("conn-dot").className="conn-dot"+(ok?" ok":"");
  document.getElementById("conn-lbl").textContent=ok?"Live":"Offline";
}

// ── plane switching ───────────────────────────────────────────────────────────
function switchPlane(plane){
  activePlane=plane;
  const pm=PLANES[plane];
  document.querySelectorAll(".pbtn").forEach(b=>b.className="pbtn");
  document.getElementById("pb-"+plane).className="pbtn active-"+pm.css;
  document.documentElement.style.setProperty("--c-active",`var(--c-${pm.css})`);
  document.getElementById("brand").style.color=`var(--c-${pm.css})`;
  document.getElementById("si-plane").textContent=pm.label;
  document.getElementById("si-size").textContent=`${pm.nIn}×${pm.nOut}`;
  clearStaged();
  selectedInput=0;
  document.getElementById("si-sel").textContent="\u2014";
  renderInputGrid();
  renderOutputGrid();
}

function toggleAllPlanes(on){
  allPlanesMode=on;
  document.getElementById("all-badge").classList.toggle("show",on);
  const strip=document.getElementById("info-strip-all");
  if(on) toast("All-planes mode: ties will route on every board simultaneously");
}

// ── mode ──────────────────────────────────────────────────────────────────────
function setMode(m){
  quickMode=m==="quick";
  document.getElementById("mbtn-quick").classList.toggle("active",quickMode);
  document.getElementById("mbtn-take").classList.toggle("active",!quickMode);
  if(quickMode) clearStaged();
}

// ── ties ──────────────────────────────────────────────────────────────────────
function getTie(out){ return (allTies[activePlane]||{})[out]||0; }

function applyTieLocal(out,inp){
  if(allPlanesMode){ PLANE_ORDER.forEach(p=>allTies[p][out]=inp); }
  else              { allTies[activePlane][out]=inp; }
}

// ── render inputs ─────────────────────────────────────────────────────────────
function renderInputGrid(){
  const box=document.getElementById("in-grid");
  box.innerHTML="";
  const n=PLANES[activePlane].nIn;
  for(let i=1;i<=n;i++){
    const btn=document.createElement("button");
    btn.className="io-btn in-btn"+(i===selectedInput?" selected":"");
    btn.dataset.n=i;
    const live=signals[i]===true;
    btn.innerHTML=`<span class="sig-dot${live?' live':''}"></span>
      <span class="bnum">IN ${i}</span>
      <span class="bname">${esc(inName(i))}</span>
      <span class="edit-dot" onclick="openEdit(event,'input',${i})">\u270e</span>`;
    btn.addEventListener("click",()=>selectInput(i));
    box.appendChild(btn);
  }
}

// ── render outputs ────────────────────────────────────────────────────────────
function renderOutputGrid(){
  const box=document.getElementById("out-grid");
  box.innerHTML="";
  const n=PLANES[activePlane].nOut;
  for(let o=1;o<=n;o++){
    const tied=getTie(o);
    const isStaged=staged.has(o);
    const isTiedSel=tied===selectedInput&&selectedInput>0;
    let cls="io-btn out-btn";
    if(isStaged)      cls+=" staged";
    else if(isTiedSel) cls+=" tied-sel";
    else if(tied>0)    cls+=" tied";
    const btn=document.createElement("button");
    btn.className=cls; btn.dataset.n=o;
    btn.innerHTML=`<span class="bnum">OUT ${o}</span>
      <span class="bname">${esc(outName(o))}</span>
      ${tied>0?`<span class="btied">\u2190 ${esc(inName(tied))}</span>`:""}
      <span class="edit-dot" onclick="openEdit(event,'output',${o})">\u270e</span>`;
    btn.addEventListener("click",()=>handleOutputClick(o));
    box.appendChild(btn);
  }
}

// ── presets ───────────────────────────────────────────────────────────────────
function renderPresets(){
  const box=document.getElementById("preset-scroll");
  box.innerHTML="";
  for(let n=1;n<=N_PRE;n++){
    const cs=confirmState[n];
    const div=document.createElement("div");
    div.className="pcard";
    div.innerHTML=`<div class="pcard-num">Preset ${n}</div>
      <div class="pcard-name" title="double-click to rename">${esc(preName(n))}</div>
      <div class="pcard-actions">
        <button class="pab pab-recall${cs?.type==="recall"?" arm":""}"
          onclick="handlePreset(event,${n},'recall')">\u25b6 Recall</button>
        <button class="pab pab-save${cs?.type==="save"?" arm":""}"
          onclick="handlePreset(event,${n},'save')">\u25cf Save</button>
      </div>`;
    div.querySelector(".pcard-name").addEventListener("dblclick",e=>{
      e.stopPropagation(); openEdit(e,"preset",n);
    });
    box.appendChild(div);
  }
}

// ── select input ──────────────────────────────────────────────────────────────
function selectInput(n){
  selectedInput=n;
  document.querySelectorAll(".in-btn").forEach(b=>{
    b.classList.toggle("selected",parseInt(b.dataset.n)===n);
  });
  document.getElementById("si-sel").textContent=`In ${n}: ${inName(n)}`;
  renderOutputGrid();
}

// ── output click ──────────────────────────────────────────────────────────────
function handleOutputClick(out){
  if(selectedInput===0){ toast("Select an input first"); return; }
  if(quickMode){
    const cur=getTie(out);
    const newIn=cur===selectedInput?0:selectedInput;
    applyTieLocal(out,newIn);
    renderOutputGrid();
    sendTie(newIn,out);
  } else {
    if(staged.has(out)) staged.delete(out); else staged.add(out);
    renderOutputGrid();
    document.getElementById("take-bar").classList.toggle("show",staged.size>0);
    document.getElementById("take-count").textContent=staged.size;
    document.getElementById("take-desc").textContent=
      `\u2192 In ${selectedInput}: ${inName(selectedInput)}`;
    document.getElementById("si-staged").textContent=staged.size||"\u2014";
  }
}

async function sendTie(inp,out){
  const isGlobal=allPlanesMode;
  setStatus(`In ${inp} \u2192 Out ${out}${isGlobal?" (all planes)":""}\u2026`);
  try{
    const r=await fetch("/api/control/smx/tie",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({plane:activePlane,input:inp,output:out,global:isGlobal})});
    const j=await r.json();
    setStatus(j.ok?(inp?`Tied In ${inp} \u2192 Out ${out}`:`Untied Out ${out}`):"Failed: "+(j.error||""));
    if(!j.ok) toast("Route failed: "+(j.error||""));
    else setConnected(true);
  }catch(e){ toast("Error: "+e.message); }
}

// ── take ──────────────────────────────────────────────────────────────────────
async function doTake(){
  if(!staged.size||!selectedInput) return;
  const outputs=[...staged];
  outputs.forEach(o=>applyTieLocal(o,selectedInput));
  staged.clear();
  renderOutputGrid();
  document.getElementById("take-bar").classList.remove("show");
  document.getElementById("si-staged").textContent="\u2014";
  setStatus(`Taking ${outputs.length} route(s)\u2026`);
  try{
    const r=await fetch("/api/control/smx/ties-batch",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({plane:activePlane,input:selectedInput,outputs,global:allPlanesMode})});
    const j=await r.json();
    setStatus(j.ok?`Took ${outputs.length} route(s)`:"Batch failed: "+(j.error||""));
    if(!j.ok) toast("Error: "+(j.error||""));
  }catch(e){ toast("Error: "+e.message); }
}

function clearStaged(){
  staged.clear();
  document.getElementById("take-bar").classList.remove("show");
  document.getElementById("si-staged").textContent="\u2014";
  renderOutputGrid();
}

// ── preset handlers ───────────────────────────────────────────────────────────
function handlePreset(e,n,type){
  e.stopPropagation();
  const cs=confirmState[n];
  if(cs&&cs.type===type){
    clearTimeout(cs.timer); delete confirmState[n]; renderPresets();
    if(type==="recall") doPresetRecall(n); else doPresetSave(n);
  } else {
    if(cs) clearTimeout(cs.timer);
    confirmState[n]={type,timer:setTimeout(()=>{ delete confirmState[n]; renderPresets(); },3000)};
    renderPresets();
    toast(`Tap again to ${type==="recall"?"recall":"SAVE OVER"} Preset ${n}: ${preName(n)}`,2800);
  }
}

async function doPresetRecall(n){
  setStatus(`Recalling Preset ${n}\u2026`);
  try{
    const r=await fetch("/api/control/smx/preset/recall",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({preset:n})});
    const j=await r.json();
    if(j.ok){ toast(`Preset ${n} recalled`); setStatus(`Recalled Preset ${n}`); pollTies(); }
    else { toast("Recall failed: "+(j.error||"")); setStatus("Failed"); }
  }catch(e){ toast("Error: "+e.message); }
}

async function doPresetSave(n){
  setStatus(`Saving Preset ${n}\u2026`);
  try{
    const r=await fetch("/api/control/smx/preset/save",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({preset:n})});
    const j=await r.json();
    if(j.ok){ toast(`Preset ${n} saved`); setStatus(`Saved Preset ${n}`); }
    else { toast("Save failed: "+(j.error||"")); setStatus("Failed"); }
  }catch(e){ toast("Error: "+e.message); }
}

// ── name edit ─────────────────────────────────────────────────────────────────
function openEdit(e,kind,num){
  e.stopPropagation();
  editTarget={kind,number:num};
  const cur=kind==="input"?inName(num):kind==="output"?outName(num):preName(num);
  document.getElementById("edit-title").textContent=`Rename ${kind} ${num}`;
  document.getElementById("edit-input").value=cur;
  document.getElementById("edit-overlay").classList.add("open");
  setTimeout(()=>document.getElementById("edit-input").select(),60);
}
function closeEdit(){
  document.getElementById("edit-overlay").classList.remove("open"); editTarget=null;
}
document.getElementById("edit-overlay").addEventListener("click",e=>{
  if(e.target===document.getElementById("edit-overlay")) closeEdit();
});
document.getElementById("edit-input").addEventListener("keydown",e=>{
  if(e.key==="Enter") saveEdit(); if(e.key==="Escape") closeEdit();
});
async function saveEdit(){
  if(!editTarget) return;
  const name=document.getElementById("edit-input").value.trim();
  if(!name){ toast("Name cannot be empty"); return; }
  const {kind,number}=editTarget;
  closeEdit();
  try{
    const r=await fetch("/api/control/smx/rename",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({kind,number,name})});
    const j=await r.json();
    names[kind+"s"][String(number)]=name;
    if(kind==="input") renderInputGrid();
    else if(kind==="output") renderOutputGrid();
    else renderPresets();
    toast(`Renamed ${kind} ${number} \u2192 "${name}"`);
  }catch(e){ toast("Error: "+e.message); }
}

// ── connect / poll ────────────────────────────────────────────────────────────
async function doConnect(){
  setStatus("Connecting\u2026");
  try{
    const r=await fetch("/api/control/smx/info");
    const j=await r.json();
    if(!j.ok){ setConnected(false); setStatus("Connect failed: "+(j.error||"")); toast("Connect failed"); return; }
    setConnected(true); setStatus("Connected");
    await loadNames();
    pollTies();
  }catch(e){ setConnected(false); setStatus("Error: "+e.message); toast("Error: "+e.message); }
}

async function loadNames(){
  try{
    const r=await fetch("/api/control/smx/names");
    names=await r.json();
    renderInputGrid(); renderOutputGrid(); renderPresets();
  }catch(e){}
}

async function pollTies(){
  setStatus("Polling ties\u2026");
  try{
    const r=await fetch("/api/control/smx/ties-all");
    const j=await r.json();
    if(j.ok){
      for(const p of PLANE_ORDER){
        allTies[p]={};
        for(const[k,v] of Object.entries(j.planes[p]||{})) allTies[p][parseInt(k)]=v;
      }
      setConnected(true); setStatus("Ties loaded"); renderOutputGrid();
    } else toast("Poll error: "+(j.error||""));
  }catch(e){ toast("Poll failed: "+e.message); }
}

async function doPollNames(){
  setStatus("Polling names\u2026");
  try{
    const r=await fetch("/api/control/smx/poll-names",{method:"POST"});
    const j=await r.json();
    if(j.ok){
      names=j.names; renderInputGrid(); renderOutputGrid();
      toast("Names loaded"); setStatus("Names loaded");
    } else toast("Failed: "+(j.error||""));
  }catch(e){ toast("Error: "+e.message); }
}

// ── signal presence from dashboard poll ───────────────────────────────────────
async function pollSignals(){
  try{
    const r=await fetch("/api/status");
    const j=await r.json();
    const devs=Object.values(j.devices||{});
    const smx=devs.find(d=>d.kind==="smx");
    if(smx){
      const next={};
      // boards is array of {slot,signals:[{label,state}],audio}
      (smx.boards||[]).forEach(b=>{
        if(b.audio) return;
        b.signals.forEach((s,idx)=>{
          const port=idx+1;
          if(!next[port]) next[port]=s.state==="ok"||s.state==="warn";
          else next[port]=next[port]||(s.state==="ok"||s.state==="warn");
        });
      });
      signals=next;
      renderInputGrid();
    }
  }catch(e){}
}

// ── init ──────────────────────────────────────────────────────────────────────
(async()=>{
  try{ const r=await fetch("/api/control/smx/names"); names=await r.json(); }catch(e){}
  switchPlane("00");
  renderPresets();
  doConnect();
  pollSignals();
  setInterval(pollSignals, 10000);
})();
</script>
</body></html>"""








