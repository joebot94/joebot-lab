"""
IR code storage — persists learned IR code assignments per remote + button.
Each entry maps remote_id.button_id → {ir_slot, ir_port, label, learned}.
"""
import json
import os
import pathlib
import threading

STORE_PATH = pathlib.Path(os.getenv("CONFIG_DIR", "/app/config")) / "ir_codes.json"
_lock = threading.Lock()


def load() -> dict:
    try:
        with open(STORE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(data: dict) -> tuple[bool, str | None]:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(STORE_PATH, "w") as f:
            json.dump(data, f, indent=2)
        return True, None
    except OSError as e:
        return False, str(e)


def get_codes(remote_id: str) -> dict:
    with _lock:
        return load().get(remote_id, {})


def set_code(remote_id: str, button_id: str, ir_slot: int | None,
             ir_port: int = 1, label: str = "") -> tuple[bool, str | None]:
    with _lock:
        data = load()
        if remote_id not in data:
            data[remote_id] = {}
        if ir_slot is None:
            data[remote_id].pop(button_id, None)
        else:
            data[remote_id][button_id] = {
                "ir_slot": ir_slot,
                "ir_port": ir_port,
                "label":   label,
            }
        return save(data)


def clear_remote(remote_id: str) -> tuple[bool, str | None]:
    with _lock:
        data = load()
        data.pop(remote_id, None)
        return save(data)


# ── Remote definitions ───────────────────────────────────────────────────────
# Each remote has a list of button defs.  Layout is driven by the HTML renderer.
# button fields: id, label, group (visual style), row, col, colspan, rowspan

RETROTINK4K_BUTTONS = [
    # ── top strip ────────────────────────────────────────────────────────────
    {"id": "power",     "label": "⏻",      "group": "power",   "row": 0, "col": 0},
    {"id": "input",     "label": "INPUT",  "group": "fn",      "row": 0, "col": 1},
    {"id": "out",       "label": "OUT",    "group": "fn",      "row": 0, "col": 2},
    {"id": "scl",       "label": "SCL",    "group": "fn",      "row": 0, "col": 3},
    {"id": "sfx",       "label": "SFX",    "group": "fn",      "row": 1, "col": 1},
    {"id": "adc",       "label": "ADC",    "group": "fn",      "row": 1, "col": 2},
    {"id": "prof",      "label": "PROF",   "group": "fn",      "row": 1, "col": 3},
    # ── number pad ───────────────────────────────────────────────────────────
    {"id": "num_1",     "label": "1",      "group": "num",     "row": 2, "col": 0},
    {"id": "num_2",     "label": "2",      "group": "num",     "row": 2, "col": 1},
    {"id": "num_3",     "label": "3",      "group": "num",     "row": 2, "col": 2},
    {"id": "num_4",     "label": "4",      "group": "num",     "row": 3, "col": 0},
    {"id": "num_5",     "label": "5",      "group": "num",     "row": 3, "col": 1},
    {"id": "num_6",     "label": "6",      "group": "num",     "row": 3, "col": 2},
    {"id": "num_7",     "label": "7",      "group": "num",     "row": 4, "col": 0},
    {"id": "num_8",     "label": "8",      "group": "num",     "row": 4, "col": 1},
    {"id": "num_9",     "label": "9",      "group": "num",     "row": 4, "col": 2},
    {"id": "num_10",    "label": "10",     "group": "num",     "row": 5, "col": 0},
    {"id": "num_11",    "label": "11",     "group": "num",     "row": 5, "col": 1},
    {"id": "num_12",    "label": "12",     "group": "num",     "row": 5, "col": 2},
    # ── nav cluster ──────────────────────────────────────────────────────────
    {"id": "menu",      "label": "MENU",   "group": "nav-side", "row": 6, "col": 0},
    {"id": "nav_up",    "label": "▲",      "group": "dpad",    "row": 6, "col": 1},
    {"id": "back",      "label": "BACK",   "group": "nav-side", "row": 6, "col": 2},
    {"id": "nav_left",  "label": "◀",      "group": "dpad",    "row": 7, "col": 0},
    {"id": "enter",     "label": "ENTER",  "group": "dpad-ctr", "row": 7, "col": 1},
    {"id": "nav_right", "label": "▶",      "group": "dpad",    "row": 7, "col": 2},
    {"id": "diag",      "label": "DIAG",   "group": "nav-side", "row": 8, "col": 0},
    {"id": "nav_down",  "label": "▼",      "group": "dpad",    "row": 8, "col": 1},
    {"id": "stat",      "label": "STAT",   "group": "nav-side", "row": 8, "col": 2},
    # ── process controls ─────────────────────────────────────────────────────
    {"id": "gain",      "label": "GAIN",   "group": "proc",    "row": 9,  "col": 0},
    {"id": "play_pause","label": "▶⏸",    "group": "proc",    "row": 9,  "col": 1},
    {"id": "gen",       "label": "GEN",    "group": "proc",    "row": 9,  "col": 2},
    {"id": "auto",      "label": "AUTO",   "group": "proc",    "row": 10, "col": 0},
    {"id": "sync",      "label": "SYNC",   "group": "proc",    "row": 10, "col": 2},
    {"id": "pha",       "label": "PHA",    "group": "proc",    "row": 11, "col": 0},
    {"id": "safe",      "label": "SAFE",   "group": "proc",    "row": 11, "col": 1},
    {"id": "buf",       "label": "BUF",    "group": "proc",    "row": 11, "col": 2},
    # ── resolution presets ───────────────────────────────────────────────────
    {"id": "res_4k",    "label": "4K",     "group": "res",     "row": 12, "col": 0},
    {"id": "res_1080p", "label": "1080p",  "group": "res",     "row": 12, "col": 1},
    {"id": "res_1440p", "label": "1440p",  "group": "res",     "row": 12, "col": 2},
    {"id": "res_480p",  "label": "480p",   "group": "res",     "row": 12, "col": 3},
    {"id": "res1",      "label": "RES1",   "group": "res",     "row": 13, "col": 0},
    {"id": "res2",      "label": "RES2",   "group": "res",     "row": 13, "col": 1},
    {"id": "res3",      "label": "RES3",   "group": "res",     "row": 13, "col": 2},
    {"id": "res4",      "label": "RES4",   "group": "res",     "row": 13, "col": 3},
    # ── AUX ──────────────────────────────────────────────────────────────────
    {"id": "aux1",      "label": "AUX1",   "group": "aux",     "row": 15, "col": 0},
    {"id": "aux2",      "label": "AUX2",   "group": "aux",     "row": 15, "col": 1},
    {"id": "aux3",      "label": "AUX3",   "group": "aux",     "row": 15, "col": 2},
    {"id": "aux4",      "label": "AUX4",   "group": "aux",     "row": 15, "col": 3},
    {"id": "aux5",      "label": "AUX5",   "group": "aux",     "row": 16, "col": 0},
    {"id": "aux6",      "label": "AUX6",   "group": "aux",     "row": 16, "col": 1},
    {"id": "aux7",      "label": "AUX7",   "group": "aux",     "row": 16, "col": 2},
    {"id": "aux8",      "label": "AUX8",   "group": "aux",     "row": 16, "col": 3},
]

REMOTE_DEFS: dict[str, dict] = {
    "retrotink4k": {
        "id":      "retrotink4k",
        "name":    "RetroTINK 4K",
        "cols":    4,
        "rows":    17,
        "accent":  "#e05a1a",   # orange accent matching RT4K branding
        "buttons": RETROTINK4K_BUTTONS,
    },
    # VSC 700D, VSC 500, VSC 900D definitions will be added here once
    # their SIS manuals are available and button layouts are confirmed.
}
