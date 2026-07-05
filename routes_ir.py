from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import config_store
import ir_store
from shared import log

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────────

def _ipcp_device():
    for d in config_store.get_devices():
        if d.get("kind") == "ipcp505":
            return d
    return None


def _ir_send(ip: str, ir_port: int, ir_slot: int) -> tuple[bool, str | None]:
    """
    Send a stored IR code via IPCP 505.
    IPCP 505 SIS command: ESC I port * slot IRSS
    e.g. ESC I 1 * 3 IRSS  →  send slot 3 from IR port 1
    NOTE: fill in exact SIS syntax once confirmed from IPCP 505 manual.
    """
    import socket as _s
    try:
        sock = _s.create_connection((ip, 23), timeout=4.0)
    except (OSError, _s.timeout) as e:
        return False, str(e)
    try:
        # Drain banner
        import time as _t
        _t.sleep(0.4)
        sock.recv(4096)
        cmd = f"\x1bI{ir_port}*{ir_slot}IRSS\r".encode("ascii")
        sock.sendall(cmd)
        _t.sleep(0.3)
        resp = sock.recv(256).decode("ascii", errors="replace").strip()
        return True, None
    except OSError as e:
        return False, str(e)
    finally:
        try: sock.close()
        except OSError: pass


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/control/ir", response_class=HTMLResponse)
def ir_hub():
    return HTMLResponse(IR_HUB_HTML)


@router.get("/control/ir/{remote_id}", response_class=HTMLResponse)
def ir_remote_page(remote_id: str):
    if remote_id not in ir_store.REMOTE_DEFS:
        return HTMLResponse("<h1>Remote not found</h1>", status_code=404)
    return HTMLResponse(IR_REMOTE_HTML)


@router.get("/api/ir/remotes")
def api_ir_remotes():
    out = []
    for rid, rdef in ir_store.REMOTE_DEFS.items():
        codes = ir_store.get_codes(rid)
        learned = sum(1 for b in rdef["buttons"] if b["id"] in codes)
        out.append({
            "id":      rid,
            "name":    rdef["name"],
            "total":   len(rdef["buttons"]),
            "learned": learned,
        })
    return JSONResponse({"remotes": out})


@router.get("/api/ir/{remote_id}/definition")
def api_ir_definition(remote_id: str):
    rdef = ir_store.REMOTE_DEFS.get(remote_id)
    if not rdef:
        return JSONResponse({"error": "remote not found"}, status_code=404)
    codes = ir_store.get_codes(remote_id)
    buttons = []
    for b in rdef["buttons"]:
        c = codes.get(b["id"])
        buttons.append({**b, "ir_slot": c["ir_slot"] if c else None,
                        "ir_port": c["ir_port"] if c else 1})
    return JSONResponse({
        "id":      rdef["id"],
        "name":    rdef["name"],
        "cols":    rdef["cols"],
        "rows":    rdef["rows"],
        "accent":  rdef["accent"],
        "buttons": buttons,
    })


@router.post("/api/ir/{remote_id}/button/{button_id}/assign")
async def api_ir_assign(remote_id: str, button_id: str, request: Request):
    rdef = ir_store.REMOTE_DEFS.get(remote_id)
    if not rdef:
        return JSONResponse({"error": "remote not found"}, status_code=404)
    if not any(b["id"] == button_id for b in rdef["buttons"]):
        return JSONResponse({"error": "button not found"}, status_code=404)
    body = await request.json()
    ir_slot = body.get("ir_slot")   # null = clear
    ir_port = int(body.get("ir_port", 1))
    label   = str(body.get("label", "")).strip()
    ok, err = ir_store.set_code(remote_id, button_id,
                                int(ir_slot) if ir_slot is not None else None,
                                ir_port, label)
    if not ok:
        return JSONResponse({"error": err}, status_code=500)
    log(f"IR assign: {remote_id}/{button_id} → slot {ir_slot} port {ir_port}")
    return JSONResponse({"ok": True})


