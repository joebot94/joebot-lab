"""
Joebot Lab Dashboard - device registry.

Each device declares:
  id        stable key used in the API / DOM
  name      human label
  ip        management IP
  hostname  DNS name
  family    family id (see FAMILIES)
  role      one-line description shown on the card
  kind      parser key -> selects how we poll/parse (see sis.py)
  policy    strict  -> RED when offline (we expect it up)
            lenient -> GRAY when offline (may legitimately be off / asleep)
            planned -> GRAY, reachable check only (reserved / not wired yet)
  port      TCP port (default 23)
  password  optional telnet password (Matrix 12800)
  meta      free-form dict shown as read-only metadata on the card

V1 is READ-ONLY. No control fields are honored by the poller.
"""

FAMILIES = [
    {"id": "core",     "name": "Core Routing"},
    {"id": "scalers",  "name": "Scalers / Distribution"},
    {"id": "mgp",      "name": "MGP Processors"},
    {"id": "power",    "name": "Power Controllers"},
    {"id": "control",  "name": "TouchLink / Control"},
    {"id": "lab",      "name": "Lab / Compute / Retro"},
    {"id": "media",    "name": "Displays / Consoles"},
]

DEVICES = [
    # ---- Core Routing -------------------------------------------------------
    {"id": "smx", "name": "SMX", "ip": "10.0.0.11", "hostname": "smx.extron.video",
     "family": "core", "role": "System Multi Matrix (modular)", "kind": "smx",
     "policy": "strict", "meta": {"part": "60-857-01"}},

    {"id": "mx", "name": "Matrix 12800", "ip": "10.0.0.12", "hostname": "mx.extron.video",
     "family": "core", "role": "128x128 core matrix", "kind": "matrix12800",
     "policy": "strict", "password": "admin",
     "meta": {"size": "128x128", "psu_target": "PSU 1 + PSU 3 (1212 acceptable)"}},

    {"id": "dms", "name": "DMS 3600", "ip": "10.0.0.13", "hostname": "dms.extron.video",
     "family": "core", "role": "36x36 digital matrix", "kind": "dms3600",
     "policy": "strict", "meta": {}},

    {"id": "dxp", "name": "DXP", "ip": "10.0.0.14", "hostname": "dxp.extron.video",
     "family": "core", "role": "HDMI matrix", "kind": "extron_info", "policy": "lenient"},

    {"id": "mtpx1", "name": "MTPX Plus #1", "ip": "10.0.0.15", "hostname": "mtpx1.extron.video",
     "family": "core", "role": "MTPX Plus (RGB skew rig)", "kind": "extron_info",
     "policy": "lenient", "meta": {"note": "RGB skew: W{in}*{r}*{g}*{b}Iseq (~35 cmd/s)"}},

    {"id": "mtpx2", "name": "MTPX Plus #2", "ip": "10.0.0.16", "hostname": "mtpx2.extron.video",
     "family": "core", "role": "MTPX Plus (RGB skew rig)", "kind": "extron_info",
     "policy": "lenient", "meta": {"note": "RGB skew: W{in}*{r}*{g}*{b}Iseq (~35 cmd/s)"}},

    # ---- Scalers / Distribution --------------------------------------------
    {"id": "sw4-1", "name": "SW4 #1", "ip": "10.0.0.21", "hostname": "sw4-1.extron.video",
     "family": "scalers", "role": "4-input switcher", "kind": "extron_info", "policy": "lenient"},
    {"id": "sw4-2", "name": "SW4 #2", "ip": "10.0.0.22", "hostname": "sw4-2.extron.video",
     "family": "scalers", "role": "4-input switcher", "kind": "extron_info", "policy": "lenient"},
    {"id": "sw4-3", "name": "SW4 #3", "ip": "10.0.0.23", "hostname": "sw4-3.extron.video",
     "family": "scalers", "role": "4-input switcher", "kind": "extron_info", "policy": "lenient"},
    {"id": "dvs304", "name": "DVS 304", "ip": "10.0.0.24", "hostname": "dvs304.extron.video",
     "family": "scalers", "role": "scaler", "kind": "extron_info", "policy": "lenient"},
    {"id": "dvs605-1", "name": "DVS 605 #1", "ip": "10.0.0.25", "hostname": "dvs605-1.extron.video",
     "family": "scalers", "role": "scaler", "kind": "extron_info", "policy": "lenient"},
    {"id": "dvs605-2", "name": "DVS 605 #2", "ip": "10.0.0.26", "hostname": "dvs605-2.extron.video",
     "family": "scalers", "role": "scaler", "kind": "extron_info", "policy": "lenient"},

    {"id": "dsc1", "name": "DSC 401A #1", "ip": "10.0.0.41", "hostname": "dsc1.extron.video",
     "family": "scalers", "role": "scaling converter", "kind": "extron_info", "policy": "lenient"},
    {"id": "dsc2", "name": "DSC 401A #2", "ip": "10.0.0.42", "hostname": "dsc2.extron.video",
     "family": "scalers", "role": "scaling converter", "kind": "extron_info", "policy": "lenient"},
    {"id": "dsc3", "name": "DSC 401A #3", "ip": "10.0.0.43", "hostname": "dsc3.extron.video",
     "family": "scalers", "role": "scaling converter", "kind": "extron_info", "policy": "lenient"},
    {"id": "dsc4", "name": "DSC 401A #4", "ip": "10.0.0.44", "hostname": "dsc4.extron.video",
     "family": "scalers", "role": "scaling converter", "kind": "extron_info", "policy": "lenient"},

    {"id": "hdhd4k-xi", "name": "HD-HD 4K xi", "ip": "10.0.0.46", "hostname": "hdhd4k-xi.extron.video",
     "family": "scalers", "role": "HD-HD 4K Plus distribution", "kind": "extron_info", "policy": "lenient"},
    {"id": "hdhd4k-a1", "name": "HD-HD 4K A1", "ip": "10.0.0.47", "hostname": "hdhd4k-a1.extron.video",
     "family": "scalers", "role": "HD-HD 4K Plus distribution", "kind": "extron_info", "policy": "lenient"},
    {"id": "hdhd4k-a2", "name": "HD-HD 4K A2", "ip": "10.0.0.48", "hostname": "hdhd4k-a2.extron.video",
     "family": "scalers", "role": "HD-HD 4K Plus distribution", "kind": "extron_info", "policy": "lenient"},

    # ---- MGP Processors -----------------------------------------------------
    {"id": "mgp1", "name": "MGP 464 #1", "ip": "10.0.0.61", "hostname": "mgp1.extron.video",
     "family": "mgp", "role": "primary 2x2 multi-window", "kind": "mgp", "policy": "strict",
     "meta": {}},
    {"id": "mgp2", "name": "MGP 464 #2", "ip": "10.0.0.62", "hostname": "mgp2.extron.video",
     "family": "mgp", "role": "second group / column for 2x4", "kind": "mgp", "policy": "strict",
     "meta": {}},
    {"id": "mgp3", "name": "MGP 464 #3", "ip": "10.0.0.63", "hostname": "mgp3.extron.video",
     "family": "mgp", "role": "reserved", "kind": "mgp", "policy": "planned"},
    {"id": "mgp4", "name": "MGP 464 #4", "ip": "10.0.0.64", "hostname": "mgp4.extron.video",
     "family": "mgp", "role": "reserved", "kind": "mgp", "policy": "planned"},
    {"id": "mgp5", "name": "MGP 464 #5", "ip": "10.0.0.65", "hostname": "mgp5.extron.video",
     "family": "mgp", "role": "reserved", "kind": "mgp", "policy": "planned"},

    # ---- Power Controllers --------------------------------------------------
    {"id": "p1", "name": "IPL T PCS4 #1", "ip": "10.0.0.101", "hostname": "p1.extron.video",
     "family": "power", "role": "outlet 2 feeds Matrix 12800", "kind": "pcs4", "policy": "strict",
     "meta": {"firmware_seen": "V1.16", "matrix_on": "outlet 2", "control": "READ-ONLY in V1"}},
    {"id": "p2", "name": "IPL T PCS4 #2", "ip": "10.0.0.102", "hostname": "p2.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p3", "name": "IPL T PCS4 #3", "ip": "10.0.0.103", "hostname": "p3.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p4", "name": "IPL T PCS4 #4", "ip": "10.0.0.104", "hostname": "p4.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p5", "name": "IPL T PCS4 #5", "ip": "10.0.0.105", "hostname": "p5.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p6", "name": "IPL T PCS4 #6", "ip": "10.0.0.106", "hostname": "p6.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p7", "name": "IPL T PCS4 #7", "ip": "10.0.0.107", "hostname": "p7.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},
    {"id": "p8", "name": "IPL T PCS4 #8", "ip": "10.0.0.108", "hostname": "p8.extron.video",
     "family": "power", "role": "power controller", "kind": "pcs4", "policy": "planned"},

    # ---- TouchLink / Control ------------------------------------------------
    {"id": "ipcp505-bridge", "name": "IPCP Pro 505 Bridge", "ip": "10.0.0.5",
     "hostname": "505.extron.video",
     "family": "control", "role": "Extron IPCP Pro 505 — serial bridge / relay controller",
     "kind": "ipcp505", "policy": "strict",
     "meta": {
         "serial_01": "VSC 700 #1",
         "serial_02": "VSC 700 #2",
         "serial_03": "VSC 700 #3",
         "serial_04": "VSC 700 #4",
         "serial_05": "USP 405 #1",
         "serial_06": "USP 405 #2",
     }},

    {"id": "ipcp555", "name": "IPCP Pro 555", "ip": "10.0.0.31", "hostname": "ipcp555.extron.video",
     "family": "control", "role": "control processor", "kind": "extron_info", "policy": "lenient"},
    {"id": "ipcp505-1", "name": "IPCP 505 #1", "ip": "10.0.0.32", "hostname": "ipcp505-1.extron.video",
     "family": "control", "role": "relay control (TitleMaker GPI)", "kind": "extron_info", "policy": "lenient",
     "meta": {"relay": "W{relay}*3*{duration}o", "http": "port 80"}},
    {"id": "ipcp505-2", "name": "IPCP 505 #2", "ip": "10.0.0.33", "hostname": "ipcp505-2.extron.video",
     "family": "control", "role": "relay control", "kind": "extron_info", "policy": "lenient"},

    {"id": "tlp1720", "name": "TLP Pro 1720", "ip": "10.0.0.111", "hostname": "tlp1720.extron.video",
     "family": "control", "role": "touchpanel", "kind": "host", "policy": "lenient"},
    {"id": "tlp1000mv-1", "name": "TLP 1000MV #1", "ip": "10.0.0.112", "hostname": "tlp1000mv-1.extron.video",
     "family": "control", "role": "touchpanel", "kind": "host", "policy": "lenient"},
    {"id": "tlp1000mv-2", "name": "TLP 1000MV #2", "ip": "10.0.0.113", "hostname": "tlp1000mv-2.extron.video",
     "family": "control", "role": "touchpanel", "kind": "host", "policy": "lenient"},
    {"id": "tlp1000tv-1", "name": "TLP 1000TV #1", "ip": "10.0.0.114", "hostname": "tlp1000tv-1.extron.video",
     "family": "control", "role": "touchpanel", "kind": "host", "policy": "lenient"},
    {"id": "tlp1000tv-2", "name": "TLP 1000TV #2", "ip": "10.0.0.115", "hostname": "tlp1000tv-2.extron.video",
     "family": "control", "role": "touchpanel", "kind": "host", "policy": "lenient"},

    # ---- Lab / Compute / Retro ---------------------------------------------
    {"id": "nas2", "name": "NAS 2", "ip": "10.0.0.50", "hostname": "nas2.joe.bot",
     "family": "lab", "role": "secondary NAS", "kind": "host", "policy": "lenient", "port": 80},
    {"id": "mister", "name": "MiSTer", "ip": "10.0.0.51", "hostname": "mister.joe.bot",
     "family": "lab", "role": "FPGA retro", "kind": "host", "policy": "lenient", "port": 22},
    {"id": "mame", "name": "MAME", "ip": "10.0.0.52", "hostname": "mame.joe.bot",
     "family": "lab", "role": "arcade box", "kind": "host", "policy": "lenient"},
    {"id": "pi", "name": "Raspberry Pi", "ip": "10.0.0.53", "hostname": "pi.joe.bot",
     "family": "lab", "role": "utility pi", "kind": "host", "policy": "lenient", "port": 22},
    {"id": "n100", "name": "N100", "ip": "10.0.0.54", "hostname": "n100.joe.bot",
     "family": "lab", "role": "mini PC", "kind": "host", "policy": "lenient"},

    # ---- Displays / Consoles ------------------------------------------------
    {"id": "kuro", "name": "Pioneer Kuro", "ip": "10.0.0.70", "hostname": "kuro.joe.bot",
     "family": "media", "role": "plasma display", "kind": "host", "policy": "lenient"},
    {"id": "roku", "name": "Roku", "ip": "10.0.0.71", "hostname": "roku.joe.bot",
     "family": "media", "role": "streamer", "kind": "host", "policy": "lenient", "port": 8060},
    {"id": "xbox", "name": "Xbox", "ip": "10.0.0.80", "hostname": "xbox.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "xbox360", "name": "Xbox 360", "ip": "10.0.0.81", "hostname": "xbox360.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "ps2", "name": "PS2", "ip": "10.0.0.82", "hostname": "ps2.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "ps3", "name": "PS3", "ip": "10.0.0.83", "hostname": "ps3.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "ps4", "name": "PS4", "ip": "10.0.0.84", "hostname": "ps4.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "ps5", "name": "PS5", "ip": "10.0.0.85", "hostname": "ps5.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "xboxone", "name": "Xbox One", "ip": "10.0.0.86", "hostname": "xboxone.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "xboxseriesx", "name": "Xbox Series X", "ip": "10.0.0.87", "hostname": "xboxseriesx.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "gamecube", "name": "GameCube", "ip": "10.0.0.88", "hostname": "gamecube.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "wii", "name": "Wii", "ip": "10.0.0.89", "hostname": "wii.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "wiiu", "name": "Wii U", "ip": "10.0.0.90", "hostname": "wiiu.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
    {"id": "switch", "name": "Switch", "ip": "10.0.0.91", "hostname": "switch.joe.bot",
     "family": "media", "role": "console", "kind": "host", "policy": "lenient"},
]

# SMX physical slot metadata (hardcoded per handoff; live signal still polled).
# slot -> (label, plane, signal_command)
SMX_SLOTS = {
    10: {"label": "VGA 16x16",         "plane": "00", "ls_cmd": "10*0LS"},
    4:  {"label": "S-VIDEO DIN 16x16", "plane": "01", "ls_cmd": "4*0LS"},
    2:  {"label": "VIDEO 16x16",       "plane": "02", "ls_cmd": "2*0LS"},
    6:  {"label": "AUDIO 16x16",       "plane": "04", "ls_cmd": "6*0LS"},
}


def device_by_id(did):
    for d in DEVICES:
        if d["id"] == did:
            return d
    return None
