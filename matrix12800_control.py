"""
Matrix 12800 control module.

SIS commands used:
  I\\r                        → system info (size, type, virtual map)
  {in}*{out}!\\r              → tie (route) input to output
  0*{out}!\\r                 → untie output
  {out}!\\r                   → query single output tie
  \\x1b0*{start}*1VC\\r      → tie block (16 outputs starting at {start})
  {preset}.\\r                → recall preset
  \\x1b{n}NI/NO/NG\\r        → read input/output/preset name
  \\x1b{n},{name}NI/NO/NG\\r → write name
  \\x1b{n}MI/MO\\r           → virtual port metadata (code, name, phys map)
"""

import socket
import time
import re

CR = b"\r"
ESC = "\x1b"
_TIMEOUT = 4.0
_INVALID_CHARS = set('+~,@=`[]{}\'<>\'";:|\\?')


def _open(ip, port, timeout=_TIMEOUT, user="admin", password="admin"):
    sock = socket.create_connection((ip, port), timeout=timeout)
    sock.settimeout(1.5)
    # Read banner / login prompt and authenticate
    buf = b""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        try:
            buf += sock.recv(4096)
            text = buf.decode("ascii", errors="replace")
            if "login" in text.lower():
                sock.sendall(user.encode("ascii") + b"\r")
                buf = b""
            elif "password" in text.lower():
                sock.sendall(password.encode("ascii") + b"\r")
                buf = b""
            elif "welcome" in text.lower() or "copyright" in text.lower() or "extron" in text.lower():
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


def _parse_virtual_meta(raw):
    """
    Parse response to \\x1b{n}MI or \\x1b{n}MO.
    Response: "{code},{name},{phys1},{phys2},..."
    e.g. "1,PC Main,0i001,0i002,0i003"
    Returns dict {code, name, phys} or None.
    """
    cleaned = raw.strip().rstrip("]\r\n")
    if not cleaned or cleaned.startswith("E"):
        return None
    parts = [p.strip() for p in cleaned.split(",")]
    if len(parts) < 2:
        return None
    code = parts[0]
    name = parts[1][:12]
    phys = [p for p in parts[2:] if p and p != "-----"]
    return {"code": code, "name": name, "phys": phys}


def _parse_tie_block(raw):
    """Parse VC block: 'Vid1 Vid3 -- Vid0 Vid2' → [1, 3, 0, 0, 2]"""
    cleaned = raw.replace("Vid", "").replace("--", "0")
    result = []
    for tok in re.split(r"[\s•]+", cleaned):
        tok = tok.strip()
        if not tok:
            continue
        try:
            result.append(int(tok))
        except ValueError:
            pass
    return result


def poll_info(ip, port=23):
    """
    Connect and query I to get virtual map size.
    Returns (info_str, n_inputs, n_outputs, error).
    """
    try:
        sock = _open(ip, port)
        raw = _send(sock, b"I\r", read_timeout=1.5)
        sock.close()
        # Virtual map: MNNxNN  e.g. M69X69
        m = re.search(r"M(\d+)X(\d+)", raw, re.IGNORECASE)
        if m:
            return raw, int(m.group(1)), int(m.group(2)), None
        # Physical size fallback: NNxNN
        m = re.search(r"(\d+)X(\d+)", raw, re.IGNORECASE)
        if m:
            return raw, int(m.group(1)), int(m.group(2)), None
        return raw, 128, 128, None
    except Exception as e:
        return "", 128, 128, str(e)


def poll_ties(ip, port=23, n_outputs=128):
    """
    Poll all output ties. Uses block query (16 at a time) with per-output fallback.
    Returns (ties{out->in}, error).
    """
    ties = {}
    try:
        sock = _open(ip, port, timeout=6.0)

        # Block queries: ESC 0 * start * 1 VC
        block_ok = True
        for start in range(1, n_outputs + 1, 16):
            cmd = f"{ESC}0*{start}*1VC".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=1.2, idle=0.12)
            parsed = _parse_tie_block(raw)
            expected = min(16, n_outputs - start + 1)
            if len(parsed) < expected:
                block_ok = False
            for j, val in enumerate(parsed):
                out = start + j
                if out <= n_outputs:
                    ties[out] = val

        # Fallback: individual queries for any missing outputs
        if not block_ok:
            for out in range(1, n_outputs + 1):
                if out not in ties:
                    raw = _send(sock, f"{out}!".encode("ascii") + CR, read_timeout=0.6)
                    m = re.search(r"\d+", raw)
                    if m:
                        ties[out] = int(m.group())

        sock.close()
        for o in range(1, n_outputs + 1):
            ties.setdefault(o, 0)
        return ties, None
    except Exception as e:
        return ties, str(e)


