"""
Auto-Switching module for Joebot Lab.
Polls SMX devices for active inputs and fires routing rules.

Features:
- Multi-action rules: one source trigger → route to N destinations + recall presets
- Destination modes: newest_wins (default) or keep_current (hold until source goes dark)
- Spill routing: if first destination is held, try the next action in the list
- Preset actions: recall an SMX preset number as a rule action
- Freq gating: 15kHz / 31kHz conditions gate rules before firing
"""

import json
import os
import re
import socket
import threading
import time
import uuid
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

import config_store
from shared import log

router = APIRouter()

CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/config")
AS_PATH = os.path.join(CONFIG_DIR, "autoswitch.json")
_store_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Data store
# --------------------------------------------------------------------------- #

def _defaults() -> dict:
    return {
        "sources": [],
        "destinations": [],
        "rules": [],
        "engine": {"enabled": True, "poll_interval": 2.0},
    }


def _load() -> dict:
    try:
        if os.path.exists(AS_PATH):
            with open(AS_PATH) as f:
                d = json.load(f)
            base = _defaults()
            base.update(d)
            return base
    except Exception:
        pass
    return _defaults()


def _save(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with _store_lock:
        with open(AS_PATH, "w") as f:
            json.dump(data, f, indent=2)


# Engine runtime state (last fired event) — persisted separately from config
# so it survives container rebuilds/redeploys.
STATE_PATH = os.path.join(CONFIG_DIR, "autoswitch_state.json")


def _load_state() -> dict:
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_state(data: dict):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log(f"autoswitch: state save failed: {e}")


# --------------------------------------------------------------------------- #
# SMX connection (persistent per-device, reconnects automatically)
# --------------------------------------------------------------------------- #

SLOT_SIZES = {
    "15": (16, 16), "09": (8, 4), "08": (8, 8), "07": (8, 8),
    "06": (8, 4),   "05": (4, 8), "04": (4, 4), "00": (0, 0),
}


class _SMXConn:
    """Persistent Telnet connection to an SMX switcher, thread-safe."""

    def __init__(self, ip: str, port: int = 23, timeout: float = 4.0):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def _reconnect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((self.ip, self.port))
            s.settimeout(0.5)
            deadline = time.time() + 1.5
            while time.time() < deadline:
                try:
                    if not s.recv(4096):
                        break
                except socket.timeout:
                    break
            s.settimeout(self.timeout)
            self._sock = s
            return True
        except Exception as e:
            log(f"autoswitch: connect {self.ip} failed: {e}")
            return False

    def send(self, cmd: str) -> str:
        with self._lock:
            for _ in range(2):
                if self._sock is None and not self._reconnect():
                    return ""
                try:
                    self._sock.sendall(f"{cmd}\r\n".encode())
                    self._sock.settimeout(2.0)
                    resp = b""
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        try:
                            chunk = self._sock.recv(4096)
                            if not chunk:
                                raise OSError("closed")
                            resp += chunk
                            if b"\n" in resp:
                                break
                        except socket.timeout:
                            break
                    return resp.decode("ascii", errors="ignore").strip()
                except Exception as e:
                    log(f"autoswitch: send error {self.ip}: {e}")
                    try:
                        self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
            return ""

    def close(self):
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class _Engine:
    """Polls SMX devices, fires routing rules on signal change."""

    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._conns: Dict[str, _SMXConn] = {}
        # device_id → {slot_idx: input_count}
        self._slot_cache: Dict[str, Dict[int, int]] = {}
        # device_id → set of currently active global input numbers
        self._active: Dict[str, Set[int]] = {}
        # dest_id → source_id currently routed there (for keep_current mode)
        self._current_route: Dict[str, str] = {}
        # device_id → True (responding) / False (unreachable) / absent (never polled)
        self._device_ok: Dict[str, bool] = {}
        # Status
        self.poll_count = 0
        self.last_event: Optional[str] = None
        self.last_event_time: Optional[float] = None
        self.recent_errors: List[str] = []
        # Rolling event feed shown on the control page
        self.events: List[dict] = []
        # Restore last-fired across restarts/redeploys
        st = _load_state()
        self.last_event = st.get("last_event")
        self.last_event_time = st.get("last_event_time")

    def _event(self, kind: str, text: str):
        """kind: fire | test | release | offline | online | error"""
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        if len(self.events) > 60:
            self.events = self.events[-60:]

    # ── connection management ────────────────────────────────────────────────

    def _conn(self, device_id: str) -> Optional[_SMXConn]:
        if device_id not in self._conns:
            devices = config_store.get_devices()
            dev = next(
                (d for d in devices if d["id"] == device_id and d.get("kind") == "smx"),
                None,
            )
            if not dev:
                return None
            self._conns[device_id] = _SMXConn(dev["ip"])
        return self._conns[device_id]

    # ── slot discovery ───────────────────────────────────────────────────────

    def _slot_inputs(self, device_id: str) -> Dict[int, int]:
        """Return {slot_idx: input_count}. Cached; refreshes every 300 polls."""
        if device_id in self._slot_cache and self.poll_count % 300 != 0:
            return self._slot_cache[device_id]
        conn = self._conn(device_id)
        if not conn:
            return {}
        resp = conn.send("*N")
        if not resp:
            return {}
        clean = resp.strip().replace("\r", "").replace("\n", "")
        if "." in clean:
            clean = clean.split(".", 1)[1]
        codes = re.findall(r"([A-Z])(\d{2})", clean.upper())
        result: Dict[int, int] = {}
        for slot_idx, (type_code, size_code) in enumerate(codes[:10], start=1):
            if type_code == "X":
                continue
            inputs, _ = SLOT_SIZES.get(size_code, (0, 0))
            if inputs > 0:
                result[slot_idx] = inputs
        self._slot_cache[device_id] = result
        return result

    def _global_to_slot_local(self, device_id: str, global_input: int) -> Optional[Tuple[int, int]]:
        """Map a global input number to (slot_idx, local_input)."""
        slots = self._slot_inputs(device_id)
        base = 1
        for slot_idx in sorted(slots):
            n = slots[slot_idx]
            if base <= global_input < base + n:
                return slot_idx, global_input - base + 1
            base += n
        return None

    # ── signal polling ───────────────────────────────────────────────────────

    def _poll_active(self, device_id: str) -> Optional[Set[int]]:
        """Return set of active global input numbers for this device,
        or None if the device is unreachable (no response to any query)."""
        conn = self._conn(device_id)
        if not conn:
            return None
        slots = self._slot_inputs(device_id)
        if not slots:
            return None
        active: Set[int] = set()
        got_response = False
        global_base = 1
        for slot_idx in sorted(slots):
            n_inputs = slots[slot_idx]
            resp = conn.send(f"{slot_idx}*0LS")
            if resp:
                got_response = True
                # Require a run of at least n_inputs status digits so we match
                # the real status string, not stray digits from command echo
                m = re.search(r"([0-3]{%d,})" % max(n_inputs, 1), resp)
                if m:
                    for local_idx, ch in enumerate(m.group(1)[:n_inputs], start=0):
                        if ch != "0":
                            active.add(global_base + local_idx)
            global_base += n_inputs
        return active if got_response else None

    # ── frequency detection ──────────────────────────────────────────────────

    def _query_freq(self, device_id: str, global_input: int) -> Optional[str]:
        """Query sync frequency for a specific input. Returns '15kHz', '31kHz', or None."""
        loc = self._global_to_slot_local(device_id, global_input)
        if not loc:
            return None
        slot_idx, local_input = loc
        conn = self._conn(device_id)
        if not conn:
            return None
        resp = conn.send(f"{slot_idx}*{local_input}LS")
        if not resp:
            return None
        # Parse any integer in the response that looks like a horizontal sync rate
        for tok in re.findall(r"\d+", resp):
            val = int(tok)
            if 15000 <= val <= 16500:
                return "15kHz"
            if 31000 <= val <= 32500:
                return "31kHz"
        # Fallback: look for text like "15kHz" or "31kHz" directly
        if re.search(r"15\s*k", resp, re.IGNORECASE):
            return "15kHz"
        if re.search(r"31\s*k", resp, re.IGNORECASE):
            return "31kHz"
        return None  # unknown → caller decides

    # ── routing & preset actions ─────────────────────────────────────────────

    def _route(self, src: dict, dst: dict, fallback_device_id: str):
        device_id = dst.get("device_id") or fallback_device_id
        conn = self._conn(device_id)
        if not conn:
            log(f"autoswitch: no connection for device {device_id}")
            return
        inp = src["input"]
        out = dst["output"]
        resp = conn.send(f"{inp}*{out}!")
        self._current_route[dst["id"]] = src["id"]
        self.last_event = f"{src['name']} → {dst['name']}"
        self.last_event_time = time.time()
        _save_state({"last_event": self.last_event, "last_event_time": self.last_event_time})
        self._event("fire", f"{src['name']} → {dst['name']}  ({inp}*{out}!)")
        log(f"autoswitch: {self.last_event}  ({inp}*{out}! → {resp!r})")

    def _preset(self, device_id: str, preset_num: int, src_name: str):
        conn = self._conn(device_id)
        if not conn:
            log(f"autoswitch: no connection for preset device {device_id}")
            return
        resp = conn.send(f"{preset_num}.")
        self.last_event = f"{src_name} → preset {preset_num}"
        self.last_event_time = time.time()
        _save_state({"last_event": self.last_event, "last_event_time": self.last_event_time})
        self._event("fire", f"{src_name} → preset {preset_num}")
        log(f"autoswitch: recalled preset {preset_num} on {device_id}  (→ {resp!r})")

    # ── poll loop ────────────────────────────────────────────────────────────

    def _loop(self):
        log("autoswitch: engine started")
        while self.running:
            try:
                cfg = _load()
                if not cfg["engine"].get("enabled", True):
                    time.sleep(2.0)
                    continue

                interval = max(0.5, float(cfg["engine"].get("poll_interval", 2.0)))
                sources = {s["id"]: s for s in cfg.get("sources", [])}
                dests   = {d["id"]: d for d in cfg.get("destinations", [])}
                rules   = [r for r in cfg.get("rules", []) if r.get("enabled", True)]

                # Build device → sources map
                by_device: Dict[str, List[dict]] = {}
                for src in sources.values():
                    by_device.setdefault(src.get("device_id", ""), []).append(src)

                for device_id, srcs in by_device.items():
                    if not device_id:
                        continue
                    try:
                        new_active = self._poll_active(device_id)
                        self.poll_count += 1

                        if new_active is None:
                            # Device unreachable — freeze last known state so we
                            # don't fire spurious rules; surface it in status.
                            if self._device_ok.get(device_id) is not False:
                                self._device_ok[device_id] = False
                                err = f"{device_id}: SMX unreachable — auto-switching blind"
                                log(f"autoswitch: {err}")
                                self.recent_errors = (self.recent_errors + [err])[-10:]
                                self._event("offline", f"{device_id} unreachable — engine blind")
                            continue

                        if self._device_ok.get(device_id) is False:
                            log(f"autoswitch: {device_id} reachable again")
                            self._event("online", f"{device_id} reachable again")
                        self._device_ok[device_id] = True

                        old_active = self._active.get(device_id, set())
                        self._active[device_id] = new_active

                        # Clear current_route entries whose source just went inactive
                        newly_inactive = old_active - new_active
                        if newly_inactive:
                            gone_ids = {
                                s["id"] for s in srcs
                                if s.get("input") in newly_inactive
                            }
                            for dst_id, holding_src_id in list(self._current_route.items()):
                                if holding_src_id in gone_ids:
                                    del self._current_route[dst_id]
                                    s_name = next((s["name"] for s in srcs if s["id"] == holding_src_id), "?")
                                    self._event("release", f"{s_name} went dark — released its destination")

                        newly_active = new_active - old_active
                        if not newly_active:
                            continue

                        # Freq cache: query once per source per poll cycle
                        freq_cache: Dict[int, Optional[str]] = {}

                        for src in srcs:
                            if src.get("input") not in newly_active:
                                continue

                            for rule in rules:
                                if rule.get("source_id") != src["id"]:
                                    continue

                                # Freq gate
                                rule_freq = rule.get("freq", "any")
                                if rule_freq != "any":
                                    inp = src.get("input", 0)
                                    if inp not in freq_cache:
                                        freq_cache[inp] = self._query_freq(device_id, inp)
                                    detected = freq_cache[inp]
                                    # If we detected a freq and it doesn't match, skip rule
                                    if detected and detected != rule_freq:
                                        continue

                                # Fire actions
                                actions = _rule_actions(rule, dests)
                                for action in actions:
                                    if action["type"] == "route":
                                        dst = action["dest"]
                                        dst_mode = dst.get("mode", "newest_wins")

                                        if dst_mode == "keep_current":
                                            holding = self._current_route.get(dst["id"])
                                            if holding:
                                                # Check if the holding source is still active
                                                holding_src = sources.get(holding)
                                                if holding_src:
                                                    h_dev = holding_src.get("device_id", device_id)
                                                    h_inp = holding_src.get("input", -1)
                                                    if h_inp in self._active.get(h_dev, set()):
                                                        # Occupied — spill to next action
                                                        continue

                                        self._route(src, dst, device_id)

                                    elif action["type"] == "preset":
                                        self._preset(
                                            action.get("device_id", device_id),
                                            int(action.get("preset_num", 1)),
                                            src.get("name", "?"),
                                        )

                    except Exception as e:
                        err = f"{device_id}: {e}"
                        log(f"autoswitch: poll error — {err}")
                        self.recent_errors = (self.recent_errors + [err])[-10:]

                time.sleep(interval)

            except Exception as e:
                log(f"autoswitch: loop error — {e}")
                time.sleep(5.0)

        for c in self._conns.values():
            c.close()
        self._conns.clear()
        log("autoswitch: engine stopped")

    # ── public ──────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=6)

    def status(self) -> dict:
        cfg = _load()
        sources = {s["id"]: s for s in cfg.get("sources", [])}
        dests   = {d["id"]: d for d in cfg.get("destinations", [])}
        routes = [
            {
                "dest_id": did,
                "dest_name": dests[did]["name"] if did in dests else "?",
                "source_id": sid,
                "source_name": sources[sid]["name"] if sid in sources else "?",
            }
            for did, sid in self._current_route.items()
        ]
        return {
            "running": self.running,
            "enabled": cfg["engine"].get("enabled", True),
            "poll_interval": cfg["engine"].get("poll_interval", 2.0),
            "poll_count": self.poll_count,
            "last_event": self.last_event,
            "last_event_ago": (
                round(time.time() - self.last_event_time)
                if self.last_event_time else None
            ),
            "active_inputs": {k: sorted(v) for k, v in self._active.items()},
            "device_status": dict(self._device_ok),
            "current_routes": routes,
            "errors": self.recent_errors[-5:],
            "events": [
                {"ago": round(time.time() - e["t"]), "kind": e["kind"], "text": e["text"]}
                for e in reversed(self.events[-25:])
            ],
        }

    def restart(self):
        self.stop()
        self._slot_cache.clear()
        self.start()


def _rule_actions(rule: dict, dests: dict) -> list:
    """Normalise a rule to resolved action dicts. Handles legacy {dest_id} and new {actions}."""
    if "actions" in rule:
        out = []
        for a in rule["actions"]:
            if a.get("type") == "route":
                dst = dests.get(a.get("dest_id", ""))
                if dst:
                    out.append({"type": "route", "dest": dst})
            elif a.get("type") == "preset":
                out.append({
                    "type": "preset",
                    "device_id": a.get("device_id", ""),
                    "preset_num": int(a.get("preset_num", 1)),
                })
        return out
    # Legacy single-dest format
    dst = dests.get(rule.get("dest_id", ""))
    if dst:
        return [{"type": "route", "dest": dst}]
    return []


_engine = _Engine()


def start_engine():
    """Called from app.py startup."""
    cfg = _load()
    if cfg["engine"].get("enabled", True):
        _engine.start()


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #

@router.get("/api/autoswitch/config")
def as_config():
    return JSONResponse(_load())


@router.get("/api/autoswitch/status")
def as_status():
    return JSONResponse(_engine.status())


# ── sources ──────────────────────────────────────────────────────────────────

@router.post("/api/autoswitch/sources")
async def as_add_source(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    src = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "device_id": body.get("device_id", ""),
        "input": int(body.get("input", 1)),
    }
    data = _load()
    data["sources"].append(src)
    _save(data)
    return JSONResponse(src)


@router.put("/api/autoswitch/sources/{sid}")
async def as_update_source(sid: str, request: Request):
    body = await request.json()
    data = _load()
    src = next((s for s in data["sources"] if s["id"] == sid), None)
    if not src:
        return JSONResponse({"error": "not found"}, status_code=404)
    src.update({k: v for k, v in body.items() if k != "id"})
    _save(data)
    return JSONResponse(src)


@router.delete("/api/autoswitch/sources/{sid}")
def as_delete_source(sid: str):
    data = _load()
    data["sources"] = [s for s in data["sources"] if s["id"] != sid]
    data["rules"] = [r for r in data["rules"] if r.get("source_id") != sid]
    _save(data)
    return JSONResponse({"ok": True})


# ── destinations ─────────────────────────────────────────────────────────────

@router.post("/api/autoswitch/destinations")
async def as_add_dest(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    dst = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "device_id": body.get("device_id", ""),
        "output": int(body.get("output", 1)),
        "mode": body.get("mode", "newest_wins"),
    }
    data = _load()
    data["destinations"].append(dst)
    _save(data)
    return JSONResponse(dst)


@router.put("/api/autoswitch/destinations/{did}")
async def as_update_dest(did: str, request: Request):
    body = await request.json()
    data = _load()
    dst = next((d for d in data["destinations"] if d["id"] == did), None)
    if not dst:
        return JSONResponse({"error": "not found"}, status_code=404)
    dst.update({k: v for k, v in body.items() if k != "id"})
    _save(data)
    return JSONResponse(dst)


@router.delete("/api/autoswitch/destinations/{did}")
def as_delete_dest(did: str):
    data = _load()
    data["destinations"] = [d for d in data["destinations"] if d["id"] != did]
    # Remove route actions referencing this destination from rules
    for rule in data["rules"]:
        if "actions" in rule:
            rule["actions"] = [
                a for a in rule["actions"]
                if not (a.get("type") == "route" and a.get("dest_id") == did)
            ]
    _save(data)
    return JSONResponse({"ok": True})


# ── rules ────────────────────────────────────────────────────────────────────

@router.post("/api/autoswitch/rules")
async def as_add_rule(request: Request):
    body = await request.json()
    src_id = body.get("source_id", "")
    if not src_id:
        return JSONResponse({"error": "source_id required"}, status_code=400)
    actions = body.get("actions")
    if not actions:
        dst_id = body.get("dest_id", "")
        if not dst_id:
            return JSONResponse({"error": "actions or dest_id required"}, status_code=400)
        actions = [{"type": "route", "dest_id": dst_id}]
    rule = {
        "id": uuid.uuid4().hex[:12],
        "source_id": src_id,
        "actions": actions,
        "freq": body.get("freq", "any"),
        "enabled": bool(body.get("enabled", True)),
    }
    data = _load()
    data["rules"].append(rule)
    _save(data)
    return JSONResponse(rule)


@router.put("/api/autoswitch/rules/{rid}")
async def as_update_rule(rid: str, request: Request):
    body = await request.json()
    data = _load()
    rule = next((r for r in data["rules"] if r["id"] == rid), None)
    if not rule:
        return JSONResponse({"error": "not found"}, status_code=404)
    rule.update({k: v for k, v in body.items() if k != "id"})
    _save(data)
    return JSONResponse(rule)


@router.delete("/api/autoswitch/rules/{rid}")
def as_delete_rule(rid: str):
    data = _load()
    data["rules"] = [r for r in data["rules"] if r["id"] != rid]
    _save(data)
    return JSONResponse({"ok": True})


@router.post("/api/autoswitch/rules/{rid}/test")
def as_test_rule(rid: str):
    """Fire a rule's actions right now, bypassing signal detection and the
    freq gate — lets you verify wiring without powering a console on/off."""
    cfg = _load()
    rule = next((r for r in cfg.get("rules", []) if r["id"] == rid), None)
    if not rule:
        return JSONResponse({"error": "rule not found"}, status_code=404)
    sources = {s["id"]: s for s in cfg.get("sources", [])}
    dests   = {d["id"]: d for d in cfg.get("destinations", [])}
    src = sources.get(rule.get("source_id"))
    if not src:
        return JSONResponse({"error": "rule's source was deleted"}, status_code=400)

    device_id = src.get("device_id", "")
    _engine._event("test", f"manual test of '{src.get('name','?')}' rule")
    fired = []
    for action in _rule_actions(rule, dests):
        if action["type"] == "route":
            _engine._route(src, action["dest"], device_id)
            fired.append(f"{src['name']} → {action['dest']['name']}")
        elif action["type"] == "preset":
            n = int(action.get("preset_num", 1))
            _engine._preset(action.get("device_id", device_id), n, src.get("name", "?"))
            fired.append(f"preset {n}")
    return JSONResponse({
        "ok": True,
        "fired": fired,
        "device_ok": _engine._device_ok.get(device_id),
    })


# ── engine control ────────────────────────────────────────────────────────────

@router.post("/api/autoswitch/engine")
async def as_engine_ctrl(request: Request):
    body = await request.json()
    data = _load()
    if "enabled" in body:
        data["engine"]["enabled"] = bool(body["enabled"])
    if "poll_interval" in body:
        data["engine"]["poll_interval"] = max(0.5, float(body["poll_interval"]))
    _save(data)
    if data["engine"]["enabled"]:
        _engine.restart()
    else:
        _engine.stop()
    return JSONResponse({"ok": True, **data["engine"]})


# --------------------------------------------------------------------------- #
# Control page
# --------------------------------------------------------------------------- #

@router.get("/control/autoswitch", response_class=HTMLResponse)
def as_page():
    return HTMLResponse(_AS_HTML)


_AS_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Joebot Lab · Auto-Switching</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
/* Shared chrome (tokens, header, .btn*, .panel, .toast) comes from lab.css.
   Everything below is page-specific. */
body{padding-bottom:60px}

main{max-width:1100px;margin:0 auto;padding:18px 16px}

.split{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:640px){.split{grid-template-columns:1fr}}

.ph-lbl.src{color:var(--blue)}
.ph-lbl.dst{color:var(--purple)}
.ph-lbl.rules{color:var(--ok)}

.item{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
  padding:8px 11px;margin-bottom:6px;display:flex;align-items:center;gap:9px}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.on{background:var(--ok);box-shadow:0 0 5px rgba(52,211,153,.6)}
.dot.off{background:var(--gray)}
.dot.sq{border-radius:2px;background:var(--purple)}
.dot.sq.held{background:var(--amber);box-shadow:0 0 5px rgba(252,211,77,.5)}
.item-body{flex:1;min-width:0}
.iname{font-size:12px;font-weight:700;color:var(--ink)}
.isub{font-size:10px;color:var(--muted);margin-top:1px}
.ic{background:none;border:none;color:var(--muted);cursor:pointer;
  padding:2px 5px;border-radius:3px;font-size:12px}
.ic:hover{color:var(--ink);background:var(--line)}
.ic.del:hover{color:var(--bad)}
.mode-badge{font-size:9px;padding:1px 6px;border-radius:3px;
  background:rgba(252,211,77,.1);color:var(--amber);border:1px solid rgba(252,211,77,.25);
  margin-left:4px;white-space:nowrap}

.add-form{background:rgba(0,0,0,.2);border:1px dashed var(--line);border-radius:7px;
  padding:10px 12px;margin-top:6px;display:none}
.add-form.open{display:block}
.frow{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:6px}
.frow:last-child{margin-bottom:0}
.ff{display:flex;flex-direction:column;gap:3px;flex:1;min-width:80px}
.ff label{font-size:9px;color:var(--muted);letter-spacing:.07em;text-transform:uppercase}
.ff input,.ff select{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
  border-radius:5px;padding:5px 8px;width:100%}
.ff input:focus,.ff select:focus{outline:none;border-color:var(--accent)}
.ff select option{background:var(--panel2)}

.rule{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
  padding:9px 12px;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.rule.disabled{opacity:.45}
.rrow{flex:1;display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.kw{font-size:10px;color:var(--muted);white-space:nowrap}
.chip{display:inline-flex;align-items:center;padding:2px 8px;border-radius:4px;
  font-size:11px;font-weight:700;white-space:nowrap;gap:4px}
.chip.src{background:rgba(96,165,250,.1);border:1px solid rgba(96,165,250,.3);color:var(--blue)}
.chip.dst{background:rgba(167,139,250,.1);border:1px solid rgba(167,139,250,.3);color:var(--purple)}
.chip.preset{background:rgba(52,211,153,.08);border:1px solid rgba(52,211,153,.3);color:var(--ok)}
.chip.freq{background:rgba(252,211,77,.08);border:1px solid rgba(252,211,77,.25);
  color:var(--amber);font-size:9.5px}
.arr{color:var(--muted);font-size:11px}
.tog{width:28px;height:15px;border-radius:20px;flex-shrink:0;
  cursor:pointer;border:none;position:relative;transition:background .12s}
.tog.on{background:var(--ok)}
.tog.on::after{content:'';position:absolute;right:2px;top:1.5px;
  width:11px;height:11px;background:#fff;border-radius:50%}
.tog.off{background:var(--line)}
.tog.off::after{content:'';position:absolute;left:2px;top:1.5px;
  width:11px;height:11px;background:var(--muted);border-radius:50%}

.eng-card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:12px 16px;margin-bottom:12px}
.eng-top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.eng-stat{display:flex;align-items:center;gap:6px;font-size:12px}
.sdot{width:7px;height:7px;border-radius:50%}
.sdot.g{background:var(--ok);box-shadow:0 0 4px rgba(52,211,153,.5)}
.sdot.r{background:var(--bad);box-shadow:0 0 4px rgba(255,84,112,.5)}
.sdot.a{background:var(--warn)}
.elab{color:var(--muted);font-size:10px;letter-spacing:.05em}
.eval{color:var(--ink)}
.eng-name{font-weight:700;font-size:12.5px}
.dev-chip{display:inline-flex;align-items:center;gap:5px;font-size:10px;
  padding:2px 9px;border-radius:20px;border:1px solid var(--line);color:var(--muted)}
.dev-chip .sdot{width:6px;height:6px}
.dev-chip.up{border-color:rgba(52,211,153,.35);color:var(--ok)}
.dev-chip.down{border-color:rgba(255,84,112,.4);color:var(--bad)}
.int-inp{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
  border-radius:5px;padding:3px 6px;width:52px;text-align:center}
.int-inp:focus{outline:none;border-color:var(--accent)}
.lf-row{display:flex;align-items:baseline;gap:10px;padding:9px 12px;
  background:rgba(0,0,0,.22);border:1px solid var(--line);border-radius:7px}
.lf-lbl{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);flex-shrink:0}
.lf-val{font-size:14px;font-weight:700;color:var(--ink)}
.lf-val.none{color:var(--muted);font-weight:400;font-size:12px}
.lf-ago{font-size:10.5px;color:var(--muted)}
.eng-err-line{margin-top:8px;font-size:11px;color:var(--warn);display:none}
.ev-toggle{background:none;border:none;color:var(--muted);cursor:pointer;
  font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;padding:8px 2px 0;
  display:flex;align-items:center;gap:6px}
.ev-toggle:hover{color:var(--ink)}
.ev-arrow{display:inline-block;transition:transform .15s;font-size:9px}
.ev-toggle.open .ev-arrow{transform:rotate(90deg)}
.ev-feed{margin-top:6px;max-height:230px;overflow-y:auto;border-top:1px solid var(--line);
  padding-top:6px;display:none}
.ev-feed.open{display:block}
.ev-row{display:flex;align-items:baseline;gap:8px;padding:3px 2px;font-size:11px;
  border-bottom:1px dashed rgba(38,43,54,.6)}
.ev-row:last-child{border-bottom:none}
.ev-tag{font-size:8.5px;font-weight:700;letter-spacing:.09em;padding:1px 6px;
  border-radius:3px;flex-shrink:0;min-width:46px;text-align:center}
.ev-tag.fire{background:rgba(52,211,153,.12);color:var(--ok)}
.ev-tag.test{background:rgba(96,165,250,.12);color:var(--blue)}
.ev-tag.release{background:rgba(167,139,250,.12);color:var(--purple)}
.ev-tag.offline{background:rgba(255,84,112,.12);color:var(--bad)}
.ev-tag.online{background:rgba(52,211,153,.12);color:var(--ok)}
.ev-tag.error{background:rgba(245,185,66,.12);color:var(--warn)}
.ev-text{flex:1;color:var(--ink)}
.ev-ago{font-size:9.5px;color:var(--muted);flex-shrink:0}
.ev-empty{color:var(--muted);font-size:11px;padding:6px 2px}
.routes-bar{display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid var(--line);
  padding-top:10px;margin-top:10px}
.route-pill{display:flex;align-items:center;gap:5px;background:var(--panel2);
  border:1px solid var(--line);border-radius:5px;padding:3px 8px;font-size:10.5px}
.route-pill .rp-dst{color:var(--purple);font-weight:700}
.route-pill .rp-src{color:var(--blue)}
.route-pill .rp-arr{color:var(--muted)}

.smx-hint{font-size:10px;color:var(--muted);padding:8px 12px;
  border-top:1px solid var(--line)}

.action-add-row{display:flex;gap:6px;margin-bottom:8px}
.action-row{background:rgba(0,0,0,.2);border:1px solid var(--line);border-radius:5px;
  padding:6px 8px;margin-bottom:5px;display:flex;align-items:center;gap:6px}
.action-type-tag{font-size:9px;font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;padding:2px 6px;border-radius:3px;white-space:nowrap;flex-shrink:0}
.tag-route{background:rgba(167,139,250,.15);color:var(--purple)}
.tag-preset{background:rgba(52,211,153,.12);color:var(--ok)}

.help-panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  margin-bottom:12px;overflow:hidden}
.help-panel summary{display:flex;align-items:center;gap:10px;
  padding:11px 16px;cursor:pointer;list-style:none;user-select:none;
  color:var(--muted);font-size:12px;transition:color .15s}
.help-panel summary::-webkit-details-marker{display:none}
.help-panel summary:hover{color:var(--ink)}
.help-panel[open] summary{color:var(--ink);border-bottom:1px solid var(--line)}
.help-icon{width:18px;height:18px;border-radius:50%;border:1.5px solid var(--muted);
  display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;
  flex-shrink:0}
.help-panel summary:hover .help-icon{border-color:var(--ok);color:var(--ok)}
.help-arrow{margin-left:auto;font-size:10px;transition:transform .15s}
.help-panel[open] .help-arrow{transform:rotate(90deg)}
.help-body{padding:16px}
.help-cols{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px;margin-bottom:14px}
.help-title{font-size:10px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ok);margin-bottom:6px}
.help-text{font-size:11.5px;color:var(--muted);line-height:1.65}
.help-text b{color:var(--ink);font-weight:600}
.help-text code{background:var(--panel2);border:1px solid var(--line);border-radius:3px;
  padding:1px 5px;font-size:10.5px;color:var(--accent)}
