"""
Joebot Lab Dashboard - device registry defaults.

On a fresh install this file provides the empty starting state.
All devices are stored in /app/config/devices.json after first run —
edits made in the UI persist there and this file is never written to.

Each device declares:
  id        stable key used in the API / DOM
  name      human label
  ip        management IP
  hostname  DNS name (optional)
  family    family id (see FAMILIES)
  role      one-line description shown on the card
  kind      parser key -> selects how we poll/parse (see sis.py)
  policy    strict  -> RED when offline (we expect it up)
            lenient -> GRAY when offline (may legitimately be off)
  port      TCP port (default 23)
  password  optional telnet password
  meta      free-form dict shown as read-only metadata on the card
"""

FAMILIES = [
    {"id": "core",    "name": "Core Routing"},
    {"id": "dist",    "name": "Scalers / Distribution"},
    {"id": "mgp",     "name": "MGP Processors"},
    {"id": "power",   "name": "Power Controllers"},
    {"id": "control", "name": "Control Processors"},
    {"id": "lab",     "name": "Lab / Compute / Retro"},
    {"id": "media",   "name": "Displays / Consoles"},
]

# Empty by default — add devices through the setup wizard or /config page
DEVICES = []

# Slot metadata for SMX boards — populated per-install via config
SMX_SLOTS = {}
