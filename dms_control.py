"""
DMS 3600 control module — V2 write commands.

All writes are video tie commands only:
  - Tie: {input}*{output}!\\r
  - Preset recall: {preset}.\\r

Polling (read-only):
  - Ties block:    \\x1b0*{start}*1VC\\r  → 16 ties at a time
  - Signal bitmap: 0LS\\r                 → "110100..." string
"""

import socket
import time
import re

CR = b"\r"
_TIMEOUT = 4.0


def _open(ip, port, timeout=_TIMEOUT):
    """Open connection and drain banner. Returns socket or raises."""
    sock = socket.create_connection((ip, port), timeout=timeout)
    sock.settimeout(1.2)
    try:
        sock.recv(4096)
    except socket.timeout:
        pass
    return sock


def _send(sock, raw_bytes, read_timeout=1.0, idle=0.2):
    """Send bytes, read response with idle-gap detection."""
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


def _parse_tie_block(raw):
    """
    Parse the VC block response.
    e.g. "Vid1 Vid3 -- Vid0 Vid2" → [1, 3, 0, 0, 2]
    -- means untied → 0.
    """
    cleaned = raw.replace("Vid", "").replace("--", "0")
    tokens = re.split(r"[\s•]+", cleaned)
    result = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        try:
            result.append(int(tok))
        except ValueError:
            pass
    return result


def poll_state(ip, port=23, n_inputs=36, n_outputs=24):
    """
    Poll tie state and input signal presence from the DMS 3600.

    Returns:
        ties    dict[int, int]  output -> input (0 = untied)
        signals dict[int, bool] input  -> has signal
        error   str | None
    """
    ties = {}
    signals = {}
    sock = None
    try:
        sock = _open(ip, port)

        # Signal presence bitmap (0LS)
        raw_sig = _send(sock, b"0LS\r", read_timeout=1.0)
        # Response is the bitmap directly, e.g. "110101..."
        bitmap = re.sub(r"[^01]", "", raw_sig)
        for i, c in enumerate(bitmap[:n_inputs], start=1):
            signals[i] = (c == "1")

        # Tie state — block queries (16 outputs per call)
        for start in range(1, n_outputs + 1, 16):
            cmd = f"\x1b0*{start}*1VC".encode("ascii") + CR
            raw_ties = _send(sock, cmd, read_timeout=1.2)
            parsed = _parse_tie_block(raw_ties)
            for j, val in enumerate(parsed):
                out_num = start + j
                if out_num <= n_outputs:
                    ties[out_num] = val

        # Fill any missing outputs as untied
        for o in range(1, n_outputs + 1):
            ties.setdefault(o, 0)

        return ties, signals, None
    except Exception as e:
        return {}, {}, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def send_tie(ip, port=23, input_num=1, output_num=1):
    """
    Send a single tie: {input}*{output}!
    Returns (ok: bool, response: str, error: str|None)
    """
    sock = None
    try:
        sock = _open(ip, port)
        cmd = f"{input_num}*{output_num}!".encode("ascii") + CR
        resp = _send(sock, cmd, read_timeout=1.0)
        return True, resp, None
    except Exception as e:
        return False, "", str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def send_ties_batch(ip, port=23, input_num=1, outputs=None):
    """
    Send multiple ties in a single connection.
    Returns (ok: bool, errors: list[str])
    """
    if not outputs:
        return True, []
    errors = []
    sock = None
    try:
        sock = _open(ip, port)
        for output_num in sorted(outputs):
            cmd = f"{input_num}*{output_num}!".encode("ascii") + CR
            _send(sock, cmd, read_timeout=0.5)
        return True, errors
    except Exception as e:
        return False, [str(e)]
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def recall_preset(ip, port=23, preset_num=1):
    """
    Recall preset {n}.
    Returns (ok: bool, error: str|None)
    """
    sock = None
    try:
        sock = _open(ip, port)
        cmd = f"{preset_num}.".encode("ascii") + CR
        _send(sock, cmd, read_timeout=1.0)
        return True, None
    except Exception as e:
        return False, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


_INVALID_CHARS = set(''+'+~,@=`[]{}\'<>\'";:|\\?')

def _sanitize_name(name, max_len=12):
    """Filter invalid chars per Extron SIS spec, trim to 12 chars."""
    return ''.join(c for c in name if 32 <= ord(c) <= 126 and c not in _INVALID_CHARS).strip()[:max_len]


def rename_io(ip, port=23, kind="input", number=1, name=""):
    """
    Rename an input, output, or preset on the switcher.
      Input:  \\x1b{n},{name}NI\\r
      Output: \\x1b{n},{name}NO\\r
      Preset: \\x1b{n},{name}NG\\r
    Returns (ok, error).
    """
    suffix = {"input": "NI", "output": "NO", "preset": "NG"}.get(kind)
    if not suffix:
        return False, f"unknown kind: {kind}"
    safe = _sanitize_name(name)
    if not safe:
        return False, "name is empty after sanitization"
    cmd = f"\x1b{number},{safe}{suffix}".encode("ascii") + CR
    sock = None
    try:
        sock = _open(ip, port)
        resp = _send(sock, cmd, read_timeout=1.0)
        # Error response starts with 'E'
        if resp.strip().startswith('E'):
            return False, f"switcher error: {resp.strip()}"
        return True, None
    except Exception as e:
        return False, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def _parse_name(raw):
    """
    Parse name SIS responses like:
      NmI3,PC-1         (input 3 named PC-1)
      NmO5,Monitor B    (output 5 named Monitor B)
      NmG2,Scene A      (preset 2 named Scene A)
    Returns the name string or None if unparseable / error.
    """
    cleaned = raw.strip().rstrip(']\r\n')
    if not cleaned:
        return None
    # Skip error / auth responses
    for skip in ('E', 'Password:', 'Login ', '(c) Copyright'):
        if cleaned.startswith(skip):
            return None
    # Tagged format:  Nm[IOG]\d+,name  (or Nm[IOG]\d+*\d+,name)
    m = re.match(r'^Nm[A-Za-z]\d+(?:\*\d+)?,(.+)$', cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip() or None
    # Fallback: anything after a comma
    if ',' in cleaned:
        return cleaned.split(',', 1)[1].strip() or None
    return cleaned or None


def poll_names(ip, port=23, n_inputs=36, n_outputs=24, n_presets=32):
    """
    Poll all input, output, and preset names from the DMS in one connection.

    Commands:
      \\x1b{n}NI\\r  → input n name
      \\x1b{n}NO\\r  → output n name
      \\x1b{n}NG\\r  → preset n name

    Returns (names_dict, error) where names_dict = {inputs:{}, outputs:{}, presets:{}}
    """
    result = {"inputs": {}, "outputs": {}, "presets": {}}
    sock = None
    try:
        sock = _open(ip, port, timeout=8.0)

        def query(suffix, n):
            cmd = f"\x1b{n}{suffix}".encode("ascii") + CR
            # Short idle — DMS on LAN responds in <30ms
            return _send(sock, cmd, read_timeout=0.5, idle=0.08)

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

        return result, None
    except Exception as e:
        return result, str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
