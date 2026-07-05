"""
routes_dsc401.py — DSC 401 A monitoring and control (v1.0)

Protocol: Extron SWIS over WebSocket (wss://<host>/api/wipc)
Auth: POST /api/login with Basic auth → NortxeSession cookie
      WebSocket requires: Origin + Referer + session cookie
URIs: session-specific hashed paths extracted from /www/main-es2018.js
"""

import base64
import json
import mimetypes
import re
import socket
import ssl
import threading
import time
import uuid

from fastapi import APIRouter, Body, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()

DEVICES = [
    {"name": "DSC 401 #1", "host": "10.0.0.41"},
    {"name": "DSC 401 #2", "host": "10.0.0.42"},
    {"name": "DSC 401 #3", "host": "10.0.0.43"},
    {"name": "DSC 401 #4", "host": "10.0.0.44"},
]
CREDS = ("admin", "extron")
SOCK_TO = 6
# Cache: host -> {session, uris: {host_uri, input_uri, output_uri}, ts}
_cache: dict = {}
_cache_lock = threading.Lock()
CACHE_TTL = 25 * 60  # refresh URIs every 25 min

def _log(msg: str):
    print(f"[dsc401] {msg}", flush=True)

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Force HTTP/1.1 — device doesn't accept WebSocket upgrade over h2
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx

