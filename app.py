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
VERSION = "2.4.0"   # bump this on every deploy so you can confirm the new code is running

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
_lock      = threading.Lock()
_state     = {"devices": {}, "families": [], "meta": {}}
_logs      = collections.deque(maxlen=300)
_history   = {}   # id -> deque of "ok"/"bad"/"warn"/"gray" strings
_uptime    = {}   # id -> {"polls": int, "online": int}
_last_seen = {}   # id -> epoch float of last successful poll
_prev_status = {} # id -> last known status string (for flash detection)


def log(msg):
    _logs.appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg})


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


# --------------------------------------------------------------------------- #
# DMS 3600 control endpoints  (V2 — write commands allowed)
# --------------------------------------------------------------------------- #
def _dms_device():
    """Find the DMS device config from config_store."""
    for d in config_store.get_devices():
        if d.get("kind") == "dms3600":
            return d
    return None


@app.get("/control/dms", response_class=HTMLResponse)
def control_dms():
    return HTMLResponse(DMS_HTML)


@app.get("/api/control/dms/state")
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


@app.post("/api/control/dms/tie")
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


@app.post("/api/control/dms/ties-batch")
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


@app.post("/api/control/dms/preset")
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


@app.get("/api/control/dms/names")
def dms_names_get():
    return JSONResponse(dms_names.load())


@app.put("/api/control/dms/names")
async def dms_names_put(request: Request):
    body = await request.json()
    data = dms_names.load()
    for section in ("inputs", "outputs", "presets"):
        if section in body:
            for k, v in body[section].items():
                data[section][str(k)] = str(v)[:32].strip()
    dms_names.save(data)
    return JSONResponse({"ok": True})


@app.post("/api/control/dms/rename")
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


@app.post("/api/control/dms/poll-names")
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


# --------------------------------------------------------------------------- #
# Config page
# --------------------------------------------------------------------------- #
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
    border-radius:7px;padding:7px 12px;font-size:12.5px}
  .toggle:hover{color:var(--ink);border-color:var(--accent)}

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
  <button class="toggle" id="btn-sound" title="Toggle sound alerts">🔕 Sound</button>
  <button class="toggle" id="btn-expand">Expand all</button>
  <button class="toggle" id="btn-logs">Logs</button>
  <a href="/config" style="text-decoration:none">
    <button class="toggle">⚙ Config</button>
  </a>
</header>
<main id="grid"></main>
<div id="logs"><div class="logbox" id="logbox"></div></div>

<script>
const OPEN_KEY="joebot_lab_open";
const SOUND_KEY="joebot_sound";
let openFams=new Set(JSON.parse(localStorage.getItem(OPEN_KEY)||"[]"));
let soundOn=localStorage.getItem(SOUND_KEY)==="1";
let pollMs=30000, built=false;
let _audioCtx=null;

// ── sound ────────────────────────────────────────────────────────────────────
function updateSoundBtn(){
  document.getElementById("btn-sound").textContent=soundOn?"🔔 Sound":"🔕 Sound";
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
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
      <a href="/control/smx" style="text-decoration:none">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(124,106,245,.1);
          color:#a78bfa;border:1px solid rgba(124,106,245,.35);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Route SMX →</button>
      </a></div>`;}
  if(d.kind==='ipcp505'){
    h+=`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--line)">
      <a href="/control/ipcp505" style="text-decoration:none">
        <button style="font-family:var(--mono);cursor:pointer;background:rgba(245,185,66,.08);
          color:#f5b942;border:1px solid rgba(245,185,66,.3);border-radius:7px;
          padding:6px 14px;font-size:12.5px;width:100%">⚡ Control IPCP →</button>
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
    const pill=document.getElementById("devpill-"+id);
    pill.className="pill s-"+d.status;
    pill.textContent=({ok:"ONLINE",warn:"WARN",bad:"FAULT",gray:"SCANNING…"})[d.status]||d.status;

    // uptime badge next to pill
    const upEl=document.getElementById("devup-"+id);
    if(upEl) upEl.innerHTML=uptimeBadge(d.uptime_pct);

    document.getElementById("devbody-"+id).innerHTML=devBody(d);

    const rawWrap=document.getElementById("rawwrap-"+id);
    const rawPre=document.getElementById("rawpre-"+id);
    if(d.raw && Object.keys(d.raw).length){
      rawWrap.style.display="block";
      rawPre.textContent=Object.entries(d.raw).map(([k,v])=>`${k}  ->  ${v}`).join("\n");
    } else { rawWrap.style.display="none"; }
  }
}

