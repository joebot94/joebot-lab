"""
SMX System Matrix control module.

SIS commands used:
  {plane}*{input}*{output}!\r     → tie (! = all signal types on that plane)
  {plane}*0*{output}!\r           → untie output on plane
  {plane}*{output}!\r             → query tie for output on plane
  Rpr{nn}\r                       → recall global preset nn (01-32)
  Spr{nn}\r                       → save global preset nn (01-32)
  \x1b{n}NI\r                     → read input name
  \x1b{n}NO\r                     → read output name
  \x1b{n},{name}NI\r              → write input name
  \x1b{n},{name}NO\r              → write output name
  I\r                             → info / firmware version
  S\r                             → system status
"""

import socket
import time
import re

CR = b"\r"
ESC = "\x1b"
_TIMEOUT = 4.0
_INVALID_CHARS = set('+~,@=`[]{}\'<>\'";:|\\?')

# Plane definitions — keyed by plane string
PLANES = {
    "00": {"label": "VGA",     "signal": "&"},
    "01": {"label": "S-Video", "signal": "&"},
    "02": {"label": "Video",   "signal": "&"},
    "04": {"label": "Audio",   "signal": "$"},
}
PLANE_ORDER = ["00", "01", "02", "04"]
N_INPUTS  = 16
N_OUTPUTS = 16
N_PRESETS = 32


def _open(ip, port, timeout=_TIMEOUT):
    sock = socket.create_connection((ip, port), timeout=timeout)
    sock.settimeout(1.5)
    # Drain banner (SMX may not need auth, but handle if it prompts)
    buf = b""
    deadline = time.time() + 2.5
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            buf += chunk
            text = buf.decode("ascii", errors="replace")
            if "password" in text.lower():
                sock.sendall(b"admin\r")
                buf = b""
            elif "login" in text.lower():
                sock.sendall(b"admin\r")
                buf = b""
            elif "welcome" in text.lower() or "copyright" in text.lower() \
                    or "extron" in text.lower() or "smx" in text.lower():
                break
        except socket.timeout:
            break
    return sock


def _send(sock, raw_bytes, read_timeout=1.0, idle=0.15):
    sock.sendall(raw_bytes)
    sock.settimeout(idle)
    buf = b""
    deadline = time.time() + read_timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            if buf:
                break
    return buf.decode("ascii", errors="replace").strip()


def _sanitize_name(name, max_len=12):
    return "".join(c for c in name if 32 <= ord(c) <= 126
                   and c not in _INVALID_CHARS).strip()[:max_len]


def _parse_name(raw):
    cleaned = raw.strip().rstrip("]\r\n")
    if not cleaned:
        return None
    for skip in ("E", "Password:", "Login ", "(c) Copyright"):
        if cleaned.startswith(skip):
            return None
    m = re.match(r"^Nm[A-Za-z]\d+(?:\*\d+)?,(.+)$", cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip() or None
    if "," in cleaned:
        return cleaned.split(",", 1)[1].strip() or None
    return cleaned or None


def poll_info(ip, port=23):
    """Returns (info_str, error)."""
    try:
        sock = _open(ip, port)
        raw = _send(sock, b"I\r", read_timeout=1.5)
        sock.close()
        return raw, None
    except Exception as e:
        return "", str(e)


def poll_ties_plane(ip, port=23, plane="00", n_outputs=N_OUTPUTS):
    """
    Query all tie states for one plane.
    Returns (ties{out->in}, error) — out and in are 1-based ints.
    """
    ties = {}
    try:
        sock = _open(ip, port, timeout=6.0)
        for out in range(1, n_outputs + 1):
            # Query: {plane}*{out}! → response like "In3 Ao4" or "3" or "In0 Ao4"
            cmd = f"{plane}*{out}!".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=0.6, idle=0.08)
            inp = _parse_tie_response(raw)
            ties[out] = inp
        sock.close()
        for o in range(1, n_outputs + 1):
            ties.setdefault(o, 0)
        return ties, None
    except Exception as e:
        return ties, str(e)


def poll_ties_all_planes(ip, port=23, n_outputs=N_OUTPUTS):
    """
    Query tie states for all planes in one connection.
    Returns (planes_ties{plane->{out->in}}, error).
    """
    result = {p: {} for p in PLANE_ORDER}
    err_str = None
    try:
        sock = _open(ip, port, timeout=10.0)
        for plane in PLANE_ORDER:
            for out in range(1, n_outputs + 1):
                cmd = f"{plane}*{out}!".encode("ascii") + CR
                raw = _send(sock, cmd, read_timeout=0.5, idle=0.07)
                result[plane][out] = _parse_tie_response(raw)
        sock.close()
        for plane in PLANE_ORDER:
            for o in range(1, n_outputs + 1):
                result[plane].setdefault(o, 0)
    except Exception as e:
        err_str = str(e)
    return result, err_str


