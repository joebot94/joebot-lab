from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
import config_store
import sis
from shared import log

router = APIRouter()

def _ipcp505_device():
    for d in config_store.get_devices():
        if d.get("kind") == "ipcp505":
            return d
    return None


def _ipcp505_send(ip, tcp_port, command_bytes, timeout=4.0, read_time=0.8):
    """Open a fresh TCP session to IPCP 505, drain banner, send bytes, return (reply, err)."""
    import socket as _s
    try:
        sock = _s.create_connection((ip, tcp_port), timeout=timeout)
    except (OSError, _s.timeout) as e:
        return "", str(e)
    try:
        sis._read(sock, 1.5)
        sock.sendall(command_bytes)
        resp = sis._read(sock, read_time, idle=0.2).strip()
        return resp, None
    except OSError as e:
        return "", str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


VTG400_TCP_PORT = 2008   # IPCP aux port for COM8 — direct serial passthrough, no ESC framing


def _vtg400_query_all(ip, timeout=8.0):
    """
    Query VTG 400 state via IPCP TCP aux port 2008 (direct COM8 passthrough).
    Returns (results_dict, error_str).
    """
    import socket as _s
    try:
        sock = _s.create_connection((ip, VTG400_TCP_PORT), timeout=timeout)
    except (OSError, _s.timeout) as e:
        return {}, str(e)
    try:
        results = {}
        for key, vtg_cmd in [("model", "N"), ("pattern", "J"),
                              ("resolution", "="), ("temp", "20S"),
                              ("ire", "15#"), ("color", "10#")]:
            sock.sendall(vtg_cmd.encode("ascii") + b"\r")
            results[key] = sis._read(sock, 1.2, idle=0.3).strip()
        return results, None
    except OSError as e:
        return {}, str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _vtg400_send(ip, vtg_cmd, timeout=5.0, read_time=1.5):
    """
    Send a VTG 400 command via IPCP HTTP serial passthrough.
    URL format: http://{ip}/?cmd=W08RS|{cmd}
    This avoids the IPCP intercepting '*' characters on TCP port 2008.
    Returns (reply, err).
    """
    import urllib.request as _ur
    import urllib.error as _ue
    import urllib.parse as _up
    url = f"http://{ip}/?cmd=W08RS%7C{_up.quote(vtg_cmd, safe='')}"
    try:
        with _ur.urlopen(url, timeout=timeout) as resp:
            _ = resp.read()   # fire-and-forget; IPCP returns its frameset HTML
        return "ok", None
    except _ue.URLError as e:
        return "", str(e)


def _usp405_send(ip: str, com_port: int, usp_cmd: str, timeout: float = 5.0):
    """Send USP 405 command via IPCP HTTP serial passthrough (handles * and other
    special characters by URL-encoding, same technique as VTG 400)."""
    import urllib.request as _ur, urllib.error as _ue, urllib.parse as _up
    url = f"http://{ip}/?cmd=W{com_port:02d}RS%7C{_up.quote(usp_cmd, safe='')}"
    try:
        with _ur.urlopen(url, timeout=timeout) as r:
            r.read()
        return "ok", None
    except _ue.URLError as e:
        return "", str(e)


_USP405_FAST_QUERIES = [
    # Runs every poll — everything visible in status bar + Signal/Picture/Output tabs
    ("info",      "I"),   ("outrate",   "="),   ("in2type",   "\\"),
    ("brightness","Y"),   ("contrast",  "^"),   ("color",     "C"),
    ("tint",      "T"),   ("freeze",    "F"),   ("testpat",   "J"),
]
_USP405_SLOW_QUERIES = [
    # Runs every 4th poll — advanced settings that rarely change
    ("firmware",   "Q"),   ("model",     "N"),   ("exemode",   "X"),
    ("hdetail",    "D"),   ("vdetail",   "d"),
    ("top_blank",  "("),   ("bot_blank", ")"),
    ("enc_filter", "10#"), ("blue_scr",  "8#"),  ("edge_smth", "16#"),
    ("enhanced",   "12#"), ("pal_film",  "18#"), ("out_sig",   "6#"),
    ("polarity",   "7#"),  ("rgb_delay", "3#"),
]
_usp405_slow_counter: dict[int, int] = {}   # com_port → poll count


def _usp405_query_all(ip: str, com_port: int, timeout: float = 12.0):
    """Query USP 405 state via TCP direct.
    Fast queries run every call (~9 × 0.2s ≈ 2s).
    Slow queries (advanced settings) run every 4th call to cut idle time.
    """
    import socket as _s
    _usp405_slow_counter[com_port] = (_usp405_slow_counter.get(com_port, 0) + 1)
    run_slow = (_usp405_slow_counter[com_port] % 4) == 1  # 1st, 5th, 9th …
    queries = _USP405_FAST_QUERIES + (_USP405_SLOW_QUERIES if run_slow else [])

    tcp_port = 2000 + com_port
    try:
        sock = _s.create_connection((ip, tcp_port), timeout=timeout)
    except (OSError, _s.timeout) as e:
        return {}, str(e)
    results = {}
    try:
        for key, cmd in queries:
            sock.sendall((cmd + "\r").encode("ascii"))
            results[key] = sis._read(sock, 1.0, idle=0.2).strip()
    except OSError as e:
        results["_error"] = str(e)
    finally:
        try: sock.close()
        except OSError: pass
    return results, None



IPCP505_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>IPCP Pro 505 · Control</title>
<style>
:root{
  --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
  --ink:#e8ebf0;--muted:#8b93a3;--faint:#1f232d;
  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--accent:#f5b942;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--mono);
  min-height:100dvh;padding-bottom:60px}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(245,185,66,.06),transparent)}
.brand{font-size:18px;font-weight:800;letter-spacing:.08em;color:var(--accent)}
.hdr-sub{font-size:12px;color:var(--muted)}
.spacer{flex:1}
.back-btn{font-family:var(--mono);font-size:12px;color:var(--muted);
  background:none;border:1px solid var(--line);border-radius:6px;
  padding:5px 12px;text-decoration:none;transition:all .15s}
.back-btn:hover{color:var(--ink);border-color:var(--muted)}
main{max-width:960px;margin:0 auto;padding:20px 16px;
  display:flex;flex-direction:column;gap:20px}

/* status bar */
.status-bar{display:flex;align-items:center;gap:10px;padding:8px 16px;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  font-size:12px;color:var(--muted)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex-shrink:0}
.sdot.ok{background:var(--ok)}
.sdot.bad{background:var(--bad)}
.stat-pill{background:var(--panel2);border:1px solid var(--line);border-radius:5px;
  padding:2px 10px;font-size:11.5px;color:var(--ink)}

/* section */
.sec{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  overflow:hidden}
.sec-head{display:flex;align-items:center;gap:10px;padding:12px 18px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(90deg,rgba(245,185,66,.04),transparent)}
.sec-title{font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--accent);font-weight:700}
.sec-count{font-size:11px;color:var(--muted)}
.sec-body{padding:14px}

/* relay grid — 4 across, 2 rows */
.relay-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.relay-card{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:12px 10px;text-align:center}
.relay-lbl{font-size:10px;color:var(--muted);letter-spacing:.1em;margin-bottom:6px}
.relay-name{font-size:12px;font-weight:600;color:var(--ink);margin-bottom:10px;
  min-height:1.3em;line-height:1.3}
.relay-btn{font-family:var(--mono);font-size:11.5px;font-weight:700;
  width:100%;padding:7px 0;border-radius:6px;cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--muted);
  transition:all .15s}
.relay-btn.closed{background:rgba(52,211,153,.13);color:var(--ok);
  border-color:rgba(52,211,153,.35);box-shadow:inset 0 0 0 1px rgba(52,211,153,.15)}
.relay-btn:hover:not(:disabled){border-color:var(--muted);color:var(--ink)}
.relay-btn.closed:hover:not(:disabled){background:rgba(52,211,153,.22)}
.relay-btn:disabled{opacity:.5;cursor:default}

/* 12V grid — 4 across in one row */
.pwr-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.pwr-card{background:var(--panel2);border:1px solid var(--line);border-radius:9px;
  padding:12px 10px;text-align:center}
.pwr-lbl{font-size:10px;color:var(--muted);letter-spacing:.1em;margin-bottom:6px}
.pwr-name{font-size:12px;font-weight:600;color:var(--ink);margin-bottom:10px;
  min-height:1.3em;line-height:1.3}
.pwr-btns{display:flex;gap:5px}
.pwr-on{font-family:var(--mono);font-size:11.5px;font-weight:700;flex:1;padding:7px 0;
  border-radius:6px;cursor:pointer;border:1px solid rgba(52,211,153,.3);
  background:rgba(52,211,153,.06);color:var(--ok);transition:all .15s}
.pwr-on:hover{background:rgba(52,211,153,.16)}
.pwr-on.active{background:rgba(52,211,153,.18);border-color:var(--ok);
  box-shadow:inset 0 0 0 1px rgba(52,211,153,.2)}
.pwr-off{font-family:var(--mono);font-size:11.5px;font-weight:700;flex:1;padding:7px 0;
  border-radius:6px;cursor:pointer;border:1px solid rgba(255,84,112,.25);
  background:rgba(255,84,112,.04);color:var(--bad);transition:all .15s}
.pwr-off:hover{background:rgba(255,84,112,.14)}
.pwr-off.active{background:rgba(255,84,112,.14);border-color:var(--bad)}