// ── poll loop ────────────────────────────────────────────────────────────────
async function refresh(){
  try{
    const r=await fetch("/api/status");const data=await r.json();
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
document.getElementById("btn-expand").addEventListener("click",e=>{
  const all=document.querySelectorAll(".fam");
  const anyClosed=[...all].some(f=>!f.classList.contains("open"));
  all.forEach(f=>{const id=f.id.replace("fam-","");
    if(anyClosed){f.classList.add("open");openFams.add(id);}
    else{f.classList.remove("open");openFams.delete(id);}});
  e.target.textContent=anyClosed?"Collapse all":"Expand all";persist();
});

setInterval(()=>{
  if(document.getElementById("logs").classList.contains("open")) loadLogs();
},6000);

refresh();
setInterval(refresh, 5000);
</script>
</body></html>"""


DMS_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<title>Joebot Lab · DMS 3600</title>
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
    font-family:var(--mono);font-size:14px;line-height:1.4;
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


# --------------------------------------------------------------------------- #
# MTX Config Editor  (/config/mtx)
# --------------------------------------------------------------------------- #

from fastapi.responses import Response as _Response

@app.get("/config/mtx", response_class=HTMLResponse)
def config_mtx():
    return HTMLResponse(MTX_HTML)


@app.post("/api/mtx/parse")
async def api_mtx_parse(request: Request):
    body = await request.json()
    text = body.get("text", "")
    try:
        model = mtx_engine.parse_text(text)
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/template")
async def api_mtx_template(request: Request):
    body = await request.json()
    try:
        model = mtx_engine.create_template(
            int(body.get("size_in", 8)),
            int(body.get("size_out", 8)),
            body.get("plane", "LC"),
            int(body.get("width", 1)),
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/serialize")
async def api_mtx_serialize(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        text = mtx_engine.build_text(model)
        return _Response(content=text, media_type="text/plain")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/remap")
async def api_mtx_remap(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_remap(
            model,
            body.get("kind", "Input"),
            int(body.get("virt_i", 1)),
            body.get("code", "04"),
            [int(x) for x in body.get("phys", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/add")
async def api_mtx_add(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_add(model, body.get("blocks", []))
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/delete")
async def api_mtx_delete(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_delete(
            model,
            [int(x) for x in body.get("del_vin", [])],
            [int(x) for x in body.get("del_vout", [])],
            bool(body.get("compact", True)),
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/reorder")
async def api_mtx_reorder(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_reorder(
            model,
            [int(x) for x in body.get("vin_order", [])],
            [int(x) for x in body.get("vout_order", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/mtx/merge-rgb")
async def api_mtx_merge(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_merge_rgb(
            model,
            [int(x) for x in body.get("in_phys", [])],
            [int(x) for x in body.get("out_phys", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


MTX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Joebot Lab · MTX Editor</title>
<style>
  :root{
    --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
    --ink:#e8ebf0;--muted:#8b93a3;--accent:#e0a040;
    --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--gray:#454b58;
    --blue:#3b82f6;--blue-dim:rgba(59,130,246,.12);
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:var(--mono);font-size:14px;line-height:1.5;padding-bottom:60px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    padding:14px 20px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(59,130,246,.05),transparent)}
  .brand{font-size:19px;font-weight:700;letter-spacing:.1em;color:#93c5fd}
  nav a{color:var(--muted);text-decoration:none;font-size:12px;padding:4px 9px;
    border-radius:6px;border:1px solid transparent}
  nav a:hover,nav a.active{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  main{max-width:1400px;margin:0 auto;padding:18px}
  button{font-family:var(--mono);cursor:pointer;font-size:13px}
  .btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:6px 13px}
  .btn:hover{border-color:var(--accent);color:var(--accent)}
  .btn-blue{background:var(--blue-dim);color:#93c5fd;border-color:rgba(59,130,246,.35)}
  .btn-blue:hover{background:rgba(59,130,246,.2)}
  .btn-ok{background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.35)}
  .btn-ok:hover{background:rgba(52,211,153,.2)}
  .btn-bad{background:rgba(255,84,112,.1);color:var(--bad);border-color:rgba(255,84,112,.3)}
  .btn-bad:hover{background:rgba(255,84,112,.2)}
  .btn-warn{background:rgba(245,185,66,.1);color:var(--warn);border-color:rgba(245,185,66,.3)}
  .btn-warn:hover{background:rgba(245,185,66,.2)}

  /* info bar */
  .info-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:10px 16px;margin-bottom:16px;font-size:13px}
  .info-bar .chip{background:var(--panel2);border:1px solid var(--line);
    border-radius:20px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .info-bar .chip span{color:var(--ink)}
  #file-name{color:#93c5fd;font-weight:600}

  /* toolbar */
  .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}

  /* tabs */
  .tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:16px}
  .tab{background:transparent;color:var(--muted);border:1px solid transparent;
    border-bottom:none;border-radius:7px 7px 0 0;padding:7px 16px;font-size:13px}
  .tab:hover{color:var(--ink)}
  .tab.active{background:var(--panel);color:#93c5fd;border-color:var(--line);
    border-bottom-color:var(--panel)}
  .tab-pane{display:none}
  .tab-pane.active{display:block}

  /* port map */
  .map-wrap{display:flex;gap:16px;flex-wrap:wrap}
  .map-section{flex:1;min-width:300px}
  .map-title{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--muted);margin-bottom:8px}
  canvas{border:1px solid var(--line);border-radius:6px;display:block;width:100%;
    cursor:crosshair}
  .legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:12px;
    color:var(--muted)}
  .legend-item{display:flex;align-items:center;gap:5px}
  .legend-swatch{width:14px;height:14px;border-radius:3px;flex-shrink:0}

  /* form elements */
  .field{margin-bottom:12px}
  .field label{display:block;color:var(--muted);font-size:11.5px;
    margin-bottom:4px;letter-spacing:.04em}
  .field input,.field select{width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:7px 10px;
    font-family:var(--mono);font-size:13px}
  .field input:focus,.field select:focus{outline:none;border-color:#3b82f6}
  .field select option{background:var(--panel2)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .section-box{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:14px 16px;margin-bottom:14px}
  .section-box h3{margin:0 0 12px;font-size:13px;font-weight:600;color:#93c5fd;
    letter-spacing:.04em}

  /* list boxes */
  .list-pair{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .list-box{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    height:220px;overflow-y:auto}
  .list-item{padding:5px 10px;font-size:12px;cursor:pointer;
    display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.04)}
  .list-item:hover{background:rgba(255,255,255,.04)}
  .list-item.selected{background:rgba(59,130,246,.15);border-left:2px solid #3b82f6}
  .list-item .sig-dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}

  /* reorder list */
  .reorder-list{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    height:240px;overflow-y:auto;padding:4px}
  .reorder-item{padding:5px 8px;font-size:12px;cursor:grab;
    display:flex;align-items:center;gap:8px;border-radius:5px;
    border:1px solid transparent;margin-bottom:2px;user-select:none}
  .reorder-item:hover{background:rgba(255,255,255,.05)}
  .reorder-item.dragging{opacity:.4}
  .reorder-item.drag-over{border-color:#3b82f6}
  .reorder-item .sig-dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}
  .reorder-btns{display:flex;gap:6px;margin-top:6px}

  /* preview box */
  .preview{background:#080a0d;border:1px solid var(--line);border-radius:7px;
    padding:10px 12px;font-size:12px;color:#9aa3b3;margin-top:8px;
    white-space:pre-wrap;word-break:break-all;max-height:160px;overflow:auto}

  /* toast */
  .toast{position:fixed;bottom:24px;right:24px;background:var(--panel2);
    border:1px solid var(--line);border-radius:8px;padding:10px 16px;
    font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}

  /* empty state */
  .empty{text-align:center;padding:60px 20px;color:var(--muted)}
  .empty h2{font-size:18px;margin-bottom:8px;color:#93c5fd}
  .empty p{font-size:13px;line-height:1.7}

  /* modal */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
    z-index:100;align-items:center;justify-content:center;padding:20px}
  .overlay.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:24px;width:100%;max-width:480px}
  .modal h3{margin:0 0 18px;font-size:15px;font-weight:600}
  .modal-foot{display:flex;gap:10px;justify-content:flex-end;
    margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
  .check-row{display:flex;align-items:center;gap:8px;font-size:13px;
    cursor:pointer;padding:4px 0}
  .check-row input{accent-color:#3b82f6;width:15px;height:15px}
  @media(max-width:640px){
    .row2,.row3,.list-pair{grid-template-columns:1fr}
    .map-wrap{flex-direction:column}
  }
</style></head>
<body>
<header>
  <div class="brand">🗂 MTX EDITOR</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/config">Config</a>
    <a href="/config/mtx" class="active">MTX</a>
  </nav>
  <div class="spacer"></div>
  <span id="file-name" style="font-size:12px;color:var(--muted)">No file loaded</span>
</header>

<main>
  <!-- toolbar -->
  <div class="toolbar">
    <label class="btn btn-blue" style="cursor:pointer">
      📂 Open .MTX
      <input type="file" id="file-input" accept=".MTX,.mtx" style="display:none"/>
    </label>
    <button class="btn" onclick="openTemplate()">📋 From Template</button>
    <button class="btn btn-ok" id="btn-download" onclick="downloadMtx()" disabled>💾 Download .MTX</button>
    <button class="btn" id="btn-preview" onclick="togglePreview()" disabled>👁 Preview text</button>
  </div>

  <!-- info bar -->
  <div class="info-bar" id="info-bar" style="display:none">
    <div class="chip">size <span id="info-size">—</span></div>
    <div class="chip">plane <span id="info-plane">—</span></div>
    <div class="chip">V-In <span id="info-vin">—</span></div>
    <div class="chip">V-Out <span id="info-vout">—</span></div>
    <div class="chip">phys in <span id="info-hi">—</span></div>
    <div class="chip">phys out <span id="info-ho">—</span></div>
  </div>

  <!-- preview -->
  <div id="preview-box" class="preview" style="display:none;margin-bottom:14px"></div>

  <!-- empty state -->
  <div class="empty" id="empty-state">
    <h2>No .MTX file loaded</h2>
    <p>Open an existing Extron .MTX file or create one from a template.<br/>
    All editing happens locally — nothing is sent to the Matrix 12800 automatically.<br/>
    Download the modified file and upload it to the switcher when ready.</p>
  </div>

  <!-- editor (hidden until model loaded) -->
  <div id="editor" style="display:none">
    <!-- tabs -->
    <div class="tabs">
      <button class="tab active" onclick="showTab('port-map')">Port Map</button>
      <button class="tab" onclick="showTab('remap')">Remap</button>
      <button class="tab" onclick="showTab('add')">Add I/O</button>
      <button class="tab" onclick="showTab('delete')">Delete</button>
      <button class="tab" onclick="showTab('reorder')">Reorder</button>
      <button class="tab" onclick="showTab('merge')">Merge RGB</button>
    </div>

    <!-- PORT MAP tab -->
    <div class="tab-pane active" id="tab-port-map">
      <div class="map-wrap">
        <div class="map-section">
          <div class="map-title">Physical Inputs (0i001–0i128)</div>
          <canvas id="canvas-in" height="200"></canvas>
        </div>
        <div class="map-section">
          <div class="map-title">Physical Outputs (0o001–0o128)</div>
          <canvas id="canvas-out" height="200"></canvas>
        </div>
      </div>
      <div class="legend">
        <div class="legend-item"><div class="legend-swatch" style="background:#ef4444"></div>R (RGB ch1)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#22c55e"></div>G (RGB ch2)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#3b82f6"></div>B (RGB ch3)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#6b7280"></div>Y / Composite</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#a855f7"></div>C (S-Video)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#1b1f28;border:1px solid #262b36"></div>Unused</div>
      </div>
    </div>

    <!-- REMAP tab -->
    <div class="tab-pane" id="tab-remap">
      <div class="section-box">
        <h3>Remap a Virtual I/O</h3>
        <div class="row3">
          <div class="field">
            <label>Type</label>
            <select id="remap-kind" onchange="populateRemapIndex()">
              <option value="Input">Input</option>
              <option value="Output">Output</option>
            </select>
          </div>
          <div class="field">
            <label>Virtual Index</label>
            <select id="remap-virt"></select>
          </div>
          <div class="field">
            <label>New Signal Type</label>
            <select id="remap-signal" onchange="updateRemapSignal()">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01">RGB (3-wide)</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>Physical Ports — comma-separated numbers (e.g. 1,2,3 for RGB) — leave blank to auto-append at tail</label>
          <input id="remap-phys" placeholder="e.g. 42,43,44  (or blank = append at highest+1)"/>
        </div>
        <div id="remap-current" class="preview" style="margin-bottom:10px">Select a virtual above</div>
        <button class="btn btn-blue" onclick="doRemap()">Apply Remap</button>
      </div>
    </div>

    <!-- ADD tab -->
    <div class="tab-pane" id="tab-add">
      <div class="row2">
        <div class="section-box">
          <h3>Add Inputs</h3>
          <div class="field"><label>Count</label>
            <input id="add-in-count" type="number" value="1" min="1" max="200"/></div>
          <div class="field"><label>Signal Type</label>
            <select id="add-in-signal">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01" selected>RGB (3-wide)</option>
            </select></div>
          <div class="field"><label>Starting Physical Port (blank = auto-tail)</label>
            <input id="add-in-start" placeholder="e.g. 70"/></div>
          <label class="check-row">
            <input type="checkbox" id="add-in-en" checked/> Include inputs in this batch
          </label>
        </div>
        <div class="section-box">
          <h3>Add Outputs</h3>
          <div class="field"><label>Count</label>
            <input id="add-out-count" type="number" value="1" min="1" max="200"/></div>
          <div class="field"><label>Signal Type</label>
            <select id="add-out-signal">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01" selected>RGB (3-wide)</option>
            </select></div>
          <div class="field"><label>Starting Physical Port (blank = auto-tail)</label>
            <input id="add-out-start" placeholder="e.g. 70"/></div>
          <label class="check-row">
            <input type="checkbox" id="add-out-en" checked/> Include outputs in this batch
          </label>
        </div>
      </div>
      <button class="btn btn-blue" onclick="doAdd()">Add I/O</button>
    </div>

    <!-- DELETE tab -->
    <div class="tab-pane" id="tab-delete">
      <div class="section-box">
        <h3>Delete Virtual I/Os</h3>
        <p style="font-size:12px;color:var(--muted);margin:0 0 10px">
          Click to select/deselect (multi-select supported). Red = selected for deletion.</p>
        <div class="list-pair">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Inputs</div>
            <div class="list-box" id="del-in-list"></div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Outputs</div>
            <div class="list-box" id="del-out-list"></div>
          </div>
        </div>
        <label class="check-row" style="margin-bottom:10px">
          <input type="checkbox" id="del-compact" checked/> Compact indices after delete (renumber 1..N)
        </label>
        <div style="display:flex;gap:8px">
          <button class="btn btn-bad" onclick="doDelete()">Delete Selected</button>
          <button class="btn" onclick="clearDelSelection()">Clear Selection</button>
        </div>
      </div>
    </div>

    <!-- REORDER tab -->
    <div class="tab-pane" id="tab-reorder">
      <div class="section-box">
        <h3>Reorder Virtuals — drag rows or use ↑↓ buttons
          <label class="check-row" style="display:inline-flex;margin-left:16px;font-weight:400;font-size:12px">
            <input type="checkbox" id="smart-groups" checked onchange="smartGroups=this.checked;populateReorderLists()"/>
            Smart grouping (link sequential Composite buddies)
          </label>
        </h3>
        <div class="row2">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Inputs</div>
            <div class="reorder-list" id="reorder-in-list"></div>
            <div class="reorder-btns">
              <button class="btn" onclick="moveSelected('in',-1)">↑ Up</button>
              <button class="btn" onclick="moveSelected('in',1)">↓ Down</button>
            </div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Outputs</div>
            <div class="reorder-list" id="reorder-out-list"></div>
            <div class="reorder-btns">
              <button class="btn" onclick="moveSelected('out',-1)">↑ Up</button>
              <button class="btn" onclick="moveSelected('out',1)">↓ Down</button>
            </div>
          </div>
        </div>
        <div style="margin-top:12px">
          <button class="btn btn-blue" onclick="doReorder()">Apply Reorder</button>
        </div>
      </div>
    </div>

    <!-- MERGE RGB tab -->
    <div class="tab-pane" id="tab-merge">
      <div class="section-box">
        <h3>Merge 3 Composite Virtuals → 1 RGB Virtual</h3>
        <p style="font-size:12px;color:var(--muted);margin:0 0 14px">
          Finds the virtual that owns each physical port, deletes all 3, and creates one new RGB virtual
          mapped to those 3 ports. Compacts indices afterward.</p>
        <div class="row2">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Input physical ports (R, G, B)</div>
            <div class="row3">
              <div class="field"><label>R port</label><input id="merge-in-r" type="number" min="1" max="128" placeholder="e.g. 1"/></div>
              <div class="field"><label>G port</label><input id="merge-in-g" type="number" min="1" max="128" placeholder="e.g. 2"/></div>
              <div class="field"><label>B port</label><input id="merge-in-b" type="number" min="1" max="128" placeholder="e.g. 3"/></div>
            </div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Output physical ports (R, G, B)</div>
            <div class="row3">
              <div class="field"><label>R port</label><input id="merge-out-r" type="number" min="1" max="128" placeholder="e.g. 1"/></div>
              <div class="field"><label>G port</label><input id="merge-out-g" type="number" min="1" max="128" placeholder="e.g. 2"/></div>
              <div class="field"><label>B port</label><input id="merge-out-b" type="number" min="1" max="128" placeholder="e.g. 3"/></div>
            </div>
          </div>
        </div>
        <label class="check-row" style="margin-bottom:12px">
          <input type="checkbox" id="merge-no-out"/> No output side (input only merge)
        </label>
        <button class="btn btn-warn" onclick="doMerge()">⚠ Merge → RGB</button>
      </div>
    </div>
  </div>
</main>

<!-- Template modal -->
<div class="overlay" id="tpl-modal">
  <div class="modal">
    <h3>New from Template</h3>
    <div class="row2">
      <div class="field"><label>Inputs</label>
        <input id="tpl-in" type="number" value="69" min="1" max="256"/></div>
      <div class="field"><label>Outputs</label>
        <input id="tpl-out" type="number" value="69" min="1" max="256"/></div>
    </div>
    <div class="row2">
      <div class="field"><label>Plane type</label>
        <select id="tpl-plane">
          <option value="LC">LC (composite / S-Video)</option>
          <option value="RGB">RGB</option>
        </select></div>
      <div class="field"><label>Default signal type</label>
        <select id="tpl-signal">
          <option value="1">Composite (1-wide)</option>
          <option value="2">S-Video (2-wide)</option>
          <option value="3" selected>RGB (3-wide)</option>
        </select></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeTpl()">Cancel</button>
      <button class="btn btn-blue" onclick="createTemplate()">Create</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let model = null;
let filename = 'output.MTX';
let delSelIn = new Set();
let delSelOut = new Set();
let reorderIn = [];   // list of {i, code, label, phys}
let reorderOut = [];
let reorderSelIn = null;
let reorderSelOut = null;

// ── helpers ──────────────────────────────────────────────────────────────────
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function sigColor(code, idx){
  if(code==='01') return ['#ef4444','#22c55e','#3b82f6'][idx]||'#3b82f6';
  if(code==='05') return ['#6b7280','#a855f7'][idx]||'#a855f7';
  return '#6b7280';
}
function sigLabel(code){ return {01:'RGB',05:'S-Vid',04:'Comp'}[code]||code; }

let _toastT;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(_toastT); _toastT=setTimeout(()=>el.classList.remove('show'),2400);
}

// ── model load / update ──────────────────────────────────────────────────────
function setModel(m, fname){
  model = m;
  if(fname) filename = fname;
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('editor').style.display = 'block';
  document.getElementById('info-bar').style.display = 'flex';
  document.getElementById('btn-download').disabled = false;
  document.getElementById('btn-preview').disabled = false;
  document.getElementById('file-name').textContent = filename;
  updateInfoBar();
  drawMaps();
  populateRemapIndex();
  populateDeleteLists();
  populateReorderLists();
}

function updateInfoBar(){
  if(!model) return;
  document.getElementById('info-size').textContent =
    (model.size||[0,0]).join('×');
  document.getElementById('info-plane').textContent = model.plane_type||'—';
  document.getElementById('info-vin').textContent   = (model.vin||[]).length;
  document.getElementById('info-vout').textContent  = (model.vout||[]).length;
  document.getElementById('info-hi').textContent    = model.highest_in||0;
  document.getElementById('info-ho').textContent    = model.highest_out||0;
}

// ── file open ────────────────────────────────────────────────────────────────
document.getElementById('file-input').addEventListener('change', async e => {
  const file = e.target.files[0]; if(!file) return;
  const text = await file.text();
  const r = await fetch('/api/mtx/parse', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text})});
  const j = await r.json();
  if(!j.ok){ toast('Parse error: '+j.error); return; }
  setModel(j.model, file.name);
  toast('Loaded: '+file.name);
});

// ── download ─────────────────────────────────────────────────────────────────
async function downloadMtx(){
  if(!model) return;
  const r = await fetch('/api/mtx/serialize', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model})});
  const text = await r.text();
  const blob = new Blob([text], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename.replace(/\.mtx$/i,'')+'_modified.MTX';
  a.click();
  toast('Downloaded!');
}

// ── preview ──────────────────────────────────────────────────────────────────
let previewVisible = false;
async function togglePreview(){
  const box = document.getElementById('preview-box');
  previewVisible = !previewVisible;
  if(previewVisible && model){
    const r = await fetch('/api/mtx/serialize', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({model})});
    box.textContent = await r.text();
  }
  box.style.display = previewVisible ? 'block' : 'none';
}

// ── template modal ───────────────────────────────────────────────────────────
function openTemplate(){ document.getElementById('tpl-modal').classList.add('open'); }
function closeTpl()    { document.getElementById('tpl-modal').classList.remove('open'); }
document.getElementById('tpl-modal').addEventListener('click', e => {
  if(e.target===document.getElementById('tpl-modal')) closeTpl();
});
async function createTemplate(){
  const body = {
    size_in:  parseInt(document.getElementById('tpl-in').value)||8,
    size_out: parseInt(document.getElementById('tpl-out').value)||8,
    plane:    document.getElementById('tpl-plane').value,
    width:    parseInt(document.getElementById('tpl-signal').value)||1,
  };
  const r = await fetch('/api/mtx/template', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  closeTpl();
  setModel(j.model, 'template.MTX');
  toast('Template created');
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function showTab(id){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  event.target.classList.add('active');
  if(id==='port-map') drawMaps();
}

// ── port map canvas ──────────────────────────────────────────────────────────
function buildUsage(items, kind){
  const usage = {};
  for(const rec of items){
    for(let ci=0; ci<rec.phys.length; ci++){
      usage[rec.phys[ci]] = {code: rec.code, ci, vi: rec.i, label: rec.label};
    }
  }
  return usage;
}

function drawMap(canvasId, usage){
  const canvas = document.getElementById(canvasId);
  const W = canvas.offsetWidth || 640;
  canvas.width  = W * devicePixelRatio;
  canvas.height = Math.round(W * 0.35) * devicePixelRatio;
  canvas.style.height = Math.round(W * 0.35) + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const cols=16, rows=8, total=128;
  const cw = W/cols, ch = (W*0.35)/rows;
  ctx.fillStyle = '#0c0e12';
  ctx.fillRect(0,0,W,W);
  for(let n=1; n<=total; n++){
    const r=(n-1)>>4, c=(n-1)&15;
    const x=c*cw+1, y=r*ch+1, w=cw-2, h=ch-2;
    const u = usage[n];
    ctx.fillStyle = u ? sigColor(u.code, u.ci) : '#1b1f28';
    ctx.fillRect(x,y,w,h);
    ctx.fillStyle = u ? 'rgba(0,0,0,.5)' : '#262b36';
    ctx.font = `${Math.min(10, cw*0.42)}px monospace`;
    ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText(n, x+w/2, y+h/2);
  }
}

function drawMaps(){
  if(!model) return;
  drawMap('canvas-in',  buildUsage(model.vin||[],  'Input'));
  drawMap('canvas-out', buildUsage(model.vout||[], 'Output'));
}
window.addEventListener('resize', ()=>{ if(model) drawMaps(); });

// ── remap tab ────────────────────────────────────────────────────────────────
function populateRemapIndex(){
  if(!model) return;
  const kind = document.getElementById('remap-kind').value;
  const items = kind==='Input' ? model.vin : model.vout;
  const sel = document.getElementById('remap-virt');
  sel.innerHTML = items.map(r=>
    `<option value="${r.i}">${r.i} — ${kind} ${String(r.label).padStart(3,'0')} (${sigLabel(r.code)}) phys:[${r.phys.join(',')}]</option>`
  ).join('');
  showCurrentRemap();
}

function showCurrentRemap(){
  if(!model) return;
  const kind = document.getElementById('remap-kind').value;
  const vi = parseInt(document.getElementById('remap-virt').value);
  const items = kind==='Input' ? model.vin : model.vout;
  const rec = items.find(r=>r.i===vi);
  const box = document.getElementById('remap-current');
  if(!rec){ box.textContent='(not found)'; return; }
  const pfx = kind==='Input'?'0i':'0o';
  box.textContent = `Current: ${kind} ${String(rec.label).padStart(3,'0')} (V-${rec.i})\n`+
    `Signal: ${sigLabel(rec.code)}  code=${rec.code}\n`+
    `Physical: ${rec.phys.map(p=>pfx+String(p).padStart(3,'0')).join(', ')||'(none)'}`;
  document.getElementById('remap-signal').value = rec.code;
}

document.getElementById('remap-virt').addEventListener('change', showCurrentRemap);
document.getElementById('remap-kind').addEventListener('change', populateRemapIndex);

function updateRemapSignal(){}

async function doRemap(){
  if(!model) return;
  const kind   = document.getElementById('remap-kind').value;
  const vi     = parseInt(document.getElementById('remap-virt').value);
  const code   = document.getElementById('remap-signal').value;
  const w      = {01:3, '01':3, '05':2, 05:2}[code] || 1;
  const physRaw= document.getElementById('remap-phys').value.trim();
  let phys;
  if(physRaw){
    phys = physRaw.split(/[\s,]+/).map(Number).filter(Boolean);
    if(phys.length !== w){ toast(`Need exactly ${w} port(s) for ${sigLabel(code)}`); return; }
  } else {
    // auto-tail: take highest used port and increment
    const hi = kind==='Input' ? (model.highest_in||0) : (model.highest_out||0);
    phys = Array.from({length:w}, (_,i)=>hi+i+1);
  }
  const r = await fetch('/api/mtx/remap', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, kind, virt_i: vi, code, phys})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast(`Remapped ${kind} ${vi} → ${sigLabel(code)}`);
  populateRemapIndex();
}

// ── add tab ──────────────────────────────────────────────────────────────────
async function doAdd(){
  if(!model) return;
  const addIn  = document.getElementById('add-in-en').checked;
  const addOut = document.getElementById('add-out-en').checked;
  if(!addIn && !addOut){ toast('Nothing to add — check at least one side'); return; }
  const block = {
    add_in:    addIn,
    count_in:  parseInt(document.getElementById('add-in-count').value)||1,
    win:       parseInt(document.getElementById('add-in-signal').value==='01'?3:
                        document.getElementById('add-in-signal').value==='05'?2:1),
    add_out:   addOut,
    count_out: parseInt(document.getElementById('add-out-count').value)||1,
    wout:      parseInt(document.getElementById('add-out-signal').value==='01'?3:
                        document.getElementById('add-out-signal').value==='05'?2:1),
  };
  // signal select returns code string; convert to width
  const inCode  = document.getElementById('add-in-signal').value;
  const outCode = document.getElementById('add-out-signal').value;
  block.win  = {01:3,'01':3,'05':2,05:2,'04':1,04:1}[inCode]  ||1;
  block.wout = {01:3,'01':3,'05':2,05:2,'04':1,04:1}[outCode] ||1;

  const inStart  = parseInt(document.getElementById('add-in-start').value);
  const outStart = parseInt(document.getElementById('add-out-start').value);
  if(!isNaN(inStart) && inStart>0 && addIn){
    block.phys_in = Array.from({length:block.win},(_,i)=>inStart+i);
    block.count_in = 1; // manual port = single at that position
  }
  if(!isNaN(outStart) && outStart>0 && addOut){
    block.phys_out = Array.from({length:block.wout},(_,i)=>outStart+i);
    block.count_out = 1;
  }
  const r = await fetch('/api/mtx/add', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, blocks:[block]})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Added I/O');
}

// ── delete tab ───────────────────────────────────────────────────────────────
function populateDeleteLists(){
  if(!model) return;
  delSelIn.clear(); delSelOut.clear();
  renderDeleteList('del-in-list',  model.vin||[],  'Input',  delSelIn);
  renderDeleteList('del-out-list', model.vout||[], 'Output', delSelOut);
}
function renderDeleteList(id, items, kind, selSet){
  const box = document.getElementById(id);
  box.innerHTML = items.map(r=>{
    const col = sigColor(r.code,0);
    return `<div class="list-item" data-vi="${r.i}" onclick="toggleDelSel(this,'${kind}')">
      <span class="sig-dot" style="background:${col}"></span>
      V${r.i} — ${kind} ${String(r.label).padStart(3,'0')} (${sigLabel(r.code)}) [${r.phys.join(',')}]
    </div>`;
  }).join('');
}
function toggleDelSel(el, kind){
  const vi = parseInt(el.dataset.vi);
  const set = kind==='Input' ? delSelIn : delSelOut;
  if(set.has(vi)){ set.delete(vi); el.classList.remove('selected'); el.style.color=''; }
  else           { set.add(vi);    el.classList.add('selected');    el.style.color='var(--bad)'; }
}
function clearDelSelection(){
  delSelIn.clear(); delSelOut.clear();
  document.querySelectorAll('#del-in-list .list-item, #del-out-list .list-item').forEach(el=>{
    el.classList.remove('selected'); el.style.color='';
  });
}
async function doDelete(){
  if(!model) return;
  if(!delSelIn.size && !delSelOut.size){ toast('Select items to delete first'); return; }
  const compact = document.getElementById('del-compact').checked;
  if(!confirm(`Delete ${delSelIn.size} input(s) + ${delSelOut.size} output(s)?`)) return;
  const r = await fetch('/api/mtx/delete', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, del_vin:[...delSelIn], del_vout:[...delSelOut], compact})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast(`Deleted — ${j.model.vin.length} inputs, ${j.model.vout.length} outputs remain`);
}

// ── reorder tab ──────────────────────────────────────────────────────────────
let smartGroups = true;  // buddy-aware grouping

// Detect groups of consecutive Composite virtuals with sequential physical ports
// (e.g. [phys:1],[phys:2],[phys:3] → one RGB group; [phys:4],[phys:5] → S-Vid group)
function detectGroups(items){
  const groups = [];
  let i = 0;
  while(i < items.length){
    const r = items[i];
    if(smartGroups && r.code === '04' && r.phys.length === 1){
      const p0 = r.phys[0];
      const next1 = items[i+1];
      const next2 = items[i+2];
      // RGB group: 3 consecutive Composite with sequential ports
      if(next2 && next1.code==='04' && next2.code==='04' &&
         next1.phys.length===1 && next2.phys.length===1 &&
         next1.phys[0]===p0+1 && next2.phys[0]===p0+2){
        groups.push({type:'rgb3', items:[items[i],items[i+1],items[i+2]], startIdx:i});
        i+=3; continue;
      }
      // S-Video group: 2 consecutive Composite with sequential ports
      if(next1 && next1.code==='04' && next1.phys.length===1 && next1.phys[0]===p0+1){
        groups.push({type:'svid2', items:[items[i],items[i+1]], startIdx:i});
        i+=2; continue;
      }
    }
    groups.push({type:'single', items:[r], startIdx:i});
    i++;
  }
  return groups;
}

function populateReorderLists(){
  if(!model) return;
  reorderIn  = (model.vin||[]).map(r=>({...r}));
  reorderOut = (model.vout||[]).map(r=>({...r}));
  reorderSelIn = null; reorderSelOut = null;
  renderReorderList('reorder-in-list',  reorderIn,  'in');
  renderReorderList('reorder-out-list', reorderOut, 'out');
}

function renderReorderList(id, items, side){
  const box = document.getElementById(id);
  const groups = detectGroups(items);
  box.innerHTML = groups.map((g, gi)=>{
    const isGroup = g.type !== 'single';
    const typeLabel = g.type==='rgb3' ? '🔴🟢🔵 RGB group' : g.type==='svid2' ? '⬜🟣 S-Vid group' : '';
    const col0 = sigColor(g.items[0].code, 0);
    const physAll = g.items.flatMap(r=>r.phys).join(',');
    const label = isGroup
      ? `${typeLabel} — V${g.items.map(r=>r.i).join('+')} [${physAll}]`
      : `V${g.items[0].i} — ${sigLabel(g.items[0].code)} [${g.items[0].phys.join(',')}]`;
    const border = isGroup ? 'border:1px solid rgba(224,160,64,.3);background:rgba(224,160,64,.05)' : '';
    return `<div class="reorder-item" data-gi="${gi}" data-side="${side}" style="${border}"
      onclick="selectReorderGroup(this,'${side}')" draggable="true"
      ondragstart="rDragStart(event,'${side}')" ondragover="rDragOver(event)"
      ondrop="rDrop(event,'${side}',${gi})" ondragleave="rDragLeave(event)">
      ⠿ <span class="sig-dot" style="background:${col0}"></span>
      ${label}
    </div>`;
  }).join('');
}

// Store groups for drag/move operations
function getGroups(side){ return detectGroups(side==='in' ? reorderIn : reorderOut); }

function selectReorderGroup(el, side){
  document.querySelectorAll(`.reorder-item[data-side="${side}"]`).forEach(e=>e.style.background='');
  el.style.background='rgba(59,130,246,.2)';
  if(side==='in') reorderSelIn=parseInt(el.dataset.gi);
  else reorderSelOut=parseInt(el.dataset.gi);
}

function moveSelected(side, delta){
  const list = side==='in' ? reorderIn : reorderOut;
  const groups = getGroups(side);
  const selGi = side==='in' ? reorderSelIn : reorderSelOut;
  if(selGi===null||selGi===undefined) return;
  const newGi = selGi + delta;
  if(newGi<0 || newGi>=groups.length) return;

  // Swap the group's items with the adjacent group's items in the flat list
  const gA = groups[selGi];
  const gB = groups[newGi];
  const flatA = gA.items; const flatB = gB.items;
  const startA = gA.startIdx; const startB = gB.startIdx;

  // Rebuild list by splicing groups
  const newList = [...list];
  if(delta > 0){
    // A is before B: remove A's items, insert after B
    newList.splice(startA, flatA.length);
    const insertAt = startA + flatB.length;
    newList.splice(insertAt, 0, ...flatA);
  } else {
    // A is after B: remove A, insert before B
    newList.splice(startA, flatA.length);
    newList.splice(startB, 0, ...flatA);
  }
  if(side==='in'){ reorderIn=newList; reorderSelIn=newGi; }
  else           { reorderOut=newList; reorderSelOut=newGi; }
  renderReorderList(side==='in'?'reorder-in-list':'reorder-out-list', newList, side);
  // restore selection highlight
  const el=document.querySelector(`.reorder-item[data-side="${side}"][data-gi="${newGi}"]`);
  if(el) el.style.background='rgba(59,130,246,.2)';
}

// drag-and-drop (group-aware)
let rDragGi=null, rDragSide=null;
function rDragStart(e, side){
  rDragGi=parseInt(e.currentTarget.dataset.gi); rDragSide=side;
  e.dataTransfer.effectAllowed='move';
  setTimeout(()=>e.currentTarget.classList.add('dragging'),0);
}
function rDragOver(e){ e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function rDragLeave(e){ e.currentTarget.classList.remove('drag-over'); }
function rDrop(e, side, dstGi){
  e.preventDefault(); e.currentTarget.classList.remove('drag-over');
  if(rDragSide!==side || rDragGi===dstGi) return;
  const list = side==='in' ? reorderIn : reorderOut;
  const groups = getGroups(side);
  const gSrc = groups[rDragGi]; const gDst = groups[dstGi];
  const newList = [...list];
  const srcItems = gSrc.items;
  // Remove source group, insert before/after destination
  if(rDragGi < dstGi){
    newList.splice(gSrc.startIdx, srcItems.length);
    const insertAt = gDst.startIdx - srcItems.length + gDst.items.length;
    newList.splice(insertAt, 0, ...srcItems);
  } else {
    newList.splice(gSrc.startIdx, srcItems.length);
    newList.splice(gDst.startIdx, 0, ...srcItems);
  }
  if(side==='in') reorderIn=newList; else reorderOut=newList;
  renderReorderList(side==='in'?'reorder-in-list':'reorder-out-list', newList, side);
  rDragGi=null;
}

async function doReorder(){
  if(!model) return;
  const r = await fetch('/api/mtx/reorder', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model,
      vin_order:  reorderIn.map(r=>r.i),
      vout_order: reorderOut.map(r=>r.i),
    })});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Reorder applied');
}

// ── merge RGB tab ────────────────────────────────────────────────────────────
async function doMerge(){
  if(!model) return;
  const noOut = document.getElementById('merge-no-out').checked;
  const inPhys  = ['r','g','b'].map(c=>parseInt(document.getElementById('merge-in-'+c).value)||0).filter(Boolean);
  const outPhys = noOut ? [] : ['r','g','b'].map(c=>parseInt(document.getElementById('merge-out-'+c).value)||0).filter(Boolean);
  if(inPhys.length!==3 && inPhys.length>0){ toast('Need 3 input ports'); return; }
  if(!noOut && outPhys.length!==3){ toast('Need 3 output ports (or check "no output side")'); return; }
  if(!confirm('This will delete the 3 Composite virtuals and create one RGB virtual. Continue?')) return;
  const r = await fetch('/api/mtx/merge-rgb', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, in_phys: inPhys, out_phys: outPhys})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Merged to RGB');
}

// ── init ─────────────────────────────────────────────────────────────────────
// Canvas resize observer
new ResizeObserver(()=>{ if(model) drawMaps(); }).observe(document.getElementById('editor'));
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# Matrix 12800 control endpoints  (/control/matrix12800)
# --------------------------------------------------------------------------- #

def _mtx12800_device():
    for d in config_store.get_devices():
        if d.get("kind") == "matrix12800":
            return d
    return None


@app.get("/control/matrix12800", response_class=HTMLResponse)
def control_matrix12800():
    return HTMLResponse(MTX12800_HTML)


@app.get("/api/control/matrix12800/info")
def mtx12800_info():
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    info, n_in, n_out, err = matrix12800_control.poll_info(dev["ip"], dev.get("port", 23))
    return JSONResponse({"ok": err is None, "info": info,
                         "n_inputs": n_in, "n_outputs": n_out, "error": err})


@app.get("/api/control/matrix12800/ties")
def mtx12800_ties():
    dev = _mtx12800_device()
    if not dev:
        return JSONResponse({"error": "Matrix 12800 not found"}, status_code=404)
    names = matrix12800_names.load()
    n_out = int(names.get("_n_outputs", 128))
    ties, err = matrix12800_control.poll_ties(dev["ip"], dev.get("port", 23), n_outputs=n_out)
    return JSONResponse({"ok": err is None, "ties": {str(k): v for k, v in ties.items()}, "error": err})


@app.post("/api/control/matrix12800/tie")
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


@app.post("/api/control/matrix12800/ties-batch")
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


@app.post("/api/control/matrix12800/preset")
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


@app.get("/api/control/matrix12800/names")
def mtx12800_names_get():
    return JSONResponse(matrix12800_names.load())


@app.put("/api/control/matrix12800/names")
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


@app.post("/api/control/matrix12800/rename")
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


@app.post("/api/control/matrix12800/poll-bank")
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


@app.post("/api/control/matrix12800/poll-presets")
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


# --------------------------------------------------------------------------- #
# SMX control endpoints  (/control/smx)
# --------------------------------------------------------------------------- #

def _smx_device():
    for d in config_store.get_devices():
        if d.get("kind") == "smx":
            return d
    return None


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


@app.get("/control/smx", response_class=HTMLResponse)
def control_smx():
    return HTMLResponse(SMX_HTML)


@app.get("/api/control/smx/info")
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


@app.get("/api/control/smx/ties")
def smx_ties(plane: str = "00"):
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    ties, err = smx_control.poll_ties_plane(dev["ip"], dev.get("port", 23), plane)
    return JSONResponse({"ok": True, "plane": plane,
                         "ties": {str(k): v for k, v in ties.items()}, "error": err})


@app.get("/api/control/smx/ties-all")
def smx_ties_all():
    dev = _smx_device()
    if not dev:
        return JSONResponse({"error": "SMX not found"}, status_code=404)
    result, err = smx_control.poll_ties_all_planes(dev["ip"], dev.get("port", 23))
    # Convert int keys to str for JSON
    out = {plane: {str(k): v for k, v in ties.items()} for plane, ties in result.items()}
    return JSONResponse({"ok": True, "planes": out, "error": err})


@app.post("/api/control/smx/tie")
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


@app.post("/api/control/smx/ties-batch")
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


@app.post("/api/control/smx/preset/recall")
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


@app.post("/api/control/smx/preset/save")
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


@app.get("/api/control/smx/names")
def smx_names_get():
    return JSONResponse(smx_names.load())


@app.put("/api/control/smx/names")
async def smx_names_put(request: Request):
    body = await request.json()
    data = smx_names.load()
    for section in ("inputs", "outputs", "presets"):
        if section in body:
            for k, v in body[section].items():
                data[section][str(k)] = str(v)[:32].strip()
    smx_names.save(data)
    return JSONResponse({"ok": True})


@app.post("/api/control/smx/rename")
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


@app.post("/api/control/smx/poll-names")
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
<style>
  :root{
    --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
    --ink:#e8ebf0;--muted:#8b93a3;
    --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--gray:#454b58;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --c-vga:#4fa3e0;--c-svid:#e06b4f;--c-vid:#4fe08a;--c-aud:#c46fe0;
    --c-active:var(--c-vga);
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:var(--mono);font-size:14px;
    height:100dvh;display:flex;flex-direction:column;overflow:hidden}

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
  .toast{position:fixed;bottom:20px;right:20px;background:var(--panel2);
    border:1px solid var(--line);border-radius:8px;padding:9px 15px;
    font-size:12px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}

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



WELCOME_HTML = '<!doctype html>\n<html lang="en"><head>\n<meta charset="utf-8"/>\n<meta name="viewport" content="width=device-width,initial-scale=1"/>\n<title>JOEBOT LAB · Setup</title>\n<style>\n:root{\n  --bg:#080a0e;--panel:#12151c;--panel2:#181c25;--line:#232836;\n  --ink:#e8ebf0;--muted:#6b7585;--faint:#2a3040;\n  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--accent:#7c6af5;\n  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;\n}\n*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}\nbody{background:var(--bg);color:var(--ink);font-family:var(--mono);\n  min-height:100dvh;display:flex;flex-direction:column;overflow-x:hidden}\n\n/* ── scanlines texture ── */\nbody::before{content:\'\';position:fixed;inset:0;pointer-events:none;z-index:0;\n  background:repeating-linear-gradient(0deg,\n    transparent,transparent 2px,rgba(0,0,0,.08) 2px,rgba(0,0,0,.08) 4px)}\n\n/* ── step progress bar ── */\n.progress-bar{position:fixed;top:0;left:0;right:0;height:2px;z-index:50;\n  background:var(--faint)}\n.progress-fill{height:100%;background:var(--ok);transition:width .5s cubic-bezier(.4,0,.2,1)}\n\n/* ── step wrapper ── */\n.step{display:none;min-height:100dvh;flex-direction:column;\n  align-items:center;justify-content:center;padding:40px 20px;\n  position:relative;z-index:1}\n.step.active{display:flex}\n\n/* ── step 1: welcome ── */\n.welcome-wrap{text-align:center;max-width:640px}\n.logo-mark{font-size:clamp(48px,10vw,80px);font-weight:900;letter-spacing:-.02em;\n  line-height:1;margin-bottom:8px}\n.logo-mark .j{color:var(--ok)}\n.logo-mark .rest{color:var(--ink)}\n.logo-sub{font-size:clamp(11px,2vw,14px);letter-spacing:.35em;text-transform:uppercase;\n  color:var(--muted);margin-bottom:48px}\n.welcome-tagline{font-size:clamp(18px,3.5vw,26px);font-weight:700;\n  line-height:1.35;margin-bottom:16px;color:var(--ink)}\n.welcome-desc{font-size:clamp(13px,2vw,15px);color:var(--muted);\n  line-height:1.7;margin-bottom:48px;max-width:480px;margin-left:auto;margin-right:auto}\n.feature-row{display:flex;gap:24px;justify-content:center;\n  flex-wrap:wrap;margin-bottom:52px}\n.feat{text-align:center;min-width:90px}\n.feat-icon{font-size:24px;margin-bottom:6px}\n.feat-label{font-size:11px;color:var(--muted);letter-spacing:.05em}\n\n/* ── step 2: modules ── */\n.step-header{text-align:center;margin-bottom:36px;max-width:640px}\n.step-num{font-size:11px;letter-spacing:.2em;text-transform:uppercase;\n  color:var(--ok);margin-bottom:10px}\n.step-title{font-size:clamp(22px,4vw,32px);font-weight:800;margin-bottom:10px}\n.step-desc{font-size:13.5px;color:var(--muted);line-height:1.6}\n\n.module-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));\n  gap:10px;width:100%;max-width:900px;margin-bottom:36px}\n.mod-card{background:var(--panel);border:1.5px solid var(--line);border-radius:12px;\n  padding:14px 16px;cursor:pointer;transition:all .15s;\n  display:flex;gap:12px;align-items:flex-start;position:relative;user-select:none}\n.mod-card:hover{border-color:var(--muted)}\n.mod-card.selected{border-color:var(--ok);background:rgba(52,211,153,.06)}\n.mod-card.required{border-color:var(--faint);cursor:default;opacity:.7}\n.mod-card.required.selected{border-color:rgba(52,211,153,.3);opacity:1}\n.mod-icon{font-size:22px;flex-shrink:0;margin-top:1px}\n.mod-body{flex:1;min-width:0}\n.mod-name{font-size:13px;font-weight:700;margin-bottom:3px;\n  display:flex;align-items:center;gap:6px}\n.mod-badge{font-size:9px;letter-spacing:.06em;padding:1px 6px;border-radius:4px;\n  background:rgba(124,106,245,.15);color:var(--accent);border:1px solid rgba(124,106,245,.25)}\n.mod-desc{font-size:11.5px;color:var(--muted);line-height:1.5}\n.mod-check{position:absolute;top:12px;right:12px;\n  width:18px;height:18px;border-radius:5px;\n  border:1.5px solid var(--line);background:var(--panel2);\n  display:flex;align-items:center;justify-content:center;\n  font-size:11px;transition:all .12s}\n.mod-card.selected .mod-check{background:var(--ok);border-color:var(--ok);color:#000}\n.mod-card.required .mod-check{background:var(--faint);border-color:var(--faint);color:var(--muted)}\n\n/* ── step 3: devices ── */\n.template-grid{display:flex;flex-wrap:wrap;justify-content:center;\n  gap:8px;width:100%;max-width:900px;margin-bottom:28px}\n.tmpl-card{background:var(--panel);border:1.5px solid var(--line);border-radius:10px;\n  padding:14px 12px;cursor:pointer;transition:all .15s;text-align:center;\n  display:flex;flex-direction:column;align-items:center;gap:6px;user-select:none;\n  width:160px;flex-shrink:0}\n.tmpl-card:hover{border-color:var(--muted);background:var(--panel2)}\n.tmpl-card.selected{border-color:var(--accent);background:rgba(124,106,245,.08)}\n.tmpl-icon{font-size:26px}\n.tmpl-name{font-size:12px;font-weight:700;color:var(--ink)}\n.tmpl-desc{font-size:10px;color:var(--muted);line-height:1.4}\n\n.device-form{width:100%;max-width:580px;background:var(--panel);\n  border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:16px;\n  display:none}\n.device-form.visible{display:block}\n.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}\n.form-row.one{grid-template-columns:1fr}\n.field label{display:block;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;\n  color:var(--muted);margin-bottom:4px}\n.field input{width:100%;background:var(--panel2);border:1px solid var(--line);\n  color:var(--ink);border-radius:7px;padding:8px 10px;\n  font-family:var(--mono);font-size:13px;transition:border-color .15s}\n.field input:focus{outline:none;border-color:var(--accent)}\n.field input::placeholder{color:var(--faint)}\n.form-actions{display:flex;gap:8px;margin-top:6px}\n\n.device-list{width:100%;max-width:580px;display:flex;flex-direction:column;gap:6px;\n  margin-bottom:20px}\n.dev-item{background:var(--panel2);border:1px solid var(--line);border-radius:8px;\n  padding:10px 14px;display:flex;align-items:center;gap:10px}\n.dev-item-icon{font-size:18px}\n.dev-item-name{font-size:13px;font-weight:600;flex:1}\n.dev-item-ip{font-size:11px;color:var(--muted)}\n.dev-item-del{background:none;border:none;color:var(--muted);cursor:pointer;\n  font-size:16px;padding:2px 6px;border-radius:4px}\n.dev-item-del:hover{color:var(--bad);background:rgba(255,84,112,.1)}\n\n/* ── step 4: done ── */\n.done-wrap{text-align:center;max-width:560px}\n.done-icon{font-size:64px;margin-bottom:20px}\n.done-title{font-size:clamp(24px,4vw,36px);font-weight:800;margin-bottom:12px}\n.done-desc{font-size:14px;color:var(--muted);line-height:1.7;margin-bottom:36px}\n.done-summary{background:var(--panel);border:1px solid var(--line);border-radius:12px;\n  padding:20px;text-align:left;margin-bottom:36px;width:100%}\n.done-row{display:flex;align-items:center;gap:10px;padding:7px 0;\n  border-bottom:1px solid var(--faint);font-size:13px}\n.done-row:last-child{border-bottom:none}\n.done-row-icon{font-size:16px;width:24px;text-align:center}\n.done-row-label{color:var(--muted);flex:1}\n.done-row-val{color:var(--ok);font-weight:600}\n\n/* ── shared buttons ── */\n.btn-primary{font-family:var(--mono);cursor:pointer;font-size:14px;font-weight:700;\n  letter-spacing:.06em;padding:13px 36px;border-radius:10px;border:none;\n  background:var(--ok);color:#061a12;transition:all .15s}\n.btn-primary:hover{background:#4aedb0;transform:translateY(-1px);\n  box-shadow:0 6px 24px rgba(52,211,153,.3)}\n.btn-primary:active{transform:translateY(0)}\n.btn-secondary{font-family:var(--mono);cursor:pointer;font-size:13px;\n  padding:10px 22px;border-radius:8px;\n  background:transparent;color:var(--muted);border:1px solid var(--line)}\n.btn-secondary:hover{color:var(--ink);border-color:var(--muted)}\n.btn-accent{font-family:var(--mono);cursor:pointer;font-size:13px;font-weight:600;\n  padding:9px 20px;border-radius:8px;border:none;\n  background:var(--accent);color:#fff;transition:all .15s}\n.btn-accent:hover{background:#9585f8}\n.btn-small{font-family:var(--mono);cursor:pointer;font-size:12px;\n  padding:7px 14px;border-radius:7px;\n  background:var(--panel2);color:var(--muted);border:1px solid var(--line)}\n.btn-small:hover{color:var(--ink);border-color:var(--muted)}\n.btn-row{display:flex;gap:10px;align-items:center;justify-content:center;flex-wrap:wrap}\n.btn-row.left{justify-content:flex-start}\n\n/* ── corner skip link ── */\n.skip-link{position:fixed;bottom:20px;right:20px;z-index:50;\n  font-size:11.5px;color:var(--muted);cursor:pointer;\n  background:var(--panel);border:1px solid var(--line);\n  border-radius:6px;padding:5px 12px;text-decoration:none;transition:all .15s}\n.skip-link:hover{color:var(--ink);border-color:var(--muted)}\n\n/* ── glow effects ── */\n.glow-ok{text-shadow:0 0 30px rgba(52,211,153,.4)}\n.glow-accent{text-shadow:0 0 30px rgba(124,106,245,.4)}\n\n/* ── toast ── */\n.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);\n  background:var(--panel2);border:1px solid var(--line);border-radius:8px;\n  padding:9px 18px;font-size:12.5px;opacity:0;transition:opacity .25s;\n  pointer-events:none;z-index:200;white-space:nowrap}\n.toast.show{opacity:1}\n\n/* ── responsive ── */\n@media(max-width:500px){\n  .feature-row{gap:16px}\n  .form-row{grid-template-columns:1fr}\n  .module-grid{grid-template-columns:1fr}\n}\n</style></head>\n<body>\n\n<div class="progress-bar"><div class="progress-fill" id="prog" style="width:0%"></div></div>\n\n<!-- STEP 1: WELCOME -->\n<div class="step active" id="step-1">\n  <div class="welcome-wrap">\n    <div class="logo-mark glow-ok">\n      <span class="j">J</span><span class="rest">OEBOT LAB</span>\n    </div>\n    <div class="logo-sub">AV rack control system</div>\n\n    <div class="welcome-tagline">Modern control for classic AV gear.</div>\n    <div class="welcome-desc">\n      A local-first dashboard for Extron and pro AV equipment.\n      Run one Docker container, open a browser, and take control of gear\n      that deserves better software than it shipped with.\n    </div>\n\n    <div class="feature-row">\n      <div class="feat"><div class="feat-icon">📊</div><div class="feat-label">Live status</div></div>\n      <div class="feat"><div class="feat-icon">⚡</div><div class="feat-label">Route & control</div></div>\n      <div class="feat"><div class="feat-icon">🗂</div><div class="feat-label">Config editor</div></div>\n      <div class="feat"><div class="feat-icon">🤖</div><div class="feat-label">Auto-switching</div></div>\n      <div class="feat"><div class="feat-icon">🐳</div><div class="feat-label">Docker-packaged</div></div>\n    </div>\n\n    <div class="btn-row">\n      <button class="btn-primary" onclick="goStep(2)">Get Started →</button>\n      <button class="btn-secondary" onclick="skipSetup()">Skip setup</button>\n    </div>\n  </div>\n</div>\n\n<!-- STEP 2: MODULES -->\n<div class="step" id="step-2">\n  <div class="step-header">\n    <div class="step-num">Step 1 of 3</div>\n    <div class="step-title">What do you want to use?</div>\n    <div class="step-desc">\n      Enable the modules that match your gear. Disabled modules have zero overhead —\n      no polling, no background tasks, nothing.\n    </div>\n  </div>\n  <div class="module-grid" id="module-grid"></div>\n  <div class="btn-row">\n    <button class="btn-secondary" onclick="goStep(1)">← Back</button>\n    <button class="btn-primary" onclick="saveModulesAndNext()">Next →</button>\n  </div>\n</div>\n\n<!-- STEP 3: DEVICES -->\n<div class="step" id="step-3">\n  <div class="step-header">\n    <div class="step-num">Step 2 of 3</div>\n    <div class="step-title">Add your devices</div>\n    <div class="step-desc">\n      Pick a device type, fill in the name and IP. You can add more anytime from the device manager.\n    </div>\n  </div>\n\n  <div class="device-list" id="device-list"></div>\n\n  <div class="template-grid" id="template-grid"></div>\n\n  <div class="device-form" id="device-form">\n    <div class="form-row">\n      <div class="field">\n        <label>Device name</label>\n        <input id="f-name" placeholder="e.g. Main Matrix"/>\n      </div>\n      <div class="field">\n        <label>IP / Hostname</label>\n        <input id="f-ip" placeholder="10.0.0.12 or device.local"/>\n      </div>\n    </div>\n    <div class="form-row">\n      <div class="field">\n        <label>Port</label>\n        <input id="f-port" placeholder="23" value="23"/>\n      </div>\n      <div class="field">\n        <label>Password <span style="color:var(--faint);font-size:9px">if required</span></label>\n        <input id="f-password" placeholder="leave blank if none" autocomplete="off"/>\n      </div>\n    </div>\n    <div class="form-row one">\n      <div class="field" style="display:flex;gap:8px">\n        <button class="btn-accent" onclick="addDevice()">+ Add Device</button>\n        <button class="btn-small" onclick="cancelForm()">Cancel</button>\n      </div>\n    </div>\n  </div>\n\n  <div class="btn-row" style="margin-top:8px">\n    <button class="btn-secondary" onclick="goStep(2)">← Back</button>\n    <button class="btn-primary" onclick="finishSetup()">\n      <span id="next-lbl">Skip for now →</span>\n    </button>\n  </div>\n</div>\n\n<!-- STEP 4: DONE -->\n<div class="step" id="step-4">\n  <div class="done-wrap">\n    <div class="done-icon">🚀</div>\n    <div class="done-title glow-ok">Lab is ready.</div>\n    <div class="done-desc">Taking you to the dashboard…</div>\n  </div>\n</div>\n\n<a class="skip-link" onclick="skipSetup()">Skip → Dashboard</a>\n<div class="toast" id="toast"></div>\n\n<script>\n// ── state ──────────────────────────────────────────────────────────────────\nlet currentStep = 1;\nlet allModules = [];\nlet allTemplates = [];\nlet filteredTemplates = [];\nlet enabledModules = new Set();\nlet addedDevices = [];\nlet selectedTemplate = null;\n\nconst STEP_PROGRESS = {1:0, 2:33, 3:66, 4:100};\n\n// ── toast ──────────────────────────────────────────────────────────────────\nlet _tt;\nfunction toast(msg, dur=2400){\n  const el = document.getElementById(\'toast\');\n  el.textContent = msg; el.classList.add(\'show\');\n  clearTimeout(_tt); _tt = setTimeout(()=>el.classList.remove(\'show\'), dur);\n}\n\n// ── step navigation ────────────────────────────────────────────────────────\nfunction goStep(n){\n  document.getElementById(\'step-\'+currentStep).classList.remove(\'active\');\n  currentStep = n;\n  document.getElementById(\'step-\'+n).classList.add(\'active\');\n  document.getElementById(\'prog\').style.width = STEP_PROGRESS[n] + \'%\';\n  window.scrollTo(0,0);\n}\n\n// ── finish setup ───────────────────────────────────────────────────────────\nasync function finishSetup(){\n  goStep(4);\n  try{ await fetch(\'/api/setup/complete\',{method:\'POST\'}); }catch(e){}\n  window.location.href = \'/\';\n}\n\n// ── module grid ────────────────────────────────────────────────────────────\nfunction renderModules(){\n  const box = document.getElementById(\'module-grid\');\n  box.innerHTML = \'\';\n  allModules.forEach(m => {\n    const on = enabledModules.has(m.id);\n    const req = m.required;\n    const card = document.createElement(\'div\');\n    card.className = \'mod-card\' + (on?\' selected\':\'\') + (req?\' required\':\'\');\n    card.dataset.id = m.id;\n    card.innerHTML = `\n      <div class="mod-icon">${m.icon||\'🔧\'}</div>\n      <div class="mod-body">\n        <div class="mod-name">${esc(m.name)}${m.badge?`<span class="mod-badge">${esc(m.badge)}</span>`:\'\'}${req?\'<span class="mod-badge" style="background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.2)">required</span>\':\'\'}</div>\n        <div class="mod-desc">${esc(m.desc)}</div>\n      </div>\n      <div class="mod-check">${on?(req?\'—\':\'✓\'):\'\'}</div>`;\n    if(!req) card.addEventListener(\'click\', ()=>toggleModule(m.id));\n    box.appendChild(card);\n  });\n}\n\nfunction toggleModule(id){\n  if(enabledModules.has(id)) enabledModules.delete(id);\n  else enabledModules.add(id);\n  renderModules();\n}\n\nasync function saveModulesAndNext(){\n  try{\n    await fetch(\'/api/setup/modules\',{method:\'POST\',\n      headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({enabled:[...enabledModules]})});\n  }catch(e){ toast(\'Could not save modules, continuing anyway\'); }\n  // Filter templates to those matching enabled modules\n  filteredTemplates = allTemplates.filter(t => t.module && enabledModules.has(t.module));\n  renderTemplates();\n  goStep(3);\n}\n\n// ── template grid ──────────────────────────────────────────────────────────\nfunction renderTemplates(){\n  const box = document.getElementById(\'template-grid\');\n  box.innerHTML = \'\';\n  filteredTemplates.forEach(t => {\n    const card = document.createElement(\'div\');\n    card.className = \'tmpl-card\' + (selectedTemplate===t.template?\' selected\':\'\');\n    card.dataset.tmpl = t.template;\n    card.innerHTML = `\n      <div class="tmpl-icon">${t.icon||\'🔧\'}</div>\n      <div class="tmpl-name">${esc(t.name)}</div>\n      <div class="tmpl-desc">${esc(t.desc)}</div>`;\n    card.addEventListener(\'click\', ()=>selectTemplate(t));\n    box.appendChild(card);\n  });\n}\n\nfunction selectTemplate(t){\n  selectedTemplate = t.template;\n  renderTemplates();\n  document.getElementById(\'f-name\').value = t.name;\n  document.getElementById(\'f-port\').value = t.port;\n  document.getElementById(\'f-ip\').value = \'\';\n  document.getElementById(\'f-password\').value = t.default_password||\'\';\n  document.getElementById(\'device-form\').classList.add(\'visible\');\n  setTimeout(()=>document.getElementById(\'f-ip\').focus(), 80);\n}\n\nfunction cancelForm(){\n  selectedTemplate = null;\n  renderTemplates();\n  document.getElementById(\'device-form\').classList.remove(\'visible\');\n}\n\nasync function addDevice(){\n  const name     = document.getElementById(\'f-name\').value.trim();\n  const ip       = document.getElementById(\'f-ip\').value.trim();\n  const port     = parseInt(document.getElementById(\'f-port\').value) || 23;\n  const password = document.getElementById(\'f-password\').value.trim() || null;\n  if(!name || !ip){ toast(\'Name and IP are required\'); return; }\n  if(!selectedTemplate){ toast(\'Select a device type\'); return; }\n  try{\n    const r = await fetch(\'/api/setup/add-device\',{method:\'POST\',\n      headers:{\'Content-Type\':\'application/json\'},\n      body:JSON.stringify({template:selectedTemplate, name, ip, port, password})});\n    const j = await r.json();\n    if(!j.ok){ toast(\'Error: \'+(j.error||\'unknown\')); return; }\n    const tmpl = allTemplates.find(t=>t.template===selectedTemplate);\n    const displayHost = j.device?.hostname || j.device?.ip || ip;\n    addedDevices.push({name, ip:displayHost, icon:tmpl?.icon||\'🔧\', kind:tmpl?.name||selectedTemplate});\n    renderDeviceList();\n    cancelForm();\n    updateNextLabel();\n    toast(`Added ${name}` + (j.device?.hostname ? ` → ${j.device.ip}` : \'\'));\n  }catch(e){ toast(\'Error: \'+e.message); }\n}\n\nfunction renderDeviceList(){\n  const box = document.getElementById(\'device-list\');\n  box.innerHTML = \'\';\n  addedDevices.forEach((d,i)=>{\n    const div = document.createElement(\'div\');\n    div.className = \'dev-item\';\n    div.innerHTML = `<span class="dev-item-icon">${d.icon}</span>\n      <span class="dev-item-name">${esc(d.name)}</span>\n      <span class="dev-item-ip">${esc(d.ip)}</span>\n      <button class="dev-item-del" onclick="removeDevice(${i})" title="Remove">×</button>`;\n    box.appendChild(div);\n  });\n}\n\nfunction removeDevice(i){\n  addedDevices.splice(i,1);\n  renderDeviceList();\n  updateNextLabel();\n}\n\nfunction updateNextLabel(){\n  document.getElementById(\'next-lbl\').textContent =\n    addedDevices.length > 0 ? \'Next →\' : \'Skip for now →\';\n}\n\nasync function skipSetup(){\n  await finishSetup();\n}\n\n// ── helpers ────────────────────────────────────────────────────────────────\nfunction esc(s){ return String(s||\'\').replace(/[&<>"]/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\'}[c])); }\n\n// ── init ───────────────────────────────────────────────────────────────────\n(async()=>{\n  try{\n    const r = await fetch(\'/api/setup/state\');\n    const j = await r.json();\n    allModules   = j.all_modules   || [];\n    allTemplates = j.device_templates || [];\n    enabledModules = new Set(j.enabled_modules || []);\n    renderModules();\n    filteredTemplates = allTemplates;\n    renderTemplates();\n    // If they already have devices, pre-populate the added list\n    if(j.device_count > 0){\n      document.getElementById(\'next-lbl\').textContent = \'Next →\';\n    }\n  }catch(e){\n    console.error(\'setup state load failed\', e);\n    toast(\'Could not load setup data\');\n  }\n})();\n</script>\n</body></html>'


# =========================================================================== #
# IPCP Pro 505 Control Hub  (/control/ipcp505)
# =========================================================================== #

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
      rightSide=`<a href="${p.page}" class="serial-btn live">Control →</a>`;
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


# ── IPCP 505 API endpoints ──────────────────────────────────────────────────

@app.get("/control/ipcp505", response_class=HTMLResponse)
def control_ipcp505_page():
    return HTMLResponse(IPCP505_HTML)


@app.get("/control/ipcp505/vtg400", response_class=HTMLResponse)
def control_vtg400_page():
    return HTMLResponse(VTG400_HTML)


@app.get("/api/control/ipcp505/state")
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


@app.post("/api/control/ipcp505/relay/{n}/set")
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


@app.post("/api/control/ipcp505/power/{n}/set")
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


@app.post("/api/control/ipcp505/vtg400/cmd")
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


@app.get("/api/control/ipcp505/vtg400/state")
def ipcp505_vtg400_state():
    dev = _ipcp505_device()
    if not dev:
        return JSONResponse({"error": "IPCP 505 not configured"}, status_code=404)
    results, err = _vtg400_query_all(dev["ip"])
    if err and not results:
        return JSONResponse({"error": err})
    results["error"] = err
    return JSONResponse(results)


@app.get("/api/control/ipcp505/com/{port}/probe")
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


def _usp405_query_all(ip: str, com_port: int, timeout: float = 18.0):
    """Query all USP 405 state via TCP direct (no * chars in any query command)."""
    import socket as _s
    tcp_port = 2000 + com_port
    try:
        sock = _s.create_connection((ip, tcp_port), timeout=timeout)
    except (OSError, _s.timeout) as e:
        return {}, str(e)
    # Note: "\\" is a one-backslash Python string — the Input 2 type query command
    queries = [
        ("info",       "I"),    ("firmware",   "Q"),    ("model",      "N"),
        ("in2type",    "\\"),   ("color",      "C"),    ("tint",       "T"),
        ("contrast",   "^"),    ("brightness", "Y"),    ("freeze",     "F"),
        ("testpat",    "J"),    ("exemode",    "X"),    ("outrate",    "="),
        ("hdetail",    "D"),    ("vdetail",    "d"),
        ("top_blank",  "("),    ("bot_blank",  ")"),
        ("enc_filter", "10#"),  ("blue_scr",   "8#"),   ("edge_smth",  "16#"),
        ("enhanced",   "12#"),  ("pal_film",   "18#"),  ("out_sig",    "6#"),
        ("polarity",   "7#"),   ("rgb_delay",  "3#"),
    ]
    results = {}
    try:
        for key, cmd in queries:
            sock.sendall((cmd + "\r").encode("ascii"))
            results[key] = sis._read(sock, 1.5, idle=0.4).strip()
    except OSError as e:
        results["_error"] = str(e)
    finally:
        try: sock.close()
        except OSError: pass
    return results, None


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
        <option value="2">60 Hz</option>
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
  sendCmd(n + '!');
  for(let i=1;i<=5;i++){
    const e=document.getElementById('inp'+i);
    if(e) e.classList.toggle('active', i===n);
  }
  setText('preset-inp-lbl', n);
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


@app.get("/control/ipcp505/usp405", response_class=HTMLResponse)
def control_usp405_page():
    return HTMLResponse(USP405_HTML)


@app.post("/api/control/ipcp505/usp405/cmd")
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


@app.get("/api/control/ipcp505/usp405/state")
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

@app.get("/api/control/ipcp505/com/scan")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
