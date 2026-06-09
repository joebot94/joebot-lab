"""
Matrix 12800 name persistence.
Stores in /app/config/matrix12800_names.json.
"""

import json, os, threading

CONFIG_DIR  = os.getenv("CONFIG_DIR", "/app/config")
NAMES_PATH  = os.path.join(CONFIG_DIR, "matrix12800_names.json")
N_INPUTS    = 128
N_OUTPUTS   = 128
N_PRESETS   = 64
_lock = threading.Lock()


def _defaults():
    return {
        "inputs":  {str(i): f"Input {i}"   for i in range(1, N_INPUTS  + 1)},
        "outputs": {str(i): f"Output {i}"  for i in range(1, N_OUTPUTS + 1)},
        "presets": {str(i): f"Preset {i}"  for i in range(1, N_PRESETS + 1)},
    }


def load():
    try:
        if os.path.exists(NAMES_PATH):
            with open(NAMES_PATH) as f:
                data = json.load(f)
            if "inputs" in data and "outputs" in data:
                d = _defaults()
                for section in ("inputs", "outputs", "presets"):
                    d[section].update(data.get(section, {}))
                return d
    except Exception:
        pass
    return _defaults()


def save(data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with _lock:
        with open(NAMES_PATH, "w") as f:
            json.dump(data, f, indent=2)


def update_names(section, updates: dict):
    data = load()
    if section not in data:
        data[section] = {}
    for k, v in updates.items():
        data[section][str(k)] = str(v)[:32].strip()
    save(data)
    return data