/* serial grid */
.serial-grid{display:flex;flex-direction:column;gap:6px}
.serial-card{background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:11px 14px;display:flex;align-items:center;gap:12px}
.serial-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;background:var(--faint)}
.serial-dot.live{background:var(--ok);box-shadow:0 0 6px rgba(52,211,153,.5)}
.serial-dot.warn{background:var(--accent);box-shadow:0 0 6px rgba(124,106,245,.5)}
.serial-dot.mismatch{background:var(--bad);box-shadow:0 0 6px rgba(255,84,112,.5)}
.serial-port{font-size:11px;color:var(--muted);letter-spacing:.06em;
  width:42px;flex-shrink:0}
.serial-info{flex:1}
.serial-name{font-size:13px;font-weight:600;color:var(--ink)}
.serial-kind{font-size:10.5px;color:var(--muted)}
.serial-btn{font-family:var(--mono);font-size:11.5px;padding:5px 14px;
  border-radius:6px;border:1px solid var(--line);background:none;
  color:var(--muted);text-decoration:none;transition:all .15s;white-space:nowrap}
.serial-btn:hover{color:var(--ink);border-color:var(--muted)}
.serial-btn.live{border-color:rgba(245,185,66,.4);color:var(--accent)}
.serial-btn.live:hover{background:rgba(245,185,66,.07)}
.serial-btn.soon{opacity:.35;cursor:default;pointer-events:none}
.serial-btn.mismatch{border-color:rgba(255,84,112,.4);color:var(--bad);cursor:default;pointer-events:none}
.serial-btn.found{border-color:rgba(124,106,245,.4);color:var(--accent)}
.serial-btn.found:hover{background:rgba(124,106,245,.07)}
.serial-card.mismatch-card{border-color:rgba(255,84,112,.25);background:rgba(255,84,112,.04)}

.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:8px 18px;font-size:12px;opacity:0;transition:opacity .25s;
  pointer-events:none;z-index:100;white-space:nowrap}
.toast.show{opacity:1}
@media(max-width:600px){
  .relay-grid{grid-template-columns:repeat(2,1fr)}
  .pwr-grid{grid-template-columns:repeat(2,1fr)}
}
</style></head>
<body>
<header>
  <span class="brand">IPCP PRO 505</span>
  <span class="hdr-sub">Control Hub</span>
  <span class="spacer"></span>
  <a href="/" class="back-btn">← Dashboard</a>
</header>
<main>
  <!-- status -->
  <div class="status-bar">
    <div class="sdot" id="sdot"></div>
    <span id="stext">Connecting…</span>
    <span class="spacer"></span>
    <span class="stat-pill" id="relay-summary">— relays</span>
    <span class="stat-pill" id="pwr-summary">— 12V</span>
  </div>

  <!-- RELAYS -->
  <div class="sec">
    <div class="sec-head">
      <span class="sec-title">Relays</span>
      <span class="sec-count" id="relay-count"></span>
    </div>
    <div class="sec-body">
      <div class="relay-grid" id="relay-grid"></div>
    </div>
  </div>

  <!-- 12V POWER -->
  <div class="sec">
    <div class="sec-head">
      <span class="sec-title">12V Power Ports</span>
      <span class="sec-count" id="pwr-count"></span>
    </div>
    <div class="sec-body">
      <div class="pwr-grid" id="pwr-grid"></div>
    </div>
  </div>

  <!-- SERIAL DEVICES -->
  <div class="sec">
    <div class="sec-head">
      <span class="sec-title">Serial Devices</span>
      <span class="sec-count">8 COM ports</span>
    </div>
    <div class="sec-body">
      <div class="serial-grid" id="serial-grid"></div>
    </div>
  </div>
</main>
<div class="toast" id="toast"></div>

<script>
const RELAY_NAMES={
  1:'Relay 1',2:'Relay 2',3:'Relay 3',4:'Relay 4',
  5:'Relay 5',6:'Relay 6',7:'Relay 7',8:'Relay 8'
};
const PWR_NAMES={1:'12V Port 1',2:'12V Port 2',3:'12V Port 3',4:'12V Port 4'};
const SERIAL_PORTS=[
  {port:1,name:'VSC 700D #1',kind:'vsc700',page:null},
  {port:2,name:'VSC 700D #2',kind:'vsc700',page:null},
  {port:3,name:'VSC 700D #3',kind:'vsc700',page:null},
  {port:4,name:'VSC 700D #4',kind:'vsc700',page:null},
  {port:5,name:'USP 405 #1', kind:'usp405',page:'/control/ipcp505/usp405?port=5'},
  {port:6,name:'USP 405 #2', kind:'usp405',page:'/control/ipcp505/usp405?port=6'},
  {port:7,name:'VSC 900D',   kind:'vsc900',page:null},
  {port:8,name:'VTG 400',    kind:'vtg400',page:'/control/ipcp505/vtg400'},
];

let relayState={}, pwrState={};
let _tt;
function toast(msg,dur=2400){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  clearTimeout(_tt);_tt=setTimeout(()=>el.classList.remove('show'),dur);
}

// ── relays ───────────────────────────────────────────────────────────────────
function buildRelayGrid(){
  const g=document.getElementById('relay-grid');g.innerHTML='';
  for(let i=1;i<=8;i++){
    const c=relayState[i]||false;
    const d=document.createElement('div');d.className='relay-card';
    d.innerHTML=`<div class="relay-lbl">RELAY ${i}</div>
      <div class="relay-name">${RELAY_NAMES[i]}</div>
      <button class="relay-btn${c?' closed':''}" id="rly-${i}"
        onclick="toggleRelay(${i})">${c?'● CLOSED':'○ OPEN'}</button>`;
    g.appendChild(d);
  }
  const n=Object.values(relayState).filter(Boolean).length;
  document.getElementById('relay-summary').textContent=`${n}/8 relays closed`;
  document.getElementById('relay-count').textContent=`${n} closed`;
}

async function toggleRelay(n){
  const newState=(relayState[n]||false)?0:1;
  const btn=document.getElementById('rly-'+n);
  btn.disabled=true;btn.textContent='…';
  try{
    const r=await fetch(`/api/control/ipcp505/relay/${n}/set`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({state:newState})});
    const j=await r.json();
    if(j.ok){relayState[n]=newState===1;toast(`Relay ${n} ${newState?'closed':'opened'}`);}
    else toast('Error: '+(j.error||'unknown'));
  }catch(e){toast('Network error');}
  buildRelayGrid();
}

// ── 12V power ────────────────────────────────────────────────────────────────
function buildPwrGrid(){
  const g=document.getElementById('pwr-grid');g.innerHTML='';
  for(let i=1;i<=4;i++){
    const on=pwrState[i]||false;
    const d=document.createElement('div');d.className='pwr-card';
    d.innerHTML=`<div class="pwr-lbl">PORT ${i}</div>
      <div class="pwr-name">${PWR_NAMES[i]}</div>
      <div class="pwr-btns">
        <button class="pwr-on${on?' active':''}" onclick="setPwr(${i},1)">ON</button>
        <button class="pwr-off${!on?' active':''}" onclick="setPwr(${i},0)">OFF</button>
      </div>`;
    g.appendChild(d);
  }
  const n=Object.values(pwrState).filter(Boolean).length;
  document.getElementById('pwr-summary').textContent=`${n}/4 12V on`;
  document.getElementById('pwr-count').textContent=`${n} on`;
}

async function setPwr(n,state){
  try{
    const r=await fetch(`/api/control/ipcp505/power/${n}/set`,{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({state})});
    const j=await r.json();
    if(j.ok){pwrState[n]=state===1;toast(`12V port ${n} ${state?'on':'off'}`);}
    else toast('Error: '+(j.error||'unknown'));
  }catch(e){toast('Network error');}
  buildPwrGrid();
}

// ── COM port probe state ──────────────────────────────────────────────────────
// probeState[port] = {model, device} from /api/control/ipcp505/com/scan
let probeState={};

// Known configured kind → expected part number prefixes
const KIND_MODELS={
  vtg400:['60-564-01','60-564-02','60-564-03'],
  usp405:['60-369-01','60-369-02','60-369-03','60-369-04'],
  // add vsc700, vsc900 here as we build their pages
};

function serialDotClass(p){
  const probe=probeState[p.port];
  if(!probe||probe.model===null) return '';          // no response — gray
  if(!p.page){
    // unconfigured port — something is there, highlight amber
    return probe.model?'warn':'';
  }
  // configured — check if model matches expected kind
  const expected=KIND_MODELS[p.kind]||[];
  if(expected.length===0) return 'live';             // kind not yet in map, trust it
  const match=expected.some(pn=>probe.model.includes(pn));
  return match?'live':'mismatch';
}

