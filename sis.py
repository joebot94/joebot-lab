"""
Joebot Lab Dashboard - SIS / Telnet comms + parsers.

Everything here is READ-ONLY. We open a socket, optionally log in, send a
short list of query commands, and parse the replies into a structured dict.

A device result looks like:
    {
        "online":  True/False,
        "status":  "ok" | "warn" | "bad" | "gray",
        "summary": "human readable one-liner",
        "details": [ {"label": "...", "value": "...", "state": "ok|warn|bad|"} ],
        "signals": [ {"label": "1", "state": "ok|warn|gray"} ],   # optional
        "raw":     { "0*01S": "1111", ... },                      # hidden in UI
        "error":   "..."                                          # optional
    }
"""

import socket
import time
import re

CR = b"\r"

STATUS_ORDER = {"gray": 0, "ok": 1, "warn": 2, "bad": 3}


def worse(a, b):
    return a if STATUS_ORDER.get(a, 0) >= STATUS_ORDER.get(b, 0) else b


# --------------------------------------------------------------------------- #
# Low level socket helpers
# --------------------------------------------------------------------------- #
def _read(sock, overall_timeout, idle=0.35):
    """Read until the device goes idle for `idle` seconds, capped by overall_timeout."""
    sock.settimeout(idle)
    buf = b""
    deadline = time.time() + overall_timeout
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            if buf:
                break
            # nothing yet; keep waiting until overall deadline
            continue
        except OSError:
            break
    return buf.decode("ascii", errors="replace")


def query(ip, port, commands, timeout=4.0, password=None):
    """
    Open one connection, optionally send a telnet password, then send each
    command (CR terminated) and collect responses.

    Returns (online: bool, replies: dict[cmd->str], error: str|None)
    """
    replies = {}
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        # Connection refused still means the host is alive, but for an SIS
        # device we need the port open, so treat as offline.
        return False, replies, str(e)

    try:
        # Drain banner.
        _read(sock, min(1.5, timeout))
        if password:
            sock.sendall(password.encode("ascii") + CR)
            _read(sock, min(1.5, timeout))
        for cmd in commands:
            sock.sendall(cmd.encode("ascii") + CR)
            replies[cmd] = _read(sock, timeout).strip()
        return True, replies, None
    except OSError as e:
        return (len(replies) > 0), replies, str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def host_alive(ip, port, timeout=3.0):
    """
    Liveness check that does NOT require an open port: a TCP RST
    (ConnectionRefused) proves the host is up just as well as a successful
    connect.  Only a timeout / no-route means down.
    """
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except ConnectionRefusedError:
        return True            # host answered with RST -> alive
    except (socket.timeout, OSError):
        return False


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _tokens(s):
    return (s or "").split()


# Voltage tolerance bands (fraction off nominal).  Power rails on this gear
# read a hair low in normal operation (the DMS +1.3V rail sits ~1.23), so the
# green band is deliberately generous.  Tune here if you want it tighter.
VOLT_GREEN  = 0.075     # <= 7.5% off nominal  -> green
VOLT_YELLOW = 0.15      # <= 15%  off nominal  -> yellow, else red


def volt_state(actual, nominal):
    """Return (state, display) for a rail. nominal=None -> redundant/optional rail."""
    if not _is_num(actual):
        return "warn", "no reading"
    a = float(actual)
    if nominal is None:                       # redundant / optional rail
        if a < 0.5:
            return "gray", "not installed"
        return "ok", f"{a:.2f} V present"
    dev = abs(a - nominal) / nominal
    nom_txt = f"{nominal:g} V nominal"
    if dev <= VOLT_GREEN:
        return "ok", nom_txt
    if dev <= VOLT_YELLOW:
        return "warn", nom_txt + " (off)"
    return "bad", nom_txt + " (OUT OF RANGE)"


# DMS board codes -> (inputs, outputs, label).  See the SIS info-request key.
DMS_BOARDS = {
    "C1": (4, 4, "DVI 4in x 4out"), "C2": (4, 0, "DVI 4in"), "C3": (0, 4, "DVI 4out"),
    "D1": (4, 4, "fiber 4in x 4out"), "D2": (4, 0, "fiber 4in"), "D3": (0, 4, "fiber 4out"),
    "X0": (0, 0, "empty"), "XO": (0, 0, "empty"),
}