@router.post("/api/ir/{remote_id}/button/{button_id}/send")
def api_ir_send(remote_id: str, button_id: str):
    rdef = ir_store.REMOTE_DEFS.get(remote_id)
    if not rdef:
        return JSONResponse({"error": "remote not found"}, status_code=404)
    codes = ir_store.get_codes(remote_id)
    c = codes.get(button_id)
    if not c:
        return JSONResponse({"error": "no IR code assigned to this button"}, status_code=400)
    dev = _ipcp_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    ok, err = _ir_send(dev["ip"], c["ir_port"], c["ir_slot"])
    log(f"IR send: {remote_id}/{button_id} slot={c['ir_slot']} port={c['ir_port']} {'OK' if ok else 'FAIL:'+str(err)}")
    return JSONResponse({"ok": ok, "error": err})


@router.post("/api/ir/{remote_id}/clear")
def api_ir_clear(remote_id: str):
    if remote_id not in ir_store.REMOTE_DEFS:
        return JSONResponse({"error": "remote not found"}, status_code=404)
    ok, err = ir_store.clear_remote(remote_id)
    log(f"IR clear: {remote_id}")
    return JSONResponse({"ok": ok, "error": err})


# ── IR Hub HTML ───────────────────────────────────────────────────────────────

IR_HUB_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Joebot Lab · IR Remotes</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
/* base tokens come from lab.css */
:root{ --accent:#e05a1a; }
*{margin:0;padding:0}
body{min-height:100dvh}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(224,90,26,.06),transparent)}
.brand{font-size:18px;font-weight:700;letter-spacing:.1em;color:var(--accent)}
nav a{color:var(--muted);text-decoration:none;font-size:13px;padding:5px 10px;
  border-radius:6px;border:1px solid transparent}
nav a:hover{color:var(--ink);border-color:var(--line)}
main{max-width:900px;margin:0 auto;padding:30px 20px}
h1{font-size:20px;font-weight:700;margin-bottom:6px}
.sub{color:var(--muted);font-size:13px;margin-bottom:32px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:20px;text-decoration:none;color:inherit;display:block;
  transition:all .15s;position:relative}
.card:hover{border-color:var(--accent);background:rgba(224,90,26,.05);
  transform:translateY(-2px);box-shadow:0 4px 20px rgba(0,0,0,.3)}
.card-name{font-size:15px;font-weight:700;margin-bottom:8px}
.card-progress{height:4px;background:var(--panel2);border-radius:2px;
  margin-bottom:8px;overflow:hidden}
.card-bar{height:100%;background:var(--ok);border-radius:2px;transition:width .4s}
.card-stats{font-size:12px;color:var(--muted)}
.card-arrow{position:absolute;top:20px;right:20px;color:var(--muted);font-size:18px}
.empty{text-align:center;padding:60px;color:var(--muted)}
</style></head>
<body>
<header>
  <span class="brand">JOEBOT LAB</span>
  <nav>
    <a href="/">← Dashboard</a>
    <a href="/control/ir" class="active" style="color:var(--accent)">IR Remotes</a>
  </nav>
</header>
<main>
  <h1>IR Remotes</h1>
  <p class="sub">Click a remote to open the control panel. Learned codes show in green.</p>
  <div class="grid" id="grid"><p class="empty">Loading…</p></div>