// ── serial devices ────────────────────────────────────────────────────────────
function buildSerialGrid(){
  const g=document.getElementById('serial-grid');g.innerHTML='';
  SERIAL_PORTS.forEach(p=>{
    const probe=probeState[p.port];
    const dotCls=serialDotClass(p);
    const isMismatch=dotCls==='mismatch';
    const isUnknown=dotCls==='warn';

    let rightSide='';
    if(isMismatch){
      rightSide=`<span class="serial-btn mismatch" title="Wrong device: ${probe?.model||'?'}">⚠ Wrong device</span>`;
    } else if(p.page){
      // Show link only if probe hasn't run yet OR probe confirmed a device is there
      const probeRan = probeState[p.port] !== undefined;
      if(probeRan && !probe?.model){
        rightSide=`<a href="${p.page}" class="serial-btn soon" style="opacity:.4">No response</a>`;
      } else {
        rightSide=`<a href="${p.page}" class="serial-btn live">Control →</a>`;
      }
    } else if(isUnknown&&probe?.model){
      if(probe?.device?.page){
        rightSide=`<a href="${probe.device.page}" class="serial-btn found">${probe.device.name} →</a>`;
      } else {
        const devName=probe?.device?.name||probe.model;
        rightSide=`<span class="serial-btn found" style="cursor:default">${devName} detected</span>`;
      }
    } else {
      rightSide=`<span class="serial-btn soon">Soon™</span>`;
    }

    const d=document.createElement('div');
    d.className='serial-card'+(isMismatch?' mismatch-card':'');
    d.innerHTML=`
      <div class="serial-dot ${dotCls}"></div>
      <div class="serial-port">COM${p.port}</div>
      <div class="serial-info">
        <div class="serial-name">${p.name}</div>
        <div class="serial-kind">${probe?.model?probe.model:p.kind}</div>
      </div>
      ${rightSide}`;
    g.appendChild(d);

    // Unconfigured port has something — toast once
    if(isUnknown&&probe?.model&&!p._notified){
      p._notified=true;
      const devName=probe?.device?.name||probe.model;
      toast(`COM${p.port}: ${devName} detected`,4000);
    }
  });
}

// ── COM port scan ─────────────────────────────────────────────────────────────
async function probeCOMPorts(){
  try{
    const r=await fetch('/api/control/ipcp505/com/scan');
    const j=await r.json();
    if(j.error) return;
    // j is {1:{model,device}, 2:…, …}
    Object.entries(j).forEach(([port,info])=>{
      probeState[parseInt(port)]=info;
    });
    buildSerialGrid();
  }catch(e){}
}

// ── poll ─────────────────────────────────────────────────────────────────────
async function pollState(){
  try{
    const r=await fetch('/api/control/ipcp505/state');
    const j=await r.json();
    const dot=document.getElementById('sdot');
    const stxt=document.getElementById('stext');
    if(j.error&&!j.online){
      dot.className='sdot bad';stxt.textContent='Offline — '+j.error;
    }else{
      dot.className='sdot ok';stxt.textContent='Online';
    }
    (j.signals||[]).forEach((s,i)=>{relayState[i+1]=s.state==='ok';});
    (j.rail_dots||[]).forEach((rd,i)=>{pwrState[i+1]=rd.state==='ok';});
    buildRelayGrid();
    buildPwrGrid();
  }catch(e){document.getElementById('stext').textContent='Poll error';}
}