.help-tip{background:rgba(52,211,153,.06);border:1px solid rgba(52,211,153,.2);
  border-radius:7px;padding:10px 13px;font-size:11.5px;color:var(--muted);line-height:1.6}
.help-tip code{background:var(--panel2);border:1px solid var(--line);border-radius:3px;
  padding:1px 5px;font-size:10.5px;color:var(--accent)}
</style></head>
<body>
<header>
  <div class="brand">🦖 <b>JOEBOT</b> LAB</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/config">Config</a>
  </nav>
  <div style="color:var(--muted);font-size:11px">/ Auto-Switching</div>
  <div class="spacer"></div>
  <button class="btn btn-sm" id="btn-pause">⏸ Pause engine</button>
</header>

<main>
  <!-- Help guide -->
  <details class="help-panel">
    <summary>
      <span class="help-icon">?</span>
      <span>How auto-switching works</span>
      <span class="help-arrow">▸</span>
    </summary>
    <div class="help-body">
      <div class="help-cols">
        <div>
          <div class="help-title">Sources</div>
          <div class="help-text">A <b>source</b> is a physical input on your SMX — e.g. "Super Nintendo" on input 4. The engine watches these every 2s. When a signal appears (you powered on the console), matching rules fire.</div>
        </div>
        <div>
          <div class="help-title">Destinations</div>
          <div class="help-text">A <b>destination</b> is a physical output — e.g. "20&quot; CRT" on output 2. Each destination has a <b>mode</b>: <b>Newest wins</b> means any new signal takes over; <b>Keep current</b> means the first thing routed there stays until it turns off.</div>
        </div>
        <div>
          <div class="help-title">Rules &amp; Actions</div>
          <div class="help-text">A rule fires when its source turns on. Each rule can have multiple <b>actions</b>: route to one or more destinations, and/or recall an SMX preset. With keep-current destinations, if the first is busy the engine spills to the next action.</div>
        </div>
        <div>
          <div class="help-title">Freq gating</div>
          <div class="help-text">Set a rule's freq condition to <b>15 kHz</b> (retro/composite signals) or <b>31 kHz</b> (progressive/VGA). The engine queries the input timing before firing — useful for routing the same console to different displays based on its output mode.</div>
        </div>
        <div>
          <div class="help-title">Engine</div>
          <div class="help-text">Polls your SMX devices continuously. It only acts on <b>newly active</b> inputs — turning something on triggers rules. When a source goes dark, its held destinations are released. Pause anytime without losing config.</div>
        </div>
      </div>
      <div class="help-tip">
        <span style="color:var(--ok)">Quick start:</span>
        add your SMX in <a href="/config" style="color:var(--blue)">Config</a> with kind <code>smx</code> →
        add a source (name + input #) →
        add a destination (name + output # + mode) →
        create a rule → add actions → done.
      </div>
    </div>
  </details>

  <!-- Engine status -->
  <div class="eng-card" id="eng-card">
    <div class="eng-top">
      <div class="eng-stat"><div class="sdot g" id="eng-dot"></div><span class="eng-name" id="eng-running">–</span></div>
      <div id="dev-chips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
      <div class="spacer"></div>
      <div class="eng-stat"><span class="elab">polls</span><span class="eval" id="eng-poll">–</span></div>
      <div class="eng-stat"><span class="elab">every</span>
        <input id="inp-interval" type="number" min="0.5" max="60" step="0.5"
          class="int-inp" onchange="setInterval_(this.value)"/>
        <span class="elab">s</span>
      </div>
    </div>
    <div class="lf-row">
      <span class="lf-lbl">Last fired</span>
      <span class="lf-val" id="eng-event">–</span>
      <span class="lf-ago" id="eng-event-ago"></span>
    </div>
    <div class="eng-err-line" id="eng-err-wrap">⚠ <span id="eng-err">–</span></div>
    <div class="routes-bar" id="routes-bar" style="display:none"></div>
    <button class="ev-toggle" id="ev-toggle" onclick="toggleEvents()">
      <span class="ev-arrow">▸</span> event history <span id="ev-count"></span>
    </button>
    <div class="ev-feed" id="ev-feed"></div>
  </div>

  <div class="split">
    <!-- Sources -->
    <div class="panel">
      <div class="ph">
        <span class="ph-lbl src">Sources</span>
        <button class="btn btn-sm btn-blue" onclick="toggleForm('src')">+ add source</button>
      </div>
      <div class="pb">
        <div class="add-form" id="form-src">
          <div class="frow">
            <div class="ff"><label>Name</label>
              <input id="src-name" placeholder="Super Nintendo"/></div>
            <div class="ff"><label>SMX device</label>
              <select id="src-device"></select></div>
          </div>
          <div class="frow">
            <div class="ff"><label>Input #</label>
              <input id="src-input" type="number" min="1" max="128" value="1" style="max-width:80px"/></div>
            <div class="ff" style="justify-content:flex-end;flex-direction:row;gap:6px;align-items:flex-end">
              <button class="btn btn-blue btn-sm" onclick="addSource()">Add</button>
              <button class="btn btn-sm" onclick="toggleForm('src')">Cancel</button>
            </div>
          </div>
        </div>
        <div id="src-list"></div>
      </div>
      <div class="smx-hint" id="smx-hint-src"></div>
    </div>

    <!-- Destinations -->
    <div class="panel">
      <div class="ph">
        <span class="ph-lbl dst">Destinations</span>
        <button class="btn btn-sm btn-purple" onclick="toggleForm('dst')">+ add destination</button>
      </div>
      <div class="pb">
        <div class="add-form" id="form-dst">
          <div class="frow">
            <div class="ff"><label>Name</label>
              <input id="dst-name" placeholder='20" CRT'/></div>
            <div class="ff"><label>SMX device</label>
              <select id="dst-device"></select></div>
          </div>
          <div class="frow">
            <div class="ff"><label>Output #</label>
              <input id="dst-output" type="number" min="1" max="128" value="1" style="max-width:80px"/></div>
            <div class="ff"><label>Mode</label>
              <select id="dst-mode">
                <option value="newest_wins">Newest wins — always switch</option>
                <option value="keep_current">Keep current — hold until source turns off</option>
              </select></div>
          </div>
          <div class="frow">
            <button class="btn btn-purple btn-sm" onclick="addDest()">Add</button>
            <button class="btn btn-sm" onclick="toggleForm('dst')">Cancel</button>
          </div>
        </div>
        <div id="dst-list"></div>
      </div>
    </div>
  </div>

  <!-- Rules -->
  <div class="panel">
    <div class="ph">
      <span class="ph-lbl rules">Rules</span>
      <button class="btn btn-sm btn-ok" onclick="toggleForm('rule')">+ add rule</button>
    </div>
    <div class="pb">
      <div class="add-form" id="form-rule">
        <div class="frow" style="margin-bottom:10px">
          <div class="ff" style="max-width:220px"><label>When this source turns on…</label>
            <select id="rule-src"></select></div>
          <div class="ff" style="max-width:180px"><label>Freq condition</label>
            <select id="rule-freq">
              <option value="any">any frequency</option>
              <option value="15kHz">15 kHz only (retro/composite)</option>
              <option value="31kHz">31 kHz only (progressive/VGA)</option>
            </select></div>
        </div>
        <div style="font-size:9px;color:var(--ok);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px">
          Actions (fires in order — spills if destination is held):</div>
        <div id="rule-actions-list"></div>
        <div class="action-add-row">
          <button class="btn btn-sm" style="border-style:dashed;color:var(--purple);border-color:rgba(167,139,250,.35)"
            onclick="addActionRow('route')">+ route to destination</button>
          <button class="btn btn-sm" style="border-style:dashed;color:var(--ok);border-color:rgba(52,211,153,.35)"
            onclick="addActionRow('preset')">+ recall SMX preset</button>
        </div>
        <div class="frow" style="margin-top:2px">
          <button class="btn btn-ok btn-sm" onclick="saveRule()">Save rule</button>
          <button class="btn btn-sm" onclick="toggleForm('rule')">Cancel</button>
        </div>
      </div>
      <div id="rule-list"></div>
      <div id="rule-empty" style="color:var(--muted);font-size:11px;padding:10px 2px;display:none">
        No rules yet — add sources and destinations above, then create a rule.
      </div>
    </div>
  </div>
</main>

<div class="toast" id="toast"></div>

<script>
let cfg = {sources:[], destinations:[], rules:[], engine:{enabled:true, poll_interval:2}};
let status = {};
let smxDevices = [];

async function load() {
  const [cfgR, stR, devR] = await Promise.all([
    fetch('/api/autoswitch/config'),
    fetch('/api/autoswitch/status'),
    fetch('/api/devices'),
  ]);
  cfg    = await cfgR.json();
  status = await stR.json();
  const devData = await devR.json();
  smxDevices = (devData.devices || []).filter(d => d.kind === 'smx');
  render();
}

// ── engine card ───────────────────────────────────────────────────────────
function renderEngine() {
  const running = status.running && status.enabled;
  const devStatus = status.device_status || {};
  const anyDown = Object.values(devStatus).some(v => v === false);

  // Status dot + label
  if (running && anyDown) {
    document.getElementById('eng-dot').className = 'sdot r';
    document.getElementById('eng-running').textContent = 'engine blind — SMX down';
  } else {
    document.getElementById('eng-dot').className = 'sdot ' + (running?'g': status.enabled?'a':'r');
    document.getElementById('eng-running').textContent =
      running ? 'engine polling' : status.enabled ? 'engine starting…' : 'engine paused';
  }

  // Per-device chips
  document.getElementById('dev-chips').innerHTML = Object.keys(devStatus).map(id => {
    const name = smxDevices.find(d=>d.id===id)?.name || id;
    const up = devStatus[id];
    return `<span class="dev-chip ${up?'up':'down'}">
      <span class="sdot ${up?'g':'r'}"></span>${esc(name)} ${up?'online':'UNREACHABLE'}</span>`;
  }).join('');

  // Interval input — don't clobber it while the user is typing in it
  const intInp = document.getElementById('inp-interval');
  if (document.activeElement !== intInp) intInp.value = status.poll_interval || 2;

  // Last fired — its own prominent row
  const evEl = document.getElementById('eng-event');
  const agoEl = document.getElementById('eng-event-ago');
  if (status.last_event) {
    evEl.textContent = status.last_event;
    evEl.className = 'lf-val';
    agoEl.textContent = status.last_event_ago != null ? fmtAgo(status.last_event_ago) : '';
  } else {
    evEl.textContent = 'nothing yet — waiting for a source to turn on';
    evEl.className = 'lf-val none';
    agoEl.textContent = '';
  }

  document.getElementById('eng-poll').textContent =
    (status.poll_count||0).toLocaleString();

  const errWrap = document.getElementById('eng-err-wrap');
  const errs = status.errors || [];
  errWrap.style.display = errs.length ? 'block' : 'none';
  if (errs.length) document.getElementById('eng-err').textContent = errs[errs.length-1];

  document.getElementById('btn-pause').textContent =
    status.enabled ? '⏸ Pause engine' : '▶ Resume engine';

  renderEvents();

  // Current routes bar
  const routes = status.current_routes || [];
  const bar = document.getElementById('routes-bar');
  const card = document.getElementById('eng-card');
  if (routes.length) {
    bar.style.display = 'flex';
    card.classList.add('has-routes');
    bar.innerHTML = '<span style="font-size:9px;color:var(--muted);letter-spacing:.06em;text-transform:uppercase;align-self:center">held&nbsp;</span>' +
      routes.map(r =>
        `<div class="route-pill">
          <span class="rp-dst">${esc(r.dest_name)}</span>
          <span class="rp-arr">←</span>
          <span class="rp-src">${esc(r.source_name)}</span>
        </div>`
      ).join('');
  } else {
    bar.style.display = 'none';
    card.classList.remove('has-routes');
  }
}

// ── event feed ────────────────────────────────────────────────────────────
let eventsOpen = false;

function toggleEvents() {
  eventsOpen = !eventsOpen;
  document.getElementById('ev-toggle').classList.toggle('open', eventsOpen);
  document.getElementById('ev-feed').classList.toggle('open', eventsOpen);
  renderEvents();
}

function renderEvents() {
  const evs = status.events || [];
  document.getElementById('ev-count').textContent = evs.length ? '('+evs.length+')' : '';
  if (!eventsOpen) return;
  const feed = document.getElementById('ev-feed');
  if (!evs.length) {
    feed.innerHTML = '<div class="ev-empty">No events yet this session.</div>';
    return;
  }
  feed.innerHTML = evs.map(e =>
    `<div class="ev-row">
      <span class="ev-tag ${esc(e.kind)}">${esc(e.kind.toUpperCase())}</span>
      <span class="ev-text">${esc(e.text)}</span>
      <span class="ev-ago">${fmtAgo(e.ago)}</span>
    </div>`
  ).join('');
}

// ── sources ───────────────────────────────────────────────────────────────
function renderSources() {
  const el = document.getElementById('src-list');
  if (!cfg.sources.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:6px 2px">No sources yet.</div>';
    return;
  }
  el.innerHTML = cfg.sources.map(s => {
    const active = (status.active_inputs?.[s.device_id] || []).includes(s.input);
    const devName = smxDevices.find(d=>d.id===s.device_id)?.name || s.device_id || '?';
    return `<div class="item">
      <div class="dot ${active?'on':'off'}"></div>
      <div class="item-body">
        <div class="iname">${esc(s.name)}</div>
        <div class="isub">${esc(devName)} · input ${s.input}${active?' · <span style="color:var(--ok)">signal active</span>':''}</div>
      </div>
      <button class="ic del" onclick="deleteSrc('${s.id}')" title="Delete">✕</button>
    </div>`;
  }).join('');

  document.getElementById('smx-hint-src').textContent = smxDevices.length
    ? 'SMX: ' + smxDevices.map(d=>d.name+' ('+d.ip+')').join(', ')
    : 'No SMX devices — add one in Config with kind "smx".';
}

// ── destinations ──────────────────────────────────────────────────────────
function renderDests() {
  const el = document.getElementById('dst-list');
  if (!cfg.destinations.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:11px;padding:6px 2px">No destinations yet.</div>';
    return;
  }
  const routeMap = Object.fromEntries((status.current_routes||[]).map(r=>[r.dest_id, r.source_name]));
  el.innerHTML = cfg.destinations.map(d => {
    const devName = smxDevices.find(x=>x.id===d.device_id)?.name || d.device_id || '?';
    const mode = d.mode || 'newest_wins';
    const held = routeMap[d.id];
    const modeBadge = mode === 'keep_current'
      ? `<span class="mode-badge">🔒 hold</span>` : '';
    const heldNote = held ? ` · <span style="color:var(--amber)">← ${esc(held)}</span>` : '';
    return `<div class="item">
      <div class="dot sq${held?' held':''}"></div>
      <div class="item-body">
        <div class="iname">${esc(d.name)}${modeBadge}</div>
        <div class="isub">${esc(devName)} · output ${d.output}${heldNote}</div>
      </div>
      <button class="ic" onclick="cycleMode('${d.id}','${mode}')" title="Toggle mode">⇄</button>
      <button class="ic del" onclick="deleteDst('${d.id}')" title="Delete">✕</button>
    </div>`;
  }).join('');
}

// ── rules ─────────────────────────────────────────────────────────────────
function normActions(r) {
  if (r.actions && r.actions.length) return r.actions;
  if (r.dest_id) return [{type:'route', dest_id: r.dest_id}];
  return [];
}

function renderRules() {
  const el = document.getElementById('rule-list');
  const emptyEl = document.getElementById('rule-empty');
  const srcMap = Object.fromEntries(cfg.sources.map(s=>[s.id,s]));
  const dstMap = Object.fromEntries(cfg.destinations.map(d=>[d.id,d]));
  if (!cfg.rules.length) { el.innerHTML=''; emptyEl.style.display='block'; return; }
  emptyEl.style.display = 'none';
  el.innerHTML = cfg.rules.map(r => {
    const src = srcMap[r.source_id];
    const srcChip = src
      ? `<span class="chip src">${esc(src.name)}</span>`
      : `<span class="chip src" style="opacity:.5">deleted</span>`;
    const freqChip = r.freq && r.freq !== 'any'
      ? `<span class="chip freq">${esc(r.freq)}</span>` : '';
    const actions = normActions(r);
    const actionChips = actions.map((a,i) => {
      if (a.type === 'route') {
        const dst = dstMap[a.dest_id];
        const dstChip = dst
          ? `<span class="chip dst">${esc(dst.name)}</span>`
          : `<span class="chip dst" style="opacity:.5">deleted</span>`;
        return (i===0 ? '<span class="kw">→</span>' : '<span class="kw">+</span>') + dstChip;
      }
      if (a.type === 'preset') {
        const devName = smxDevices.find(d=>d.id===a.device_id)?.name || 'SMX';
        return (i===0 ? '<span class="kw">→</span>' : '<span class="kw">+</span>') +
          `<span class="chip preset">▶ preset ${a.preset_num} on ${esc(devName)}</span>`;
      }
      return '';
    }).join(' ');
    const togCls = r.enabled ? 'on' : 'off';
    return `<div class="rule ${r.enabled?'':'disabled'}">
      <div class="rrow">
        <span class="kw">when</span>${srcChip}<span class="kw">turns on</span>
        ${actionChips}${freqChip}
      </div>
      <button class="ic" title="Fire this rule right now (test)" onclick="testRule('${r.id}')"
        style="color:var(--blue)">▶</button>
      <button class="tog ${togCls}" onclick="toggleRule('${r.id}',${!r.enabled})"></button>
      <button class="ic del" onclick="deleteRule('${r.id}')">✕</button>
    </div>`;
  }).join('');
}

async function testRule(id) {
  toast('Firing rule…');
  const j = await post('/api/autoswitch/rules/'+id+'/test', {});
  if (j.ok) {
    toast('Fired: ' + (j.fired||[]).join(' + ') +
      (j.device_ok === false ? ' — ⚠ device unreachable, commands lost' : ''));
  } else {
    toast('Test failed: ' + (j.error || 'unknown error'));
  }
  load();
}

// ── render all ────────────────────────────────────────────────────────────
function render() {
  renderEngine();
  renderSources();
  renderDests();
  renderRules();
  const smxOpts = smxDevices.map(d=>`<option value="${d.id}">${esc(d.name)} (${esc(d.ip)})</option>`).join('');
  const noSmx = '<option value="">— no SMX devices —</option>';
  ['src-device','dst-device'].forEach(id => {
    document.getElementById(id).innerHTML = smxOpts || noSmx;
  });
  document.getElementById('rule-src').innerHTML =
    cfg.sources.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('') ||
    '<option>— add a source first —</option>';
}

// ── source CRUD ───────────────────────────────────────────────────────────
async function addSource() {
  const name      = document.getElementById('src-name').value.trim();
  const device_id = document.getElementById('src-device').value;
  const input     = parseInt(document.getElementById('src-input').value) || 1;
  if (!name) { toast('Name required'); return; }
  await post('/api/autoswitch/sources', {name, device_id, input});
  document.getElementById('src-name').value = '';
  toggleForm('src'); toast('Source added'); await load();
}

async function deleteSrc(id) {
  if (!confirm('Remove this source and its rules?')) return;
  await del('/api/autoswitch/sources/'+id);
  toast('Source removed'); await load();
}

// ── destination CRUD ──────────────────────────────────────────────────────
async function addDest() {
  const name      = document.getElementById('dst-name').value.trim();
  const device_id = document.getElementById('dst-device').value;
  const output    = parseInt(document.getElementById('dst-output').value) || 1;
  const mode      = document.getElementById('dst-mode').value;
  if (!name) { toast('Name required'); return; }
  await post('/api/autoswitch/destinations', {name, device_id, output, mode});
  document.getElementById('dst-name').value = '';
  toggleForm('dst'); toast('Destination added'); await load();
}

async function cycleMode(id, current) {
  const next = current === 'newest_wins' ? 'keep_current' : 'newest_wins';
  await put('/api/autoswitch/destinations/'+id, {mode: next});
  toast(next === 'keep_current' ? '🔒 Keep current — holds until source turns off' : '⇄ Newest wins — always switches');
  await load();
}

async function deleteDst(id) {
  if (!confirm('Remove this destination?')) return;
  await del('/api/autoswitch/destinations/'+id);
  toast('Destination removed'); await load();
}

// ── rule builder ──────────────────────────────────────────────────────────
let _actionIdx = 0;

function addActionRow(type) {
  _actionIdx++;
  const rowId = 'ar-'+_actionIdx;
  const div = document.createElement('div');
  div.className = 'action-row';
  div.id = rowId;
  div.dataset.actionType = type;

  if (type === 'route') {
    const dstOpts = cfg.destinations.map(d=>
      `<option value="${d.id}">${esc(d.name)}</option>`).join('') ||
      '<option value="">— add destinations first —</option>';
    div.innerHTML = `
      <span class="action-type-tag tag-route">route</span>
      <select class="ff route-dst" style="flex:1">${dstOpts}</select>
      <button class="btn btn-sm btn-bad" style="flex:0;padding:3px 7px"
        onclick="document.getElementById('${rowId}').remove()">✕</button>`;
  } else {
    const smxOpts = smxDevices.map(d=>
      `<option value="${d.id}">${esc(d.name)}</option>`).join('') ||
      '<option value="">— no SMX devices —</option>';
    div.innerHTML = `
      <span class="action-type-tag tag-preset">preset</span>
      <select class="preset-device" style="flex:1">${smxOpts}</select>
      <input class="preset-num" type="number" min="1" max="32" value="1"
        style="width:62px;background:var(--panel2);border:1px solid var(--line);
          color:var(--ink);border-radius:5px;padding:4px 6px" placeholder="#"/>
      <span style="font-size:10px;color:var(--muted)">preset #</span>
      <button class="btn btn-sm btn-bad" style="flex:0;padding:3px 7px"
        onclick="document.getElementById('${rowId}').remove()">✕</button>`;
  }
  document.getElementById('rule-actions-list').appendChild(div);
}

async function saveRule() {
  const source_id = document.getElementById('rule-src').value;
  const freq      = document.getElementById('rule-freq').value;
  if (!source_id || source_id.startsWith('—')) { toast('Select a source'); return; }

  const rows = document.querySelectorAll('#rule-actions-list .action-row');
  if (!rows.length) { toast('Add at least one action'); return; }

  const actions = [...rows].map(row => {
    const type = row.dataset.actionType;
    if (type === 'route') {
      return {type: 'route', dest_id: row.querySelector('.route-dst').value};
    }
    if (type === 'preset') {
      return {
        type: 'preset',
        device_id: row.querySelector('.preset-device').value,
        preset_num: parseInt(row.querySelector('.preset-num').value) || 1,
      };
    }
    return null;
  }).filter(Boolean);

  await post('/api/autoswitch/rules', {source_id, actions, freq, enabled: true});
  document.getElementById('rule-actions-list').innerHTML = '';
  _actionIdx = 0;
  toggleForm('rule'); toast('Rule added'); await load();
}

async function deleteRule(id) {
  await del('/api/autoswitch/rules/'+id);
  toast('Rule removed'); await load();
}

async function toggleRule(id, enabled) {
  await put('/api/autoswitch/rules/'+id, {enabled}); await load();
}

// ── engine controls ───────────────────────────────────────────────────────
document.getElementById('btn-pause').addEventListener('click', async () => {
  const nowEnabled = !status.enabled;
  await post('/api/autoswitch/engine', {enabled: nowEnabled});
  toast(nowEnabled ? 'Engine resumed' : 'Engine paused'); await load();
});

async function setInterval_(val) {
  const v = parseFloat(val);
  if (!v || v < 0.5) return;
  await post('/api/autoswitch/engine', {poll_interval: v}); await load();
}

// ── forms ─────────────────────────────────────────────────────────────────
function toggleForm(which) {
  const el = document.getElementById('form-'+which);
  const opening = !el.classList.contains('open');
  el.classList.toggle('open');
  if (which === 'rule' && opening) {
    document.getElementById('rule-actions-list').innerHTML = '';
    _actionIdx = 0;
    addActionRow('route');
  }
}

// ── helpers ───────────────────────────────────────────────────────────────
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function fmtAgo(s){if(s==null)return '';if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago';}
async function post(url,body){return (await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();}
async function put(url,body){return (await fetch(url,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();}
async function del(url){return fetch(url,{method:'DELETE'});}
let _tt;
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');clearTimeout(_tt);_tt=setTimeout(()=>el.classList.remove('show'),2800);}

load();
setInterval(load, 4000);
</script>
</body></html>"""
