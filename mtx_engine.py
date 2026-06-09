"""
MTX Engine — pure Python parser / formatter for Extron .MTX virtual config files.
No Tkinter or external dependencies. Used by /config/mtx web editor.

MTX line format:
    {virt_idx:02d}={code},{kind} {label:03d},,{phys_tokens}
  code: 01=RGB(w=3), 05=S-Video(w=2), 04=Composite(w=1)
  phys tokens: 0i001,0i002,0i003 (inputs)  or  0o001 (output)
"""

import re

_PAT   = re.compile(r"^(\d{2})=(\d{2}),(Input|Output)\s+(\d{3}),(.*)$")
_SIZE  = re.compile(r"^size=(\d+)x(\d+)\s*$",            re.IGNORECASE)
_PTYPE = re.compile(r"^plane_type\s*=\s*(\S+)\s*$",       re.IGNORECASE)
_PUNIT = re.compile(r"^plane_unit\s*=\s*(\S+)\s*$",       re.IGNORECASE)


# ── helpers ───────────────────────────────────────────────────────────────────

def width_from_code(code: str) -> int:
    return {"01": 3, "05": 2}.get(code, 1)


def code_from_width(width: int) -> str:
    return {3: "01", 2: "05"}.get(int(width), "04")


def _parse_phys(rest: str, kind: str) -> list:
    pfx = "0i" if kind == "Input" else "0o"
    out = []
    for t in rest.split(","):
        t = t.strip()
        if t.startswith(pfx) and t[2:].isdigit():
            out.append(int(t[2:]))
    return out


def _make_line(vi: int, code: str, kind: str, label: int,
               phys: list, plane_type: str) -> str:
    pfx = "0i" if kind == "Input" else "0o"
    tokens = [f"{pfx}{n:03d}" for n in phys]
    if plane_type and plane_type.upper() == "RGB":
        missing = max(0, 3 - len(tokens))
        phys_str = ",".join(tokens) + "," * missing
    else:
        phys_str = ",".join(tokens)
    return f"{vi:02d}={code},{kind} {label:03d},,{phys_str}"


# ── parse / build ─────────────────────────────────────────────────────────────

def parse_text(text: str) -> dict:
    """Parse .MTX file text → model dict."""
    size = None; plane_type = None; plane_unit = None
    for ln in text.splitlines():
        s = ln.strip()
        m = _SIZE.match(s)
        if m: size = [int(m.group(1)), int(m.group(2))]; continue
        m = _PTYPE.match(s)
        if m: plane_type = m.group(1); continue
        m = _PUNIT.match(s)
        if m: plane_unit = m.group(1); continue

    vin = []; vout = []; hi = 0; ho = 0
    for ln in text.splitlines():
        m = _PAT.match(ln)
        if not m: continue
        vi = int(m.group(1)); code = m.group(2)
        kind = m.group(3); label = int(m.group(4))
        phys = _parse_phys(m.group(5), kind)
        if kind == "Input":
            vin.append({"i": vi, "code": code, "label": label, "phys": phys})
            if phys: hi = max(hi, max(phys))
        else:
            vout.append({"i": vi, "code": code, "label": label, "phys": phys})
            if phys: ho = max(ho, max(phys))

    vin.sort(key=lambda r: r["i"]); vout.sort(key=lambda r: r["i"])
    return {
        "size": size or [len(vin), len(vout)],
        "plane_type": plane_type or "LC",
        "plane_unit": plane_unit or "00",
        "vin": vin, "vout": vout,
        "highest_in": hi, "highest_out": ho,
    }


def build_text(model: dict) -> str:
    """Serialize model → .MTX file text (always rebuilds clean from vin/vout)."""
    plane = (model.get("plane_type") or "LC").upper()
    vin  = sorted(model.get("vin",  []), key=lambda r: r["i"])
    vout = sorted(model.get("vout", []), key=lambda r: r["i"])

    lines = ["[virt_config]", f"size={len(vin)}x{len(vout)}"]
    if plane == "RGB":
        lines += ["plane_type=RGB", "plane_unit=000"]
    else:
        lines += ["plane_type=LC",  "plane_unit=00"]

    for rec in vin:
        lines.append(_make_line(rec["i"], rec["code"], "Input",
                                rec["label"], rec["phys"], plane))
    for rec in vout:
        lines.append(_make_line(rec["i"], rec["code"], "Output",
                                rec["label"], rec["phys"], plane))
    return "\n".join(lines)


def create_template(size_in: int, size_out: int, plane: str, width: int) -> dict:
    """Build a fresh model with sequential physical port mapping."""
    code = code_from_width(width)
    vin = []; vout = []
    p = 1
    for i in range(1, size_in + 1):
        vin.append({"i": i, "code": code, "label": i,
                    "phys": list(range(p, p + width))})
        p += width
    p = 1
    for o in range(1, size_out + 1):
        vout.append({"i": o, "code": code, "label": o,
                     "phys": list(range(p, p + width))})
        p += width
    return {
        "size": [size_in, size_out],
        "plane_type": plane.upper(),
        "plane_unit": "000" if plane.upper() == "RGB" else "00",
        "vin": vin, "vout": vout,
        "highest_in": size_in * width,
        "highest_out": size_out * width,
    }


