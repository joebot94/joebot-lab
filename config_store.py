"""
Joebot Lab Dashboard — persistent config store.

Loads from /app/config/devices.json (NAS-mounted volume).
Falls back to devices.py defaults on first run, then writes devices.json
so subsequent edits via the /config UI survive container rebuilds.
"""

import json
import os
import threading
import devices as _defaults

CONFIG_DIR  = os.getenv("CONFIG_DIR", "/app/config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "devices.json")
_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Default data from devices.py
# --------------------------------------------------------------------------- #
def _defaults_data():
    return {
        "version":  "1.2.0",
        "families": _defaults.FAMILIES,
        "devices":  [dict(d) for d in _defaults.DEVICES],
    }


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #
def load():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            if "devices" in data and "families" in data:
                return data
        except Exception:
            pass
    return _defaults_data()


def get_devices():
    return load()["devices"]


def get_families():
    return load()["families"]


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #
def save(data):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with _lock:
            with open(CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        return True, None
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def update_device(did, updates):
    """Update fields on an existing device. Returns updated device or None."""
    data = load()
    for i, d in enumerate(data["devices"]):
        if d["id"] == did:
            # Don't allow id change via this path
            updates.pop("id", None)
            data["devices"][i] = {**d, **updates}
            save(data)
            return data["devices"][i]
    return None


def add_device(device):
    """Add a new device. Returns (device, error)."""
    data = load()
    existing = {d["id"] for d in data["devices"]}
    if not device.get("id"):
        return None, "id is required"
    if device["id"] in existing:
        return None, f"id '{device['id']}' already exists"
    # Ensure required keys have defaults
    device.setdefault("port",   23)
    device.setdefault("policy", "lenient")
    device.setdefault("meta",   {})
    data["devices"].append(device)
    save(data)
    return device, None


def remove_device(did):
    """Remove a device by id. Returns True if removed."""
    data = load()
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d["id"] != did]
    if len(data["devices"]) == before:
        return False
    save(data)
    return True


def bootstrap():
    """Write defaults to JSON if no config file exists yet."""
    if not os.path.exists(CONFIG_PATH):
        ok, err = save(_defaults_data())
        return ok
    return True


# --------------------------------------------------------------------------- #
# Templates — pre-fill form when adding a new device
# --------------------------------------------------------------------------- #
TEMPLATES = {
    "matrix12800": {
        "kind": "matrix12800", "port": 23, "policy": "strict",
        "password": "admin", "family": "core",
        "role": "Extron Matrix 12800",
    },
    "dms3600": {
        "kind": "dms3600", "port": 23, "policy": "strict",
        "family": "core", "role": "Extron DMS digital matrix",
    },
    "smx": {
        "kind": "smx", "port": 23, "policy": "strict",
        "family": "core", "role": "Extron SMX modular matrix",
    },
    "mgp": {
        "kind": "mgp", "port": 23, "policy": "strict",
        "family": "mgp", "role": "Extron MGP multi-window processor",
    },
    "pcs4": {
        "kind": "pcs4", "port": 23, "policy": "strict",
        "family": "power", "role": "Extron IPL T PCS4 power controller",
    },
    "extron_info": {
        "kind": "extron_info", "port": 23, "policy": "lenient",
        "family": "scalers", "role": "Extron device",
    },
    "host": {
        "kind": "host", "port": 80, "policy": "lenient",
        "family": "lab", "role": "network host",
    },
    "ipcp505": {
        "kind": "ipcp505", "port": 23, "policy": "strict",
        "family": "control", "role": "Extron IPCP Pro 505 control processor",
        "meta": {
            "serial_01": "VSC 700 #1",
            "serial_02": "VSC 700 #2",
            "serial_03": "VSC 700 #3",
            "serial_04": "VSC 700 #4",
            "serial_05": "USP 405 #1",
            "serial_06": "USP 405 #2",
        },
    },
}