def parse_dms_info(info):
    """
    Parse the DMS `I` response, e.g.  V36X36 A00X00 SC1C1C1C1C1C1C2C2C2
    The V field is the frame max and is misleading; the real configured I/O is
    the sum of the per-slot board codes (6x C1 + 3x C2 = 36 in / 24 out).
    """
    v_in = v_out = None
    slots, board_in, board_out, populated = [], 0, 0, 0
    for tok in (info or "").split():
        if tok[:1] == "V" and "X" in tok:
            m = re.match(r"V(\d+)X(\d+)", tok)
            if m:
                v_in, v_out = int(m.group(1)), int(m.group(2))
        elif tok[:1] == "S" and len(tok) > 2:
            codes = tok[1:]
            for i in range(0, len(codes) - 1, 2):
                code = codes[i:i + 2].upper()
                inp, out, desc = DMS_BOARDS.get(code, (0, 0, code))
                slots.append({"slot": len(slots) + 1, "code": code, "desc": desc})
                board_in += inp
                board_out += out
                if code not in ("X0", "XO"):
                    populated += 1
    return {"v_in": v_in, "v_out": v_out, "slots": slots,
            "total_slots": len(slots), "populated": populated,
            "in": board_in, "out": board_out}


def _group_boards(slots):
    """6x C1 (DVI 4in x 4out), 3x C2 (DVI 4in)"""
    out, last, count = [], None, 0
    for sl in slots + [None]:
        code = sl["code"] if sl else None
        if code == last:
            count += 1
        else:
            if last and last not in ("X0", "XO"):
                desc = DMS_BOARDS.get(last, (0, 0, last))[2]
                out.append(f"{count}x {last} ({desc})")
            last, count = code, 1
    return ", ".join(out) if out else "none"


# --------------------------------------------------------------------------- #
# Matrix 12800
# --------------------------------------------------------------------------- #
def parse_matrix12800(replies):
    fans = _digits(replies.get("0*01S", ""))[:4]
    psu  = _digits(replies.get("0*02S", ""))[:4]
    ctrl = _digits(replies.get("0*03S", ""))[:2]

    details, worst = [], "ok"

    def bump(level):
        nonlocal worst
        order = {"ok": 0, "warn": 1, "bad": 2}
        if order[level] > order[worst]:
            worst = level

    # Fans: 0 not installed, 1 ok, 2 failed
    if len(fans) == 4:
        bad_fans = fans.count("2")
        installed = sum(c in "12" for c in fans)
        fstate = "bad" if bad_fans else "ok"
        bump(fstate)
        details.append({"label": "Fans",
                        "value": f"{installed - bad_fans}/{installed} OK ({fans})",
                        "state": fstate})
    else:
        bump("warn")
        details.append({"label": "Fans", "value": "no reading", "state": "warn"})

    # PSU split-pair redundancy: need a working unit in {1,2} AND in {3,4}
    if len(psu) == 4:
        pair_a = "1" in psu[0:2]
        pair_b = "1" in psu[2:4]
        psu_map = {"0": ("not installed", ""), "1": ("OK", "ok"),
                   "2": ("inactive", "")}
        for i, c in enumerate(psu):
            txt, st = psu_map.get(c, ("?", "warn"))
            details.append({"label": f"PSU {i + 1}", "value": txt, "state": st})
        if pair_a and pair_b:
            pstate = "ok"
            redun = "split-pair redundancy OK (one of 1-2 + one of 3-4)"
        else:
            pstate = "bad"
            redun = "REDUNDANCY LOST"
        bump(pstate)
        details.append({"label": "Redundancy", "value": f"{redun} ({psu})",
                        "state": pstate})
    else:
        bump("warn")
        details.append({"label": "PSUs", "value": "no reading", "state": "warn"})

    # Controller: 0 not installed, 1 ok, 2/3/4 failure modes
    if len(ctrl) == 2:
        cmap = {"0": "not installed", "1": "OK", "2": "FAIL +rail",
                "3": "FAIL both rails", "4": "FAIL -rail"}
        prim = cmap.get(ctrl[0], "?")
        sec  = cmap.get(ctrl[1], "?")
        cstate = "bad" if ctrl[0] in "234" else "ok"
        bump(cstate)
        details.append({"label": "Controller",
                        "value": f"primary {prim}, secondary {sec} ({ctrl})",
                        "state": cstate})
    else:
        bump("warn")
        details.append({"label": "Controller", "value": "no reading", "state": "warn"})

    info = replies.get("I", "").strip()
    if info:
        details.append({"label": "Matrix info", "value": info[:60], "state": ""})

    # I/O card status: 0*04S = input cards, 0*05S = output cards
    card_rows = []
    for label, key in [("Input cards", "0*04S"), ("Output cards", "0*05S")]:
        dots = parse_card_status(replies.get(key, ""))
        if dots:
            bad = sum(1 for d in dots if d["state"] == "bad")
            if bad:
                bump("bad")
            card_rows.append({"label": label, "dots": dots})

    summary = {"ok": "Healthy", "warn": "Degraded (redundancy holding)",
               "bad": "FAULT"}[worst]
    return {"status": worst, "summary": summary, "details": details,
            "card_rows": card_rows}