buildSerialGrid();
buildRelayGrid();
buildPwrGrid();
pollState();
probeCOMPorts();
setInterval(pollState,15000);
setInterval(probeCOMPorts,12000);
</script>
</body></html>"""


# =========================================================================== #
# VTG 400 Control  (/control/ipcp505/vtg400)
# =========================================================================== #

VTG400_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>VTG 400 · Control</title>
<style>
:root{
  --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
  --ink:#e8ebf0;--muted:#8b93a3;--faint:#1f232d;
  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--accent:#f5b942;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--mono);
  min-height:100dvh;padding-bottom:60px}
header{display:flex;align-items:center;gap:14px;padding:14px 20px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(245,185,66,.06),transparent)}
.brand{font-size:18px;font-weight:800;letter-spacing:.08em;color:var(--accent)}
.hdr-sub{font-size:12px;color:var(--muted)}
.spacer{flex:1}
.back-btn{font-family:var(--mono);font-size:12px;color:var(--muted);
  background:none;border:1px solid var(--line);border-radius:6px;
  padding:5px 12px;cursor:pointer;text-decoration:none}
.back-btn:hover{color:var(--ink);border-color:var(--muted)}
main{max-width:760px;margin:0 auto;padding:20px 16px;display:flex;flex-direction:column;gap:16px}

.status-bar{display:flex;align-items:center;gap:10px;padding:8px 14px;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;
  font-size:12px;color:var(--muted)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex-shrink:0}
.sdot.ok{background:var(--ok)}
.sdot.bad{background:var(--bad)}

/* section card */
.sec{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.sec-title{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);margin-bottom:12px}

/* power row */
.pwr-row{display:flex;gap:10px}
.btn-on{font-family:var(--mono);font-size:13px;font-weight:700;flex:1;padding:11px 0;
  border-radius:8px;cursor:pointer;border:1px solid rgba(52,211,153,.35);
  background:rgba(52,211,153,.1);color:var(--ok);transition:all .15s}
.btn-on:hover{background:rgba(52,211,153,.22)}
.btn-off{font-family:var(--mono);font-size:13px;font-weight:700;flex:1;padding:11px 0;
  border-radius:8px;cursor:pointer;border:1px solid rgba(255,84,112,.35);
  background:rgba(255,84,112,.08);color:var(--bad);transition:all .15s}
.btn-off:hover{background:rgba(255,84,112,.18)}

/* pattern/resolution grids */
.btn-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.btn-grid.wide{grid-template-columns:repeat(4,1fr)}
.grid-btn{font-family:var(--mono);font-size:12px;padding:9px 6px;
  border-radius:7px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--muted);transition:all .15s;text-align:center}
.grid-btn:hover{color:var(--ink);border-color:var(--muted)}
.grid-btn.active{background:rgba(245,185,66,.12);color:var(--accent);
  border-color:rgba(245,185,66,.45)}

/* IRE row */
.ire-row{display:flex;flex-wrap:wrap;gap:6px}
.ire-btn{font-family:var(--mono);font-size:11.5px;padding:7px 10px;
  border-radius:6px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--muted);transition:all .15s;min-width:44px;text-align:center}
.ire-btn:hover{color:var(--ink);border-color:var(--muted)}
.ire-btn.active{background:rgba(245,185,66,.12);color:var(--accent);
  border-color:rgba(245,185,66,.45)}

/* color grid */
.color-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}
.color-btn{font-family:var(--mono);font-size:12px;font-weight:600;
  padding:10px 4px;border-radius:7px;cursor:pointer;
  border:2px solid transparent;transition:all .15s;text-align:center}
.color-btn.active{border-color:#fff !important;box-shadow:0 0 12px rgba(255,255,255,.25)}
.cb-black{background:#111;color:#aaa}
.cb-red{background:#c00;color:#fff}
.cb-green{background:#0a0;color:#fff}
.cb-blue{background:#00a;color:#fff}
.cb-white{background:#eee;color:#111}
.cb-magenta{background:#a0a;color:#fff}
.cb-yellow{background:#880;color:#111}
.cb-cyan{background:#088;color:#fff}

/* info strip */
.info-strip{display:flex;gap:20px;flex-wrap:wrap}
.info-item{display:flex;flex-direction:column;gap:2px}
.info-lbl{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.info-val{font-size:14px;font-weight:600;color:var(--ink)}

.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;
  padding:8px 18px;font-size:12px;opacity:0;transition:opacity .25s;
  pointer-events:none;z-index:100;white-space:nowrap}
.toast.show{opacity:1}
@media(max-width:520px){
  .btn-grid.wide{grid-template-columns:repeat(2,1fr)}
  .color-grid{grid-template-columns:repeat(4,1fr)}
}
</style></head>
<body>
<header>
  <span class="brand">VTG 400</span>
  <span class="hdr-sub">Test Pattern Generator · COM8</span>
  <span class="spacer"></span>
  <a href="/control/ipcp505" class="back-btn">← IPCP Hub</a>
</header>
<main>
  <!-- status -->
  <div class="status-bar">
    <div class="sdot" id="sdot"></div>
    <span id="stext">Connecting…</span>
    <span class="spacer"></span>
    <span id="model-txt" style="color:var(--ink)"></span>
    <span style="color:var(--line);padding:0 8px">|</span>
    <span id="temp-txt"></span>
  </div>

  <!-- power -->
  <div class="sec">
    <div class="sec-title">Power</div>
    <div class="pwr-row">
      <button class="btn-on" onclick="cmd('1P')">⏻ Power On</button>
      <button class="btn-off" onclick="cmd('0P')">⏻ Power Off</button>
    </div>
  </div>

  <!-- IRE -->
  <div class="sec" id="sec-ire">
    <div class="sec-title">IRE Level</div>
    <div class="ire-row" id="ire-row"></div>
  </div>

  <!-- patterns -->
  <div class="sec">
    <div class="sec-title">Test Patterns</div>
    <div class="btn-grid" id="pat-grid"></div>
  </div>

  <!-- colors -->
  <div class="sec" id="sec-color">
    <div class="sec-title">Color Field</div>
    <div class="color-grid" id="color-grid"></div>
  </div>

  <!-- resolution -->
  <div class="sec">
    <div class="sec-title">Resolution / Format</div>
    <div class="btn-grid wide" id="res-grid"></div>
  </div>
</main>
<div class="toast" id="toast"></div>

<script>
const PATTERNS=[
  {num:15,name:'Window 20'},{num:14,name:'Window 80'},{num:16,name:'Var IRE'},
  {num:6,name:'4×4 Cross'},{num:7,name:'Coarse'},{num:8,name:'Fine Cross'},
  {num:13,name:'Color Bar'},{num:17,name:'Full Screen'},{num:9,name:'PLUGE'},
];
const PATTERN_MAP={};
PATTERNS.forEach(p=>PATTERN_MAP[p.num]=p.name);

const COLORS=[
  {name:'Black',cmd:'0*10#',cls:'cb-black'},
  {name:'Red',  cmd:'4*10#',cls:'cb-red'},
  {name:'Green',cmd:'2*10#',cls:'cb-green'},
  {name:'Blue', cmd:'1*10#',cls:'cb-blue'},
  {name:'White',cmd:'7*10#',cls:'cb-white'},
  {name:'Magenta',cmd:'5*10#',cls:'cb-magenta'},
  {name:'Yellow',cmd:'6*10#',cls:'cb-yellow'},
  {name:'Cyan', cmd:'3*10#',cls:'cb-cyan'},
];

const RESOLUTIONS=[
  {lbl:'240p', cmd:'001*99='},
  {lbl:'NTSC/U',cmd:'001*07='},
  {lbl:'NTSC/J',cmd:'002*07='},
  {lbl:'PAL',  cmd:'003*07='},
  {lbl:'480p', cmd:'001*06='},
  {lbl:'576p', cmd:'002*06='},
  {lbl:'720p', cmd:'004*06='},
  {lbl:'1080i',cmd:'010*06='},
];
const RES_MAP={};
RESOLUTIONS.forEach(r=>RES_MAP[r.cmd.replace(/=$/,'')]=r.lbl);

let activeIre=null, activePat=null, activeColor=null, activeRes=null;
let _tt;
function toast(msg,dur=2400){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  clearTimeout(_tt);_tt=setTimeout(()=>el.classList.remove('show'),dur);
}

async function cmd(vtgCmd, label=''){
  try{
    const r=await fetch('/api/control/ipcp505/vtg400/cmd',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd:vtgCmd})});
    const j=await r.json();
    if(!j.ok) toast('Error: '+(j.error||'failed'));
    else if(label) toast(label);
  }catch(e){toast('Network error');}
}

// ── IRE ──────────────────────────────────────────────────────────────────────
function buildIre(){
  const row=document.getElementById('ire-row');
  row.innerHTML='';
  for(let v=0;v<=100;v+=10){
    const b=document.createElement('button');
    b.className='ire-btn'+(activeIre===v?' active':'');
    b.textContent=v;
    b.onclick=()=>{ activeIre=v; buildIre(); cmd(`${v}*15#`,`IRE ${v}`); };
    row.appendChild(b);
  }
}

// ── Patterns ─────────────────────────────────────────────────────────────────
function buildPatterns(){
  const g=document.getElementById('pat-grid');
  g.innerHTML='';
  PATTERNS.forEach(p=>{
    const b=document.createElement('button');
    b.className='grid-btn'+(activePat===p.num?' active':'');
    b.textContent=p.name;
    b.onclick=()=>{ activePat=p.num; buildPatterns(); cmd(`${p.num}J`,p.name); };
    g.appendChild(b);
  });
}

// ── Colors ───────────────────────────────────────────────────────────────────
function buildColors(){
  const g=document.getElementById('color-grid');
  g.innerHTML='';
  COLORS.forEach(c=>{
    const b=document.createElement('button');
    b.className=`color-btn ${c.cls}`+(activeColor===c.name?' active':'');
    b.textContent=c.name;
    b.onclick=()=>{ activeColor=c.name; buildColors(); cmd(c.cmd,c.name); };
    g.appendChild(b);
  });
}

// ── Resolution ───────────────────────────────────────────────────────────────
function buildRes(){
  const g=document.getElementById('res-grid');
  g.innerHTML='';
  RESOLUTIONS.forEach(r=>{
    const b=document.createElement('button');
    b.className='grid-btn'+(activeRes===r.lbl?' active':'');
    b.textContent=r.lbl;
    b.onclick=()=>{ activeRes=r.lbl; buildRes(); cmd(r.cmd,r.lbl); };
    g.appendChild(b);
  });
}

// ── Wrong-device overlay ──────────────────────────────────────────────────────
const VTG_MODELS={'60-564-01':'VTG 400','60-564-02':'VTG 400D','60-564-03':'VTG 400DVI'};
function isVtgModel(raw){ return Object.keys(VTG_MODELS).some(k=>raw.includes(k)); }

function showWrongDevice(foundRaw){
  let overlay=document.getElementById('wrong-overlay');
  if(!overlay){
    overlay=document.createElement('div');
    overlay.id='wrong-overlay';
    overlay.style.cssText=`position:fixed;inset:0;background:rgba(8,10,14,.92);
      display:flex;flex-direction:column;align-items:center;justify-content:center;
      z-index:100;gap:12px;font-family:var(--mono)`;
    document.body.appendChild(overlay);
  }
  const label=foundRaw&&foundRaw.length>0&&foundRaw!=='E10'
    ?`Found: <span style="color:var(--warn)">${foundRaw}</span>`
    :`<span style="color:var(--muted)">No response from COM8</span>`;
  overlay.innerHTML=`
    <div style="font-size:28px">⚠️</div>
    <div style="font-size:16px;font-weight:700;color:var(--bad)">Wrong device on COM8</div>
    <div style="font-size:13px;color:var(--muted)">Expected: <span style="color:var(--ink)">VTG 400 / 400D / 400DVI</span></div>
    <div style="font-size:13px">${label}</div>
    <div style="font-size:11px;color:var(--muted);margin-top:8px">Polling every 10s — plug in a VTG 400 to continue</div>`;
}

function hideWrongDevice(){
  const o=document.getElementById('wrong-overlay');
  if(o) o.remove();
}

// ── State poll ────────────────────────────────────────────────────────────────
async function pollState(){
  const dot=document.getElementById('sdot');
  const stxt=document.getElementById('stext');
  try{
    const r=await fetch('/api/control/ipcp505/vtg400/state');
    const j=await r.json();
    if(j.error){
      dot.className='sdot bad';stxt.textContent='Serial error — '+j.error;
      showWrongDevice('');return;
    }

    // model check — if not a VTG 400, show overlay and keep polling
    const modelRaw=(j.model||'').trim();
    if(!isVtgModel(modelRaw)){
      dot.className='sdot bad';stxt.textContent='Wrong device';
      showWrongDevice(modelRaw);return;
    }

    hideWrongDevice();
    dot.className='sdot ok';stxt.textContent='Online';

    const isDVI=modelRaw.includes('60-564-03');
    const modelName=VTG_MODELS[Object.keys(VTG_MODELS).find(k=>modelRaw.includes(k))]||modelRaw;
    document.getElementById('model-txt').textContent=modelName;

    // temp
    const tm=(j.temp||'').match(/([+-]?\d+\.?\d*)F/);
    document.getElementById('temp-txt').textContent=tm?`${parseFloat(tm[1]).toFixed(0)}°F`:'';

    // IRE
    if(j.ire&&/^\d+$/.test(j.ire.trim())){
      const v=parseInt(j.ire.trim());
      if(v>=0&&v<=100){ activeIre=Math.round(v/10)*10; buildIre(); }
    }
    // Pattern
    if(j.pattern&&/^\d+$/.test(j.pattern.trim())){
      const pn=parseInt(j.pattern.trim());
      if(PATTERN_MAP[pn]){ activePat=pn; buildPatterns(); }
    }
    // Resolution
    if(j.resolution){
      const m=j.resolution.match(/(\d+\*\d+)/);
      if(m&&RES_MAP[m[1]]){ activeRes=RES_MAP[m[1]]; buildRes(); }
    }
  }catch(e){
    dot.className='sdot bad';stxt.textContent='Poll error';
  }
}

// ── init ──────────────────────────────────────────────────────────────────────
buildIre();buildPatterns();buildColors();buildRes();
pollState();
setInterval(pollState,10000);
</script>
</body></html>"""


USP405_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>USP 405 · Control</title>
<style>
:root{
  --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
  --ink:#e8ebf0;--muted:#8b93a3;--faint:#1f232d;
  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--accent:#22d3ee;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font-family:var(--mono);
  min-height:100dvh;padding-bottom:60px}
header{display:flex;align-items:center;gap:12px;padding:14px 20px;
  border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(34,211,238,.06),transparent)}
.brand{font-size:18px;font-weight:800;letter-spacing:.08em;color:var(--accent)}
.hdr-sub{font-size:11px;color:var(--muted)}
.spacer{flex:1}
.port-pick{display:flex;gap:6px}
.port-btn{font-family:var(--mono);font-size:11px;padding:4px 10px;
  border-radius:5px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--muted);transition:all .15s}
.port-btn.active{background:rgba(34,211,238,.12);color:var(--accent);
  border-color:rgba(34,211,238,.4)}
.back-btn{font-family:var(--mono);font-size:12px;color:var(--muted);
  background:none;border:1px solid var(--line);border-radius:6px;
  padding:5px 12px;cursor:pointer;text-decoration:none}
.back-btn:hover{color:var(--ink);border-color:var(--muted)}
main{max-width:780px;margin:0 auto;padding:18px 16px;display:flex;flex-direction:column;gap:14px}

/* status bar */
.status-bar{display:flex;align-items:center;gap:8px;padding:9px 14px;flex-wrap:wrap;
  background:var(--panel);border:1px solid var(--line);border-radius:8px;font-size:11.5px;color:var(--muted)}
.sdot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex-shrink:0}
.sdot.ok{background:var(--ok)}.sdot.bad{background:var(--bad)}
.stat-seg{display:flex;align-items:center;gap:4px}
.stat-sep{color:var(--line);margin:0 3px}
.stat-val{color:var(--ink);font-weight:700}

/* tabs */
.tabs{display:flex;border-bottom:1px solid var(--line);gap:0}
.tab{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  padding:9px 15px;cursor:pointer;border:none;background:none;color:var(--muted);
  border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .15s}
