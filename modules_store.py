"""
Module enable/disable state and setup completion flag.
Persists to /app/config/setup.json
"""

import json, os, threading

CONFIG_DIR  = os.getenv("CONFIG_DIR", "/app/config")
SETUP_PATH  = os.path.join(CONFIG_DIR, "setup.json")
_lock = threading.Lock()

# All available modules — order matters (display order)
ALL_MODULES = [
    {
        "id": "dashboard",
        "name": "Device Dashboard",
        "desc": "Live status monitoring for all your devices — signal presence, PSU health, temps, fans.",
        "icon": "📊",
        "required": True,
    },
    {
        "id": "dms_control",
        "name": "DMS 3600 Control",
        "desc": "Full routing control for the Extron DMS 3600 distribution matrix.",
        "icon": "🎛",
    },
    {
        "id": "matrix12800_control",
        "name": "Matrix 12800 Control",
        "desc": "Route the 128×128 core matrix — ties, presets, bank navigation, name editing.",
        "icon": "⚡",
    },
    {
        "id": "mtx_editor",
        "name": "MTX File Editor",
        "desc": "Edit Matrix 12800 virtual I/O config (.MTX) files — remap ports, change signal types, batch add, reorder.",
        "icon": "🗂",
    },
    {
        "id": "smx_control",
        "name": "SMX Control",
        "desc": "Route the System Multi Matrix — per-plane or all-planes ties, 32 global presets.",
        "icon": "🔀",
    },
    {
        "id": "mtpx_control",
        "name": "MTPX Control",
        "desc": "Control the Extron MTPX Plus multi-format presentation matrix.",
        "icon": "📺",
    },
    {
        "id": "ipcp_control",
        "name": "IPCP Controller",
        "desc": "Manage IPCP 505 sub-devices, serial bridges, COM ports, IR, relays, and flex I/O.",
        "icon": "🔌",
    },
    {
        "id": "vtg_control",
        "name": "VTG Controller",
        "desc": "Control the Extron VTG 400 video test generator — format, output, pattern.",
        "icon": "📡",
    },
    {
        "id": "autoswitch",
        "name": "Auto-Switching",
        "desc": "Signal-aware automatic routing rules — when input goes active, fire a command chain.",
        "icon": "🤖",
        "badge": "coming soon",
    },
    {
        "id": "virtual_presets",
        "name": "Virtual Presets",
        "desc": "Multi-device macro presets that span your whole rack — tie, recall, power, all in one tap.",
        "icon": "⭐",
        "badge": "coming soon",
    },
]

DEVICE_TEMPLATES = [
    {"template": "dms3600",      "name": "DMS 3600",       "kind": "dms3600",      "port": 23,
     "desc": "Distribution Matrix Switcher",              "icon": "🎛"},
    {"template": "matrix12800",  "name": "Matrix 12800",   "kind": "matrix12800",  "port": 23,
     "desc": "128×128 Core Routing Matrix",               "icon": "⚡"},
    {"template": "smx",          "name": "SMX",            "kind": "smx",          "port": 23,
     "desc": "System Multi Matrix (modular)",             "icon": "🔀"},
    {"template": "mtpx",         "name": "MTPX Plus",      "kind": "extron_info",  "port": 23,
     "desc": "Multi-Format Presentation Matrix",          "icon": "📺"},
    {"template": "vtg400",       "name": "VTG 400",        "kind": "extron_info",  "port": 23,
     "desc": "Video Test Generator",                      "icon": "📡"},
    {"template": "ipcp505",      "name": "IPCP 505",       "kind": "ipcp505",      "port": 23,
     "desc": "IP Link Control Processor",                 "icon": "🔌"},
    {"template": "mgp",          "name": "MGP 464",        "kind": "mgp",          "port": 23,
     "desc": "Multi-Graphic Processor",                   "icon": "🖥"},
    {"template": "custom_sis",   "name": "Custom SIS Device", "kind": "extron_info", "port": 23,
     "desc": "Any SIS-compatible Extron device",          "icon": "🔧"},
]


def _defaults():
    return {
        "setup_complete": False,
        "enabled_modules": [m["id"] for m in ALL_MODULES if m.get("required")],
    }


def load():
    try:
        if os.path.exists(SETUP_PATH):
            with open(SETUP_PATH) as f:
                data = json.load(f)
            d = _defaults()
            d.update(data)
            return d
    except Exception:
        pass
    return _defaults()


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with _lock:
        with open(SETUP_PATH, "w") as f:
            json.dump(data, f, indent=2)


def is_setup_complete():
    return load().get("setup_complete", False)


def complete_setup():
    data = load()
    data["setup_complete"] = True
    save(data)


def get_enabled_modules():
    return load().get("enabled_modules", [])


def set_enabled_modules(module_ids: list):
    data = load()
    # Always keep required modules
    required = [m["id"] for m in ALL_MODULES if m.get("required")]
    enabled = list(set(required) | set(module_ids))
    data["enabled_modules"] = enabled
    save(data)
    return enabled