# --------------------------------------------------------------------------- #
# DMS 3600
# --------------------------------------------------------------------------- #
def parse_dms3600(replies):
    s    = _tokens(replies.get("S", ""))
    ls   = _digits(replies.get("0LS", ""))
    info = replies.get("I", "").strip()
    fw   = replies.get("Q", "").strip()

    details, status = [], "ok"

    def bump(st):
        nonlocal status
        order = {"ok": 0, "warn": 1, "bad": 2}
        if order.get(st, 0) > order[status]:
            status = st

    # ---- configured size / slots (from board codes, not the V field) -------
    bi = parse_dms_info(info)
    cfg_in  = bi["in"]  or bi["v_in"]  or 0
    cfg_out = bi["out"] or bi["v_out"] or 0
    if bi["total_slots"]:
        details.append({"label": "Configured matrix",
                        "value": f"{cfg_in} x {cfg_out}  ({cfg_in} in / {cfg_out} out)",
                        "state": "ok"})
        details.append({"type": "signals_here"})   # signal dots render inline here

    # ---- power rails as colored dots only (actual values -> raw/debug) ------
    _DMS_RAILS = [("+3.3",   0, 3.3),  ("+5",     1, 5.0),
                  ("+1.3",   2, 1.3),  ("+1.2",   3, 1.2),
                  ("+12sys", 4, 12.0), ("redund",  5, None),
                  ("+12pri", 6, 12.0)]
    rail_dots = []
    if len(s) >= 7:
        rd, rv = build_rail_dots(s, _DMS_RAILS)
        rail_dots = rd
        bump(rv)

        # temperature (kept as a real reading; > ~100F warns)
        if _is_num(s[7]):
            t = float(s[7])
            tstate = "ok" if t < 95 else ("warn" if t < 105 else "bad")
            bump(tstate)
            details.append({"label": "Temperature", "value": f"{t:.1f} °F", "state": tstate})

        # fans (0 RPM = stopped = bad)
        for i, lab in enumerate(["Fan 1", "Fan 2", "Fan 3", "Fan 4"]):
            rpm = int(s[8 + i]) if s[8 + i].isdigit() else 0
            fstate = "ok" if rpm > 0 else "bad"
            bump(fstate)
            details.append({"label": lab, "value": f"{rpm} RPM", "state": fstate})

        prim_ok = s[12] == "1"
        bump("ok" if prim_ok else "bad")
        details.append({"label": "Primary PSU", "value": "OK" if prim_ok else "FAULT",
                        "state": "ok" if prim_ok else "bad"})
        details.append({"label": "Redundant PSU",
                        "value": "installed" if s[13] == "1" else "not installed",
                        "state": ""})
    else:
        bump("warn")
        details.append({"label": "Health", "value": "no S reading", "state": "warn"})

    if fw:
        details.append({"label": "Firmware", "value": fw, "state": ""})

    # ---- signal map (configured inputs) ------------------------------------
    signals, active = [], 0
    width = cfg_in or 36
    if ls:
        for i, c in enumerate(ls[:max(width, 36)], start=1):
            on = c == "1"
            active += on
            signals.append({"label": str(i), "state": "ok" if on else "gray"})

    summary = f"{cfg_in or 36}x{cfg_out or '?'} • {active}/{cfg_in or 36} inputs active"
    if status == "bad":
        summary = "FAULT — " + summary
    return {"status": status, "summary": summary, "details": details,
            "rail_dots": rail_dots, "signals": signals, "info": info}