.tab:hover{color:var(--ink)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-panel{display:none;flex-direction:column;gap:14px}
.tab-panel.active{display:flex}

/* section card */
.sec{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.sec-title{font-size:10px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);margin-bottom:12px}

/* input buttons */
.inp-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}
.inp-btn{font-family:var(--mono);font-size:12px;font-weight:700;padding:14px 4px;
  border-radius:9px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--muted);transition:all .15s;text-align:center;
  display:flex;flex-direction:column;gap:4px;align-items:center}
.inp-btn .inp-num{font-size:20px;font-weight:900;line-height:1}
.inp-btn .inp-lbl{font-size:9px;letter-spacing:.04em;text-transform:uppercase;line-height:1.3}
.inp-btn:hover{color:var(--ink);border-color:var(--muted)}
.inp-btn.active{background:rgba(34,211,238,.1);color:var(--accent);
  border-color:rgba(34,211,238,.45);box-shadow:0 0 0 1px rgba(34,211,238,.15)}

/* sliders */
.ctrl-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.ctrl-row:last-child{margin-bottom:0}
.ctrl-lbl{font-size:11px;color:var(--muted);min-width:82px;flex-shrink:0}
.ctrl-val{font-size:12px;font-weight:700;min-width:36px;text-align:right;
  color:var(--ink);flex-shrink:0}
input[type=range]{flex:1;accent-color:var(--accent);cursor:pointer}

/* +/- steppers */
.stepper{display:flex;align-items:center;gap:8px;flex:1}
.step-btn{font-family:var(--mono);font-size:18px;font-weight:700;width:38px;height:36px;
  border-radius:8px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--ink);transition:all .15s;
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.step-btn:hover{background:rgba(34,211,238,.1);border-color:rgba(34,211,238,.4);color:var(--accent)}
.step-btn:active{transform:scale(.93)}
.step-mid{flex:1;text-align:center;font-size:11px;color:var(--muted)}

/* selects */
select{font-family:var(--mono);font-size:12px;background:var(--panel2);
  color:var(--ink);border:1px solid var(--line);border-radius:7px;
  padding:8px 10px;cursor:pointer;flex:1;outline:none;width:100%}
select:focus{border-color:var(--accent)}

/* toggles */
.toggle-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.toggle-row{display:flex;align-items:center;gap:10px;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.toggle-lbl{flex:1;font-size:12px;color:var(--muted)}
.tog{position:relative;width:40px;height:22px;flex-shrink:0}
.tog input{opacity:0;width:0;height:0;position:absolute}
.tog-sl{position:absolute;inset:0;border-radius:11px;cursor:pointer;
  background:var(--panel);border:1px solid var(--line);transition:.2s}
.tog-sl::before{content:'';position:absolute;width:16px;height:16px;border-radius:50%;
  left:2px;top:2px;background:var(--muted);transition:.2s}
.tog input:checked+.tog-sl{background:rgba(34,211,238,.18);border-color:rgba(34,211,238,.5)}
.tog input:checked+.tog-sl::before{transform:translateX(18px);background:var(--accent)}

/* presets */
.preset-row{display:flex;gap:8px}
.preset-slot{flex:1;display:flex;flex-direction:column;gap:6px;
  background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:10px}
.preset-num{font-size:10px;color:var(--muted);text-align:center;letter-spacing:.1em;
  text-transform:uppercase;margin-bottom:2px}
.preset-btns{display:flex;gap:5px}
.p-recall{font-family:var(--mono);font-size:11px;flex:1;padding:6px;
  border-radius:6px;cursor:pointer;border:1px solid rgba(52,211,153,.35);
  background:rgba(52,211,153,.08);color:var(--ok);transition:all .15s}
.p-recall:hover{background:rgba(52,211,153,.18)}
.p-save{font-family:var(--mono);font-size:11px;flex:1;padding:6px;
  border-radius:6px;cursor:pointer;border:1px solid rgba(245,185,66,.35);
  background:rgba(245,185,66,.08);color:var(--warn);transition:all .15s}
.p-save:hover{background:rgba(245,185,66,.18)}

/* action buttons */
.action-btn{font-family:var(--mono);font-size:12px;font-weight:700;padding:10px 18px;
  border-radius:8px;cursor:pointer;border:1px solid var(--line);
  background:var(--panel2);color:var(--muted);transition:all .15s}
.action-btn.danger{border-color:rgba(255,84,112,.35);color:var(--bad);background:rgba(255,84,112,.08)}
.action-btn.danger:hover{background:rgba(255,84,112,.18)}

/* two-col */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}

/* toast */
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:rgba(25,28,38,.96);border:1px solid var(--line);border-radius:8px;
  padding:9px 18px;font-size:12px;color:var(--ink);pointer-events:none;
  opacity:0;transition:opacity .25s;z-index:100;white-space:nowrap}
.toast.show{opacity:1}

@media(max-width:560px){
  .two-col{grid-template-columns:1fr}
  .inp-grid{grid-template-columns:repeat(3,1fr)}
  .toggle-grid{grid-template-columns:1fr}
}
</style>
</head><body>

<header>
  <div>
    <div class="brand">USP 405</div>
    <div class="hdr-sub" id="hdr-sub">Universal Signal Processor</div>
  </div>
  <div class="port-pick">
    <button class="port-btn" id="pb5" onclick="switchPort(5)">COM 5</button>
    <button class="port-btn" id="pb6" onclick="switchPort(6)">COM 6</button>
    <button class="port-btn" id="pb7" onclick="switchPort(7)">COM 7</button>
  </div>
  <div class="spacer"></div>
  <a class="back-btn" href="/control/ipcp505">← IPCP 505</a>
</header>

<main>

<div class="status-bar">
  <span class="sdot" id="conn-dot"></span>
  <span class="stat-seg">IN <span class="stat-val" id="st-input">—</span></span>
  <span class="stat-sep">|</span>
  <span class="stat-seg">STD <span class="stat-val" id="st-std">—</span></span>
  <span class="stat-sep">|</span>
  <span class="stat-seg">H <span class="stat-val" id="st-hrt">—</span> kHz</span>
  <span class="stat-sep">·</span>
  <span class="stat-seg">V <span class="stat-val" id="st-vrt">—</span> Hz</span>
  <span class="stat-sep">|</span>
  <span class="stat-seg">OUT <span class="stat-val" id="st-out">—</span></span>
  <div style="flex:1"></div>
  <span id="st-fw" style="color:var(--muted);font-size:10.5px"></span>
</div>

<div class="tabs">
  <button class="tab active" id="tab-btn-signal"   onclick="showTab('signal',this)">Signal</button>
  <button class="tab"        id="tab-btn-picture"  onclick="showTab('picture',this)">Picture</button>
  <button class="tab"        id="tab-btn-position" onclick="showTab('position',this)">Position</button>
  <button class="tab"        id="tab-btn-output"   onclick="showTab('output',this)">Output</button>
  <button class="tab"        id="tab-btn-advanced" onclick="showTab('advanced',this)">Advanced</button>
</div>

<!-- ═══ SIGNAL ═══ -->
<div class="tab-panel active" id="tab-signal">

  <div class="sec">
    <div class="sec-title">Input Selection</div>
    <div class="inp-grid">
      <button class="inp-btn" id="inp1" onclick="selectInput(1)">
        <span class="inp-num">1</span><span class="inp-lbl">RGB DB15</span>
      </button>
      <button class="inp-btn" id="inp2" onclick="selectInput(2)">
        <span class="inp-num">2</span><span class="inp-lbl">RGB / Comp BNC</span>
      </button>
      <button class="inp-btn" id="inp3" onclick="selectInput(3)">
        <span class="inp-num">3</span><span class="inp-lbl">Composite / S-VHS</span>
      </button>
      <button class="inp-btn" id="inp4" onclick="selectInput(4)">
        <span class="inp-num">4</span><span class="inp-lbl">Composite / S-VHS</span>
      </button>
      <button class="inp-btn" id="inp5" onclick="selectInput(5)">
        <span class="inp-num">5</span><span class="inp-lbl">SDI</span>
      </button>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Input 2 Video Type</div>
    <div class="ctrl-row" style="margin-bottom:0">
      <div class="ctrl-lbl">Signal Type</div>
      <select id="in2type-sel" onchange="setIn2Type(this.value)">
        <option value="0">RGB</option>
        <option value="1">RGBcvS</option>
        <option value="2">YUVi — Component Interlaced</option>
        <option value="3">YUVp — Component Progressive</option>
        <option value="4">Betacam 50 Hz</option>
        <option value="5">Betacam 60 Hz</option>
        <option value="6">HDTV</option>
        <option value="7">S-Video</option>
        <option value="8">Composite</option>
      </select>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Memory Presets — Input <span id="preset-inp-lbl">?</span></div>
    <div class="preset-row">
      <div class="preset-slot">
        <div class="preset-num">Preset 1</div>
        <div class="preset-btns">
          <button class="p-recall" onclick="recallPreset(1)">Recall</button>
          <button class="p-save"   onclick="savePreset(1)">Save</button>
        </div>
      </div>
      <div class="preset-slot">
        <div class="preset-num">Preset 2</div>
        <div class="preset-btns">
          <button class="p-recall" onclick="recallPreset(2)">Recall</button>
          <button class="p-save"   onclick="savePreset(2)">Save</button>
        </div>
      </div>
      <div class="preset-slot">
        <div class="preset-num">Preset 3</div>
        <div class="preset-btns">
          <button class="p-recall" onclick="recallPreset(3)">Recall</button>
          <button class="p-save"   onclick="savePreset(3)">Save</button>
        </div>
      </div>
    </div>
  </div>

