"""
Joebot Lab Dashboard  -  Extron lab status & control.

  http://10.0.0.2:8080

V2: DMS 3600 crosspoint control added at /control/dms.
All other device polling remains read-only.

Run:
    uvicorn app:app --host 0.0.0.0 --port 8080
(the __main__ block does this for you)
"""

import os
import re
import time
import socket
import threading
import collections
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import devices as dev_cfg
import config_store
import sis
import dms_control
import dms_names
import mtx_engine
import matrix12800_control
import matrix12800_names
import smx_control
import smx_names
import modules_store

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
VERSION = "2.15.0"  # bump this on every deploy so you can confirm the new code is running

PORT          = int(os.getenv("DASHBOARD_PORT", "8080"))
POLL_SECONDS  = int(os.getenv("POLL_SECONDS", "10"))
SOCK_TIMEOUT  = float(os.getenv("SOCKET_TIMEOUT_SECONDS", "4"))
MAX_WORKERS   = int(os.getenv("POLL_WORKERS", "16"))

STATUS_RANK = {"bad": 3, "warn": 2, "ok": 1, "gray": 0}

_repoll_event = threading.Event()   # set to trigger an immediate poll

HISTORY_LEN = 60   # keep last 60 poll results per device (~10 min at 10s)

# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #
from shared import _logs, log

_lock      = threading.Lock()
_state     = {"devices": {}, "families": [], "meta": {}}
_history   = {}   # id -> deque of "ok"/"bad"/"warn"/"gray" strings
_uptime    = {}   # id -> {"polls": int, "online": int}
_last_seen = {}   # id -> epoch float of last successful poll
_prev_status = {} # id -> last known status string (for flash detection)


# --------------------------------------------------------------------------- #
# Polling one device
# --------------------------------------------------------------------------- #
def _offline(policy, err=None):
    if policy == "strict":
        return {"online": False, "status": "bad",
                "summary": "OFFLINE / unreachable", "details": [], "error": err,
                "hint": "Check power, Ethernet link, IP/subnet, or a stale ARP "
                        "entry if this unit should be online."}
    if policy == "planned":
        return {"online": False, "status": "gray",
                "summary": "reserved / not present", "details": [], "error": err}
    return {"online": False, "status": "gray", "summary": "offline", "details": [], "error": err}


def poll_device(d):
    kind   = d["kind"]
    policy = d.get("policy", "lenient")
    port   = d.get("port", 23)
    ip     = d["ip"]

    try:
        if kind == "host":
            up = sis.host_alive(ip, port, timeout=min(SOCK_TIMEOUT, 3.0))
            if up:
                return {"online": True, "status": "ok", "summary": "reachable", "details": []}
            return _offline(policy)

        if kind == "ipcp505":
            # Build serial_ports map from meta field if configured
            # meta: {"serial_01": "VSC 700 #1", "serial_02": "VSC 700 #2", ...}
            meta = d.get("meta", {})
            serial_ports = {}
            for k, v in meta.items():
                m = re.match(r"serial_?(\d+)", k)
                if m:
                    serial_ports[int(m.group(1))] = {"label": v}
            online, replies, err = sis.query_ipcp505(
                ip, port, timeout=SOCK_TIMEOUT, serial_ports=serial_ports)
            if not online:
                return _offline(policy, err)
            res = sis.parse_ipcp505(replies, serial_ports=serial_ports)
            res["online"] = True
            res["raw"] = replies
            return res

        cmds = list(sis.COMMANDS.get(kind, ["I"]))
        if kind == "smx":
            # Add 0LS (video) and 4LS (audio) for each configured slot
            for slot, meta in sorted(dev_cfg.SMX_SLOTS.items()):
                cmds.append(meta["ls_cmd"])           # e.g. "10*0LS"
                cmds.append(f"{slot}*4LS")            # audio fallback
        online, replies, err = sis.query(
            ip, port, cmds, timeout=SOCK_TIMEOUT, password=d.get("password"))
        if not online:
            return _offline(policy, err)

        if kind == "matrix12800":
            res = sis.parse_matrix12800(replies)
        elif kind == "dms3600":
            res = sis.parse_dms3600(replies)
        elif kind == "smx":
            # slot_meta from devices.py used for human labels; discovery is dynamic
            res = sis.parse_smx(replies, dev_cfg.SMX_SLOTS)
        elif kind == "mgp":
            res = sis.parse_mgp(replies)
        elif kind == "pcs4":
            res = sis.parse_pcs4(replies)
        else:
            res = sis.parse_extron_info(replies)

        res["online"] = True
        res["raw"] = replies
        # a planned device that turns out to be alive is fine -> keep parsed status
        return res
    except Exception as e:                       # never let one device kill the poll
        return {"online": False, "status": "warn",
                "summary": "poll error", "details": [], "error": repr(e)}


# --------------------------------------------------------------------------- #
# Poll cycle
# --------------------------------------------------------------------------- #
def poll_once():
    t0 = time.time()
    results = {}
    current_devices = config_store.get_devices()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(poll_device, d): d for d in current_devices}
        for fut, d in futures.items():
            did = d.get("id", "?")
            try:
                r = fut.result(timeout=SOCK_TIMEOUT + 3)
            except Exception as e:
                r = {"online": False, "status": "warn", "summary": "timeout",
                     "details": [], "error": repr(e)}
            try:
                result = {**_static(d), **r}
            except Exception as e:
                log(f"poll_once: error for {did} — {e}")
                result = {**r, "id": did, "name": d.get("name","?"),
                          "ip": d.get("ip",""), "hostname": d.get("hostname",""),
                          "family": d.get("family","core"), "role": "",
                          "kind": d.get("kind",""), "policy": "lenient", "meta": {}}

            # Per-device history / uptime / derived fields
            now = time.time()
            with _lock:
                if did not in _history:
                    _history[did] = collections.deque(maxlen=HISTORY_LEN)
                _history[did].append(result["status"])
                if did not in _uptime:
                    _uptime[did] = {"polls": 0, "online": 0}
                _uptime[did]["polls"] += 1
                if result.get("online"):
                    _uptime[did]["online"] += 1
                    _last_seen[did] = now
                prev = _prev_status.get(did)
                result["status_changed"] = (prev is not None and prev != result["status"])
                _prev_status[did] = result["status"]
                ut = _uptime[did]
                result["uptime_pct"] = round(100 * ut["online"] / ut["polls"]) if ut["polls"] else None
                ls = _last_seen.get(did)
                result["last_seen_ago"] = round(now - ls) if ls else None
                result["history"] = list(_history[did])

                # Update state incrementally — dashboard sees each device as it finishes
                results[did] = result
                _state["devices"][did] = result
                _state["families"] = _aggregate(_state["devices"])

    dur = time.time() - t0
    bad = sum(1 for r in results.values() if r["status"] == "bad")
    with _lock:
        _state["meta"] = {
            "version":      VERSION,
            "last_poll":    time.strftime("%Y-%m-%d %H:%M:%S"),
            "poll_seconds": POLL_SECONDS,
            "duration":     round(dur, 2),
            "device_count": len(results),
        }
    log(f"poll done in {dur:.1f}s  ({len(results)} devices, {bad} bad)")


def _static(d):
    return {"id": d["id"], "name": d["name"], "ip": d.get("ip", ""),
            "hostname": d.get("hostname", ""), "family": d["family"], "role": d.get("role", ""),
            "kind": d.get("kind", ""), "policy": d.get("policy", "lenient"),
            "meta": d.get("meta", {})}


def _aggregate(results):
    # preserve device order from config
    cfg_order = {d["id"]: i for i, d in enumerate(config_store.get_devices())}
    out = []
    for fam in config_store.get_families():
        members = [r for r in results.values() if r["family"] == fam["id"]]
        members.sort(key=lambda x: cfg_order.get(x["id"], 9999))
        counts = {"ok": 0, "warn": 0, "bad": 0, "gray": 0}
        for m in members:
            counts[m["status"]] = counts.get(m["status"], 0) + 1
        worst = "gray"
        for m in members:
            if STATUS_RANK[m["status"]] > STATUS_RANK[worst]:
                worst = m["status"]
        out.append({
            "id": fam["id"], "name": fam["name"], "status": worst,
            "counts": counts, "total": len(members),
            "dots": [{"id": m["id"], "status": m["status"]} for m in members],
        })
    return out


# --------------------------------------------------------------------------- #
# Background poller thread
# --------------------------------------------------------------------------- #
def _poll_loop():
    while True:
        try:
            poll_once()
        except Exception as e:
            log(f"poll cycle error: {e!r}")
        _repoll_event.wait(timeout=POLL_SECONDS)
        _repoll_event.clear()


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI(title="Joebot Lab Dashboard", version="1.0")


def _prime_state():
    """Populate _state with all configured devices in gray/unknown state
    so the dashboard shows a complete layout before the first poll finishes."""
    devices = config_store.get_devices()
    gray = {d["id"]: {**_static(d), "online": False, "status": "gray",
                      "summary": "pending…", "details": [], "uptime_pct": None,
                      "last_seen_ago": None, "history": [], "status_changed": False}
            for d in devices}
    with _lock:
        _state["devices"]  = gray
        _state["families"] = _aggregate(gray)
        _state["meta"]     = {"version": VERSION, "last_poll": "—",
                              "poll_seconds": POLL_SECONDS, "duration": None,
                              "device_count": len(gray)}


@app.on_event("startup")
def _startup():
    config_store.bootstrap()
    log("dashboard starting")
    _prime_state()
    threading.Thread(target=_poll_loop, daemon=True).start()
    _start_autoswitch()


@app.get("/static/lab.css")
def lab_css():
    from fastapi.responses import Response
    import shared as _shared
    return Response(_shared.LAB_CSS, media_type="text/css",
                    headers={"Cache-Control": "max-age=300"})


@app.get("/api/status")
def api_status():
    with _lock:
        return JSONResponse({"meta": _state["meta"],
                             "families": _state["families"],
                             "devices": _state["devices"]})


@app.get("/api/families")
def api_families():
    with _lock:
        return JSONResponse({"meta": _state["meta"], "families": _state["families"]})


@app.get("/api/devices")
def api_devices():
    with _lock:
        return JSONResponse({"meta": _state["meta"],
                             "devices": list(_state["devices"].values())})


@app.get("/api/device/{did}")
def api_device(did: str):
    with _lock:
        d = _state["devices"].get(did)
    if not d:
        return JSONResponse({"error": "unknown device"}, status_code=404)
    return JSONResponse(d)


@app.get("/api/logs")
def api_logs():
    return JSONResponse({"logs": list(_logs)})


@app.get("/", response_class=HTMLResponse)
def index():
    if not modules_store.is_setup_complete():
        return RedirectResponse(url="/welcome")
    return HTMLResponse(FRONTEND_HTML)


@app.get("/config", response_class=HTMLResponse)
def config_page():
    return HTMLResponse(CONFIG_HTML)


@app.get("/api/config")
def api_config_get():
    return JSONResponse({
        "devices":   config_store.get_devices(),
        "families":  config_store.get_families(),
        "templates": config_store.TEMPLATES,
    })