def _https_request(host: str, method: str, path: str, headers: dict, timeout: int = SOCK_TO, body: bytes = b"") -> tuple[int, dict, bytes]:
    """Raw socket HTTPS request — avoids http.client quirks with this device."""
    ctx = _ssl_ctx()
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        hdr_lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        req = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: JoebotLab/2.10\r\nAccept: */*\r\n{hdr_lines}\r\nConnection: close\r\n\r\n"
        ssock.sendall(req.encode() + body)
        # Read full response
        buf = b""
        ssock.settimeout(timeout)
        while True:
            try:
                chunk = ssock.recv(65536)
                if not chunk:
                    break
                buf += chunk
            except socket.timeout:
                break
        ssock.close()
        # Parse status + headers
        sep = buf.find(b"\r\n\r\n")
        if sep < 0:
            return 0, {}, b""
        head = buf[:sep].decode("utf-8", errors="replace")
        body = buf[sep + 4:]
        lines = head.split("\r\n")
        status = int(lines[0].split(" ")[1]) if lines else 0
        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()
        return status, resp_headers, body
    except Exception as e:
        _log(f"{host} https_request {path}: {e}")
        return 0, {}, b""

def _login(host: str) -> str | None:
    auth = base64.b64encode(f"{CREDS[0]}:{CREDS[1]}".encode()).decode()
    status, hdrs, _ = _https_request(host, "POST", "/api/login",
                                     {"Authorization": f"Basic {auth}"})
    if status != 200:
        _log(f"{host} login status={status}")
        return None
    sc = hdrs.get("set-cookie", "")
    m = re.search(r"NortxeSession=([^;]+)", sc)
    return m.group(1) if m else None

def _get_uris(host: str, session: str) -> dict | None:
    status, _, body = _https_request(host, "GET", "/www/main-es2018.js",
                                     {"Cookie": f"NortxeSession={session}"}, timeout=10)
    if status != 200:
        _log(f"{host} get_uris status={status}")
        return None
    js = body.decode("utf-8", errors="replace")
    vpa_pos = js.find("vpa/in/1")
    if vpa_pos < 0:
        return None
    chunk = js[max(0, vpa_pos - 2000):vpa_pos + 2000]
    mh = re.search(r'hostName:"([^"]+)"', chunk)
    mi = re.search(r'(?<!\w)input:"([^"]+)"', chunk)
    mo = re.search(r'(?<!\w)output:"([^"]+)"', chunk)
    mp = re.search(r'getPubUri\(\)\{return \w+\(\)\?"[^"]+":"([^"]+)"', js)
    return {
        "host_uri": mh.group(1) if mh else None,
        "input_uri": mi.group(1) if mi else None,
        "output_uri": mo.group(1) if mo else None,
        "pub_uri": mp.group(1) if mp else None,
    }

def _ws_frame(payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        hdr = bytes([0x81, 0x80 | n])
    else:
        hdr = bytes([0x81, 0x80 | 126, n >> 8, n & 0xFF])
    mask = b"\x01\x02\x03\x04"
    return hdr + mask + bytes([payload[i] ^ mask[i % 4] for i in range(n)])

def _ws_parse_frames(buf: bytes) -> list[str]:
    msgs = []
    idx = 0
    while idx + 2 <= len(buf):
        opcode = buf[idx] & 0x0F
        has_mask = (buf[idx + 1] & 0x80) != 0
        length = buf[idx + 1] & 0x7F
        idx += 2
        if length == 126:
            if idx + 2 > len(buf): break
            length = (buf[idx] << 8) | buf[idx + 1]; idx += 2
        elif length == 127:
            if idx + 8 > len(buf): break
            length = int.from_bytes(buf[idx:idx + 8], "big"); idx += 8
        if has_mask:
            idx += 4
        if idx + length > len(buf): break
        payload = buf[idx:idx + length]; idx += length
        if opcode == 1:
            msgs.append(payload.decode("utf-8", errors="replace"))
    return msgs

def _ws_get_resources(host: str, session: str, uris: dict) -> dict | None:
    ctx = _ssl_ctx()
    try:
        nonce = base64.b64encode(b"jb2dsc401key16!!").decode()
        sock = socket.create_connection((host, 443), timeout=SOCK_TO)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
        upgrade = (
            f"GET /api/wipc HTTP/1.1\r\nHost: {host}\r\nUser-Agent: JoebotLab/2.10\r\n"
            f"Accept: */*\r\nCookie: NortxeSession={session}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Protocol: extron-wipc\r\n"
            f"Origin: https://{host}\r\nReferer: https://{host}/www/index.html\r\n\r\n"
        )
        ssock.sendall(upgrade.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += ssock.recv(4096)
        if b"101" not in resp[:20]:
            ssock.close()
            return None
        # Send GET requests
        cid = 0
        for key in ("host_uri", "input_uri", "output_uri"):
            uri = uris.get(key)
            if uri:
                cid += 1
                payload = json.dumps({
                    "uri": uri, "method": "get",
                    "replyto": f"{uri}?callback_id={cid}"
                }).encode()
                ssock.sendall(_ws_frame(payload))
        # Collect responses
        ssock.settimeout(5.0)
        buf = b""
        deadline = time.time() + 6.0
        while time.time() < deadline:
            try:
                chunk = ssock.recv(16384)
                if not chunk: break
                buf += chunk
            except socket.timeout:
                break
        ssock.close()
        # Parse
        results = {}
        for raw in _ws_parse_frames(buf):
            try:
                p = json.loads(raw)
                if p.get("returncode", "0") != "0":
                    continue
                base_uri = p.get("uri", "").split("?")[0]
                results[base_uri] = p.get("value")
            except Exception:
                pass
        return results
    except Exception as e:
        _log(f"{host} ws_get error: {e}")
        return None

def _get_cached_session(host: str):
    """Return (session, uris) from cache or re-auth if needed."""
    with _cache_lock:
        entry = _cache.get(host)
    if entry and (time.time() - entry["ts"]) < CACHE_TTL:
        return entry["session"], entry["uris"]
    # Re-authenticate
    session = _login(host)
    if not session:
        return None, None
    uris = _get_uris(host, session)
    if not uris:
        return session, None
    with _cache_lock:
        _cache[host] = {"session": session, "uris": uris, "ts": time.time()}
    return session, uris

def _poll_device(device: dict) -> dict:
    host = device["host"]
    name = device["name"]
    try:
        session, uris = _get_cached_session(host)
        if not session:
            return {"host": host, "name": name, "online": False, "error": "auth_failed"}
        if not uris:
            return {"host": host, "name": name, "online": False, "error": "uri_fetch_failed"}
        results = _ws_get_resources(host, session, uris)
        if results is None:
            # Session may be stale — invalidate cache and retry once
            with _cache_lock:
                _cache.pop(host, None)
            session, uris = _get_cached_session(host)
            if session and uris:
                results = _ws_get_resources(host, session, uris)
        if not results:
            return {"host": host, "name": name, "online": False, "error": "no_data"}
        hostname = results.get(uris.get("host_uri", ""))
        inp = results.get(uris.get("input_uri", ""))
        out = results.get(uris.get("output_uri", ""))
        return {
            "host": host, "name": name, "online": True,
            "hostname": hostname or "",
            "input": inp or {},
            "output": out or {},
        }
    except Exception as e:
        _log(f"{host} poll error: {e}")
        return {"host": host, "name": name, "online": False, "error": str(e)}

@router.get("/api/dsc401/status")
def api_dsc401_status():
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_poll_device, d) for d in DEVICES]
    results = [f.result() for f in futures]
    return JSONResponse(results)