</div><!-- /tab-signal -->

<!-- ═══ PICTURE ═══ -->
<div class="tab-panel" id="tab-picture">

  <div class="sec">
    <div class="sec-title">Picture Adjustments</div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Brightness</div>
      <input type="range" id="sl-bright" min="0" max="63" value="32"
        oninput="slLive('bright',this.value)" onchange="slCommit('bright',this.value)"/>
      <div class="ctrl-val" id="val-bright">32</div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Contrast</div>
      <input type="range" id="sl-contrast" min="0" max="255" value="128"
        oninput="slLive('contrast',this.value)" onchange="slCommit('contrast',this.value)"/>
      <div class="ctrl-val" id="val-contrast">128</div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Color</div>
      <input type="range" id="sl-color" min="0" max="127" value="64"
        oninput="slLive('color',this.value)" onchange="slCommit('color',this.value)"/>
      <div class="ctrl-val" id="val-color">64</div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Tint</div>
      <input type="range" id="sl-tint" min="0" max="255" value="128"
        oninput="slLive('tint',this.value)" onchange="slCommit('tint',this.value)"/>
      <div class="ctrl-val" id="val-tint">128</div>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Detail Filter</div>
    <div class="ctrl-row" style="font-size:10.5px;color:var(--muted);margin-bottom:12px;margin-top:-4px">
      H and V detail filters apply to RGB &amp; HDTV inputs only.
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">H Detail</div>
      <div class="stepper">
        <button class="step-btn" onclick="stepDetail('h',-1)">−</button>
        <div class="step-mid"><span id="val-hdetail">—</span> / 7</div>
        <button class="step-btn" onclick="stepDetail('h',+1)">+</button>
      </div>
    </div>
    <div class="ctrl-row" style="margin-bottom:0">
      <div class="ctrl-lbl">V Detail</div>
      <div class="stepper">
        <button class="step-btn" onclick="stepDetail('v',-1)">−</button>
        <div class="step-mid"><span id="val-vdetail">—</span> / 7</div>
        <button class="step-btn" onclick="stepDetail('v',+1)">+</button>
      </div>
    </div>
  </div>

</div><!-- /tab-picture -->

<!-- ═══ POSITION ═══ -->
<div class="tab-panel" id="tab-position">

  <div class="sec">
    <div class="sec-title">Position &amp; Size</div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">H Center</div>
      <div class="stepper">
        <button class="step-btn" onclick="sendCmd('-H')" title="Shift left">←</button>
        <div class="step-mid">shift left / right</div>
        <button class="step-btn" onclick="sendCmd('+H')" title="Shift right">→</button>
      </div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">V Center</div>
      <div class="stepper">
        <button class="step-btn" onclick="sendCmd('-/')" title="Shift down">↓</button>
        <div class="step-mid">shift down / up</div>
        <button class="step-btn" onclick="sendCmd('+/')" title="Shift up">↑</button>
      </div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">H Size</div>
      <div class="stepper">
        <button class="step-btn" onclick="sendCmd('-:')">−</button>
        <div class="step-mid">narrower / wider</div>
        <button class="step-btn" onclick="sendCmd('+:')">+</button>
      </div>
    </div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">V Size</div>
      <div class="stepper">
        <button class="step-btn" onclick="sendCmd('-;')">−</button>
        <div class="step-mid">shorter / taller</div>
        <button class="step-btn" onclick="sendCmd('+;')">+</button>
      </div>
    </div>
    <div class="ctrl-row" style="margin-bottom:0">
      <div class="ctrl-lbl">Zoom</div>
      <div class="stepper">
        <button class="step-btn" onclick="sendCmd('-[')">−</button>
        <div class="step-mid">zoom out / in</div>
        <button class="step-btn" onclick="sendCmd('+[')">+</button>
      </div>
    </div>
  </div>

</div><!-- /tab-position -->

<!-- ═══ OUTPUT ═══ -->
<div class="tab-panel" id="tab-output">

  <div class="sec">
    <div class="sec-title">Output Resolution &amp; Rate</div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Resolution</div>
      <select id="sel-res" onchange="applyOutputRate()">
        <option value="0">640×480</option>
        <option value="1">800×600</option>
        <option value="2">832×624</option>
        <option value="3">848×480</option>
        <option value="4">852×480</option>
        <option value="5">1024×768</option>
        <option value="6">1280×768</option>
        <option value="7">1280×1024</option>
        <option value="8">1360×765</option>
        <option value="9">1365×1024</option>
        <option value="10">1400×1050</option>
        <option value="11">576p</option>
        <option value="12">720p</option>
        <option value="13">1080p</option>
        <option value="14">1080i</option>
        <option value="15">NTSC</option>
        <option value="16">PAL</option>
        <option value="17">Custom / Per Input</option>
      </select>
    </div>
    <div class="ctrl-row" style="margin-bottom:0">
      <div class="ctrl-lbl">Refresh Rate</div>
      <select id="sel-rate" onchange="applyOutputRate()">
        <option value="0">50 Hz</option>
        <option value="1">56 Hz (1280×768)</option>
        <option value="2" selected>60 Hz</option>
        <option value="3">75 Hz</option>
        <option value="4">85 Hz (1024×768)</option>
        <option value="5">AFL / Frame Lock</option>
        <option value="6">NTSC or PAL Refresh</option>
        <option value="7">N/A</option>
      </select>
    </div>
  </div>

  <div class="two-col">
    <div class="sec">
      <div class="sec-title">Output Signal</div>
      <select id="sel-outsig" onchange="sendSpecial(6,parseInt(this.value))">
        <option value="0">RGB (Default)</option>
        <option value="1">Y, R-Y, B-Y</option>
      </select>
    </div>
    <div class="sec">
      <div class="sec-title">Sync Polarity</div>
      <select id="sel-pol" onchange="sendSpecial(7,parseInt(this.value))">
        <option value="0">H−/V− (Default)</option>
        <option value="1">H−/V+</option>
        <option value="2">H+/V−</option>
        <option value="3">H+/V+</option>
      </select>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Blanking</div>
    <div class="ctrl-row">
      <div class="ctrl-lbl">Top Blank</div>
      <input type="range" id="sl-topblank" min="0" max="237" value="0"
        oninput="slLive('topblank',this.value)" onchange="slCommit('topblank',this.value)"/>
      <div class="ctrl-val" id="val-topblank">0</div>
    </div>
    <div class="ctrl-row" style="margin-bottom:0">
      <div class="ctrl-lbl">Bot Blank</div>
      <input type="range" id="sl-botblank" min="0" max="237" value="0"
        oninput="slLive('botblank',this.value)" onchange="slCommit('botblank',this.value)"/>
      <div class="ctrl-val" id="val-botblank">0</div>
    </div>
  </div>

</div><!-- /tab-output -->

<!-- ═══ ADVANCED ═══ -->
<div class="tab-panel" id="tab-advanced">

  <div class="sec">
    <div class="sec-title">Toggles</div>
    <div class="toggle-grid">
      <div class="toggle-row">
        <span class="toggle-lbl">Freeze Output</span>
        <label class="tog">
          <input type="checkbox" id="tog-freeze" onchange="sendCmd(this.checked?'1F':'0F')"/>
          <span class="tog-sl"></span></label>
      </div>
      <div class="toggle-row">
        <span class="toggle-lbl">Blue Screen</span>
        <label class="tog">
          <input type="checkbox" id="tog-blue" onchange="sendSpecial(8,this.checked?1:0)"/>
          <span class="tog-sl"></span></label>
      </div>
      <div class="toggle-row">
        <span class="toggle-lbl">Edge Smoothing</span>
        <label class="tog">
          <input type="checkbox" id="tog-edge" onchange="sendSpecial(16,this.checked?1:0)"/>
          <span class="tog-sl"></span></label>
      </div>
      <div class="toggle-row">
        <span class="toggle-lbl">Enhanced Mode</span>
        <label class="tog">
          <input type="checkbox" id="tog-enh" onchange="sendSpecial(12,this.checked?1:0)"/>
          <span class="tog-sl"></span></label>
      </div>
      <div class="toggle-row">
        <span class="toggle-lbl">PAL Film Mode</span>
        <label class="tog">
          <input type="checkbox" id="tog-pal" onchange="sendSpecial(18,this.checked?1:0)"/>
          <span class="tog-sl"></span></label>
      </div>
      <div class="toggle-row">
        <span class="toggle-lbl">Front Panel Lock</span>
        <label class="tog">
          <input type="checkbox" id="tog-exe" onchange="sendCmd(this.checked?'1X':'0X')"/>
          <span class="tog-sl"></span></label>
      </div>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Test Pattern</div>
    <select id="sel-testpat" onchange="sendCmd(this.value+'J')">
      <option value="0">Off</option>
      <option value="1">Color Bars</option>
      <option value="2">Crosshatch</option>
      <option value="3">4×4 Crosshatch</option>
      <option value="4">Grey</option>
      <option value="5">Crop</option>
      <option value="6">Film Aspect 1.78</option>
      <option value="7">Film Aspect 1.85</option>
      <option value="8">Film Aspect 2.35</option>
      <option value="9">Ramp</option>
      <option value="10">Alternating Pixels</option>
    </select>
  </div>

  <div class="two-col">
    <div class="sec">
      <div class="sec-title">Encoder Filter</div>
      <div class="ctrl-row" style="margin-bottom:0">
        <input type="range" id="sl-enc" min="0" max="12" value="0"
          oninput="slLive('enc',this.value)" onchange="slCommit('enc',this.value)"/>
        <div class="ctrl-val" id="val-enc">0</div>
      </div>
    </div>
    <div class="sec">
      <div class="sec-title">RGB Delay</div>
      <div class="ctrl-row" style="margin-bottom:0">
        <input type="range" id="sl-rgbdly" min="0" max="50" value="0"
          oninput="slLive('rgbdly',this.value)" onchange="slCommit('rgbdly',this.value)"/>
        <div class="ctrl-val" id="val-rgbdly">0.0s</div>
      </div>
    </div>
  </div>

  <div class="sec">
    <div class="sec-title">Factory Reset</div>
    <div style="display:flex;gap:12px;align-items:center">
      <span style="flex:1;font-size:11.5px;color:var(--muted)">
        Reset all USP 405 settings to factory defaults. Cannot be undone.
      </span>
      <button class="action-btn danger" onclick="factoryReset()">ZAP · Reset</button>
    </div>
  </div>