# ── operations ────────────────────────────────────────────────────────────────

def op_remap(model: dict, kind: str, virt_i: int,
             new_code: str, new_phys: list) -> dict:
    """Change signal type / physical ports for one virtual I/O."""
    items = model["vin"] if kind == "Input" else model["vout"]
    for rec in items:
        if rec["i"] == virt_i:
            rec["code"] = new_code
            rec["phys"] = new_phys
            break
    _refresh_meta(model)
    return model


def op_add(model: dict, blocks: list) -> dict:
    """
    Add virtual I/Os.
    Block keys: add_in, count_in, win, phys_in (list|None for auto-tail)
                add_out, count_out, wout, phys_out (list|None for auto-tail)
    """
    hi = model.get("highest_in", 0)
    ho = model.get("highest_out", 0)
    next_vi = (model["vin"][-1]["i"]  + 1) if model["vin"]  else 1
    next_vo = (model["vout"][-1]["i"] + 1) if model["vout"] else 1

    for b in blocks:
        if b.get("add_in"):
            w = int(b["win"]); code = code_from_width(w)
            manual = b.get("phys_in") or None
            for _ in range(int(b.get("count_in", 1))):
                if manual:
                    phys = list(manual)
                else:
                    phys = list(range(hi + 1, hi + w + 1)); hi += w
                model["vin"].append({"i": next_vi, "code": code,
                                     "label": next_vi, "phys": phys})
                next_vi += 1

        if b.get("add_out"):
            w = int(b["wout"]); code = code_from_width(w)
            manual = b.get("phys_out") or None
            for _ in range(int(b.get("count_out", 1))):
                if manual:
                    phys = list(manual)
                else:
                    phys = list(range(ho + 1, ho + w + 1)); ho += w
                model["vout"].append({"i": next_vo, "code": code,
                                      "label": next_vo, "phys": phys})
                next_vo += 1

    model["highest_in"] = hi; model["highest_out"] = ho
    _refresh_meta(model)
    return model


def op_delete(model: dict, del_vin: list, del_vout: list,
              compact: bool = True) -> dict:
    """Delete virtuals by index; optionally compact remaining 1..N."""
    ds_in  = set(del_vin);  ds_out = set(del_vout)
    model["vin"]  = [r for r in model["vin"]  if r["i"] not in ds_in]
    model["vout"] = [r for r in model["vout"] if r["i"] not in ds_out]
    if compact:
        for new_i, rec in enumerate(model["vin"],  1): rec["i"] = new_i; rec["label"] = new_i
        for new_i, rec in enumerate(model["vout"], 1): rec["i"] = new_i; rec["label"] = new_i
    _refresh_meta(model)
    return model


def op_reorder(model: dict, vin_order: list, vout_order: list) -> dict:
    """Reorder virtuals; renumbers 1..N in new order."""
    by_in  = {r["i"]: r for r in model["vin"]}
    by_out = {r["i"]: r for r in model["vout"]}
    model["vin"]  = [by_in[i]  for i in vin_order  if i in by_in]
    model["vout"] = [by_out[i] for i in vout_order if i in by_out]
    for new_i, rec in enumerate(model["vin"],  1): rec["i"] = new_i; rec["label"] = new_i
    for new_i, rec in enumerate(model["vout"], 1): rec["i"] = new_i; rec["label"] = new_i
    _refresh_meta(model)
    return model


def op_merge_rgb(model: dict, in_phys: list, out_phys: list) -> dict:
    """Merge 3 Composite virtuals (by physical port number) into one RGB virtual."""
    def find_vi(items, p):
        for r in items:
            if p in r["phys"]: return r["i"]
        return None

    del_vin  = list({find_vi(model["vin"],  p) for p in in_phys}  - {None})
    del_vout = list({find_vi(model["vout"], p) for p in out_phys} - {None})

    model = op_delete(model, del_vin, del_vout, compact=False)
    model = op_add(model, [{
        "add_in":   bool(in_phys),  "count_in":  1, "win":  3, "phys_in":  list(in_phys),
        "add_out":  bool(out_phys), "count_out": 1, "wout": 3, "phys_out": list(out_phys),
    }])
    model = op_delete(model, [], [], compact=True)
    model["plane_type"] = "RGB"; model["plane_unit"] = "000"
    _refresh_meta(model)
    return model


def _refresh_meta(model: dict):
    model["size"] = [len(model["vin"]), len(model["vout"])]
    if model["vin"]:
        all_in = [p for r in model["vin"] for p in r["phys"]]
        model["highest_in"] = max(all_in) if all_in else 0
    if model["vout"]:
        all_out = [p for r in model["vout"] for p in r["phys"]]
        model["highest_out"] = max(all_out) if all_out else 0