def _resolve_device_host(device: dict) -> dict:
    """If ip looks like a hostname (or ip is blank but hostname is set),
    resolve it and store the resolved IP in 'ip', keeping the original in 'hostname'."""
    raw = (device.get("ip") or "").strip()
    hostname = (device.get("hostname") or "").strip()

    # If ip field is empty but hostname is provided, try to resolve hostname
    if not raw and hostname:
        raw = hostname

    if raw and not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", raw):
        try:
            resolved = socket.gethostbyname(raw)
            device["ip"] = resolved
            if not device.get("hostname"):
                device["hostname"] = raw
        except OSError:
            pass  # Leave as-is; connection will fail with a clear error
    elif raw:
        device["ip"] = raw

    return device


@app.post("/api/config/device")
async def api_config_add(request: Request):
    device = _resolve_device_host(await request.json())
    result, err = config_store.add_device(device)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    _repoll_event.set()
    log(f"config: added device {result['id']}")
    return JSONResponse(result)


@app.put("/api/config/device/{did}")
async def api_config_update(did: str, request: Request):
    updates = _resolve_device_host(await request.json())
    result = config_store.update_device(did, updates)
    if result is None:
        return JSONResponse({"error": "device not found"}, status_code=404)
    _repoll_event.set()
    log(f"config: updated device {did}")
    return JSONResponse(result)


@app.delete("/api/config/device/{did}")
def api_config_delete(did: str):
    ok = config_store.remove_device(did)
    if not ok:
        return JSONResponse({"error": "device not found"}, status_code=404)
    _repoll_event.set()
    log(f"config: removed device {did}")
    return JSONResponse({"ok": True})


@app.post("/api/config/repoll")
def api_repoll():
    _repoll_event.set()
    return JSONResponse({"ok": True})


@app.post("/api/config/reorder")
async def api_reorder(request: Request):
    """Accept {devices: [id, ...], families: [id, ...]} and persist new order."""
    body = await request.json()
    data = config_store.load()
    if "devices" in body:
        id_order = body["devices"]
        by_id = {d["id"]: d for d in data["devices"]}
        # put known ids in requested order, append any unknowns at end
        data["devices"] = [by_id[i] for i in id_order if i in by_id] + \
                          [d for d in data["devices"] if d["id"] not in id_order]
    if "families" in body:
        fid_order = body["families"]
        by_fid = {f["id"]: f for f in data["families"]}
        data["families"] = [by_fid[i] for i in fid_order if i in by_fid] + \
                            [f for f in data["families"] if f["id"] not in fid_order]
    ok, err = config_store.save(data)
    if not ok:
        return JSONResponse({"error": err}, status_code=500)
    # Immediately rebuild family order in _state so dashboard reflects it
    # without waiting for the next full poll cycle
    with _lock:
        _state["families"] = _aggregate(_state["devices"])
    return JSONResponse({"ok": True})


CONFIG_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Joebot Lab · Config</title>
<style>
  :root{
    --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
    --ink:#e8ebf0;--muted:#8b93a3;--accent:#e0a040;
    --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--gray:#454b58;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:var(--mono);font-size:14px;line-height:1.5;padding-bottom:60px}
  header{display:flex;align-items:center;gap:16px;flex-wrap:wrap;
    padding:16px 22px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(224,160,64,.05),transparent)}
  .brand{font-size:20px;font-weight:700;letter-spacing:.1em;color:var(--accent)}
  nav a{color:var(--muted);text-decoration:none;font-size:13px;padding:5px 10px;
    border-radius:6px;border:1px solid transparent}
  nav a:hover,nav a.active{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  main{max-width:1400px;margin:0 auto;padding:20px 18px}
  h2{font-size:15px;font-weight:600;letter-spacing:.06em;color:var(--muted);
    margin:24px 0 10px;text-transform:uppercase}
  button{font-family:var(--mono);cursor:pointer;font-size:13px}
  .btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:7px 14px}
  .btn:hover{border-color:var(--accent);color:var(--accent)}
  .btn-accent{background:rgba(224,160,64,.15);color:var(--accent);
    border-color:rgba(224,160,64,.4)}
  .btn-accent:hover{background:rgba(224,160,64,.25)}
  .btn-bad{background:rgba(255,84,112,.1);color:var(--bad);
    border-color:rgba(255,84,112,.3);padding:4px 10px}
  .btn-bad:hover{background:rgba(255,84,112,.2)}
  .btn-sm{padding:4px 10px;font-size:12px}

  /* families drag list */
  .fam-list{display:flex;flex-direction:column;gap:4px;margin-bottom:32px}
  .fam-section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    overflow:hidden}
  .fam-section.drag-over{border-color:var(--accent);background:rgba(224,160,64,.05)}
  .fam-header{display:flex;align-items:center;gap:10px;padding:10px 14px;
    border-bottom:1px solid var(--line);user-select:none}
  .fam-header .drag-handle{cursor:grab;color:var(--muted);font-size:16px;
    padding:0 4px;touch-action:none}
  .fam-header .drag-handle:active{cursor:grabbing}
  .fam-title{font-weight:600;font-size:13px;letter-spacing:.04em;color:var(--accent)}
  .fam-hint{color:var(--muted);font-size:11px;margin-left:auto}

  /* device table */
  .tbl{width:100%;border-collapse:collapse;font-size:13px}
  .tbl th{text-align:left;color:var(--muted);font-weight:400;
    padding:6px 10px;border-bottom:1px solid var(--line);font-size:11.5px;
    letter-spacing:.06em;text-transform:uppercase}
  .tbl td{padding:7px 10px;border-bottom:1px dashed rgba(255,255,255,.04);
    vertical-align:middle}
  .dev-row{transition:background .15s}
  .dev-row:hover td{background:rgba(255,255,255,.02)}
  .dev-row.drag-over td{background:rgba(224,160,64,.08)}
  .dev-row.dragging{opacity:.4}
  .drag-handle{cursor:grab;color:var(--muted);padding:0 6px;font-size:15px;
    user-select:none;touch-action:none}
  .drag-handle:active{cursor:grabbing}
  .fam-badge{font-size:11px;padding:2px 8px;border-radius:20px;
    background:rgba(255,255,255,.06);color:var(--muted);border:1px solid var(--line)}
  .kind-badge{font-size:11px;padding:2px 8px;border-radius:4px;
    background:rgba(224,160,64,.08);color:var(--accent);border:1px solid rgba(224,160,64,.2)}
  .policy-btn{font-family:var(--mono);font-size:11px;padding:3px 9px;
    border-radius:20px;border:1px solid var(--line);cursor:pointer;background:transparent}
  .policy-strict{color:var(--bad);border-color:rgba(255,84,112,.4)}
  .policy-lenient{color:var(--warn);border-color:rgba(245,185,66,.4)}
  .policy-planned{color:var(--muted);border-color:var(--line)}
  .dot{width:9px;height:9px;border-radius:50%;display:inline-block}
  .s-ok{background:var(--ok)}.s-bad{background:var(--bad)}
  .s-warn{background:var(--warn)}.s-gray{background:var(--gray)}

  /* modal */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
    z-index:100;align-items:center;justify-content:center;padding:20px}
  .overlay.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:24px;width:100%;max-width:580px;max-height:90vh;overflow-y:auto}
  .modal h3{margin:0 0 18px;font-size:16px;font-weight:600}
  .field{margin-bottom:14px}
  .field label{display:block;color:var(--muted);font-size:12px;
    margin-bottom:4px;letter-spacing:.04em}
  .field input,.field select,.field textarea{
    width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:8px 10px;
    font-family:var(--mono);font-size:13px}
  .field input:focus,.field select:focus,.field textarea:focus{
    outline:none;border-color:var(--accent)}
  .field textarea{resize:vertical;min-height:80px}
  .field select option{background:var(--panel2)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .modal-foot{display:flex;gap:10px;justify-content:flex-end;margin-top:20px;
    padding-top:16px;border-top:1px solid var(--line)}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--panel2);
    border:1px solid var(--line);border-radius:8px;padding:10px 16px;
    font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}
</style></head>
<body>
<header>
  <div class="brand">🦖 JOEBOT LAB</div>
  <nav>
    <a href="/" onclick="event.preventDefault();window.location.href='/?r='+Date.now()">Dashboard</a>
    <a href="/config" class="active">Config</a>
  </nav>
  <div class="spacer"></div>
  <button class="btn btn-accent" onclick="openAdd()">+ Add Device</button>
  <button class="btn btn-sm" onclick="repoll()" style="margin-left:8px">Re-poll now</button>
</header>
<main>
  <h2>Devices <span style="color:var(--muted);font-size:11px;font-weight:400;letter-spacing:0">— drag ⠿ to reorder within or between families</span></h2>
  <div class="fam-list" id="fam-list"></div>
</main>

<!-- Edit/Add modal -->
<div class="overlay" id="modal">
  <div class="modal">
    <h3 id="modal-title">Edit Device</h3>
    <div class="row2">
      <div class="field"><label>ID (unique key)</label>
        <input id="f-id" placeholder="e.g. mgp3"/></div>
      <div class="field"><label>Name</label>
        <input id="f-name" placeholder="e.g. MGP 464 #3"/></div>
    </div>
    <div class="row2">
      <div class="field"><label>IP Address <span style="color:var(--muted);font-weight:400;font-size:10px">resolved &amp; stored</span></label>
        <input id="f-ip" placeholder="10.0.0.x or leave blank"/></div>
      <div class="field"><label>Hostname <span style="color:var(--muted);font-weight:400;font-size:10px">auto-resolves if IP blank</span></label>
        <input id="f-hostname" placeholder="device.local or FQDN"/></div>
    </div>
    <div class="field"><label>Role (description)</label>
      <input id="f-role" placeholder="e.g. scaler / switcher / reserved"/></div>
    <div class="row2">
      <div class="field"><label>Family</label>
        <select id="f-family"></select></div>
      <div class="field"><label>Template / Kind</label>
        <select id="f-kind" onchange="applyTemplate()"></select></div>
    </div>
    <div class="row2">
      <div class="field"><label>Port</label>
        <input id="f-port" type="number" value="23"/></div>
      <div class="field"><label>Policy</label>
        <select id="f-policy">
          <option value="strict">strict — red if offline</option>
          <option value="lenient" selected>lenient — gray if offline</option>
          <option value="planned">planned — gray, expected offline</option>
        </select></div>
    </div>
    <div class="field"><label>Password (leave blank if none)</label>
      <input id="f-password" placeholder="e.g. admin"/></div>
    <div class="field"><label>Meta (JSON — extra info shown on card)</label>
      <textarea id="f-meta">{}</textarea></div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-accent" onclick="saveDevice()">Save</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let cfg={devices:[],families:[],templates:{}};
let liveStatus={};
let editingId=null;

// ── drag state ───────────────────────────────────────────────────────────────
let dragSrc=null, dragSrcFam=null, dragType=null; // 'device' or 'family'
let dragAllowed=false; // set true only when a handle is mousedown'd

document.addEventListener('mouseup',()=>{dragAllowed=false;});

async function load(){
  const [cfgR, stR] = await Promise.all([fetch('/api/config'), fetch('/api/status')]);
  cfg = await cfgR.json();
  const st = await stR.json();
  liveStatus = st.devices || {};
  render();
  populateSelects();
}

function populateSelects(){
  document.getElementById('f-family').innerHTML =
    cfg.families.map(f=>`<option value="${f.id}">${f.name}</option>`).join('');
  document.getElementById('f-kind').innerHTML =
    Object.keys(cfg.templates).map(k=>`<option value="${k}">${k}</option>`).join('');
}