def _parse_tie_response(raw):
    """
    Parse SMX tie query response.
    Could be: "In3 Ao4", "In 3", "Ao4", "0", "3", blank, etc.
    Returns input number (int), 0 = untied.
    """
    raw = raw.strip()
    # "In3 Ao4" or "In 3" → find input number
    m = re.search(r"In\s*(\d+)", raw, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Plain number
    m = re.match(r"^(\d+)$", raw)
    if m:
        return int(m.group(1))
    return 0


def send_tie(ip, port=23, plane="00", input_num=0, output_num=1):
    """
    Tie input to output on the given plane.
    input_num=0 → untie.  Returns (ok, response, error).
    """
    signal = PLANES.get(plane, {}).get("signal", "!")
    try:
        sock = _open(ip, port)
        cmd = f"{plane}*{input_num}*{output_num}{signal}".encode("ascii") + CR
        resp = _send(sock, cmd, read_timeout=1.0)
        sock.close()
        return True, resp, None
    except Exception as e:
        return False, "", str(e)


def send_tie_global(ip, port=23, input_num=0, output_num=1):
    """
    Tie input to output on ALL planes in one connection.
    Returns (ok, errors_list).
    """
    errors = []
    try:
        sock = _open(ip, port)
        for plane in PLANE_ORDER:
            signal = PLANES[plane]["signal"]
            cmd = f"{plane}*{input_num}*{output_num}{signal}".encode("ascii") + CR
            _send(sock, cmd, read_timeout=0.4, idle=0.07)
        sock.close()
        return True, errors
    except Exception as e:
        return False, [str(e)]


def send_ties_batch(ip, port=23, plane="00", input_num=1, outputs=None):
    """
    Tie one input to multiple outputs on a plane.
    Returns (ok, errors_list).
    """
    if not outputs:
        return True, []
    signal = PLANES.get(plane, {}).get("signal", "!")
    errors = []
    try:
        sock = _open(ip, port)
        for out in sorted(outputs):
            cmd = f"{plane}*{input_num}*{out}{signal}".encode("ascii") + CR
            _send(sock, cmd, read_timeout=0.4, idle=0.07)
        sock.close()
        return True, errors
    except Exception as e:
        return False, [str(e)]


def send_ties_batch_global(ip, port=23, input_num=1, outputs=None):
    """Send batch tie on all planes."""
    if not outputs:
        return True, []
    errors = []
    try:
        sock = _open(ip, port)
        for plane in PLANE_ORDER:
            signal = PLANES[plane]["signal"]
            for out in sorted(outputs):
                cmd = f"{plane}*{input_num}*{out}{signal}".encode("ascii") + CR
                _send(sock, cmd, read_timeout=0.3, idle=0.06)
        sock.close()
        return True, errors
    except Exception as e:
        return False, [str(e)]


def recall_preset(ip, port=23, preset_num=1):
    """Recall global preset. Returns (ok, error)."""
    try:
        sock = _open(ip, port)
        cmd = f"Rpr{preset_num:02d}".encode("ascii") + CR
        _send(sock, cmd, read_timeout=1.0)
        sock.close()
        return True, None
    except Exception as e:
        return False, str(e)


def save_preset(ip, port=23, preset_num=1):
    """Save global preset. Returns (ok, error)."""
    try:
        sock = _open(ip, port)
        cmd = f"Spr{preset_num:02d}".encode("ascii") + CR
        _send(sock, cmd, read_timeout=1.0)
        sock.close()
        return True, None
    except Exception as e:
        return False, str(e)


def rename_io(ip, port=23, kind="input", number=1, name=""):
    """Rename input or output. Returns (ok, error)."""
    suffix = {"input": "NI", "output": "NO"}.get(kind)
    if not suffix:
        return False, f"unknown kind: {kind}"
    safe = _sanitize_name(name)
    if not safe:
        return False, "name is empty after sanitization"
    cmd = f"{ESC}{number},{safe}{suffix}".encode("ascii") + CR
    try:
        sock = _open(ip, port)
        resp = _send(sock, cmd, read_timeout=1.0)
        sock.close()
        if resp.strip().startswith("E"):
            return False, f"switcher error: {resp.strip()}"
        return True, None
    except Exception as e:
        return False, str(e)


def poll_names(ip, port=23, n_inputs=N_INPUTS, n_outputs=N_OUTPUTS):
    """
    Poll all I/O names from the switcher.
    Returns (names{inputs:{}, outputs:{}}, error).
    """
    result = {"inputs": {}, "outputs": {}}
    try:
        sock = _open(ip, port, timeout=8.0)
        for i in range(1, n_inputs + 1):
            cmd = f"{ESC}{i}NI".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=0.5, idle=0.06)
            name = _parse_name(raw)
            if name:
                result["inputs"][str(i)] = name
        for o in range(1, n_outputs + 1):
            cmd = f"{ESC}{o}NO".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=0.5, idle=0.06)
            name = _parse_name(raw)
            if name:
                result["outputs"][str(o)] = name
        sock.close()
        return result, None
    except Exception as e:
        return result, str(e)
