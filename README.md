# JOEBOT LAB

**Modern browser-based control for classic Extron / professional AV gear.**

Run one Docker container, open a browser, and get a clean control room dashboard for hardware that deserves better software than it shipped with.

---

## What it does

- **Live device dashboard** — status, signal presence, PSU health, temps, fans
- **Matrix routing control** — tie inputs to outputs on Matrix 12800, SMX, DMS 3600
- **MTX file editor** — edit Matrix 12800 virtual I/O config files right in the browser
- **Preset recall** — global and per-device presets with confirmation flow
- **Name editing** — rename inputs, outputs, presets and push to the switcher
- **First-run setup wizard** — pick your modules, add your devices, done

### Supported devices

| Device | Status monitoring | Routing control | Notes |
|--------|:-----------------:|:---------------:|-------|
| Extron Matrix 12800 | ✅ | ✅ | 128×128, banking, presets, MTX editor |
| Extron SMX | ✅ | ✅ | Per-plane or all-planes routing, 32 global presets |
| Extron DMS 3600 | ✅ | ✅ | Signal status, presets, name editing |
| Extron MGP 464 | ✅ | — | Status only |
| Extron IPCP 505 | ✅ | — | Serial bridge, sub-device status |
| Extron VTG 400 | ✅ | — | Status only |
| Generic SIS device | ✅ | — | Anything that speaks Extron SIS over Telnet |

---

## Quick start

### Requirements

- Docker + Docker Compose (v2)
- A machine on the same LAN as your AV gear (NAS, server, Pi, whatever)

### Install

```bash
git clone https://github.com/rustjay/joebot-lab.git
cd joebot-lab
docker compose up -d --build
```

Then open **`http://<your-server-ip>:8080`** in a browser.

On a fresh install the setup wizard will appear automatically.

### Update

```bash
git pull
docker compose up -d --build
```

Your config in `./config/` is never touched by updates.

---

## Configuration

All config lives in `./config/` (mounted into the container). You can back it up, copy it between machines, or wipe it to start fresh. Nothing is baked into the image.

| File | What it stores |
|------|----------------|
| `devices.json` | Your devices (IPs, types, groups) |
| `setup.json` | Which modules are enabled, setup complete flag |
| `dms_names.json` | DMS input/output/preset names |
| `matrix12800_names.json` | Matrix 12800 names |
| `smx_names.json` | SMX names |

### Environment variables

Set these in `docker-compose.yml` under `environment:`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PORT` | `8080` | Port the web UI listens on |
| `POLL_SECONDS` | `10` | How often to poll devices (seconds) |
| `SOCKET_TIMEOUT_SECONDS` | `4` | Telnet connection timeout |
| `POLL_WORKERS` | `16` | Concurrent device poll threads |
| `CONFIG_DIR` | `/app/config` | Config directory inside container |

---

## Architecture

```
One Docker container
├── FastAPI backend          (Python, no heavy deps)
├── Web UI                   (vanilla JS, no framework)
├── Device polling           (threaded, only active devices)
├── Routing/control API      (SIS over Telnet)
├── MTX file editor          (pure Python parser)
└── Setup wizard

Mounted ./config/            (persistent, survives updates)
```

Disabled modules have zero overhead — no polling, no sockets, no background tasks.

---

## Development

```bash
# Run locally without Docker (needs Python 3.12+)
pip install fastapi "uvicorn[standard]"
python app.py
```

---

## Roadmap

- [ ] Auto-switching rules engine (signal-aware routing)
- [ ] Virtual/macro presets spanning multiple devices
- [ ] IPCP sub-device config page
- [ ] Signal scan rate detection and safety routing
- [ ] MTX config push to Matrix 12800

---

## License

MIT — use it, fork it, share it with other people who still run Extron gear.