// ── render ───────────────────────────────────────────────────────────────────
function render(){
  const groups={};
  cfg.families.forEach(f=>groups[f.id]={fam:f,devs:[]});
  cfg.devices.forEach(d=>{
    if(groups[d.family]) groups[d.family].devs.push(d);
    else{
      if(!groups['__other']) groups['__other']={fam:{id:'__other',name:'Other'},devs:[]};
      groups['__other'].devs.push(d);
    }
  });

  const list=document.getElementById('fam-list');
  list.innerHTML='';

  cfg.families.forEach(fam=>{
    const g=groups[fam.id];
    if(!g||!g.devs.length) return;
    const sec=document.createElement('div');
    sec.className='fam-section';
    sec.dataset.famid=fam.id;
    sec.draggable=false; // families dragged by handle only

    // family header with drag handle
    const hdr=document.createElement('div');
    hdr.className='fam-header';
    hdr.innerHTML=`<span class="drag-handle fam-drag" title="Drag to reorder family">⠿</span>
      <span class="fam-title">${esc(fam.name)}</span>
      <span class="fam-hint">${g.devs.length} device${g.devs.length!==1?'s':''} — drag rows to reorder</span>`;
    sec.appendChild(hdr);
    sec.draggable=true;
    hdr.querySelector('.fam-drag').addEventListener('mousedown',()=>{dragAllowed=true;});

    // family-level drag events
    sec.addEventListener('dragstart', e=>{
      if(!dragAllowed){e.preventDefault();return;}
      dragAllowed=false;
      dragType='family'; dragSrc=sec;
      e.dataTransfer.effectAllowed='move';
      e.dataTransfer.setData('text/plain',fam.id);
      setTimeout(()=>sec.classList.add('dragging'),0);
    });
    sec.addEventListener('dragend', ()=>{
      sec.classList.remove('dragging','drag-over');
      dragSrc=null; dragType=null;
      saveFamilyOrder();
    });
    sec.addEventListener('dragover', e=>{
      if(dragType!=='family'||dragSrc===sec) return;
      e.preventDefault();
      sec.classList.add('drag-over');
      const list=sec.parentNode;
      const items=[...list.querySelectorAll('.fam-section')];
      const srcIdx=items.indexOf(dragSrc), dstIdx=items.indexOf(sec);
      if(srcIdx<dstIdx) list.insertBefore(dragSrc,sec.nextSibling);
      else list.insertBefore(dragSrc,sec);
    });
    sec.addEventListener('dragleave', ()=>sec.classList.remove('drag-over'));
    sec.addEventListener('drop', e=>{e.preventDefault();sec.classList.remove('drag-over');});

    // device rows table
    const tbl=document.createElement('table');
    tbl.className='tbl';
    tbl.innerHTML=`<thead><tr>
      <th style="width:28px"></th><th></th><th>Name</th><th>IP</th><th>Hostname</th>
      <th>Kind</th><th>Policy</th><th></th>
    </tr></thead>`;
    const tbody=document.createElement('tbody');
    tbody.dataset.famid=fam.id;

    g.devs.forEach(d=>{
      const st=liveStatus[d.id];
      const dotCls=st?'s-'+st.status:'s-gray';
      const tr=document.createElement('tr');
      tr.className='dev-row';
      tr.dataset.devid=d.id;
      tr.dataset.famid=fam.id;
      tr.draggable=true;
      tr.innerHTML=`
        <td><span class="drag-handle dev-drag" title="Drag to reorder">⠿</span></td>
        <td><span class="dot ${dotCls}"></span></td>
        <td>${esc(d.name)}</td>
        <td style="color:var(--muted)">${esc(d.ip)}</td>
        <td style="color:var(--muted);font-size:12px">${esc(d.hostname||'')}</td>
        <td><span class="kind-badge">${esc(d.kind)}</span></td>
        <td><button class="policy-btn policy-${d.policy}"
          onclick="cyclePolicy('${d.id}','${d.policy}')">${d.policy}</button></td>
        <td style="display:flex;gap:6px">
          <button class="btn btn-sm" onclick="openEdit('${d.id}')">Edit</button>
          <button class="btn btn-bad btn-sm" onclick="del('${d.id}','${esc(d.name)}')">✕</button>
        </td>`;

      // device drag events — only start when handle was mousedown'd
      tr.querySelector('.dev-drag').addEventListener('mousedown',()=>{dragAllowed=true;});
      tr.addEventListener('dragstart', e=>{
        if(!dragAllowed){e.preventDefault();return;}
        dragAllowed=false;
        dragType='device'; dragSrc=tr; dragSrcFam=fam.id;
        e.dataTransfer.effectAllowed='move';
        e.dataTransfer.setData('text/plain',d.id);
        e.stopPropagation(); // prevent bubbling to family dragstart
        setTimeout(()=>tr.classList.add('dragging'),0);
      });
      tr.addEventListener('dragend', ()=>{
        tr.classList.remove('dragging','drag-over');
        dragSrc=null; dragType=null; dragSrcFam=null;
        saveDeviceOrder();
      });
      tr.addEventListener('dragover', e=>{
        if(dragType!=='device'||dragSrc===tr) return;
        e.preventDefault();
        tr.classList.add('drag-over');
        const tb=tr.parentNode;
        const rows=[...tb.querySelectorAll('.dev-row')];
        const si=rows.indexOf(dragSrc), di=rows.indexOf(tr);
        if(si<di) tb.insertBefore(dragSrc,tr.nextSibling);
        else tb.insertBefore(dragSrc,tr);
      });
      tr.addEventListener('dragleave', ()=>tr.classList.remove('drag-over'));
      tr.addEventListener('drop', e=>{e.preventDefault();tr.classList.remove('drag-over');});

      tbody.appendChild(tr);
    });

    // allow dropping into an empty tbody zone (cross-family drop)
    tbody.addEventListener('dragover', e=>{
      if(dragType!=='device') return;
      e.preventDefault();
    });
    tbody.addEventListener('drop', e=>{
      if(dragType!=='device') return;
      e.preventDefault();
      // if dropped onto tbody but not a row, append to bottom
      const rows=[...tbody.querySelectorAll('.dev-row')];
      if(!rows.includes(dragSrc)) tbody.appendChild(dragSrc);
      saveDeviceOrder();
    });

    tbl.appendChild(tbody);
    sec.appendChild(tbl);
    list.appendChild(sec);
  });
}

// ── persist order ────────────────────────────────────────────────────────────
async function saveDeviceOrder(){
  // collect device ids in DOM order, grouped by which tbody they're in
  const famSections=[...document.querySelectorAll('.fam-section')];
  const deviceOrder=[];
  const familyUpdates={}; // devId -> new famId if moved cross-family
  famSections.forEach(sec=>{
    const fid=sec.dataset.famid;
    sec.querySelectorAll('.dev-row').forEach(tr=>{
      const did=tr.dataset.devid;
      deviceOrder.push(did);
      if(tr.dataset.famid!==fid) familyUpdates[did]=fid;
    });
  });
  // apply cross-family moves
  const updates=Object.entries(familyUpdates).map(([id,fam])=>
    fetch(`/api/config/device/${id}`,{method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({family:fam})})
  );
  if(updates.length) await Promise.all(updates);
  await fetch('/api/config/reorder',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({devices:deviceOrder})});
  // refresh cfg without full re-render (avoids losing drag state)
  const r=await fetch('/api/config');
  cfg=await r.json();
}

async function saveFamilyOrder(){
  const fids=[...document.querySelectorAll('.fam-section')].map(s=>s.dataset.famid);
  await fetch('/api/config/reorder',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({families:fids})});
  const r=await fetch('/api/config');
  cfg=await r.json();
}

// ── rest of the UI ───────────────────────────────────────────────────────────
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

async function cyclePolicy(id,current){
  const order=['strict','lenient','planned'];
  const next=order[(order.indexOf(current)+1)%order.length];
  await fetch(`/api/config/device/${id}`,{method:'PUT',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({policy:next})});
  toast(`${id} → ${next}`);
  load();
}

function openAdd(){
  editingId=null;
  document.getElementById('modal-title').textContent='Add Device';
  clearForm();
  document.getElementById('modal').classList.add('open');
}

function openEdit(id){
  editingId=id;
  const d=cfg.devices.find(x=>x.id===id);
  if(!d) return;
  document.getElementById('modal-title').textContent=`Edit — ${d.name}`;
  document.getElementById('f-id').value=d.id;
  document.getElementById('f-id').disabled=true;
  document.getElementById('f-name').value=d.name||'';
  document.getElementById('f-ip').value=d.ip||'';
  document.getElementById('f-hostname').value=d.hostname||'';
  document.getElementById('f-role').value=d.role||'';
  document.getElementById('f-family').value=d.family||'core';
  document.getElementById('f-kind').value=d.kind||'extron_info';
  document.getElementById('f-port').value=d.port||23;
  document.getElementById('f-policy').value=d.policy||'lenient';
  document.getElementById('f-password').value=d.password||'';
  document.getElementById('f-meta').value=JSON.stringify(d.meta||{},null,2);
  document.getElementById('modal').classList.add('open');
}

function clearForm(){
  ['f-id','f-name','f-ip','f-hostname','f-role','f-password'].forEach(
    id=>document.getElementById(id).value='');
  document.getElementById('f-id').disabled=false;
  document.getElementById('f-port').value=23;
  document.getElementById('f-policy').value='lenient';
  document.getElementById('f-meta').value='{}';
  document.getElementById('f-family').value=cfg.families[0]?.id||'core';
  document.getElementById('f-kind').value='extron_info';
}

function applyTemplate(){
  const kind=document.getElementById('f-kind').value;
  const t=cfg.templates[kind];
  if(!t) return;
  if(t.port) document.getElementById('f-port').value=t.port;
  if(t.policy) document.getElementById('f-policy').value=t.policy;
  if(t.family) document.getElementById('f-family').value=t.family;
  if(t.role&&!document.getElementById('f-role').value)
    document.getElementById('f-role').value=t.role;
  if(t.password!==undefined) document.getElementById('f-password').value=t.password||'';
}

async function saveDevice(){
  let meta={};
  try{meta=JSON.parse(document.getElementById('f-meta').value||'{}');}
  catch(e){toast('Meta is not valid JSON');return;}
  const d={
    id:       document.getElementById('f-id').value.trim(),
    name:     document.getElementById('f-name').value.trim(),
    ip:       document.getElementById('f-ip').value.trim(),
    hostname: document.getElementById('f-hostname').value.trim(),
    role:     document.getElementById('f-role').value.trim(),
    family:   document.getElementById('f-family').value,
    kind:     document.getElementById('f-kind').value,
    port:     parseInt(document.getElementById('f-port').value)||23,
    policy:   document.getElementById('f-policy').value,
    password: document.getElementById('f-password').value||undefined,
    meta:     meta,
  };
  if(!d.id||!d.name){toast('ID and Name are required');return;}
  if(!d.ip&&!d.hostname){toast('IP address or Hostname is required');return;}
  const url=editingId?`/api/config/device/${editingId}`:'/api/config/device';
  const method=editingId?'PUT':'POST';
  const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},
    body:JSON.stringify(d)});
  const j=await r.json();
  if(j.error){toast('Error: '+j.error);return;}
  closeModal();
  toast(editingId?'Device updated':'Device added');
  load();
}