</div><!-- /tab-advanced -->

</main>

<div class="toast" id="toast"></div>

<script>
// Backslash char — used for Input 2 type query/set commands (avoids r-string confusion)
const BS = String.fromCharCode(92);

let currentPort = 5;
let pollTimer = null;
let slTimers  = {};

// ─── Init ─────────────────────────────────────────────────────────────────────
function init(){
  const p = parseInt(new URLSearchParams(location.search).get('port') || '5');
  currentPort = (p >= 1 && p <= 8) ? p : 5;
  updatePortBtns();
  pollState();
  pollTimer = setInterval(pollState, 8000);
}

function switchPort(p){
  currentPort = p;
  updatePortBtns();
  const u = new URL(location.href);
  u.searchParams.set('port', p);
  history.replaceState({}, '', u);
  clearInterval(pollTimer);
  pollState();
  pollTimer = setInterval(pollState, 8000);
}

function updatePortBtns(){
  [5,6,7].forEach(p=>{
    const e=document.getElementById('pb'+p);
    if(e) e.classList.toggle('active', currentPort===p);
  });
}

// ─── Tabs ─────────────────────────────────────────────────────────────────────
function showTab(name, btn){
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
}

// ─── API ──────────────────────────────────────────────────────────────────────
async function sendCmd(cmd){
  try{
    const r = await fetch('/api/control/ipcp505/usp405/cmd',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({port: currentPort, cmd})
    });
    const j = await r.json();
    if(j.error) toast('⚠ ' + j.error, true);
    else toast('✓ ' + (cmd.length < 14 ? cmd : cmd.slice(0,12)+'…'));
    return j;
  } catch(e){ toast('⚠ network error', true); }
}

async function pollState(){
  try{
    const r = await fetch('/api/control/ipcp505/usp405/state?port='+currentPort);
    if(!r.ok){ setDot('bad'); return; }
    const s = await r.json();
    if(s.error){ setDot('bad'); return; }
    setDot('ok');
    applyState(s);
  } catch(e){ setDot('bad'); }
}

// ─── State → UI ───────────────────────────────────────────────────────────────
const STD_MAP = {0:'None',1:'NTSC 3.58',2:'PAL',3:'NTSC 4.43',4:'SECAM'};
const RES_MAP = {
  0:'640×480',1:'800×600',2:'832×624',3:'848×480',4:'852×480',
  5:'1024×768',6:'1280×768',7:'1280×1024',8:'1360×765',9:'1365×1024',
  10:'1400×1050',11:'576p',12:'720p',13:'1080p',14:'1080i',
  15:'NTSC',16:'PAL',17:'Per Input'
};
const RATE_MAP = {0:'50Hz',1:'56Hz',2:'60Hz',3:'75Hz',4:'85Hz',5:'AFL',6:'NTSC/PAL',7:'N/A'};