# --------------------------------------------------------------------------- #
# SMX
# --------------------------------------------------------------------------- #
def parse_smx(replies, slot_meta):
    s = _tokens(replies.get("S", ""))
    details, status = [], "ok"

    rail_dots = []
    if len(s) >= 8:
        _SMX_RAILS = [("+3.3", 0, 3.3), ("+5", 1, 5.0), ("+24", 2, 24.0)]
        rd, rv = build_rail_dots(s, _SMX_RAILS)
        rail_dots, status = rd, worse(status, rv)
        if _is_num(s[3]):
            t = float(s[3])
            tstate = "ok" if t < 100 else ("warn" if t < 110 else "bad")
            details.append({"label": "Temperature", "value": f"{t:.1f} °F", "state": tstate})
        for i, lab in enumerate(["Fan 1", "Fan 2"]):
            rpm = int(s[4 + i]) if s[4 + i].isdigit() else 0
            details.append({"label": lab, "value": f"{rpm} RPM",
                            "state": "ok" if rpm > 0 else "bad"})
        prim_ok = s[6] == "1"
        details.append({"label": "Primary PSU", "value": "OK" if prim_ok else "FAULT",
                        "state": "ok" if prim_ok else "bad"})
        details.append({"label": "Redundant PSU",
                        "value": "installed" if s[7] == "1" else "not installed",
                        "state": ""})
        if not prim_ok:
            status = "bad"
    else:
        status = "warn"
        details.append({"label": "Health", "value": "no S reading", "state": "warn"})

    # ── dynamic board discovery ──────────────────────────────────────────────
    # We query n*0LS (video/signal presence) and n*4LS (audio level-sense)
    # for slots 1-12. A slot with a card returns a non-empty digit string.
    # Audio cards respond to 4LS rather than 0LS; detect by which one fires.
    # Use slot_meta for human labels when available, fall back to "Slot N".
    boards = []
    total_active = 0
    seen_slots = set()

    # Build label lookup from slot_meta (kept for backward compat)
    meta_by_slot = slot_meta or {}

    for slot in range(1, 13):
        audio_mode = False
        ls_raw = _digits(replies.get(f"{slot}*0LS", ""))
        if not ls_raw:
            # Try audio LS — audio cards don't respond to 0LS
            ls_raw = _digits(replies.get(f"{slot}*4LS", ""))
            if ls_raw:
                audio_mode = True
        if not ls_raw:
            continue   # no card in this slot

        seen_slots.add(slot)
        meta = meta_by_slot.get(slot, {})
        label = meta.get("label", f"Slot {slot}")
        plane = meta.get("plane", "??")

        if audio_mode:
            # Audio cards: signal presence detection is unreliable via LS;
            # show port count but mark as audio — no signal dots
            n_ports = len(ls_raw)
            boards.append({
                "slot": slot, "plane": plane, "label": label,
                "signals": [],   # no dots for audio
                "audio": True, "port_count": n_ports,
            })
        else:
            dots = []
            for i, c in enumerate(ls_raw[:16], start=1):
                if c == "3":
                    st, on = "ok", True
                elif c in "12":
                    st, on = "warn", True
                else:
                    st, on = "gray", False
                total_active += on
                dots.append({"label": str(i), "state": st})
            boards.append({
                "slot": slot, "plane": plane, "label": label,
                "signals": dots, "audio": False,
            })

    summary = f"{len(boards)} boards • {total_active} active signal(s)"
    if status == "bad":
        summary = "PSU FAULT — " + summary
    return {"status": status, "summary": summary, "details": details,
            "rail_dots": rail_dots, "boards": boards}


# MGP signal type codes (X503)
MGP_TYPE = {
    "0": "—",       "1": "RGB",      "2": "YUV-HD",  "3": "RGBcvS",
    "4": "YUVi",    "5": "S-Video",  "6": "Composite", "7": "DVI/HDI",
}
# MGP signal standard codes (X509)
MGP_STD  = {
    "0": "no sync", "1": "NTSC", "2": "PAL", "4": "SECAM", "-": "N/A",
}