def _build_logo_multipart(remote_filename: str, file_bytes: bytes, content_type: str, base_name: str) -> tuple[bytes, str]:
    boundary = "----JoebotDSCUpload" + uuid.uuid4().hex
    def field(name, value):
        return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode()
    body = b""
    body += field("nortxe_filename", remote_filename)
    body += field("nortxe_options", "as_json")
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"nortxe_file\"; filename=\"{base_name}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    body += file_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"

def _sis_logo(host: str, commands: list[str], timeout: float = 8.0) -> list[str]:
    """Send ESC-prefixed SIS logo commands over raw TCP port 23 with password auth."""
    results = []
    try:
        s = socket.create_connection((host, 23), timeout=timeout)
        s.settimeout(0.4)
        # Drain banner
        buf = b""
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                chunk = s.recv(4096)
                if not chunk: break
                buf += chunk
            except socket.timeout:
                break
        # Auth if prompted
        if b"password" in buf.lower():
            s.sendall((CREDS[1] + "\r").encode())
            time.sleep(0.4)
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    if not s.recv(4096): break
                except socket.timeout:
                    break
        # Send each command
        for cmd in commands:
            s.sendall(b"\x1b" + cmd.encode("ascii", errors="replace") + b"\r")
            time.sleep(0.25)
            resp = b""
            deadline = time.time() + 1.2
            while time.time() < deadline:
                try:
                    chunk = s.recv(4096)
                    if not chunk: break
                    resp += chunk
                    deadline = time.time() + 0.15
                except socket.timeout:
                    break
            # Strip banner noise
            lines = [ln.strip() for ln in resp.decode("ascii", errors="replace").replace("\r", "\n").split("\n") if ln.strip()]
            clean = [ln for ln in lines if not any(x in ln.lower() for x in ["copyright", "extron electronics", "login administrator", "password:"])]
            results.append("\n".join(clean) if clean else resp.decode("ascii", errors="replace").strip())
        s.close()
    except Exception as e:
        results.append(f"ERROR: {e}")
    return results

@router.post("/api/dsc401/logo/upload")
async def api_dsc401_logo_upload(
    host: str = Form(...),
    slot: int = Form(1),
    folder: str = Form("Graphics"),
    use_folder: bool = Form(True),
    show_after: bool = Form(True),
    file: UploadFile = File(...),
):
    logs: list[str] = []
    def log(msg): logs.append(msg); _log(msg)
    try:
        file_bytes = await file.read()
        base_name = file.filename or "logo.png"
        ct = file.content_type or mimetypes.guess_type(base_name)[0] or "application/octet-stream"
        if use_folder and folder:
            remote_filename = f"{folder.strip('/')}/{base_name}"
            sis_filename = base_name
        else:
            remote_filename = base_name
            sis_filename = f"/{base_name}"
        log(f"Device: {host}")
        log(f"Upload → /{remote_filename}  SIS name: {sis_filename}  Slot: {slot}")
        session = _login(host)
        if not session:
            log("ERROR: web login failed")
            return JSONResponse({"ok": False, "logs": logs})
        log("Web login OK.")
        body, multipart_ct = _build_logo_multipart(remote_filename, file_bytes, ct, base_name)
        log(f"Uploading {len(file_bytes):,} bytes…")
        status, _, resp_body = _https_request(host, "POST", "/upload", {
            "Cookie": f"NortxeSession={session}",
            "Content-Type": multipart_ct,
            "Content-Length": str(len(body)),
            "X-Requested-With": "XMLHttpRequest",
            "Origin": f"https://{host}",
            "Referer": f"https://{host}/www/index.html",
        }, timeout=20, body=body)
        resp_text = resp_body.decode("utf-8", errors="replace")
        if status == 200 and '"success": true' in resp_text:
            log(f"Upload OK (HTTP {status})")
        else:
            log(f"Upload warning HTTP {status}: {resp_text[:300]}")
        cmds = [f"A{slot},{sis_filename}LOGO", f"A{slot}LOGO"]
        labels = [f"Assign slot {slot}", f"Verify slot {slot}"]
        if show_after:
            cmds += [f"E1*{slot}LOGO", "ELOGO"]
            labels += [f"Enable slot {slot}", "Query enabled"]
        log("Sending SIS commands…")
        for label, resp in zip(labels, _sis_logo(host, cmds)):
            log(f"  {label}: {resp}")
        log("Done.")
        return JSONResponse({"ok": True, "logs": logs})
    except Exception as e:
        logs.append(f"ERROR: {e}")
        return JSONResponse({"ok": False, "logs": logs})