function applyState(s){
  // Parse "I" response: Vid1 Hrt15.625 Vrt50.00 Std2 Pre1
  const info = s.info || '';
  const inpM = info.match(/Vid\s*(\d)/i);
  const hrtM = info.match(/Hrt\s*([\d.]+)/i);
  const vrtM = info.match(/Vrt\s*([\d.]+)/i);
  const stdM = info.match(/Std\s*(\d)/i);
  const preM = info.match(/Pre\s*(\d)/i);

  const curInput = inpM ? parseInt(inpM[1]) : 0;
  setText('st-input', curInput || '—');
  setText('st-hrt',   hrtM ? hrtM[1] : '—');
  setText('st-vrt',   vrtM ? vrtM[1] : '—');
  setText('st-std',   stdM ? (STD_MAP[parseInt(stdM[1])] || stdM[1]) : '—');
  if(s.firmware) setText('st-fw', 'v' + s.firmware);
  if(curInput) setText('preset-inp-lbl', curInput);

  // Highlight active input button
  for(let i=1;i<=5;i++){
    const el = document.getElementById('inp'+i);
    if(el) el.classList.toggle('active', i===curInput);
  }

  // Output rate: parse "5 * 2" or "Rte5*2"
  const outR = s.outrate || '';
  const rtM  = outR.match(/(\d+)\s*\*\s*(\d+)/);
  if(rtM){
    const rn = RES_MAP[parseInt(rtM[1])] || rtM[1];
    const rr = RATE_MAP[parseInt(rtM[2])] || rtM[2];
    setText('st-out', rn + ' @ ' + rr);
    setSelect('sel-res',  rtM[1]);
    setSelect('sel-rate', rtM[2]);
  } else {
    setText('st-out', outR || '—');
  }

  // Input 2 type — response to '\' query is bare number e.g. "3"
  const typM = (s.in2type||'').match(/Typ\s*(\d)/i) || (s.in2type||'').match(/^(\d)$/);
  if(typM) setSelect('in2type-sel', typM[1]);

  // Sliders — query responses are bare numbers; set responses have prefix like "Brt47"
  setSliderFromRaw('bright',   s.brightness, /Brt\s*(\d+)/i);
  setSliderFromRaw('contrast', s.contrast,   /Con\s*(\d+)/i);
  setSliderFromRaw('color',    s.color,      /Col\s*(\d+)/i);
  setSliderFromRaw('tint',     s.tint,       /Tin\s*(\d+)/i);
  setSliderFromRaw('topblank', s.top_blank,  /Blt\s*(\d+)/i);
  setSliderFromRaw('botblank', s.bot_blank,  /Blb\s*(\d+)/i);

  // Detail values
  const hdM = (s.hdetail||'').match(/Dhz\s*(\d+)/i) || (s.hdetail||'').match(/^(\d+)$/);
  if(hdM) setText('val-hdetail', hdM[1]);
  const vdM = (s.vdetail||'').match(/Dvz\s*(\d+)/i) || (s.vdetail||'').match(/^(\d+)$/);
  if(vdM) setText('val-vdetail', vdM[1]);

  // Toggles — query response is bare 0 or 1
  setTog('tog-freeze', s.freeze,   /Frz\s*(\d)/i);
  setTog('tog-blue',   s.blue_scr, /Blu\s*(\d)/i);
  setTog('tog-edge',   s.edge_smth,/Fil\s*(\d)/i);
  setTog('tog-enh',    s.enhanced, /Enh\s*(\d)/i);
  setTog('tog-pal',    s.pal_film, /Flm\s*(\d)/i);
  setTog('tog-exe',    s.exemode,  /Exe\s*(\d)/i);

  // Test pattern
  const tpM = (s.testpat||'').match(/Tst\s*(\d+)/i) || (s.testpat||'').match(/^(\d+)$/);
  if(tpM) setSelect('sel-testpat', tpM[1]);

  // Encoder filter: response "Enc03" or bare number
  const encM = (s.enc_filter||'').match(/Enc\s*0*(\d+)/i) || (s.enc_filter||'').match(/^0*(\d+)$/);
  if(encM){ const v=parseInt(encM[1]); setSl('sl-enc',v); setText('val-enc',v); }

  // RGB delay: response "Dly35" → 35 × 0.1s
  const dlyM = (s.rgb_delay||'').match(/Dly\s*(\d+)/i) || (s.rgb_delay||'').match(/^(\d+)$/);
  if(dlyM){ const v=parseInt(dlyM[1]); setSl('sl-rgbdly',v); setText('val-rgbdly',(v*0.1).toFixed(1)+'s'); }

  // Output signal / polarity
  const osgM = (s.out_sig||'').match(/Tpo\s*(\d)/i)  || (s.out_sig||'').match(/^(\d)$/);
  if(osgM) setSelect('sel-outsig', osgM[1]);
  const polM = (s.polarity||'').match(/Pol\s*(\d)/i)  || (s.polarity||'').match(/^(\d)$/);
  if(polM) setSelect('sel-pol', polM[1]);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function setText(id,v){ const e=document.getElementById(id); if(e)e.textContent=v; }
function setSelect(id,v){ const e=document.getElementById(id); if(e)e.value=v; }
function setSl(id,v){ const e=document.getElementById(id); if(e)e.value=v; }
function setDot(s){
  const e=document.getElementById('conn-dot');
  if(e) e.className='sdot '+(s==='ok'?'ok':'bad');
}
function setSliderFromRaw(name, raw, re){
  if(!raw) return;
  const m = raw.match(re) || raw.match(/^(\d+)$/);
  if(!m) return;
  const v = parseInt(m[1]);
  setSl('sl-'+name, v);
  setText('val-'+name, v);
}
function setTog(id, raw, re){
  if(!raw) return;
  const m = raw.match(re) || raw.match(/^(\d)$/);
  if(!m) return;
  const e = document.getElementById(id);
  if(e) e.checked = parseInt(m[1]) === 1;
}

// ─── Commands ─────────────────────────────────────────────────────────────────
function selectInput(n){
  // Optimistic UI update immediately
  for(let i=1;i<=5;i++){
    const e=document.getElementById('inp'+i);
    if(e) e.classList.toggle('active', i===n);
  }
  setText('preset-inp-lbl', n);
  sendCmd(n + '!').then(()=> setTimeout(pollState, 1200));
}

function setIn2Type(v){ sendCmd(v + BS); }
function recallPreset(n){ sendCmd(n + ','); }
function savePreset(n)  { sendCmd(n + '.'); }

function slLive(name, v){
  const e = document.getElementById('val-'+name);
  if(!e) return;
  e.textContent = (name==='rgbdly') ? (parseInt(v)*0.1).toFixed(1)+'s' : v;
}

function slCommit(name, v){
  clearTimeout(slTimers[name]);
  slTimers[name] = setTimeout(()=>{
    const vi = parseInt(v);
    const MAP = {
      bright:   vi + 'Y',
      contrast: vi + '^',
      color:    vi + 'C',
      tint:     vi + 'T',
      topblank: vi + '(',
      botblank: vi + ')',
      enc:      '10*' + vi + '#',
      rgbdly:   '3*'  + vi + '#',
    };
    if(MAP[name]) sendCmd(MAP[name]);
  }, 120);
}

function stepDetail(axis, dir){
  if(axis === 'h') sendCmd(dir > 0 ? '+D' : '-D');
  else             sendCmd(dir > 0 ? '+d' : '-d');
}

function applyOutputRate(){
  const res  = document.getElementById('sel-res').value;
  const rate = document.getElementById('sel-rate').value;
  if(res === '' || rate === '') return;  // don't send if either is unset
  sendCmd(res + '*' + rate + '=');
}

function sendSpecial(fn, val){ sendCmd(fn + '*' + val + '#'); }

function factoryReset(){
  if(!confirm('ZAP: Reset ALL USP 405 settings to factory defaults?\n\nThis cannot be undone.')) return;
  sendCmd('\x1bzZXX');
}

function toast(msg, err=false){
  const e = document.getElementById('toast');
  e.textContent = msg;
  e.style.color = err ? 'var(--bad)' : 'var(--ok)';
  e.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(()=>e.classList.remove('show'), 2400);
}

init();
</script>
</body></html>"""


@router.get("/control/ipcp505", response_class=HTMLResponse)
def control_ipcp505_page():
    return HTMLResponse(IPCP505_HTML)


@router.get("/control/ipcp505/vtg400", response_class=HTMLResponse)
def control_vtg400_page():
    return HTMLResponse(VTG400_HTML)


@router.get("/api/control/ipcp505/state")
def ipcp505_state():
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured", "online": False}, status_code=404)
    online, replies, err = sis.query_ipcp505(dev["ip"], int(dev.get("port", 23)))
    result = sis.parse_ipcp505(replies)
    return JSONResponse({
        "online":    online,
        "error":     err,
        "signals":   result["signals"],
        "rail_dots": result["rail_dots"],
        "details":   result["details"],
        "summary":   result["summary"],
    })


@router.post("/api/control/ipcp505/relay/{n}/set")
async def ipcp505_relay_set(n: int, request: Request):
    body  = await request.json()
    state = int(body.get("state", 0))
    if n < 1 or n > 8 or state not in (0, 1):
        return JSONResponse({"error": "Invalid relay or state"}, status_code=400)
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    cmd_b = f"{n:02d}*{state}O\r".encode("ascii")
    resp, err = _ipcp505_send(dev["ip"], int(dev.get("port", 23)), cmd_b)
    action = "close" if state else "open"
    log(f"IPCP505 relay {n} {action}  resp={resp!r}  err={err}")
    return JSONResponse({"ok": err is None, "response": resp, "error": err})


@router.post("/api/control/ipcp505/power/{n}/set")
async def ipcp505_power_set(n: int, request: Request):
    body  = await request.json()
    state = int(body.get("state", 0))
    if n < 1 or n > 4 or state not in (0, 1):
        return JSONResponse({"error": "Invalid port or state"}, status_code=400)
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    cmd_b = f"\x1bP{n}*{state}DCPP\r".encode("ascii")
    resp, err = _ipcp505_send(dev["ip"], int(dev.get("port", 23)), cmd_b)
    log(f"IPCP505 12V port {n} {'on' if state else 'off'}  resp={resp!r}  err={err}")
    return JSONResponse({"ok": err is None, "response": resp, "error": err})


@router.post("/api/control/ipcp505/vtg400/cmd")
async def ipcp505_vtg400_cmd(request: Request):
    body = await request.json()
    vtg_cmd = str(body.get("cmd", "")).strip()
    if not vtg_cmd:
        return JSONResponse({"error": "cmd required"}, status_code=400)
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    resp, err = _vtg400_send(dev["ip"], vtg_cmd)
    log(f"VTG400 cmd {vtg_cmd!r}  resp={resp!r}  err={err}")
    return JSONResponse({"ok": err is None, "response": resp, "error": err})


@router.get("/api/control/ipcp505/vtg400/state")
def ipcp505_vtg400_state():
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    results, err = _vtg400_query_all(dev["ip"])
    if err and not results:
        return JSONResponse({"error": err})
    results["error"] = err
    return JSONResponse(results)


@router.get("/api/control/ipcp505/com/{port}/probe")
def ipcp505_com_probe(port: int):
    """Query N (model/part number) on any IPCP COM port via its TCP aux port (2000+port)."""
    if port < 1 or port > 8:
        return JSONResponse({"error": "port must be 1-8"}, status_code=400)
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    import socket as _s
    tcp_port = 2000 + port
    try:
        sock = _s.create_connection((dev["ip"], tcp_port), timeout=4.0)
    except (OSError, _s.timeout) as e:
        return JSONResponse({"port": port, "model": None, "error": str(e)})
    try:
        sock.sendall(b"N\r")
        raw = sis._read(sock, 1.5, idle=0.3).strip()
        return JSONResponse({"port": port, "model": raw or None, "error": None})
    except OSError as e:
        return JSONResponse({"port": port, "model": None, "error": str(e)})
    finally:
        try: sock.close()
        except OSError: pass


# =========================================================================== #
# USP 405 Control  (/control/ipcp505/usp405)
# =========================================================================== #


# Known part-number → device info mapping for auto-detection
@router.get("/control/ipcp505/usp405", response_class=HTMLResponse)
def control_usp405_page():
    return HTMLResponse(USP405_HTML)


@router.post("/api/control/ipcp505/usp405/cmd")
async def ipcp505_usp405_cmd(request: Request):
    body = await request.json()
    com_port = int(body.get("port", 5))
    usp_cmd  = body.get("cmd", "")
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    if not usp_cmd:
        return JSONResponse({"error": "empty cmd"}, status_code=400)
    resp, err = _usp405_send(dev["ip"], com_port, usp_cmd)
    if err:
        return JSONResponse({"error": err}, status_code=502)
    return JSONResponse({"ok": True, "response": resp})


@router.get("/api/control/ipcp505/usp405/state")
def ipcp505_usp405_state(port: int = 5):
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    results, err = _usp405_query_all(dev["ip"], port)
    if err and not results:
        return JSONResponse({"error": err}, status_code=502)
    return JSONResponse(results)


# Known part-number → device info mapping for auto-detection
COM_DEVICE_MAP = {
    "60-564-01": {"name": "VTG 400",    "page": "/control/ipcp505/vtg400"},
    "60-564-02": {"name": "VTG 400D",   "page": "/control/ipcp505/vtg400"},
    "60-564-03": {"name": "VTG 400DVI", "page": "/control/ipcp505/vtg400"},
    "60-369-01": {"name": "USP 405",    "page": "/control/ipcp505/usp405?port={port}"},
    "60-369-02": {"name": "USP 405",    "page": "/control/ipcp505/usp405?port={port}"},
    "60-369-03": {"name": "USP 405",    "page": "/control/ipcp505/usp405?port={port}"},
    "60-369-04": {"name": "USP 405",    "page": "/control/ipcp505/usp405?port={port}"},
    # Add VSC 700D, VSC 900D part numbers here as we build their pages
}

@router.get("/api/control/ipcp505/com/scan")
def ipcp505_com_scan():
    """Probe all 8 COM ports and return detected device info."""
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    import socket as _s
    results = {}
    for port in range(1, 9):
        tcp_port = 2000 + port
        try:
            sock = _s.create_connection((dev["ip"], tcp_port), timeout=3.0)
            sock.sendall(b"N\r")
            raw = sis._read(sock, 1.2, idle=0.3).strip()
            sock.close()
        except Exception:
            raw = None
        info = None
        if raw:
            for pn, d in COM_DEVICE_MAP.items():
                if pn in raw:
                    info = d
                    break
        device_info = None
        if info:
            page = info.get("page")
            if page and "{port}" in page:
                page = page.format(port=port)
            device_info = {"name": info["name"], "page": page}
        results[str(port)] = {"model": raw or None, "device": device_info}
    return JSONResponse(results)