# --------------------------------------------------------------------------- #
# MGP 464 — windows + temperature
# --------------------------------------------------------------------------- #
def parse_mgp(replies):
    banner = replies.get("I", "").strip()
    model = ver = ""
    m = re.search(r"(MGP\s*\w+).*?(V[\d.]+)", banner)
    if m:
        model, ver = m.group(1), m.group(2)

    model_name = replies.get("1I", "").strip()
    model_desc = replies.get("2I", "").strip()
    part       = replies.get("N",  "").strip()

    details = []
    if model_name:
        details.append({"label": "Model",       "value": model_name, "state": ""})
    if model_desc:
        details.append({"label": "Description", "value": model_desc, "state": ""})
    if part:
        details.append({"label": "Part",        "value": part,       "state": ""})
    if ver:
        details.append({"label": "Firmware",    "value": ver,        "state": ""})

    # Internal temperature (20S -> "084.20")
    temp_raw = replies.get("20S", "")
    tm = re.search(r"[\d.]+", temp_raw)
    if tm and _is_num(tm.group()):
        t = float(tm.group())
        tstate = "ok" if t < 100 else ("warn" if t < 110 else "bad")
        details.append({"label": "Temperature", "value": f"{t:.1f} °F", "state": tstate})

    # Per-window input info: 1*I .. 4*I -> "Chn05 Typ6 Std0 Blk0"
    windows, active = [], 0
    for w in range(1, 5):
        raw = replies.get(f"{w}*I", "")
        wm = re.search(
            r"Chn(\d+)\s+Typ(\d+)\s+Std(-|\d+)\s+Blk(\d+)", raw)
        if wm:
            chan = int(wm.group(1))
            typ  = wm.group(2)
            std  = wm.group(3)
            blk  = wm.group(4)
            type_label = MGP_TYPE.get(typ, typ)

            if blk == "1":
                state, std_label = "warn", "muted"
            elif std == "-":                          # DVI/HDI — std not applicable
                state, std_label = "ok", "DVI conn"
                active += 1
            elif std != "0":                          # NTSC / PAL / SECAM
                state, std_label = "ok", MGP_STD.get(std, std)
                active += 1
            else:
                state, std_label = "gray", "no sync"

            windows.append({
                "window":    w,
                "input":     chan,
                "type":      type_label,
                "std":       std_label,
                "state":     state,
            })
        else:
            # Command sent but no parseable reply yet
            windows.append({
                "window": w, "input": 0, "type": "—",
                "std": "—", "state": "gray",
            })

    # metadata (preset range etc) stays in raw/debug only now
    summary = (f"{model_name or model or 'MGP'} {ver} • {active}/4 windows active".strip())
    return {"status": "ok", "summary": summary, "details": details,
            "windows": windows}


# --------------------------------------------------------------------------- #
# IPL T PCS4 power controller (read-only query of outlet 2)
# --------------------------------------------------------------------------- #
def parse_pcs4(replies):
    pc = replies.get("2PC", "")
    ps = replies.get("2PS", "")
    details = []
    state_txt, dot = "unknown", ""
    m = re.search(r"(\d)\s*$", pc.strip())
    if m:
        on = m.group(1) == "1"
        state_txt = "ON" if on else "OFF"
        dot = "ok" if on else "warn"   # OFF isn't a fault, just informational
    details.append({"label": "Outlet 2 (Matrix 12800)", "value": state_txt, "state": dot})
    if ps.strip():
        details.append({"label": "Outlet 2 current", "value": ps.strip(), "state": ""})
    details.append({"label": "Control", "value": "READ-ONLY (V1)", "state": ""})
    return {"status": "ok", "summary": f"Outlet 2: {state_txt}", "details": details}


# --------------------------------------------------------------------------- #
# Generic Extron info
# --------------------------------------------------------------------------- #
def parse_extron_info(replies):
    info = replies.get("I", "").strip()
    part = replies.get("N", "").strip()
    fw   = replies.get("Q", "").strip() or replies.get("*Q", "").strip()
    details = []
    if info:
        details.append({"label": "Info", "value": info[:120], "state": ""})
    if part:
        details.append({"label": "Part / N", "value": part, "state": ""})
    if fw:
        details.append({"label": "Firmware", "value": fw, "state": ""})
    summary = info[:48] if info else "online"
    return {"status": "ok", "summary": summary, "details": details}