</main>
<script>
async function load(){
  const r = await fetch('/api/ir/remotes');
  const j = await r.json();
  const grid = document.getElementById('grid');
  if(!j.remotes.length){ grid.innerHTML='<p class="empty">No remotes configured.</p>'; return; }
  grid.innerHTML = j.remotes.map(rem=>{
    const pct = rem.total ? Math.round(100*rem.learned/rem.total) : 0;
    return `<a class="card" href="/control/ir/${rem.id}">
      <div class="card-name">${esc(rem.name)}</div>
      <div class="card-progress"><div class="card-bar" style="width:${pct}%"></div></div>
      <div class="card-stats">${rem.learned} / ${rem.total} buttons learned</div>
      <div class="card-arrow">→</div>
    </a>`;
  }).join('');
}
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
load();
</script>
</body></html>"""


# ── IR Remote HTML ─────────────────────────────────────────────────────────────

IR_REMOTE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Joebot Lab · IR Remote</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
/* base tokens come from lab.css */
:root{ --accent:#e05a1a; }
*{margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{min-height:100dvh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(224,90,26,.06),transparent);flex-shrink:0}
.brand{font-size:18px;font-weight:700;letter-spacing:.1em;color:var(--accent)}
nav a{color:var(--muted);text-decoration:none;font-size:13px;padding:5px 10px;
  border-radius:6px;border:1px solid transparent}
nav a:hover{color:var(--ink);border-color:var(--line)}
.spacer{flex:1}
.mode-toggle{display:flex;gap:6px}
.mode-btn{font-family:var(--mono);font-size:12px;padding:5px 14px;border-radius:6px;
  border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}
.mode-btn.active{background:rgba(224,90,26,.15);color:var(--accent);border-color:rgba(224,90,26,.4)}

/* ── layout ── */
.workspace{display:flex;flex:1;overflow:hidden;min-height:0}

/* ── remote body ── */
.remote-wrap{flex:0 0 auto;display:flex;align-items:flex-start;justify-content:center;
  padding:24px;overflow-y:auto}
.remote-body{background:#1a1a1a;border-radius:28px;padding:20px 16px 28px;
  width:220px;flex-shrink:0;
  box-shadow:0 8px 40px rgba(0,0,0,.7),inset 0 1px 0 rgba(255,255,255,.06)}
.remote-name{text-align:center;font-size:10px;letter-spacing:.15em;color:#888;
  text-transform:uppercase;margin-bottom:16px}
.remote-grid{display:grid;gap:5px}
.rbtn{border-radius:6px;border:none;cursor:pointer;font-family:var(--mono);
  font-size:10px;font-weight:600;letter-spacing:.04em;
  padding:0;height:34px;display:flex;align-items:center;justify-content:center;
  position:relative;transition:all .12s;user-select:none}
.rbtn:active{transform:scale(.93)}
.rbtn.learned{outline:2px solid var(--ok);outline-offset:1px}
.rbtn.sending{outline:2px solid var(--warn);outline-offset:1px}
.rbtn .dot{position:absolute;top:3px;right:3px;width:5px;height:5px;
  border-radius:50%;background:var(--ok)}

/* button groups */
.rbtn.g-power{background:#c0392b;color:#fff;font-size:14px;border-radius:50%;
  width:38px;height:38px;margin:0 auto}
.rbtn.g-fn{background:#2a2d35;color:#c8cdd8}
.rbtn.g-fn:hover{background:#363a45}
.rbtn.g-num{background:#2a2d35;color:#e8ebf0;font-size:12px}
.rbtn.g-num:hover{background:#363a45}
.rbtn.g-dpad{background:#3a3d46;color:#e8ebf0;font-size:13px}
.rbtn.g-dpad:hover{background:#454852}
.rbtn.g-dpad-ctr{background:#4a4d58;color:#e8ebf0;font-size:9px;font-weight:700;
  border-radius:50%;width:46px;height:46px;margin:0 auto}
.rbtn.g-dpad-ctr:hover{background:#555862}
.rbtn.g-nav-side{background:#232630;color:#9ca3af;font-size:9px}
.rbtn.g-nav-side:hover{background:#2e3140}
.rbtn.g-proc{background:#262932;color:#c8cdd8}
.rbtn.g-proc:hover{background:#31343e}
.rbtn.g-res{background:#1e2128;color:#a0a8b8;font-size:9px}
.rbtn.g-res:hover{background:#282c36;color:var(--ink)}
.rbtn.g-aux{background:#1a1d24;color:#7a8090;font-size:9px}
.rbtn.g-aux:hover{background:#232630;color:var(--muted)}

/* gap row */
.row-gap{height:10px}

/* ── sidebar ── */
.sidebar{flex:1;border-left:1px solid var(--line);overflow-y:auto;
  display:flex;flex-direction:column}
.sidebar-header{padding:16px 20px;border-bottom:1px solid var(--line);flex-shrink:0}
.sidebar-header h2{font-size:14px;font-weight:700;margin-bottom:2px}
.sidebar-header p{font-size:12px;color:var(--muted)}
.btn-list{padding:0 16px 16px;flex:1}
.btn-row{display:flex;align-items:center;gap:10px;padding:8px 0;
  border-bottom:1px solid rgba(255,255,255,.04)}
.btn-row:last-child{border-bottom:none}
.btn-row:hover{background:rgba(255,255,255,.02);margin:0 -16px;padding:8px 16px;border-radius:6px}
.btn-label{font-size:12px;font-weight:600;min-width:70px;color:var(--ink)}
.btn-slot{flex:1;display:flex;align-items:center;gap:8px}
.slot-badge{font-size:11px;padding:2px 8px;border-radius:4px;
  background:rgba(52,211,153,.1);color:var(--ok);border:1px solid rgba(52,211,153,.25)}
.slot-empty{font-size:11px;color:var(--gray);font-style:italic}
.edit-btn{font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:5px;
  border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer}
.edit-btn:hover{color:var(--ink);border-color:var(--muted)}

/* ── assign modal ── */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;
  align-items:center;justify-content:center;padding:20px}
.overlay.open{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;
  padding:24px;width:100%;max-width:420px}
.modal h3{font-size:15px;font-weight:700;margin-bottom:4px}
.modal .sub{font-size:12px;color:var(--muted);margin-bottom:20px}
.field{margin-bottom:14px}
.field label{display:block;font-size:11px;color:var(--muted);margin-bottom:5px;letter-spacing:.05em;text-transform:uppercase}
.field input{width:100%;background:var(--panel2);border:1px solid var(--line);
  color:var(--ink);border-radius:7px;padding:8px 10px;font-family:var(--mono);font-size:13px}
.field input:focus{outline:none;border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.btn-row-modal{display:flex;gap:8px;margin-top:6px;justify-content:flex-end}
.btn-prim{font-family:var(--mono);cursor:pointer;font-size:13px;font-weight:600;
  padding:8px 20px;border-radius:7px;border:none;background:var(--accent);color:#fff}
.btn-prim:hover{background:#f06a2e}
.btn-sec{font-family:var(--mono);cursor:pointer;font-size:13px;
  padding:8px 16px;border-radius:7px;background:transparent;color:var(--muted);border:1px solid var(--line)}
.btn-sec:hover{color:var(--ink);border-color:var(--muted)}
.btn-danger{font-family:var(--mono);cursor:pointer;font-size:12px;
  padding:6px 14px;border-radius:7px;background:rgba(255,84,112,.1);color:var(--bad);
  border:1px solid rgba(255,84,112,.3);margin-right:auto}
.btn-danger:hover{background:rgba(255,84,112,.2)}

/* toast */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:8px 18px;font-size:12px;opacity:0;transition:opacity .25s;
  pointer-events:none;z-index:300;white-space:nowrap}
.toast.show{opacity:1}

/* progress bar at top of sidebar */
.progress-strip{height:3px;background:var(--panel2);flex-shrink:0}
.progress-fill{height:100%;background:var(--ok);transition:width .5s}

@media(max-width:600px){
  .workspace{flex-direction:column}
  .sidebar{border-left:none;border-top:1px solid var(--line)}
  .remote-wrap{padding:16px 12px}
}
</style></head>
<body>
<header>
  <span class="brand">JOEBOT LAB</span>
  <nav>
    <a href="/">← Dashboard</a>
    <a href="/control/ir">IR Remotes</a>
  </nav>
  <div class="spacer"></div>
  <div class="mode-toggle">
    <button class="mode-btn active" id="btn-send" onclick="setMode('send')">Send</button>
    <button class="mode-btn" id="btn-assign" onclick="setMode('assign')">Assign</button>
  </div>
</header>

<div class="workspace">
  <!-- Remote visual -->
  <div class="remote-wrap">
    <div class="remote-body" id="remote-body">
      <div class="remote-name" id="remote-name">Loading…</div>
      <div class="remote-grid" id="remote-grid"></div>
    </div>
  </div>

  <!-- Sidebar list -->
  <div class="sidebar">
    <div class="progress-strip"><div class="progress-fill" id="progress-fill" style="width:0%"></div></div>
    <div class="sidebar-header">
      <h2 id="sidebar-title">Buttons</h2>
      <p id="sidebar-sub">Loading…</p>
    </div>
    <div class="btn-list" id="btn-list"></div>
  </div>
</div>

<!-- Assign modal -->
<div class="overlay" id="modal">
  <div class="modal">
    <h3 id="modal-title">Assign IR Code</h3>
    <p class="sub" id="modal-sub">Enter the IR slot number from the IPCP 505 IR learner.</p>
    <div class="row2">
      <div class="field">
        <label>IR Slot #</label>
        <input id="m-slot" type="number" min="1" max="255" placeholder="e.g. 1"/>
      </div>
      <div class="field">
        <label>IR Port (1–8)</label>
        <input id="m-port" type="number" min="1" max="8" value="1"/>
      </div>
    </div>
    <div class="btn-row-modal">
      <button class="btn-danger" id="m-clear" onclick="clearCurrent()">Clear</button>
      <button class="btn-sec" onclick="closeModal()">Cancel</button>
      <button class="btn-prim" onclick="saveAssign()">Save</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const remoteId = location.pathname.split('/').pop();
let remoteDef = null;
let mode = 'send';
let currentBtn = null;

// ── init ──────────────────────────────────────────────────────────────────
async function init(){
  const r = await fetch(`/api/ir/${remoteId}/definition`);
  if(!r.ok){ document.body.innerHTML='<p style="padding:40px;color:#ff5470">Remote not found.</p>'; return; }
  remoteDef = await r.json();

  // apply accent colour
  document.documentElement.style.setProperty('--accent', remoteDef.accent || '#e05a1a');

  document.title = `Joebot Lab · ${remoteDef.name}`;
  document.getElementById('remote-name').textContent = remoteDef.name;
  document.getElementById('sidebar-title').textContent = remoteDef.name;

  buildRemote();
  buildList();
  updateProgress();
}

// ── build visual remote ───────────────────────────────────────────────────
function buildRemote(){
  const grid = document.getElementById('remote-grid');
  const cols = remoteDef.cols;

  // Index buttons by [row][col]
  const byPos = {};
  remoteDef.buttons.forEach(b => {
    if(!byPos[b.row]) byPos[b.row] = {};
    byPos[b.row][b.col] = b;
  });

  // Find row gaps (missing rows = visual separator)
  const allRows = [...new Set(remoteDef.buttons.map(b=>b.row))].sort((a,b)=>a-b);

  grid.style.gridTemplateColumns = `repeat(${cols},1fr)`;
  grid.innerHTML = '';

  let prevRow = -1;
  allRows.forEach(row => {
    if(prevRow !== -1 && row - prevRow > 1){
      const gap = document.createElement('div');
      gap.className = 'row-gap';
      gap.style.gridColumn = `1 / span ${cols}`;
      grid.appendChild(gap);
    }
    prevRow = row;

    for(let col=0; col<cols; col++){
      const b = byPos[row]?.[col];
      const cell = document.createElement('div');
      if(!b){
        cell.style.cssText = 'height:34px';
        grid.appendChild(cell);
        continue;
      }
      cell.className = `rbtn g-${b.group}`;
      cell.dataset.id = b.id;
      cell.innerHTML = esc(b.label) + (b.ir_slot != null ? '<div class="dot"></div>' : '');
      cell.title = b.label + (b.ir_slot != null ? ` (slot ${b.ir_slot})` : ' — not learned');
      cell.addEventListener('click', ()=> onBtnClick(b));
      grid.appendChild(cell);
    }
  });
}

// ── build sidebar list ────────────────────────────────────────────────────
function buildList(){
  const list = document.getElementById('btn-list');
  list.innerHTML = remoteDef.buttons.map(b => {
    const learned = b.ir_slot != null;
    return `<div class="btn-row" id="row-${b.id}">
      <span class="btn-label">${esc(b.label)}</span>
      <span class="btn-slot">
        ${learned
          ? `<span class="slot-badge">port ${b.ir_port} · slot ${b.ir_slot}</span>`
          : `<span class="slot-empty">not learned</span>`}
      </span>
      ${mode==='assign'
        ? `<button class="edit-btn" onclick="openAssign('${b.id}')">Assign</button>`
        : (learned ? `<button class="edit-btn" onclick="sendBtn('${b.id}')">Send</button>` : '')}
    </div>`;
  }).join('');
}

function updateProgress(){
  const total = remoteDef.buttons.length;
  const learned = remoteDef.buttons.filter(b=>b.ir_slot!=null).length;
  const pct = total ? Math.round(100*learned/total) : 0;
  document.getElementById('progress-fill').style.width = pct+'%';
  document.getElementById('sidebar-sub').textContent =
    `${learned} of ${total} buttons learned (${pct}%)`;
}

// ── mode ──────────────────────────────────────────────────────────────────
function setMode(m){
  mode = m;
  document.getElementById('btn-send').classList.toggle('active', m==='send');
  document.getElementById('btn-assign').classList.toggle('active', m==='assign');
  buildList();
}

// ── button click ──────────────────────────────────────────────────────────
function onBtnClick(b){
  if(mode === 'assign'){ openAssign(b.id); return; }
  if(b.ir_slot == null){ toast('No IR code assigned — switch to Assign mode'); return; }
  sendBtn(b.id);
}

async function sendBtn(buttonId){
  const el = document.querySelector(`.rbtn[data-id="${buttonId}"]`);
  if(el){ el.classList.add('sending'); setTimeout(()=>el.classList.remove('sending'),600); }
  try{
    const r = await fetch(`/api/ir/${remoteId}/button/${buttonId}/send`,{method:'POST'});
    const j = await r.json();
    if(!j.ok) toast('Send failed: '+(j.error||'unknown'));
  }catch(e){ toast('Error: '+e.message); }
}

// ── assign modal ──────────────────────────────────────────────────────────
function openAssign(buttonId){
  currentBtn = remoteDef.buttons.find(b=>b.id===buttonId);
  if(!currentBtn) return;
  document.getElementById('modal-title').textContent = `Assign: ${currentBtn.label}`;
  document.getElementById('m-slot').value = currentBtn.ir_slot ?? '';
  document.getElementById('m-port').value = currentBtn.ir_port ?? 1;
  document.getElementById('m-clear').style.display = currentBtn.ir_slot != null ? '' : 'none';
  document.getElementById('modal').classList.add('open');
  setTimeout(()=>document.getElementById('m-slot').focus(), 80);
}

function closeModal(){ document.getElementById('modal').classList.remove('open'); currentBtn=null; }

async function saveAssign(){
  if(!currentBtn) return;
  const slot = parseInt(document.getElementById('m-slot').value);
  const port = parseInt(document.getElementById('m-port').value)||1;
  if(isNaN(slot)||slot<1){ toast('Enter a valid slot number'); return; }
  const r = await fetch(`/api/ir/${remoteId}/button/${currentBtn.id}/assign`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ir_slot:slot, ir_port:port, label:currentBtn.label})
  });
  const j = await r.json();
  if(!j.ok){ toast('Save failed: '+(j.error||'unknown')); return; }
  currentBtn.ir_slot = slot;
  currentBtn.ir_port = port;
  closeModal();
  buildRemote();
  buildList();
  updateProgress();
  toast(`✓ ${currentBtn.label} → port ${port} slot ${slot}`);
}

async function clearCurrent(){
  if(!currentBtn) return;
  const r = await fetch(`/api/ir/${remoteId}/button/${currentBtn.id}/assign`,{
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ir_slot:null})
  });
  const j = await r.json();
  if(!j.ok){ toast('Clear failed'); return; }
  currentBtn.ir_slot = null;
  closeModal();
  buildRemote();
  buildList();
  updateProgress();
  toast('Cleared');
}

// close modal on overlay click
document.getElementById('modal').addEventListener('click', e=>{
  if(e.target === document.getElementById('modal')) closeModal();
});

// ── toast ──────────────────────────────────────────────────────────────────
let _tt;
function toast(msg,dur=2400){
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(_tt); _tt=setTimeout(()=>el.classList.remove('show'),dur);
}

function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

init();
</script>
</body></html>"""