async function del(id,name){
  if(!confirm(`Remove "${name}"?`)) return;
  await fetch(`/api/config/device/${id}`,{method:'DELETE'});
  toast(`Removed ${name}`);
  load();
}

async function repoll(){
  await fetch('/api/config/repoll',{method:'POST'});
  toast('Re-poll triggered');
}

function closeModal(){
  document.getElementById('modal').classList.remove('open');
  document.getElementById('f-id').disabled=false;
}

let _toastTimer;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>el.classList.remove('show'),2500);
}

document.getElementById('modal').addEventListener('click',e=>{
  if(e.target===document.getElementById('modal')) closeModal();
});

// Reload when navigating back to this page (catches bfcache & config→dashboard)
window.addEventListener('pageshow', e=>{
  if(e.persisted) window.location.reload();
});

load();
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# Frontend (single file, no external deps -> works on an offline lan)
# --------------------------------------------------------------------------- #
FRONTEND_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Joebot Lab</title>
<style>
  :root{
    --bg:#0c0e12; --panel:#15181f; --panel2:#1b1f28; --line:#262b36;
    --ink:#e8ebf0; --muted:#8b93a3; --accent:#e0a040;
    --ok:#34d399; --warn:#f5b942; --bad:#ff5470; --gray:#454b58;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Cascadia Code",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:
      radial-gradient(1200px 600px at 80% -10%,rgba(224,160,64,.06),transparent),
      var(--bg);
    color:var(--ink);font-family:var(--mono);font-size:15px;line-height:1.45;
    -webkit-font-smoothing:antialiased;padding-bottom:40px}
  header{display:flex;align-items:center;gap:18px;flex-wrap:wrap;
    padding:18px 22px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(224,160,64,.05),transparent)}
  .brand{font-size:22px;font-weight:700;letter-spacing:.14em}
  .brand b{color:var(--accent)}
  .meta{color:var(--muted);font-size:12.5px;display:flex;gap:16px;flex-wrap:wrap}
  .meta .v{color:var(--ink)}
  .spacer{flex:1}
  button{font-family:var(--mono);cursor:pointer}
  .toggle{background:var(--panel2);color:var(--muted);border:1px solid var(--line);
    border-radius:7px;padding:7px 12px;font-size:12.5px;
    display:inline-flex;align-items:center;gap:6px}
  .toggle:hover{color:var(--ink);border-color:var(--accent)}
  .hdr-actions{display:flex;gap:8px;flex-wrap:wrap}

  main{max-width:1500px;margin:0 auto;padding:20px 18px}
  .fam{background:var(--panel);border:1px solid var(--line);border-radius:12px;
    margin-bottom:14px;overflow:hidden}
  .fam-head{width:100%;display:flex;align-items:center;gap:16px;
    background:transparent;border:0;color:var(--ink);
    padding:15px 18px;text-align:left}
  .fam-head:hover{background:rgba(255,255,255,.02)}
  .fam-name{font-size:16px;font-weight:600;letter-spacing:.04em;min-width:210px}
  .fam-dots{display:flex;gap:5px;flex-wrap:wrap;flex:1}
  .fam-counts{color:var(--muted);font-size:12px;white-space:nowrap}
  .chev{color:var(--muted);transition:transform .18s ease;font-size:13px}
  .fam.open .chev{transform:rotate(90deg)}
  .fam-body{display:none;padding:4px 16px 18px;
    grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}
  .fam.open .fam-body{display:grid}

  @media(max-width:640px){
    main{padding:12px 10px}
    header{padding:14px 14px;gap:10px}
    .brand{font-size:18px}
    /* compact icon-only header buttons */
    .hdr-actions{width:100%;justify-content:flex-end;gap:6px}
    .toggle .tl{display:none}
    .toggle{padding:7px 10px;font-size:15px;line-height:1}
    .fam-head{flex-wrap:wrap;padding:12px 14px;gap:2px 0}
    .fam-name{width:100%;min-width:0;font-size:15px;margin-bottom:4px}
    .fam-dots{flex:1;gap:4px}
    .fam-dots .dot{width:9px;height:9px}
    .fam-counts{font-size:11px;white-space:nowrap;align-self:center}
    .fam-body{padding:4px 10px 14px;
      grid-template-columns:1fr}
  }

  .dot{width:11px;height:11px;border-radius:50%;display:inline-block;flex:none}
  .s-ok{background:var(--ok);box-shadow:0 0 7px rgba(52,211,153,.6)}
  .s-warn{background:var(--warn);box-shadow:0 0 7px rgba(245,185,66,.55)}
  .s-bad{background:var(--bad);box-shadow:0 0 8px rgba(255,84,112,.65)}
  .s-gray{background:var(--gray)}
  .dot.s-gray{animation:pulse-gray 1.4s ease-in-out infinite}
  @keyframes pulse-gray{0%,100%{opacity:.3}50%{opacity:1}}

  .dev{background:var(--panel2);border:1px solid var(--line);border-radius:10px;
    padding:13px 14px}
  .dev-top{display:flex;align-items:center;gap:9px;margin-bottom:3px}
  .dev-name{font-weight:600;font-size:14.5px}
  .pill{margin-left:auto;font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
    padding:3px 9px;border-radius:20px;border:1px solid var(--line);color:var(--muted)}
  .pill.s-ok{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.08)}
  .pill.s-warn{color:var(--warn);border-color:rgba(245,185,66,.4);background:rgba(245,185,66,.08)}
  .pill.s-bad{color:var(--bad);border-color:rgba(255,84,112,.4);background:rgba(255,84,112,.08)}
  .pill.s-gray{color:var(--muted)}
  .dev-net{color:var(--muted);font-size:11.5px;margin-bottom:7px}
  .dev-net .role{color:#aeb6c6}
  .dev-sum{font-size:13px;margin-bottom:9px}
  .kv{display:flex;justify-content:space-between;gap:10px;font-size:12px;
    padding:2px 0;border-top:1px dashed rgba(255,255,255,.05)}
  .kv .k{color:var(--muted)}
  .kv .val.ok{color:var(--ok)} .kv .val.warn{color:var(--warn)} .kv .val.bad{color:var(--bad)}
  .kv .val.gray{color:var(--muted)}
  .hint{margin-top:8px;padding:7px 9px;border-left:2px solid var(--accent);
    background:rgba(224,160,64,.06);color:#cdd3df;font-size:11.5px;border-radius:3px}
  details.raw{margin-top:9px;border-top:1px solid var(--line);padding-top:6px}
  details.raw>summary{cursor:pointer;color:var(--muted);font-size:11px;
    letter-spacing:.05em;list-style:none}
  details.raw>summary:hover{color:var(--accent)}
  details.raw>summary::before{content:"▸ ";}
  details.raw[open]>summary::before{content:"▾ ";}
  details.raw pre{margin:7px 0 0;background:#080a0d;border:1px solid var(--line);
    border-radius:6px;padding:8px;font-size:11px;color:#9aa3b3;
    white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto}
  .siglabel{color:var(--muted);font-size:10.5px;margin:9px 0 4px;letter-spacing:.05em}
  .sigrow{display:flex;gap:3px;flex-wrap:wrap}
  .sig{width:9px;height:9px;border-radius:2px}
  .board{margin-top:9px;padding-top:7px;border-top:1px solid var(--line)}
  .board .bname{font-size:11.5px;color:#aeb6c6}
  .rails-hdr{color:var(--muted);font-size:10.5px;letter-spacing:.08em;margin:9px 0 5px}
  .rails{display:flex;flex-wrap:wrap;gap:7px 14px;margin-bottom:4px}
  .rail{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted)}
  .cards-hdr{color:var(--muted);font-size:10.5px;letter-spacing:.08em;margin:9px 0 4px}
  .card-row-label{color:#aeb6c6;font-size:11.5px;margin:7px 0 3px}
  .carddots{display:flex;gap:3px;flex-wrap:wrap}
  .wins-hdr{color:var(--muted);font-size:10.5px;letter-spacing:.08em;margin:9px 0 4px}
  .win-row{display:flex;align-items:center;gap:8px;font-size:12px;
    padding:4px 0;border-top:1px dashed rgba(255,255,255,.05)}
  .win-n{color:var(--ink);font-weight:600;min-width:44px}
  .win-in{color:var(--muted);min-width:40px;font-size:11.5px}
  .win-type{min-width:74px;color:#aeb6c6}
  .win-std{font-size:11.5px}
  .err{color:var(--bad);font-size:11px;margin-top:6px;word-break:break-word}

  #logs{display:none;max-width:1500px;margin:0 auto;padding:0 18px}
  #logs.open{display:block}
  .logbox{background:#080a0d;border:1px solid var(--line);border-radius:10px;
    padding:12px 14px;font-size:12px;max-height:260px;overflow:auto}
  .logbox .ln{color:var(--muted)} .logbox .ln .t{color:var(--accent)}

  /* uptime badge */
  .uptime{font-size:10px;color:var(--muted);margin-left:6px;opacity:.7}
  .uptime.high{color:var(--ok)} .uptime.mid{color:var(--warn)} .uptime.low{color:var(--bad)}

  /* last seen */
  .last-seen{font-size:10.5px;color:var(--muted);margin-top:4px}
  .last-seen .ago{color:#9aa3b3}

  /* sparkline */
  .sparkline{display:flex;gap:2px;align-items:flex-end;height:20px;margin-top:6px}
  .spark-bar{width:4px;border-radius:1px;flex:none;min-height:2px}

  /* offline / powered-down handling */
  .off-banner{max-width:1500px;margin:14px auto 0;padding:0 18px;display:none}
  .off-banner.show{display:block}
  .off-banner-inner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
    background:rgba(69,75,88,.12);border:1px solid var(--line);border-radius:10px;
    padding:10px 16px;font-size:12.5px;color:var(--muted)}
  .off-banner-inner b{color:var(--ink)}
  .off-banner-inner .off-btn{margin-left:auto;background:var(--panel2);color:var(--muted);
    border:1px solid var(--line);border-radius:6px;padding:5px 11px;font-size:11.5px}
  .off-banner-inner .off-btn:hover{color:var(--ink);border-color:var(--accent)}
  .dev.powered-off{opacity:.55}
  .dev.powered-off .pill.s-bad{color:var(--muted);border-color:var(--line);background:transparent}
  .off-note{font-size:11.5px;color:var(--muted);margin-top:4px}
  @media(max-width:640px){.off-banner{padding:0 10px}}

  /* flash animation on status change */
  @keyframes statusflash{
    0%{box-shadow:0 0 0 0 rgba(255,255,255,.5)}
    70%{box-shadow:0 0 0 8px rgba(255,255,255,0)}
    100%{box-shadow:0 0 0 0 rgba(255,255,255,0)}
  }
  .dev.flash{animation:statusflash .6s ease-out}
</style></head>
<body>
<header>
  <div class="brand">🦖 <b>JOEBOT</b> LAB</div>
  <div class="meta">
    <span>status <span class="dot s-gray" id="hdr-dot"></span></span>
    <span>v<span class="v" id="hdr-ver">—</span></span>
    <span>last poll <span class="v" id="hdr-poll">—</span></span>
    <span>cycle <span class="v" id="hdr-dur">—</span></span>
    <span>polling <span class="v" id="hdr-int">—</span></span>
  </div>
  <div class="spacer"></div>
  <div class="hdr-actions">
    <button class="toggle" id="btn-sound" title="Toggle sound alerts">
      <span class="ti" id="snd-ico">🔕</span><span class="tl">Sound</span></button>
    <button class="toggle" id="btn-expand" title="Expand / collapse all families">
      <span class="ti">⊞</span><span class="tl" id="expand-lbl">Expand all</span></button>
    <button class="toggle" id="btn-logs" title="Show logs">
      <span class="ti">📜</span><span class="tl">Logs</span></button>
    <a href="/control/autoswitch" style="text-decoration:none">
      <button class="toggle" title="Auto-Switching"><span class="ti">🤖</span><span class="tl">Auto-Switch</span></button>
    </a>
    <a href="/config" style="text-decoration:none">
      <button class="toggle" title="Config"><span class="ti">⚙</span><span class="tl">Config</span></button>
    </a>
  </div>
</header>
<div class="off-banner" id="off-banner"><div class="off-banner-inner">
  <span>🔌 <b id="off-count">0</b> devices are unreachable and have <b>never been seen</b> since startup — rack powered down?</span>
  <button class="off-btn" id="off-toggle">Show full cards</button>
</div></div>
<main id="grid"></main>
<div id="logs"><div class="logbox" id="logbox"></div></div>

<script>
const OPEN_KEY="joebot_lab_open";
const SOUND_KEY="joebot_sound";
const OFFLINE_KEY="joebot_show_offline_full";
let openFams=new Set(JSON.parse(localStorage.getItem(OPEN_KEY)||"[]"));
let soundOn=localStorage.getItem(SOUND_KEY)==="1";
let showOfflineFull=localStorage.getItem(OFFLINE_KEY)==="1";
let pollMs=30000, built=false, lastData=null;
let _audioCtx=null;

// A device is "powered off" (vs faulted) if it's unreachable AND has never
// responded since the server started — collapse those to one quiet line.
function isPoweredOff(d){
  return d.status==="bad" && d.last_seen_ago==null;
}

// ── sound ────────────────────────────────────────────────────────────────────
function updateSoundBtn(){
  document.getElementById("snd-ico").textContent=soundOn?"🔔":"🔕";
}
document.getElementById("btn-sound").addEventListener("click",()=>{
  soundOn=!soundOn;
  localStorage.setItem(SOUND_KEY,soundOn?"1":"0");
  updateSoundBtn();
});
updateSoundBtn();

function playAlert(bad){
  if(!soundOn) return;
  try{
    if(!_audioCtx) _audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    const o=_audioCtx.createOscillator();
    const g=_audioCtx.createGain();
    o.connect(g);g.connect(_audioCtx.destination);
    o.type='sine';
    o.frequency.setValueAtTime(bad?440:660,_audioCtx.currentTime);
    o.frequency.exponentialRampToValueAtTime(bad?280:880,_audioCtx.currentTime+0.3);
    g.gain.setValueAtTime(0.18,_audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001,_audioCtx.currentTime+0.35);
    o.start();o.stop(_audioCtx.currentTime+0.35);
  }catch(e){}
}

// ── helpers ──────────────────────────────────────────────────────────────────
function esc(s){return (s==null?"":String(s)).replace(/[&<>]/g,
  c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function persist(){localStorage.setItem(OPEN_KEY,JSON.stringify([...openFams]));}

function fmtAgo(sec){
  if(sec==null) return "never seen";
  if(sec<60) return sec+"s ago";
  if(sec<3600) return Math.floor(sec/60)+"m ago";
  return Math.floor(sec/3600)+"h ago";
}

function sparklineHtml(history){
  if(!history||!history.length) return "";
  const cols={ok:"var(--ok)",warn:"var(--warn)",bad:"var(--bad)",gray:"var(--gray)"};
  const bars=history.slice(-40).map(s=>
    `<span class="spark-bar" style="background:${cols[s]||cols.gray};height:${
      s==="ok"?14:s==="bad"?20:s==="warn"?17:8}px" title="${s}"></span>`
  ).join("");
  return `<div class="sparkline">${bars}</div>`;
}

function uptimeBadge(pct){
  if(pct==null) return "";
  const cls=pct>=95?"high":pct>=80?"mid":"low";
  return `<span class="uptime ${cls}">${pct}%</span>`;
}

// ── device card body ─────────────────────────────────────────────────────────
function devBody(d){
  // Powered-off devices collapse to two quiet lines unless expanded
  if(isPoweredOff(d) && !showOfflineFull){
    return `<div class="dev-net"><span class="role">${esc(d.role)}</span> · `+
           `${esc(d.ip)}</div>`+
           `<div class="off-note">unreachable · never seen since startup</div>`;
  }
  let h="";
  h+=`<div class="dev-net"><span class="role">${esc(d.role)}</span> · `+
     `${esc(d.ip)} · ${esc(d.hostname)}</div>`;
  h+=`<div class="dev-sum">${esc(d.summary||"")}</div>`;

  const signalsInline=(d.details||[]).some(x=>x.type==='signals_here');
  (d.details||[]).forEach(x=>{
    if(x.type==='signals_here'){
      if(d.signals&&d.signals.length){
        const act=d.signals.filter(s=>s.state!=='gray').length;
        h+=`<div class="siglabel">SIGNAL (${act}/${d.signals.length})</div><div class="sigrow">`;
        d.signals.forEach(s=>h+=`<span class="sig s-${s.state}" title="${esc(s.label)}"></span>`);
        h+=`</div>`;}
    } else {
      h+=`<div class="kv"><span class="k">${esc(x.label)}</span>`+
         `<span class="val ${x.state||""}">${esc(x.value)}</span></div>`;
    }});
  if(!signalsInline&&d.signals&&d.signals.length){
    const sigLbl=d.signals_label||`SIGNAL (${d.signals.filter(s=>s.state!=="gray").length}/${d.signals.length})`;
    h+=`<div class="siglabel">${esc(sigLbl)}</div><div class="sigrow">`;
    d.signals.forEach(s=>h+=`<span class="sig s-${s.state}" title="${esc(s.label)}"${d.signals_label?` style="border-radius:50%;width:10px;height:10px"`:''} ></span>`);
    h+=`</div>`;}
  if(d.windows&&d.windows.length){
    const activeW=d.windows.filter(w=>w.state==='ok').length;
    h+=`<div class="wins-hdr">WINDOWS (${activeW}/${d.windows.length} active)</div>`;
    d.windows.forEach(w=>{
      const stdClass=w.state==='ok'?'ok':w.state==='warn'?'warn':'';
      h+=`<div class="win-row">
        <span class="sig s-${w.state}"></span>
        <span class="win-n">Win ${w.window}</span>
        <span class="win-in">In ${w.input||'—'}</span>
        <span class="win-type">${esc(w.type)}</span>
        <span class="win-std val ${stdClass}">${esc(w.std)}</span>
      </div>`;
    });}
  if(d.rail_dots&&d.rail_dots.length){
    h+=`<div class="rails-hdr">${esc(d.rail_dots_label||"RAILS")}</div><div class="rails">`;
    d.rail_dots.forEach(r=>{
      h+=`<span class="rail"><span class="sig s-${r.state}"></span>${esc(r.label)}</span>`;
    });
    h+='</div>';}
  if(d.card_rows&&d.card_rows.length){
    h+='<div class="cards-hdr">I/O CARDS</div>';
    d.card_rows.forEach(cr=>{
      const ok=cr.dots.filter(x=>x.state==='ok').length;
      const tot=cr.dots.length;
      h+=`<div class="card-row-label">${esc(cr.label)} — ${ok}/${tot} OK</div>`;
      h+='<div class="carddots">';
      cr.dots.forEach((cd,i)=>{
        h+=`<span class="sig s-${cd.state}" style="width:12px;height:12px;border-radius:3px" title="Card ${i+1}"></span>`;
      });
      h+='</div>';
    });}
  (d.boards||[]).forEach(b=>{
    if(b.audio){
      h+=`<div class="board"><div class="bname">Slot ${b.slot} · ${esc(b.label)} · <span style="color:var(--muted);font-style:italic">audio</span></div></div>`;
    } else {
      const act=b.signals.filter(s=>s.state!=="gray").length;
      h+=`<div class="board"><div class="bname">Slot ${b.slot} · plane ${b.plane} · ${esc(b.label)} (${act}/${b.signals.length})</div><div class="sigrow">`;
      b.signals.forEach(s=>h+=`<span class="sig s-${s.state}" title="${esc(s.label)}"></span>`);
      h+=`</div></div>`;
    }});
  if(Object.keys(d.meta||{}).length){
    for(const k in d.meta)
      h+=`<div class="kv"><span class="k">${esc(k)}</span><span class="val">${esc(d.meta[k])}</span></div>`;}
  if(d.hint) h+=`<div class="hint">${esc(d.hint)}</div>`;
  if(d.error) h+=`<div class="err">${esc(d.error)}</div>`;

  // last seen + sparkline (shown for all devices, useful context)
  if(d.last_seen_ago!=null||d.history){
    h+=`<div class="last-seen"><span class="ago">last seen ${fmtAgo(d.last_seen_ago)}</span></div>`;
    h+=sparklineHtml(d.history);
  }

  // Control button for controllable devices
  if(d.kind==='dms3600'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
      <a href="/control/dms" style="text-decoration:none">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(224,160,64,.12);
          color:var(--accent);border:1px solid rgba(224,160,64,.35);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Control Matrix →</button>
      </a></div>`;}
  if(d.kind==='matrix12800'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line);display:flex;gap:6px">
      <a href="/control/matrix12800" style="text-decoration:none;flex:1">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(52,211,153,.1);
          color:var(--ok);border:1px solid rgba(52,211,153,.3);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Route Matrix →</button>
      </a>
      <a href="/config/mtx" style="text-decoration:none;flex:1">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(59,130,246,.1);
          color:#93c5fd;border:1px solid rgba(59,130,246,.3);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">🗂 MTX Editor →</button>
      </a></div>`;}
  if(d.kind==='smx'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line);display:flex;gap:6px">
      <a href="/control/smx" style="text-decoration:none;flex:1">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(124,106,245,.1);
          color:#a78bfa;border:1px solid rgba(124,106,245,.35);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Route SMX →</button>
      </a>
      <a href="/control/autoswitch" style="text-decoration:none;flex:1">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(52,211,153,.08);
          color:#34d399;border:1px solid rgba(52,211,153,.3);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">🤖 Auto-Switch →</button>
      </a></div>`;}
  if(d.kind==='ipcp505'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
      <a href="/control/ipcp505" style="text-decoration:none">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(245,185,66,.08);
          color:#f5b942;border:1px solid rgba(245,185,66,.3);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Control IPCP →</button>
      </a></div>`;}
  if(d.kind==='dsc401a'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
      <a href="/control/dsc401" style="text-decoration:none">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(29,78,216,.1);
          color:#93c5fd;border:1px solid rgba(29,78,216,.35);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">📺 DSC 401 Monitor →</button>
      </a></div>`;}

  return h;
}

// ── build DOM shells once ────────────────────────────────────────────────────
function build(data){
  const grid=document.getElementById("grid");grid.innerHTML="";
  data.families.forEach(f=>{
    if(!f.dots.length) return; // hide empty families
    const fam=document.createElement("div");
    fam.className="fam"+(openFams.has(f.id)?" open":"");
    fam.id="fam-"+f.id;
    fam.innerHTML=
      `<button class="fam-head" data-fam="${f.id}">
         <span class="fam-name">${esc(f.name)}</span>
         <span class="fam-dots" id="famdots-${f.id}"></span>
         <span class="fam-counts" id="famcounts-${f.id}"></span>
         <span class="chev">▸</span>
       </button>
       <div class="fam-body" id="fambody-${f.id}"></div>`;
    grid.appendChild(fam);
    const body=fam.querySelector(".fam-body");
    f.dots.forEach(dt=>{
      const card=document.createElement("div");
      card.className="dev";card.id="dev-"+dt.id;
      card.innerHTML=`<div class="dev-top"><span class="dot" id="devdot-${dt.id}"></span>
        <span class="dev-name" id="devname-${dt.id}"></span>
        <span class="pill" id="devpill-${dt.id}"></span>
        <span id="devup-${dt.id}"></span></div>
        <div id="devbody-${dt.id}"></div>
        <details class="raw" id="rawwrap-${dt.id}" style="display:none">
          <summary>raw / debug</summary><pre id="rawpre-${dt.id}"></pre></details>`;
      body.appendChild(card);
    });
    // Auto-open families that have devices (so gray/scanning cards are visible)
    if(f.dots.length && !openFams.size){
      fam.classList.add("open"); openFams.add(f.id);
    }
    fam.querySelector(".fam-head").addEventListener("click",()=>{
      fam.classList.toggle("open");
      if(fam.classList.contains("open")) openFams.add(f.id); else openFams.delete(f.id);
      persist();
    });
  });
  built=true;
}

// ── patch live data into DOM ─────────────────────────────────────────────────
function patch(data){
  const m=data.meta||{};
  document.getElementById("hdr-poll").textContent=m.last_poll||"—";
  document.getElementById("hdr-dur").textContent=(m.duration!=null?m.duration+"s":"—");
  if(m.version) document.getElementById("hdr-ver").textContent=m.version;
  let worst="gray";const rank={bad:3,warn:2,ok:1,gray:0};
  data.families.forEach(f=>{if(rank[f.status]>rank[worst])worst=f.status;});
  document.getElementById("hdr-dot").className="dot s-"+worst;
  if(m.poll_seconds){pollMs=m.poll_seconds*1000;
    document.getElementById("hdr-int").textContent="every "+m.poll_seconds+"s";}

  data.families.forEach(f=>{
    const dots=document.getElementById("famdots-"+f.id);
    if(dots) dots.innerHTML=f.dots.map(d=>`<span class="dot s-${d.status}"></span>`).join("");
    const c=document.getElementById("famcounts-"+f.id);
    if(c){const p=[];if(f.counts.bad)p.push(f.counts.bad+" bad");
      if(f.counts.warn)p.push(f.counts.warn+" warn");
      p.push(f.counts.ok+" ok");if(f.counts.gray)p.push(f.counts.gray+" —");
      c.textContent=p.join(" · ")+`  (${f.total})`;}
  });

  for(const id in data.devices){
    const d=data.devices[id];
    const dot=document.getElementById("devdot-"+id);
    if(!dot) continue;

    // flash + sound on status change
    if(d.status_changed){
      const card=document.getElementById("dev-"+id);
      if(card){card.classList.remove("flash");void card.offsetWidth;card.classList.add("flash");}
      if(d.status==="bad"||d.status==="warn") playAlert(true);
      else if(d.status==="ok") playAlert(false);
    }

    dot.className="dot s-"+d.status;
    document.getElementById("devname-"+id).textContent=d.name;
    const card=document.getElementById("dev-"+id);
    const off=isPoweredOff(d)&&!showOfflineFull;
    if(card) card.classList.toggle("powered-off",off);
    const pill=document.getElementById("devpill-"+id);
    pill.className="pill s-"+d.status;
    pill.textContent=off?"OFF?":(({ok:"ONLINE",warn:"WARN",bad:"FAULT",gray:"SCANNING…"})[d.status]||d.status);

    // uptime badge next to pill
    const upEl=document.getElementById("devup-"+id);
    if(upEl) upEl.innerHTML=uptimeBadge(d.uptime_pct);

    document.getElementById("devbody-"+id).innerHTML=devBody(d);

    const rawWrap=document.getElementById("rawwrap-"+id);
    const rawPre=document.getElementById("rawpre-"+id);
    if(d.raw && Object.keys(d.raw).length && !off){
      rawWrap.style.display="block";
      rawPre.textContent=Object.entries(d.raw).map(([k,v])=>`${k}  ->  ${v}`).join("\n");
    } else { rawWrap.style.display="none"; }
  }

  // Powered-down banner
  const offCount=Object.values(data.devices).filter(isPoweredOff).length;
  document.getElementById("off-banner").classList.toggle("show",offCount>=3);
  document.getElementById("off-count").textContent=offCount;
}

// ── poll loop ────────────────────────────────────────────────────────────────
document.getElementById("off-toggle").addEventListener("click",()=>{
  showOfflineFull=!showOfflineFull;
  localStorage.setItem(OFFLINE_KEY,showOfflineFull?"1":"0");
  document.getElementById("off-toggle").textContent=
    showOfflineFull?"Collapse offline cards":"Show full cards";
  if(lastData) patch(lastData);
});
document.getElementById("off-toggle").textContent=
  showOfflineFull?"Collapse offline cards":"Show full cards";

async function refresh(){
  try{
    const r=await fetch("/api/status");const data=await r.json();
    lastData=data;
    // Rebuild DOM if: never built, new devices appeared, or families changed
    const needsBuild = !built ||
      data.families.some(f=>f.dots.some(d=>!document.getElementById("dev-"+d.id))) ||
      data.families.filter(f=>f.dots.length).length !== document.querySelectorAll(".fam").length;
    if(needsBuild) build(data);
    patch(data);
  }catch(e){console.error(e);}
}

async function loadLogs(){
  try{const r=await fetch("/api/logs");const d=await r.json();
    document.getElementById("logbox").innerHTML=
      d.logs.map(l=>`<div class="ln"><span class="t">${esc(l.t)}</span>  ${esc(l.msg)}</div>`).join("");
  }catch(e){}
}

document.getElementById("btn-logs").addEventListener("click",()=>{
  const el=document.getElementById("logs");el.classList.toggle("open");
  if(el.classList.contains("open")) loadLogs();
});
document.getElementById("btn-expand").addEventListener("click",()=>{
  const all=document.querySelectorAll(".fam");
  const anyClosed=[...all].some(f=>!f.classList.contains("open"));
  all.forEach(f=>{const id=f.id.replace("fam-","");
    if(anyClosed){f.classList.add("open");openFams.add(id);}
    else{f.classList.remove("open");openFams.delete(id);}});
  document.getElementById("expand-lbl").textContent=anyClosed?"Collapse all":"Expand all";
  persist();
});

setInterval(()=>{
  if(document.getElementById("logs").classList.contains("open")) loadLogs();
},6000);

refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""



# --------------------------------------------------------------------------- #
# Setup / Welcome  (/welcome  /setup  /api/setup/*)
# --------------------------------------------------------------------------- #

@app.get("/welcome", response_class=HTMLResponse)
def welcome_page():
    return HTMLResponse(WELCOME_HTML)


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    return HTMLResponse(WELCOME_HTML)


@app.get("/api/setup/state")
def setup_state():
    data = modules_store.load()
    devices = config_store.get_devices()
    return JSONResponse({
        "setup_complete": data.get("setup_complete", False),
        "enabled_modules": data.get("enabled_modules", []),
        "all_modules": modules_store.ALL_MODULES,
        "device_templates": modules_store.DEVICE_TEMPLATES,
        "device_count": len(devices),
    })


@app.post("/api/setup/modules")
async def setup_modules(request: Request):
    body = await request.json()
    ids = body.get("enabled", [])
    enabled = modules_store.set_enabled_modules(ids)
    log(f"Setup: modules saved — {enabled}")
    return JSONResponse({"ok": True, "enabled_modules": enabled})


@app.post("/api/setup/complete")
async def setup_complete():
    modules_store.complete_setup()
    log("Setup: marked complete")
    return JSONResponse({"ok": True})

@app.get("/api/setup/reset")
async def setup_reset():
    """Dev helper — wipe setup.json so the wizard shows again."""
    import os as _os
    p = modules_store.SETUP_PATH
    if _os.path.exists(p):
        _os.remove(p)
        log("Setup: reset (setup.json deleted)")
    return JSONResponse({"ok": True, "message": "Setup reset — reload / to see wizard"})


@app.post("/api/setup/add-device")
async def setup_add_device(request: Request):
    body = await request.json()
    template = body.get("template", "")
    tmpl = next((t for t in modules_store.DEVICE_TEMPLATES if t["template"] == template), None)
    if not tmpl:
        return JSONResponse({"error": "unknown template"}, status_code=400)
    import uuid
    pw = body.get("password") or None
    device = {
        "id": body.get("id") or template + "_" + uuid.uuid4().hex[:6],
        "name": body.get("name") or tmpl["name"],
        "ip":   (body.get("ip") or "").strip(),
        "port": int(body.get("port") or tmpl["port"]),
        "kind": tmpl["kind"],
        "family": "core",
        "role": tmpl["desc"],
        "policy": "strict",
        "meta": {},
    }
    if pw:
        device["password"] = pw
    device = _resolve_device_host(device)
    result, err = config_store.add_device(device)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    _prime_state()        # update layout so new device appears immediately
    _repoll_event.set()   # kick off a poll in the background while user finishes setup
    log(f"Setup: added device {device['name']} ({device['kind']}) @ {device['ip']}")
    return JSONResponse({"ok": True, "device": result})




WELCOME_HTML = '<!doctype html>\n<html lang="en"><head>\n<meta charset="utf-8"/>\n<meta name="viewport" content="width=device-width,initial-scale=1"/>\n<title>JOEBOT LAB · Setup</title>\n<style>\n:root{\n  --bg:#080a0e;--panel:#12151c;--panel2:#181c25;--line:#232836;\n  --ink:#e8ebf0;--muted:#6b7585;--faint:#2a3040;\n  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--accent:#7c6af5;\n  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;\n}\n*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\nbody{background:var(--bg);color:var(--ink);font-family:var(--mono);\n  min-height:100dvh;display:flex;flex-direction:column;overflow-x:hidden}\n\n/* ── scanlines texture ── */\nbody::before{content:\'\';position:fixed;inset:0;pointer-events:none;z-index:0;\n  background:repeating-linear-gradient(0deg,\n    transparent,transparent 2px,rgba(0,0,0,.08) 2px,rgba(0,0,0,.08) 4px)}\n\n/* ── step progress bar ── */\n.progress-bar{position:fixed;top:0;left:0;right:0;height:2px;z-index:50;\n  background:var(--faint)}\n.progress-fill{height:100%;background:var(--ok);transition:width .5s cubic-bezier(.4,0,.2,1)}\n\n/* ── step wrapper ── */\n.step{display:none;min-height:100dvh;flex-direction:column;\n  align-items:center;justify-content:center;padding:40px 20px;\n  position:relative;z-index:1}\n.step.active{display:flex}\n\n/* ── step 1: welcome ── */\n.welcome-wrap{text-align:center;max-width:640px}\n.logo-mark{font-size:clamp(48px,10vw,80px);font-weight:900;letter-spacing:-.02em;\n  line-height:1;margin-bottom:8px}\n.logo-mark .j{color:var(--ok)}\n.logo-mark .rest{color:var(--ink)}\n.logo-sub{font-size:clamp(11px,2vw,14px);letter-spacing:.35em;text-transform:uppercase;\n  color:var(--muted);margin-bottom:48px}\n.welcome-tagline{font-size:clamp(18px,3.5vw,26px);font-weight:700;\n  line-height:1.35;margin-bottom:16px;color:var(--ink)}\n.welcome-desc{font-size:clamp(13px,2vw,15px);color:var(--muted);\n  line-height:1.7;margin-bottom:48px;max-width:480px;margin-left:auto;margin-right:auto}\n.feature-row{display:flex;gap:24px;justify-content:center;\n  flex-wrap:wrap;margin-bottom:52px}\n.feat{text-align:center;min-width:90px}\n.feat-icon{font-size:24px;margin-bottom:6px}\n.feat-label{font-size:11px;color:var(--muted);letter-spacing:.05em}\n\n/* ── step 2: modules ── */\n.step-header{text-align:center;margin-bottom:36px;max-width:640px}\n.step-num{font-size:11px;letter-spacing:.2em;text-transform:uppercase;\n  color:var(--ok);margin-bottom:10px}\n.step-title{font-size:clamp(22px,4vw,32px);font-weight:800;margin-bottom:10px}\n.step-desc{font-size:13.5px;color:var(--muted);line-height:1.6}\n\n.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));\n  gap:10px;width:100%;max-width:900px;margin-bottom:36px}\n.mod-card{background:var(--panel);border:1.5px solid var(--line);border-radius:12px;\n  padding:14px 16px;cursor:pointer;transition:all .15s;\n  display:flex;gap:12px;align-items:flex-start;position:relative;user-select:none}\n.mod-card:hover{border-color:var(--muted)}\n.mod-card.selected{border-color:var(--ok);background:rgba(52,211,153,.06)}\n.mod-card.required{border-color:var(--faint);cursor:default;opacity:.7}\n.mod-card.required.selected{border-color:rgba(52,211,153,.3);opacity:1}\n.mod-icon{font-size:22px;flex-shrink:0;margin-top:1px}\n.mod-body{flex:1;min-width:0}\n.mod-name{font-size:13px;font-weight:700;margin-bottom:3px;\n  display:flex;align-items:center;gap:6px}\n.mod-badge{font-size:9px;letter-spacing:.06em;padding:1px 6px;border-radius:4px;\n  background:rgba(124,106,245,.15);color:var(--accent);border:1px solid rgba(124,106,245,.25)}\n.mod-desc{font-size:11.5px;color:var(--muted);line-height:1.5}\n.mod-check{position:absolute;top:12px;right:12px;\n  width:18px;height:18px;border-radius:5px;\n  border:1.5px solid var(--line);background:var(--panel2);\n  display:flex;align-items:center;justify-content:center;\n  font-size:11px;transition:all .12s}\n.mod-card.selected .mod-check{background:var(--ok);border-color:var(--ok);color:#000}\n.mod-card.required .mod-check{background:var(--faint);border-color:var(--faint);color:var(--muted)}\n\n/* ── step 3: devices ── */\n.template-grid{display:flex;flex-wrap:wrap;justify-content:center;\n  gap:8px;width:100%;max-width:900px;margin-bottom:28px}\n.tmpl-card{background:var(--panel);border:1.5px solid var(--line);border-radius:10px;\n  padding:14px 12px;cursor:pointer;transition:all .15s;text-align:center;\n  display:flex;flex-direction:column;align-items:center;gap:6px;user-select:none;\n  width:160px;flex-shrink:0}\n.tmpl-card:hover{border-color:var(--muted);background:var(--panel2)}\n.tmpl-card.selected{border-color:var(--accent);background:rgba(124,106,245,.08)}\n.tmpl-icon{font-size:26px}\n.tmpl-name{font-size:12px;font-weight:700;color:var(--ink)}\n.tmpl-desc{font-size:10px;color:var(--muted);line-height:1.4}\n\n.device-form{width:100%;max-width:580px;background:var(--panel);\n  border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;\n  display:none}\n.device-form.visible{display:block}\n.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}\n.form-row.one{grid-template-columns:1fr}\n.field label{display:block;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;\n  color:var(--muted);margin-bottom:4px}\n.field input{width:100%;background:var(--panel2);border:1px solid var(--line);\n  color:var(--ink);border-radius:7px;padding:8px 10px;\n  font-family:var(--mono);font-size:13px;transition:border-color .15s}\n.field input:focus{outline:none;border-color:var(--accent)}\n.field input::placeholder{color:var(--faint)}\n.form-actions{display:flex;gap:8px;margin-top:6px}\n\n.device-list{width:100%;max-width:580px;display:flex;flex-direction:column;gap:6px;\n  margin-bottom:20px}\n.dev-item{background:var(--panel2);border:1px solid var(--line);border-radius:8px;\n  padding:10px 14px;display:flex;align-items:center;gap:10px}\n.dev-item-icon{font-size:18px}\n.dev-item-name{font-size:13px;font-weight:600;flex:1}\n.dev-item-ip{font-size:11px;color:var(--muted)}\n.dev-item-del{background:none;border:none;color:var(--muted);cursor:pointer;\n  font-size:16px;padding:2px 6px;border-radius:4px}\n.dev-item-del:hover{color:var(--bad);background:rgba(255,84,112,.1)}\n\n/* ── step 4: done ── */\n.done-wrap{text-align:center;max-width:560px}\n.done-icon{font-size:64px;margin-bottom:20px}\n.done-title{font-size:clamp(24px,4vw,36px);font-weight:800;margin-bottom:12px}\n.done-desc{font-size:14px;color:var(--muted);line-height:1.7;margin-bottom:36px}\n.done-summary{background:var(--panel);border:1px solid var(--line);border-radius:12px;\n  padding:20px;text-align:left;margin-bottom:36px;width:100%}\n.done-row{display:flex;align-items:center;gap:10px;padding:7px 0;\n  border-bottom:1px solid var(--faint);font-size:13px}\n.done-row:last-child{border-bottom:none}\n.done-row-icon{font-size:16px;width:24px;text-align:center}\n.done-row-label{color:var(--muted);flex:1}\n.done-row-val{color:var(--ok);font-weight:600}\n\n/* ── shared buttons ── */\n.btn-primary{font-family:var(--mono);cursor:pointer;font-size:14px;font-weight:700;\n  letter-spacing:.06em;padding:13px 36px;border-radius:10px;border:none;\n  background:var(--ok);color:#061a12;transition:all .15s}\n.btn-primary:hover{background:#4aedb0;transform:translateY(-1px);\n  box-shadow:0 6px 24px rgba(52,211,153,.3)}\n.btn-primary:active{transform:translateY(0)}\n.btn-secondary{font-family:var(--mono);cursor:pointer;font-size:13px;\n  padding:10px 22px;border-radius:8px;\n  background:transparent;color:var(--muted);border:1px solid var(--line)}\n.btn-secondary:hover{color:var(--ink);border-color:var(--muted)}\n.btn-accent{font-family:var(--mono);cursor:pointer;font-size:13px;font-weight:600;\n  padding:9px 20px;border-radius:8px;border:none;\n  background:var(--accent);color:#fff;transition:all .15s}\n.btn-accent:hover{background:#9585f8}\n.btn-small{font-family:var(--mono);cursor:pointer;font-size:12px;\n  padding:7px 14px;border-radius:7px;\n  background:var(--panel2);color:var(--muted);border:1px solid var(--line)}\n.btn-small:hover{color:var(--ink);border-color:var(--muted)}\n.btn-row{display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap}\n.btn-row.left{justify-content:flex-start}\n\n/* ── corner skip link ── */\n.skip-link{position:fixed;bottom:20px;right:20px;z-index:50;\n  font-size:11.5px;color:var(--muted);cursor:pointer;\n  background:var(--panel);border:1px solid var(--line);\n  border-radius:6px;padding:5px 12px;text-decoration:none;transition:all .15s}\n.skip-link:hover{color:var(--ink);border-color:var(--muted)}\n\n/* ── glow effects ── */\n.glow-ok{text-shadow:0 0 30px rgba(52,211,153,.4)}\n.glow-accent{text-shadow:0 0 30px rgba(124,106,245,.4)}\n\n/* ── toast ── */\n.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);\n  background:var(--panel2);border:1px solid var(--line);border-radius:8px;\n  padding:9px 18px;font-size:12.5px;opacity:0;transition:opacity .25s;\n  pointer-events:none;z-index:200;white-space:nowrap}\n.toast.show{opacity:1}\n\n/* ── responsive ── */\n@media(max-width:500px){\n  .feature-row{gap:16px}\n  .form-row{grid-template-columns:1fr}\n  .module-grid{grid-template-columns:1fr}\n}\n</style></head>\n<body>\n\n<div class="progress-bar"><div class="progress-fill" id="prog" style="width:0%"></div></div>\n\n<!-- STEP 1: WELCOME -->\n<div class="step active" id="step-1">\n  <div class="welcome-wrap">\n    <div class="logo-mark glow-ok">\n      <span class="j">J</span><span class="rest">OEBOT LAB</span>\n    </div>\n    <div class="logo-sub">AV rack control system</div>\n\n    <div class="welcome-tagline">Modern control for classic AV gear.</div>\n    <div class="welcome-desc">\n      A local-first dashboard for Extron and pro AV equipment.\n      Run one Docker container, open a browser, and take control of gear\n      that deserves better software than it shipped with.\n    </div>\n\n    <div class="feature-row">\n      <div class="feat"><div class="feat-icon">📊</div><div class="feat-label">Live status</div></div>\n      <div class="feat"><div class="feat-icon">⚡</div><div class="feat-label">Route & control</div></div>\n      <div class="feat"><div class="feat-icon">🗂</div><div class="feat-label">Config editor</div></div>\n      <div class="feat"><div class="feat-icon">🤖</div><div class="feat-label">Auto-switching</div></div>\n      <div class="feat"><div class="feat-icon">🐳</div><div class="feat-label">Docker-packaged</div></div>\n    </div>\n\n    <div class="btn-row">\n      <button class="btn-primary" onclick="goStep(2)">Get Started →</button>\n      <button class="btn-secondary" onclick="skipSetup()">Skip setup</button>\n    </div>\n  </div>\n</div>\n\n<!-- STEP 2: MODULES -->\n<div class="step" id="step-2">\n  <div class="step-header">\n    <div class="step-num">Step 1 of 3</div>\n    <div class="step-title">What do you want to use?</div>\n    <div class="step-desc">\n      Enable the modules that match your gear. Disabled modules have zero overhead —\n      no polling, no background tasks, nothing.\n    </div>\n  </div>\n  <div class="module-grid" id="module-grid"></div>\n  <div class="btn-row">\n    <button class="btn-secondary" onclick="goStep(1)">← Back</button>\n    <button class="btn-primary" onclick="saveModulesAndNext()">Next →</button>\n  </div>\n</div>\n\n<!-- STEP 3: DEVICES -->\n<div class="step" id="step-3">\n  <div class="step-header">\n    <div class="step-num">Step 2 of 3</div>\n    <div class="step-title">Add your devices</div>\n    <div class="step-desc">\n      Pick a device type, fill in the name and IP. You can add more anytime from the device manager.\n    </div>\n  </div>\n\n  <div class="device-list" id="device-list"></div>\n\n  <div class="template-grid" id="template-grid"></div>\n\n  <div class="device-form" id="device-form">\n    <div class="form-row">\n      <div class="field">\n        <label>Device name</label>\n        <input id="f-name" placeholder="e.g. Main Matrix"/>\n      </div>\n      <div class="field">\n        <label>IP / Hostname</label>\n        <input id="f-ip" placeholder="10.0.0.12 or device.local"/>\n      </div>\n    </div>\n    <div class="form-row">\n      <div class="field">\n        <label>Port</label>\n        <input id="f-port" placeholder="23" value="23"/>\n      </div>\n      <div class="field">\n        <label>Password <span style="color:var(--faint);font-size:9px">if required</span></label>\n        <input id="f-password" placeholder="leave blank if none" autocomplete="off"/>\n      </div>\n    </div>\n    <div class="form-row one">\n      <div class="field" style="display:flex;gap:8px">\n        <button class="btn-accent" onclick="addDevice()">+ Add Device</button>\n        <button class="btn-small" onclick="cancelForm()">Cancel</button>\n      </div>\n    </div>\n  </div>\n\n  <div class="btn-row" style="margin-top:8px">\n    <button class="btn-secondary" onclick="goStep(2)">← Back</button>\n    <button class="btn-primary" onclick="finishSetup()">\n      <span id="next-lbl">Skip for now →</span>\n    </button>\n  </div>\n</div>\n\n<!-- STEP 4: DONE -->\n<div class="step" id="step-4">\n  <div class="done-wrap">\n    <div class="done-icon">🚀</div>\n    <div class="done-title glow-ok">Lab is ready.</div>\n    <div class="done-desc">Taking you to the dashboard…</div>\n  </div>\n</div>\n\n<a class="skip-link" onclick="skipSetup()">Skip → Dashboard</a>\n<div class="toast" id="toast"></div>\n\n<script>\n// ── state ──────────────────────────────────────────────────────────────────\nlet currentStep = 1;\nlet allModules = [];\nlet allTemplates = [];\nlet filteredTemplates = [];\nlet enabledModules = new Set();\nlet addedDevices = [];\nlet selectedTemplate = null;\n\nconst STEP_PROGRESS = {1:0, 2:33, 3:66, 4:100};\n\n// ── toast ──────────────────────────────────────────────────────────────────\nlet _tt;\nfunction toast(msg, dur=2400){\n  const el = document.getElementById(\'toast\');\n  el.textContent = msg; el.classList.add(\'show\');\n  clearTimeout(_tt); _tt = setTimeout(()=>el.classList.remove(\'show\'), dur);\n}\n\n// ── step navigation ────────────────────────────────────────────────────────\nfunction goStep(n){\n  document.getElementById(\'step-\'+currentStep).classList.remove(\'active\');\n  currentStep = n;\n  document.getElementById(\'step-\'+n).classList.add(\'active\');\n  document.getElementById(\'prog\').style.width = STEP_PROGRESS[n] + \'%\';\n  window.scrollTo(0,0);\n}\n\n// ── finish setup ───────────────────────────────────────────────────────────\nasync function finishSetup(){\n  goStep(4);\n  try{ await fetch(\'/api/setup/complete\',{method:\'POST\'}); }catch(e){}\n  window.location.href = \'/\';\n}\n\n// ── module grid ────────────────────────────────────────────────────────────\nfunction renderModules(){\n  const box = document.getElementById(\'module-grid\');\n  box.innerHTML = \'\';\n  allModules.forEach(m => {\n    const on = enabledModules.has(m.id);\n    const req = m.required;\n    const card = document.createElement(\'div\');\n    card.className = \'mod-card\' + (on?\' selected\':\'\') + (req?\' required\':\'\');\n    card.dataset.id = m.id;\n    card.innerHTML = `\n      <div class="mod-icon">${m.icon||\'🔧\'}</div>\n      <div class="mod-body">\n        <div class="mod-name">${esc(m.name)}${m.badge?`<span class="mod-badge">${esc(m.badge)}</span>`:\'\'}${req?\'<span class="mod-badge" style="background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.2)">required</span>\':\'\'}</div>\n        <div class="mod-desc">${esc(m.desc)}</div>\n      </div>\n      <div class="mod-check">${on?(req?\'—\':\'✓\'):\'\'}</div>`;\n    if(!req) card.addEventListener(\'click\', ()=>toggleModule(m.id));\n    box.appendChild(card);\n  });\n}\n\nfunction toggleModule(id){\n  if(enabledModules.has(id)) enabledModules.delete(id);\n  else enabledModules.add(id);\n  renderModules();\n}\n\nasync function saveModulesAndNext(){\n  try{\n    await fetch(\'/api/setup/modules\',{method:\'POST\',\n      headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({enabled:[...enabledModules]})});\n  }catch(e){ toast(\'Could not save modules, continuing anyway\'); }\n  // Filter templates to those matching enabled modules. Templates without a\n  // module (MGP 464, Custom SIS) are always available — they are dashboard-only.\n  filteredTemplates = allTemplates.filter(t => !t.module || enabledModules.has(t.module));\n  renderTemplates();\n  goStep(3);\n}\n\n// ── template grid ──────────────────────────────────────────────────────────\nfunction renderTemplates(){\n  const box = document.getElementById(\'template-grid\');\n  box.innerHTML = \'\';\n  filteredTemplates.forEach(t => {\n    const card = document.createElement(\'div\');\n    card.className = \'tmpl-card\' + (selectedTemplate===t.template?\' selected\':\'\');\n    card.dataset.tmpl = t.template;\n    card.innerHTML = `\n      <div class="tmpl-icon">${t.icon||\'🔧\'}</div>\n      <div class="tmpl-name">${esc(t.name)}</div>\n      <div class="tmpl-desc">${esc(t.desc)}</div>`;\n    card.addEventListener(\'click\', ()=>selectTemplate(t));\n    box.appendChild(card);\n  });\n}\n\nfunction selectTemplate(t){\n  selectedTemplate = t.template;\n  renderTemplates();\n  document.getElementById(\'f-name\').value = t.name;\n  document.getElementById(\'f-port\').value = t.port;\n  document.getElementById(\'f-ip\').value = \'\';\n  document.getElementById(\'f-password\').value = t.default_password||\'\';\n  document.getElementById(\'device-form\').classList.add(\'visible\');\n  setTimeout(()=>document.getElementById(\'f-ip\').focus(), 80);\n}\n\nfunction cancelForm(){\n  selectedTemplate = null;\n  renderTemplates();\n  document.getElementById(\'device-form\').classList.remove(\'visible\');\n}\n\nasync function addDevice(){\n  const name     = document.getElementById(\'f-name\').value.trim();\n  const ip       = document.getElementById(\'f-ip\').value.trim();\n  const port     = parseInt(document.getElementById(\'f-port\').value) || 23;\n  const password = document.getElementById(\'f-password\').value.trim() || null;\n  if(!name || !ip){ toast(\'Name and IP are required\'); return; }\n  if(!selectedTemplate){ toast(\'Select a device type\'); return; }\n  try{\n    const r = await fetch(\'/api/setup/add-device\',{method:\'POST\',\n      headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({template:selectedTemplate, name, ip, port, password})});\n    const j = await r.json();\n    if(!j.ok){ toast(\'Error: \'+(j.error||\'unknown\')); return; }\n    const tmpl = allTemplates.find(t=>t.template===selectedTemplate);\n    const displayHost = j.device?.hostname || j.device?.ip || ip;\n    addedDevices.push({name, ip:displayHost, icon:tmpl?.icon||\'🔧\', kind:tmpl?.name||selectedTemplate});\n    renderDeviceList();\n    cancelForm();\n    updateNextLabel();\n    toast(`Added ${name}` + (j.device?.hostname ? ` → ${j.device.ip}` : \'\'));\n  }catch(e){ toast(\'Error: \'+e.message); }\n}\n\nfunction renderDeviceList(){\n  const box = document.getElementById(\'device-list\');\n  box.innerHTML = \'\';\n  addedDevices.forEach((d,i)=>{\n    const div = document.createElement(\'div\');\n    div.className = \'dev-item\';\n    div.innerHTML = `<span class="dev-item-icon">${d.icon}</span>\n      <span class="dev-item-name">${esc(d.name)}</span>\n      <span class="dev-item-ip">${esc(d.ip)}</span>\n      <button class="dev-item-del" onclick="removeDevice(${i})" title="Remove">×</button>`;\n    box.appendChild(div);\n  });\n}\n\nfunction removeDevice(i){\n  addedDevices.splice(i,1);\n  renderDeviceList();\n  updateNextLabel();\n}\n\nfunction updateNextLabel(){\n  document.getElementById(\'next-lbl\').textContent =\n    addedDevices.length > 0 ? \'Next →\' : \'Skip for now →\';\n}\n\nasync function skipSetup(){\n  await finishSetup();\n}\n\n// ── helpers ────────────────────────────────────────────────────────────────\nfunction esc(s){ return String(s||\'\').replace(/[&<>"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\'}[c])); }\n\n// ── init ───────────────────────────────────────────────────────────────────\n(async()=>{\n  try{\n    const r = await fetch(\'/api/setup/state\');\n    const j = await r.json();\n    allModules   = j.all_modules   || [];\n    allTemplates = j.device_templates || [];\n    enabledModules = new Set(j.enabled_modules || []);\n    renderModules();\n    filteredTemplates = allTemplates;\n    renderTemplates();\n    // If they already have devices, pre-populate the added list\n    if(j.device_count > 0){\n      document.getElementById(\'next-lbl\').textContent = \'Next →\';\n    }\n  }catch(e){\n    console.error(\'setup state load failed\', e);\n    toast(\'Could not load setup data\');\n  }\n})();\n</script>\n</body></html>'



# --------------------------------------------------------------------------- #
# Route modules
# --------------------------------------------------------------------------- #
from routes_dms          import router as _dms_router
from routes_mtx_config   import router as _mtx_config_router
from routes_matrix12800  import router as _matrix12800_router
from routes_smx          import router as _smx_router
from routes_ipcp505      import router as _ipcp505_router
from routes_ir           import router as _ir_router
from routes_vsc          import router as _vsc_router
from routes_mtpx         import router as _mtpx_router
from routes_dsc401       import router as _dsc401_router
from routes_autoswitch   import router as _autoswitch_router, start_engine as _start_autoswitch

app.include_router(_dms_router)
app.include_router(_mtx_config_router)
app.include_router(_matrix12800_router)
app.include_router(_smx_router)
app.include_router(_ipcp505_router)
app.include_router(_ir_router)
app.include_router(_vsc_router)
app.include_router(_mtpx_router)
app.include_router(_dsc401_router)
app.include_router(_autoswitch_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