def _is_num(x):
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def parse_card_status(resp):
    """
    Parse 0*04S / 0*05S card-status response.
    Each character is one card slot: 0=not installed, 1=OK,
    2=failed +rail, 3=failed both, 4=failed -rail.
    Returns list of {state} dicts.
    """
    dots = []
    for c in re.sub(r"\s", "", resp or ""):
        if c == "1":
            dots.append({"state": "ok"})
        elif c == "0":
            dots.append({"state": "gray"})
        elif c in "234":
            dots.append({"state": "bad"})
        else:
            dots.append({"state": "warn"})
    return dots


def build_rail_dots(tokens, rail_config):
    """
    Build a compact rail_dots list: [{label, state}, ...].
    rail_config: [(short_label, token_index, nominal)]  nominal=None -> optional rail.
    Returns (rail_dots, worst_state).
    """
    dots, worst = [], "ok"
    for label, idx, nom in rail_config:
        val = tokens[idx] if idx < len(tokens) else None
        st, _ = volt_state(val, nom)
        if st not in ("gray",):
            worst = worse(worst, st)
        dots.append({"label": label, "state": st})
    return dots, worst


# --------------------------------------------------------------------------- #
# IPCP Pro 505 — specialized query + parser
# --------------------------------------------------------------------------- #