def send_tie(ip, port=23, input_num=0, output_num=1):
    """
    Send a single tie.  input_num=0 → untie.
    Returns (ok, response, error).
    """
    try:
        sock = _open(ip, port)
        cmd = f"{input_num}*{output_num}!".encode("ascii") + CR
        resp = _send(sock, cmd, read_timeout=1.0)
        sock.close()
        return True, resp, None
    except Exception as e:
        return False, "", str(e)


def send_ties_batch(ip, port=23, input_num=1, outputs=None):
    """Send multiple ties in one connection."""
    if not outputs:
        return True, []
    errors = []
    try:
        sock = _open(ip, port)
        for out in sorted(outputs):
            cmd = f"{input_num}*{out}!".encode("ascii") + CR
            _send(sock, cmd, read_timeout=0.4, idle=0.08)
        sock.close()
        return True, errors
    except Exception as e:
        return False, [str(e)]


def recall_preset(ip, port=23, preset_num=1):
    """Recall preset {n}."""
    try:
        sock = _open(ip, port)
        cmd = f"{preset_num}.".encode("ascii") + CR
        _send(sock, cmd, read_timeout=1.0)
        sock.close()
        return True, None
    except Exception as e:
        return False, str(e)


def rename_io(ip, port=23, kind="input", number=1, name=""):
    """
    Rename input / output / preset on the switcher.
    Returns (ok, error).
    """
    suffix = {"input": "NI", "output": "NO", "preset": "NG"}.get(kind)
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


def poll_names(ip, port=23, n_inputs=128, n_outputs=128, n_presets=64):
    """
    Poll all names from the switcher.  Returns (names_dict, error).
    names_dict = {inputs:{}, outputs:{}, presets:{}}
    NOTE: polling 128+128+64=320 names takes ~30–60s on the 12800.
    Use poll_bank_names for per-bank lazy loading instead.
    """
    result = {"inputs": {}, "outputs": {}, "presets": {}}
    try:
        sock = _open(ip, port, timeout=10.0)

        def query(suffix, n):
            cmd = f"{ESC}{n}{suffix}".encode("ascii") + CR
            return _send(sock, cmd, read_timeout=0.5, idle=0.06)

        for i in range(1, n_inputs + 1):
            name = _parse_name(query("NI", i))
            if name:
                result["inputs"][str(i)] = name

        for o in range(1, n_outputs + 1):
            name = _parse_name(query("NO", o))
            if name:
                result["outputs"][str(o)] = name

        for p in range(1, n_presets + 1):
            name = _parse_name(query("NG", p))
            if name:
                result["presets"][str(p)] = name

        sock.close()
        return result, None
    except Exception as e:
        return result, str(e)


def poll_bank_names(ip, port=23, kind="input", start=1, count=32):
    """
    Poll names for one bank (start..start+count-1).
    kind: "input" | "output" | "preset"
    Returns (names{number:name}, error).
    """
    suffix = {"input": "NI", "output": "NO", "preset": "NG"}.get(kind)
    if not suffix:
        return {}, f"unknown kind: {kind}"
    result = {}
    try:
        sock = _open(ip, port, timeout=8.0)
        for n in range(start, start + count):
            cmd = f"{ESC}{n}{suffix}".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=0.5, idle=0.06)
            name = _parse_name(raw)
            if name:
                result[n] = name
        sock.close()
        return result, None
    except Exception as e:
        return result, str(e)


def poll_bank_metadata(ip, port=23, kind="input", start=1, count=32):
    """
    Poll virtual port metadata (MI/MO) for one bank.
    Returns (meta{number:{code,name,phys}}, error).
    """
    suffix = {"input": "MI", "output": "MO"}.get(kind)
    if not suffix:
        return {}, f"unknown kind: {kind}"
    result = {}
    try:
        sock = _open(ip, port, timeout=8.0)
        for n in range(start, start + count):
            cmd = f"{ESC}{n}{suffix}".encode("ascii") + CR
            raw = _send(sock, cmd, read_timeout=0.5, idle=0.06)
            meta = _parse_virtual_meta(raw)
            if meta:
                result[n] = meta
        sock.close()
        return result, None
    except Exception as e:
        return result, str(e)