@router.post("/api/dsc401/logo/sis")
async def api_dsc401_logo_sis(body: dict = Body(...)):
    host = body.get("host", "")
    commands = body.get("commands", [])
    if not host or not commands:
        return JSONResponse({"ok": False, "error": "host and commands required"})
    results = _sis_logo(host, commands)
    return JSONResponse({"ok": True, "results": results})

@router.get("/control/dsc401", response_class=HTMLResponse)
def page_dsc401():
    return HTMLResponse(DSC401_HTML)

DSC401_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSC 401 Monitor — Joebot Lab</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Inter','Segoe UI',sans-serif;font-size:14px;min-height:100vh}
header{background:#1e293b;border-bottom:1px solid #334155;padding:12px 20px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.1rem;font-weight:700;color:#e2e8f0}
header .badge{background:#1d4ed8;color:#fff;font-size:.72rem;font-weight:700;padding:2px 8px;border-radius:20px}
.back{color:#94a3b8;text-decoration:none;font-size:.8rem}
.back:hover{color:#60a5fa}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:20px}
.card{background:#1e293b;border-radius:12px;border:1px solid #334155;overflow:hidden;transition:border-color .2s}
.card.online{border-color:#1d4ed8}
.card.offline{border-color:#ef4444;opacity:.7}
.card-header{background:#0f172a;padding:12px 16px;display:flex;align-items:center;justify-content:space-between}
.card-header .title{font-weight:700;font-size:.95rem;color:#e2e8f0}
.card-header .sub{font-size:.75rem;color:#64748b;margin-top:2px}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot.green{background:#22c55e;box-shadow:0 0 6px #22c55e88}
.dot.red{background:#ef4444}
.dot.gray{background:#475569}
.card-body{padding:14px 16px;display:flex;flex-direction:column;gap:12px}
.section-title{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#60a5fa;margin-bottom:6px;border-bottom:1px solid #1e3a5f;padding-bottom:4px}
.row{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:4px}
.row .label{color:#94a3b8;font-size:.78rem;white-space:nowrap}
.row .val{color:#e2e8f0;font-size:.82rem;font-weight:600;text-align:right;word-break:break-all}
.val.green{color:#4ade80}
.val.red{color:#f87171}
.val.amber{color:#fbbf24}
.val.blue{color:#60a5fa}
.badge-sm{display:inline-block;font-size:.65rem;font-weight:700;padding:1px 6px;border-radius:8px;background:#1e3a5f;color:#93c5fd;margin-left:4px}
.badge-sm.green{background:#14532d;color:#4ade80}
.badge-sm.red{background:#450a0a;color:#f87171}
.badge-sm.amber{background:#451a03;color:#fbbf24}
.offline-msg{text-align:center;padding:20px;color:#64748b;font-size:.85rem}
.refresh-bar{display:flex;align-items:center;gap:10px;padding:10px 20px;background:#1e293b;border-top:1px solid #334155}
.refresh-bar span{color:#64748b;font-size:.78rem}
.refresh-btn{background:#1d4ed8;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:.78rem;font-weight:700}
.refresh-btn:hover{background:#2563eb}
.poll-info{margin-left:auto;color:#475569;font-size:.72rem}
</style>
</head>
<body>
<header>
  <a href="/control/ipcp505" class="back">← IPCP 505</a>
  <h1>DSC 401 A Monitor</h1>
  <span class="badge">×4</span>
</header>
<div class="refresh-bar">
  <button class="refresh-btn" onclick="poll()">⟳ Refresh</button>
  <span id="status-text">Loading…</span>
  <span class="poll-info" id="poll-info"></span>
</div>
<div class="grid" id="grid">
  <!-- Cards injected by JS -->
</div>
<script>
let _lastPoll = 0;

function fmt(v){
  if(v===null||v===undefined||v==='') return '—';
  if(typeof v==='boolean') return v?'Yes':'No';
  return String(v);
}
function signalBadge(present){
  return present
    ? '<span class="badge-sm green">SIGNAL</span>'
    : '<span class="badge-sm red">NO SIGNAL</span>';
}
function hdcpBadge(hdcp){
  if(!hdcp) return '—';
  const st = hdcp.status||'';
  if(st.includes('authenticated')) return '<span class="badge-sm green">HDCP OK</span>';
  if(st==='no_device'||st==='none') return '<span class="badge-sm">No HDCP</span>';
  return '<span class="badge-sm amber">'+st+'</span>';
}
function audioBadge(aud){
  if(!aud||!aud.hdmi) return '—';
  const h=aud.hdmi;
  if(h.present) return '<span class="badge-sm green">'+fmt(h.format)+'</span>';
  return '<span class="badge-sm">None</span>';
}

function renderCard(d){
  if(!d.online){
    return `<div class="card offline">
      <div class="card-header">
        <div><div class="title">${d.name}</div><div class="sub">${d.host}</div></div>
        <div class="dot red"></div>
      </div>
      <div class="card-body">
        <div class="offline-msg">⚠ Offline — ${d.error||'connection failed'}</div>
      </div>
    </div>`;
  }
  const inp=d.input||{};
  const out=d.output||{};
  const inTiming=inp.timing||{};
  const outTiming=out.timing||{};
  const outRes=out.resolution||{};
  const inPresent=inp.signal_present;
  const inFmt=inTiming.friendly_name||inp.format||'—';
  const outFmt=outTiming.friendly_name||`${outRes.h_active||0}×${outRes.v_active||0}`;
  const outRate=out.rate?`${out.rate}Hz`:'—';
  const outFormat=out.format||'—';
  const screenSaver=out.screen_saver||{};
  const ssModeState=screenSaver.mode_state||'';

  return `<div class="card online">
    <div class="card-header">
      <div><div class="title">${d.name}</div><div class="sub">${d.hostname||d.host}</div></div>
      <div class="dot green"></div>
    </div>
    <div class="card-body">
      <div>
        <div class="section-title">Input</div>
        <div class="row"><span class="label">Signal</span><span class="val">${signalBadge(inPresent)}</span></div>
        <div class="row"><span class="label">Format</span><span class="val ${inPresent?'green':'red'}">${inFmt}</span></div>
        <div class="row"><span class="label">5V</span><span class="val ${inp['5v']?'green':'red'}">${inp['5v']?'Present':'None'}</span></div>
        <div class="row"><span class="label">HDCP</span><span class="val">${hdcpBadge(inp.hdcp)}</span></div>
        <div class="row"><span class="label">Audio In</span><span class="val">${audioBadge(inp.audio)}</span></div>
      </div>
      <div>
        <div class="section-title">Output</div>
        <div class="row"><span class="label">Resolution</span><span class="val blue">${outFmt}</span></div>
        <div class="row"><span class="label">Rate</span><span class="val">${outRate}</span></div>
        <div class="row"><span class="label">Format</span><span class="val">${outFormat}</span></div>
        <div class="row"><span class="label">HDCP</span><span class="val">${hdcpBadge(out.hdcp)}</span></div>
        <div class="row"><span class="label">Audio Out</span><span class="val">${audioBadge(out.audio)}</span></div>
        <div class="row"><span class="label">Freeze</span><span class="val ${out.freeze?'amber':''}">${out.freeze?'ON':'OFF'}</span></div>
        <div class="row"><span class="label">Video Mute</span><span class="val ${out.video_mute&&out.video_mute!='off'?'red':''}">${out.video_mute||'off'}</span></div>
        <div class="row"><span class="label">Test Pattern</span><span class="val ${out.test_pattern&&out.test_pattern!='off'?'amber':''}">${out.test_pattern||'off'}</span></div>
        ${ssModeState&&ssModeState!='off'?`<div class="row"><span class="label">Screen Saver</span><span class="val amber">${ssModeState}</span></div>`:''}
      </div>
    </div>
  </div>`;
}

async function poll(){
  const t0=performance.now();
  document.getElementById('status-text').textContent='Polling…';
  try{
    const r=await fetch('/api/dsc401/status');
    const data=await r.json();
    const t1=performance.now();
    const grid=document.getElementById('grid');
    grid.innerHTML=data.map(renderCard).join('');
    const online=data.filter(d=>d.online).length;
    document.getElementById('status-text').textContent=`${online}/${data.length} online`;
    document.getElementById('poll-info').textContent=`${Math.round(t1-t0)}ms`;
    _lastPoll=Date.now();
  }catch(e){
    document.getElementById('status-text').textContent='Poll error: '+e.message;
  }
}

// Auto-poll every 15s
poll();
setInterval(()=>{if(Date.now()-_lastPoll>14000)poll();},5000);
</script>

<!-- ─── LOGO MANAGER ─────────────────────────────────────────────────── -->
<style>
.lm{margin:0 20px 24px;background:#1e293b;border:1px solid #334155;border-radius:12px;overflow:hidden}
.lm-head{background:#0f172a;padding:11px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #334155}
.lm-head h2{font-size:.9rem;font-weight:700;color:#e2e8f0;margin:0}
.lm-head .badge{background:#7c3aed;color:#fff;font-size:.65rem;font-weight:700;padding:2px 7px;border-radius:20px}
.lm-body{padding:16px}
.lm-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.lm-row label{color:#94a3b8;font-size:.78rem;white-space:nowrap;min-width:56px}
.lm-sel,.lm-inp{background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:5px 9px;font-size:.8rem;font-family:inherit}
.lm-sel:focus,.lm-inp:focus{outline:none;border-color:#60a5fa}
.lm-file{background:#0f172a;border:1px solid #334155;color:#94a3b8;border-radius:6px;padding:4px 8px;font-size:.78rem;cursor:pointer;flex:1}
.lm-file::file-selector-button{background:#1d4ed8;color:#fff;border:none;padding:3px 10px;border-radius:4px;font-size:.75rem;cursor:pointer;margin-right:8px}
.lm-cb{display:flex;align-items:center;gap:5px;color:#94a3b8;font-size:.78rem;cursor:pointer;white-space:nowrap}
.lm-cb input{accent-color:#60a5fa}
.lm-btns{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 10px}
.lm-btn{background:#1d4ed8;color:#fff;border:none;padding:7px 14px;border-radius:7px;font-size:.78rem;font-weight:700;cursor:pointer;font-family:inherit;white-space:nowrap}
.lm-btn:hover{background:#2563eb}
.lm-btn.sec{background:#334155;color:#cbd5e1}
.lm-btn.sec:hover{background:#475569}
.lm-btn.red{background:#7f1d1d;color:#fca5a5}
.lm-btn.red:hover{background:#991b1b}
.lm-log{width:100%;background:#0f172a;border:1px solid #334155;border-radius:7px;color:#4ade80;font-family:'JetBrains Mono','Fira Code',ui-monospace,monospace;font-size:.73rem;padding:10px;resize:vertical;min-height:120px;line-height:1.55}
</style>
<div class="lm">
  <div class="lm-head">
    <h2>Logo Manager</h2>
    <span class="badge">SIS + HTTPS</span>
  </div>
  <div class="lm-body">
    <div class="lm-row">
      <label>Device</label>
      <select class="lm-sel" id="lm-device">
        <option value="10.0.0.41">DSC 401 #1 — 10.0.0.41</option>
        <option value="10.0.0.42">DSC 401 #2 — 10.0.0.42</option>
        <option value="10.0.0.43">DSC 401 #3 — 10.0.0.43</option>
        <option value="10.0.0.44">DSC 401 #4 — 10.0.0.44</option>
      </select>
    </div>
    <div class="lm-row">
      <label>Image</label>
      <input type="file" class="lm-file" id="lm-file" accept=".png,.jpg,.jpeg,.bmp,.gif">
    </div>
    <div class="lm-row">
      <label>Slot</label>
      <input type="number" class="lm-inp" id="lm-slot" value="1" min="1" max="16" style="width:60px">
      <label class="lm-cb"><input type="checkbox" id="lm-use-folder" checked onchange="lmFolderToggle()"> /Graphics folder</label>
      <input type="text" class="lm-inp" id="lm-folder" value="Graphics" style="width:100px">
      <label class="lm-cb"><input type="checkbox" id="lm-show-after" checked> Show after assign</label>
    </div>
    <div class="lm-btns">
      <button class="lm-btn" onclick="lmUpload()">⬆ Upload + Assign</button>
      <button class="lm-btn sec" onclick="lmPollSlots()">🔍 Poll Slots 1-16</button>
      <button class="lm-btn sec" onclick="lmSis(['QLOGO'])">📋 Availability</button>
      <button class="lm-btn sec" onclick="lmSis(['ELOGO'])">👁 Enabled Logo</button>
      <button class="lm-btn red" onclick="lmSis(['E1*0LOGO'])">✕ Disable Logo</button>
    </div>
    <textarea class="lm-log" id="lm-log" readonly placeholder="Log output will appear here…"></textarea>
  </div>
</div>
<script>
function lmLog(msg){
  const el=document.getElementById('lm-log');
  el.value+=(el.value?'\\n':'')+msg;
  el.scrollTop=el.scrollHeight;
}
function lmClear(){document.getElementById('lm-log').value='';}
function lmFolderToggle(){
  document.getElementById('lm-folder').disabled=!document.getElementById('lm-use-folder').checked;
}

async function lmUpload(){
  const fileEl=document.getElementById('lm-file');
  if(!fileEl.files.length){lmLog('Pick an image file first.');return;}
  lmClear();
  lmLog('Starting upload…');
  const fd=new FormData();
  fd.append('host',document.getElementById('lm-device').value);
  fd.append('slot',document.getElementById('lm-slot').value);
  fd.append('folder',document.getElementById('lm-folder').value);
  fd.append('use_folder',document.getElementById('lm-use-folder').checked?'true':'false');
  fd.append('show_after',document.getElementById('lm-show-after').checked?'true':'false');
  fd.append('file',fileEl.files[0]);
  try{
    const r=await fetch('/api/dsc401/logo/upload',{method:'POST',body:fd});
    const j=await r.json();
    (j.logs||[]).forEach(lmLog);
  }catch(e){lmLog('Fetch error: '+e.message);}
}

async function lmPollSlots(){
  lmClear();
  const host=document.getElementById('lm-device').value;
  lmLog('Polling slots 1-16 on '+host+'…');
  const cmds=[...Array(16)].map((_,i)=>`A${i+1}LOGO`);
  try{
    const r=await fetch('/api/dsc401/logo/sis',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({host,commands:cmds})});
    const j=await r.json();
    (j.results||[]).forEach((resp,i)=>lmLog(`Slot ${String(i+1).padStart(2,' ')}: ${resp||'—'}`));
  }catch(e){lmLog('Error: '+e.message);}
}

async function lmSis(commands){
  lmClear();
  const host=document.getElementById('lm-device').value;
  lmLog('SIS → '+host+': '+commands.join(', '));
  try{
    const r=await fetch('/api/dsc401/logo/sis',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({host,commands})});
    const j=await r.json();
    (j.results||[]).forEach((resp,i)=>lmLog(`  [${commands[i]}]: ${resp||'(empty)'}`));
  }catch(e){lmLog('Error: '+e.message);}
}
</script>
</body>
</html>"""