def query_ipcp505(ip, port=23, timeout=4.0, serial_ports=None):
    """
    Specialized query for IPCP Pro 505.
    Handles plain SIS commands plus ESC-prefixed 12V power queries.
    Also polls any serial-bridge devices listed in serial_ports:
      serial_ports = {port_num: {"kind": "vsc700", "label": "VSC 700 #1"}, ...}
    Returns (online, replies_dict, error)
    """
    replies = {}
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except (socket.timeout, ConnectionRefusedError, OSError) as e:
        return False, replies, str(e)

    try:
        _read(sock, min(1.5, timeout))   # drain banner

        # 505 responds in <50ms on LAN; 150ms idle gives plenty of margin
        def send(raw_bytes, key, rt=0.8):
            sock.sendall(raw_bytes)
            replies[key] = _read(sock, rt, idle=0.15).strip()

        # Basic info
        for cmd in ["1Q", "N", "1I", "2I"]:
            send(cmd.encode("ascii") + b"\r", cmd)

        # Relay states (8): "01O" = port 01, view state (capital O, not zero)
        for i in range(1, 9):
            send(f"{i:02d}O".encode("ascii") + b"\r", f"rly{i}")

        # 12V power port on/off: ESC P {port} DCPP CR
        for i in range(1, 5):
            send(f"\x1bP{i:02d}DCPP".encode("ascii") + b"\r", f"pwr{i}")

        # Total 12V power draw (tenths of watts)
        send(b"\x1bADCPP\r", "pwrtotal")

        # Load condition: 0=ok (<40W), 1=near limit (40-44W), 2=fault (>44W)
        send(b"\x1bSDCPP\r", "pwrload")

        # Serial bridge passthrough: only poll ports marked connected=True
        # When nothing is wired this block is skipped entirely.
        # To enable: set meta key "serial_01_connected": "true" in config.
        for pnum, pinfo in (serial_ports or {}).items():
            if not pinfo.get("connected"):
                continue
            key = f"serial_{pnum:02d}"
            cmd = f"\x1b{pnum:02d}\x1eI".encode("ascii") + b"\r"
            send(cmd, key, rt=2.0)   # serial devices need more time

        return True, replies, None
    except OSError as e:
        return (len(replies) > 0), replies, str(e)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def parse_ipcp505(replies, serial_ports=None):
    details = []
    status  = "ok"

    def bump(st):
        nonlocal status
        order = {"ok": 0, "warn": 1, "bad": 2}
        if order.get(st, 0) > order[status]:
            status = st

    # ── Basic info ───────────────────────────────────────────────────────────
    fw    = replies.get("1Q", "").strip()
    part  = replies.get("N",  "").strip()
    model = replies.get("1I", "").strip()

    if model:
        details.append({"label": "Model",    "value": model, "state": ""})
    if part:
        details.append({"label": "Part",     "value": part,  "state": ""})
    if fw:
        details.append({"label": "Firmware", "value": fw,    "state": ""})

    # ── Relay states (8 relays) ───────────────────────────────────────────────
    # Response: "Cpn1*Rly0" (open) or "Cpn1*Rly1" (closed)
    relay_signals = []
    relay_closed  = 0
    for i in range(1, 9):
        raw = replies.get(f"rly{i}", "").strip()
        # Response is "1" (closed/energized) or "0" (open/de-energized)
        if raw in ("0", "1"):
            closed = raw == "1"
            relay_closed += closed
            relay_signals.append({"label": f"R{i}", "state": "ok" if closed else "gray"})
        else:
            relay_signals.append({"label": f"R{i}", "state": "gray"})

    # ── 12V power ports (4) ───────────────────────────────────────────────────
    # Response: "WP 01DCPP |1" — value is after the pipe
    pwr_rail_dots = []
    pwr_on_count  = 0
    for i in range(1, 5):
        raw = replies.get(f"pwr{i}", "")
        m   = re.search(r"\|(\d)", raw) or re.search(r"(\d)\s*$", raw)
        if m:
            on = m.group(1) == "1"
            pwr_on_count += on
            pwr_rail_dots.append({"label": f"12V-{i}", "state": "ok" if on else "gray"})
        else:
            pwr_rail_dots.append({"label": f"12V-{i}", "state": "gray"})

    # ── Total power draw ─────────────────────────────────────────────────────
    pwr_raw  = replies.get("pwrtotal", "")
    load_raw = replies.get("pwrload",  "")
    mp = re.search(r"\|(\d+)", pwr_raw) or re.search(r"(\d+)\s*$", pwr_raw)
    if mp:
        watts = int(mp.group(1)) / 10.0
        ml = re.search(r"\|(\d)", load_raw) or re.search(r"(\d)\s*$", load_raw)
        lval = ml.group(1) if ml else ""
        lstate, ltxt = {"0": ("ok",   "normal"),
                        "1": ("warn", "near limit (40-44 W)"),
                        "2": ("bad",  "OVERLOAD >44 W")}.get(lval, ("", ""))
        if lstate:
            bump(lstate)
        details.append({"label": "12V power draw",
                        "value": f"{watts:.1f} W" + (f" — {ltxt}" if ltxt else ""),
                        "state": lstate})

    # ── Serial bridge device results ─────────────────────────────────────────
    serial_details = []
    for pnum, pinfo in (serial_ports or {}).items():
        key  = f"serial_{pnum:02d}"
        raw  = replies.get(key, "").strip()
        kind = pinfo.get("kind", "")
        label = pinfo.get("label", f"COM{pnum:02d}")
        if raw and len(raw) > 2:
            serial_details.append({"label": label, "value": raw[:60], "state": "ok"})
        else:
            serial_details.append({"label": label, "value": "no response", "state": "gray"})

    details.extend(serial_details)

    active_serial = sum(1 for d in serial_details if d["state"] == "ok")
    summary = (f"IPCP Pro 505 · {relay_closed}/8 relays · "
               f"{pwr_on_count}/4 12V on"
               + (f" · {active_serial}/{len(serial_details)} serial" if serial_details else ""))

    return {
        "status":          status,
        "summary":         summary,
        "details":         details,
        "signals":         relay_signals,
        "signals_label":   f"RELAYS ({relay_closed}/8 closed)",
        "rail_dots":       pwr_rail_dots,
        "rail_dots_label": "12V POWER PORTS",
    }


# --------------------------------------------------------------------------- #
# Command sets per kind
# --------------------------------------------------------------------------- #
COMMANDS = {
    "matrix12800": ["0*01S", "0*02S", "0*03S", "0*04S", "0*05S", "I"],
    "dms3600":     ["S", "I", "0LS", "N", "Q"],
    # Poll slots 1-12 with 0LS (video/signal) and 4LS (audio level-sense).
    # parse_smx detects which slots actually responded with valid data.
    "smx":         ["S", "I"] + [f"{n}*0LS" for n in range(1, 13)]
                               + [f"{n}*4LS" for n in range(1, 13)],
    "mgp":         ["I", "1I", "2I", "N", "20S", "1*I", "2*I", "3*I", "4*I"],
    "pcs4":        ["2PC", "2PS"],
    "extron_info": ["I", "N", "Q"],
    "ipcp505":     [],   # handled by query_ipcp505(), not the generic query()
}
