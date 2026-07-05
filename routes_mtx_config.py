from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
import mtx_engine

router = APIRouter()

@router.get("/config/mtx", response_class=HTMLResponse)
def config_mtx():
    return HTMLResponse(MTX_HTML)


@router.post("/api/mtx/parse")
async def api_mtx_parse(request: Request):
    body = await request.json()
    text = body.get("text", "")
    try:
        model = mtx_engine.parse_text(text)
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/template")
async def api_mtx_template(request: Request):
    body = await request.json()
    try:
        model = mtx_engine.create_template(
            int(body.get("size_in", 8)),
            int(body.get("size_out", 8)),
            body.get("plane", "LC"),
            int(body.get("width", 1)),
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/serialize")
async def api_mtx_serialize(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        text = mtx_engine.build_text(model)
        return _Response(content=text, media_type="text/plain")
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/remap")
async def api_mtx_remap(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_remap(
            model,
            body.get("kind", "Input"),
            int(body.get("virt_i", 1)),
            body.get("code", "04"),
            [int(x) for x in body.get("phys", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/add")
async def api_mtx_add(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_add(model, body.get("blocks", []))
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/delete")
async def api_mtx_delete(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_delete(
            model,
            [int(x) for x in body.get("del_vin", [])],
            [int(x) for x in body.get("del_vout", [])],
            bool(body.get("compact", True)),
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/reorder")
async def api_mtx_reorder(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_reorder(
            model,
            [int(x) for x in body.get("vin_order", [])],
            [int(x) for x in body.get("vout_order", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/mtx/merge-rgb")
async def api_mtx_merge(request: Request):
    body = await request.json()
    model = body.get("model", {})
    try:
        model = mtx_engine.op_merge_rgb(
            model,
            [int(x) for x in body.get("in_phys", [])],
            [int(x) for x in body.get("out_phys", [])],
        )
        return JSONResponse({"ok": True, "model": model})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


MTX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Joebot Lab · MTX Editor</title>
<link rel="stylesheet" href="/static/lab.css"/>
<style>
  /* base tokens come from lab.css; page overrides blue accent */
  :root{ --blue:#3b82f6;--blue-dim:rgba(59,130,246,.12); }
  body{font-size:14px;padding-bottom:60px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    padding:14px 20px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,rgba(59,130,246,.05),transparent)}
  .brand{font-size:19px;font-weight:700;letter-spacing:.1em;color:#93c5fd}
  nav a{color:var(--muted);text-decoration:none;font-size:12px;padding:4px 9px;
    border-radius:6px;border:1px solid transparent}
  nav a:hover,nav a.active{color:var(--ink);border-color:var(--line)}
  .spacer{flex:1}
  main{max-width:1400px;margin:0 auto;padding:18px}
  button{font-family:var(--mono);cursor:pointer;font-size:13px}
  .btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
    border-radius:7px;padding:6px 13px}
  .btn:hover{border-color:var(--accent);color:var(--accent)}
  .btn-blue{background:var(--blue-dim);color:#93c5fd;border-color:rgba(59,130,246,.35)}
  .btn-blue:hover{background:rgba(59,130,246,.2)}
  .btn-ok{background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.35)}
  .btn-ok:hover{background:rgba(52,211,153,.2)}
  .btn-bad{background:rgba(255,84,112,.1);color:var(--bad);border-color:rgba(255,84,112,.3)}
  .btn-bad:hover{background:rgba(255,84,112,.2)}
  .btn-warn{background:rgba(245,185,66,.1);color:var(--warn);border-color:rgba(245,185,66,.3)}
  .btn-warn:hover{background:rgba(245,185,66,.2)}

  /* info bar */
  .info-bar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:10px 16px;margin-bottom:16px;font-size:13px}
  .info-bar .chip{background:var(--panel2);border:1px solid var(--line);
    border-radius:20px;padding:3px 10px;font-size:12px;color:var(--muted)}
  .info-bar .chip span{color:var(--ink)}
  #file-name{color:#93c5fd;font-weight:600}

  /* toolbar */
  .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}

  /* tabs */
  .tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:16px}
  .tab{background:transparent;color:var(--muted);border:1px solid transparent;
    border-bottom:none;border-radius:7px 7px 0 0;padding:7px 16px;font-size:13px}
  .tab:hover{color:var(--ink)}
  .tab.active{background:var(--panel);color:#93c5fd;border-color:var(--line);
    border-bottom-color:var(--panel)}
  .tab-pane{display:none}
  .tab-pane.active{display:block}

  /* port map */
  .map-wrap{display:flex;gap:16px;flex-wrap:wrap}
  .map-section{flex:1;min-width:300px}
  .map-title{font-size:12px;letter-spacing:.06em;text-transform:uppercase;
    color:var(--muted);margin-bottom:8px}
  canvas{border:1px solid var(--line);border-radius:6px;display:block;width:100%;
    cursor:crosshair}
  .legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:12px;
    color:var(--muted)}
  .legend-item{display:flex;align-items:center;gap:5px}
  .legend-swatch{width:14px;height:14px;border-radius:3px;flex-shrink:0}

  /* form elements */
  .field{margin-bottom:12px}
  .field label{display:block;color:var(--muted);font-size:11.5px;
    margin-bottom:4px;letter-spacing:.04em}
  .field input,.field select{width:100%;background:var(--panel2);border:1px solid var(--line);
    color:var(--ink);border-radius:7px;padding:7px 10px;
    font-family:var(--mono);font-size:13px}
  .field input:focus,.field select:focus{outline:none;border-color:#3b82f6}
  .field select option{background:var(--panel2)}
  .row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .section-box{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:14px 16px;margin-bottom:14px}
  .section-box h3{margin:0 0 12px;font-size:13px;font-weight:600;color:#93c5fd;
    letter-spacing:.04em}

  /* list boxes */
  .list-pair{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
  .list-box{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    height:220px;overflow-y:auto}
  .list-item{padding:5px 10px;font-size:12px;cursor:pointer;
    display:flex;align-items:center;gap:8px;border-bottom:1px solid rgba(255,255,255,.04)}
  .list-item:hover{background:rgba(255,255,255,.04)}
  .list-item.selected{background:rgba(59,130,246,.15);border-left:2px solid #3b82f6}
  .list-item .sig-dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}

  /* reorder list */
  .reorder-list{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
    height:240px;overflow-y:auto;padding:4px}
  .reorder-item{padding:5px 8px;font-size:12px;cursor:grab;
    display:flex;align-items:center;gap:8px;border-radius:5px;
    border:1px solid transparent;margin-bottom:2px;user-select:none}
  .reorder-item:hover{background:rgba(255,255,255,.05)}
  .reorder-item.dragging{opacity:.4}
  .reorder-item.drag-over{border-color:#3b82f6}
  .reorder-item .sig-dot{width:10px;height:10px;border-radius:2px;flex-shrink:0}
  .reorder-btns{display:flex;gap:6px;margin-top:6px}

  /* preview box */
  .preview{background:#080a0d;border:1px solid var(--line);border-radius:7px;
    padding:10px 12px;font-size:12px;color:#9aa3b3;margin-top:8px;
    white-space:pre-wrap;word-break:break-all;max-height:160px;overflow:auto}

  /* toast */
  .toast{position:fixed;bottom:24px;right:24px;background:var(--panel2);
    border:1px solid var(--line);border-radius:8px;padding:10px 16px;
    font-size:13px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:200}
  .toast.show{opacity:1}

  /* empty state */
  .empty{text-align:center;padding:60px 20px;color:var(--muted)}
  .empty h2{font-size:18px;margin-bottom:8px;color:#93c5fd}
  .empty p{font-size:13px;line-height:1.7}

  /* modal */
  .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);
    z-index:100;align-items:center;justify-content:center;padding:20px}
  .overlay.open{display:flex}
  .modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:24px;width:100%;max-width:480px}
  .modal h3{margin:0 0 18px;font-size:15px;font-weight:600}
  .modal-foot{display:flex;gap:10px;justify-content:flex-end;
    margin-top:18px;padding-top:14px;border-top:1px solid var(--line)}
  .check-row{display:flex;align-items:center;gap:8px;font-size:13px;
    cursor:pointer;padding:4px 0}
  .check-row input{accent-color:#3b82f6;width:15px;height:15px}
  @media(max-width:640px){
    .row2,.row3,.list-pair{grid-template-columns:1fr}
    .map-wrap{flex-direction:column}
  }
</style></head>
<body>
<header>
  <div class="brand">🗂 MTX EDITOR</div>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/config">Config</a>
    <a href="/config/mtx" class="active">MTX</a>
  </nav>
  <div class="spacer"></div>
  <span id="file-name" style="font-size:12px;color:var(--muted)">No file loaded</span>
</header>

<main>
  <!-- toolbar -->
  <div class="toolbar">
    <label class="btn btn-blue" style="cursor:pointer">
      📂 Open .MTX
      <input type="file" id="file-input" accept=".MTX,.mtx" style="display:none"/>
    </label>
    <button class="btn" onclick="openTemplate()">📋 From Template</button>
    <button class="btn btn-ok" id="btn-download" onclick="downloadMtx()" disabled>💾 Download .MTX</button>
    <button class="btn" id="btn-preview" onclick="togglePreview()" disabled>👁 Preview text</button>
  </div>

  <!-- info bar -->
  <div class="info-bar" id="info-bar" style="display:none">
    <div class="chip">size <span id="info-size">—</span></div>
    <div class="chip">plane <span id="info-plane">—</span></div>
    <div class="chip">V-In <span id="info-vin">—</span></div>
    <div class="chip">V-Out <span id="info-vout">—</span></div>
    <div class="chip">phys in <span id="info-hi">—</span></div>
    <div class="chip">phys out <span id="info-ho">—</span></div>
  </div>

  <!-- preview -->
  <div id="preview-box" class="preview" style="display:none;margin-bottom:14px"></div>

  <!-- empty state -->
  <div class="empty" id="empty-state">
    <h2>No .MTX file loaded</h2>
    <p>Open an existing Extron .MTX file or create one from a template.<br/>
    All editing happens locally — nothing is sent to the Matrix 12800 automatically.<br/>
    Download the modified file and upload it to the switcher when ready.</p>
  </div>

  <!-- editor (hidden until model loaded) -->
  <div id="editor" style="display:none">
    <!-- tabs -->
    <div class="tabs">
      <button class="tab active" onclick="showTab('port-map')">Port Map</button>
      <button class="tab" onclick="showTab('remap')">Remap</button>
      <button class="tab" onclick="showTab('add')">Add I/O</button>
      <button class="tab" onclick="showTab('delete')">Delete</button>
      <button class="tab" onclick="showTab('reorder')">Reorder</button>
      <button class="tab" onclick="showTab('merge')">Merge RGB</button>
    </div>

    <!-- PORT MAP tab -->
    <div class="tab-pane active" id="tab-port-map">
      <div class="map-wrap">
        <div class="map-section">
          <div class="map-title">Physical Inputs (0i001–0i128)</div>
          <canvas id="canvas-in" height="200"></canvas>
        </div>
        <div class="map-section">
          <div class="map-title">Physical Outputs (0o001–0o128)</div>
          <canvas id="canvas-out" height="200"></canvas>
        </div>
      </div>
      <div class="legend">
        <div class="legend-item"><div class="legend-swatch" style="background:#ef4444"></div>R (RGB ch1)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#22c55e"></div>G (RGB ch2)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#3b82f6"></div>B (RGB ch3)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#6b7280"></div>Y / Composite</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#a855f7"></div>C (S-Video)</div>
        <div class="legend-item"><div class="legend-swatch" style="background:#1b1f28;border:1px solid #262b36"></div>Unused</div>
      </div>
    </div>

    <!-- REMAP tab -->
    <div class="tab-pane" id="tab-remap">
      <div class="section-box">
        <h3>Remap a Virtual I/O</h3>
        <div class="row3">
          <div class="field">
            <label>Type</label>
            <select id="remap-kind" onchange="populateRemapIndex()">
              <option value="Input">Input</option>
              <option value="Output">Output</option>
            </select>
          </div>
          <div class="field">
            <label>Virtual Index</label>
            <select id="remap-virt"></select>
          </div>
          <div class="field">
            <label>New Signal Type</label>
            <select id="remap-signal" onchange="updateRemapSignal()">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01">RGB (3-wide)</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label>Physical Ports — comma-separated numbers (e.g. 1,2,3 for RGB) — leave blank to auto-append at tail</label>
          <input id="remap-phys" placeholder="e.g. 42,43,44  (or blank = append at highest+1)"/>
        </div>
        <div id="remap-current" class="preview" style="margin-bottom:10px">Select a virtual above</div>
        <button class="btn btn-blue" onclick="doRemap()">Apply Remap</button>
      </div>
    </div>

    <!-- ADD tab -->
    <div class="tab-pane" id="tab-add">
      <div class="row2">
        <div class="section-box">
          <h3>Add Inputs</h3>
          <div class="field"><label>Count</label>
            <input id="add-in-count" type="number" value="1" min="1" max="200"/></div>
          <div class="field"><label>Signal Type</label>
            <select id="add-in-signal">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01" selected>RGB (3-wide)</option>
            </select></div>
          <div class="field"><label>Starting Physical Port (blank = auto-tail)</label>
            <input id="add-in-start" placeholder="e.g. 70"/></div>
          <label class="check-row">
            <input type="checkbox" id="add-in-en" checked/> Include inputs in this batch
          </label>
        </div>
        <div class="section-box">
          <h3>Add Outputs</h3>
          <div class="field"><label>Count</label>
            <input id="add-out-count" type="number" value="1" min="1" max="200"/></div>
          <div class="field"><label>Signal Type</label>
            <select id="add-out-signal">
              <option value="04">Composite (1-wide)</option>
              <option value="05">S-Video (2-wide)</option>
              <option value="01" selected>RGB (3-wide)</option>
            </select></div>
          <div class="field"><label>Starting Physical Port (blank = auto-tail)</label>
            <input id="add-out-start" placeholder="e.g. 70"/></div>
          <label class="check-row">
            <input type="checkbox" id="add-out-en" checked/> Include outputs in this batch
          </label>
        </div>
      </div>
      <button class="btn btn-blue" onclick="doAdd()">Add I/O</button>
    </div>

    <!-- DELETE tab -->
    <div class="tab-pane" id="tab-delete">
      <div class="section-box">
        <h3>Delete Virtual I/Os</h3>
        <p style="font-size:12px;color:var(--muted);margin:0 0 10px">
          Click to select/deselect (multi-select supported). Red = selected for deletion.</p>
        <div class="list-pair">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Inputs</div>
            <div class="list-box" id="del-in-list"></div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Outputs</div>
            <div class="list-box" id="del-out-list"></div>
          </div>
        </div>
        <label class="check-row" style="margin-bottom:10px">
          <input type="checkbox" id="del-compact" checked/> Compact indices after delete (renumber 1..N)
        </label>
        <div style="display:flex;gap:8px">
          <button class="btn btn-bad" onclick="doDelete()">Delete Selected</button>
          <button class="btn" onclick="clearDelSelection()">Clear Selection</button>
        </div>
      </div>
    </div>

    <!-- REORDER tab -->
    <div class="tab-pane" id="tab-reorder">
      <div class="section-box">
        <h3>Reorder Virtuals — drag rows or use ↑↓ buttons
          <label class="check-row" style="display:inline-flex;margin-left:16px;font-weight:400;font-size:12px">
            <input type="checkbox" id="smart-groups" checked onchange="smartGroups=this.checked;populateReorderLists()"/>
            Smart grouping (link sequential Composite buddies)
          </label>
        </h3>
        <div class="row2">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Inputs</div>
            <div class="reorder-list" id="reorder-in-list"></div>
            <div class="reorder-btns">
              <button class="btn" onclick="moveSelected('in',-1)">↑ Up</button>
              <button class="btn" onclick="moveSelected('in',1)">↓ Down</button>
            </div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Virtual Outputs</div>
            <div class="reorder-list" id="reorder-out-list"></div>
            <div class="reorder-btns">
              <button class="btn" onclick="moveSelected('out',-1)">↑ Up</button>
              <button class="btn" onclick="moveSelected('out',1)">↓ Down</button>
            </div>
          </div>
        </div>
        <div style="margin-top:12px">
          <button class="btn btn-blue" onclick="doReorder()">Apply Reorder</button>
        </div>
      </div>
    </div>

    <!-- MERGE RGB tab -->
    <div class="tab-pane" id="tab-merge">
      <div class="section-box">
        <h3>Merge 3 Composite Virtuals → 1 RGB Virtual</h3>
        <p style="font-size:12px;color:var(--muted);margin:0 0 14px">
          Finds the virtual that owns each physical port, deletes all 3, and creates one new RGB virtual
          mapped to those 3 ports. Compacts indices afterward.</p>
        <div class="row2">
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Input physical ports (R, G, B)</div>
            <div class="row3">
              <div class="field"><label>R port</label><input id="merge-in-r" type="number" min="1" max="128" placeholder="e.g. 1"/></div>
              <div class="field"><label>G port</label><input id="merge-in-g" type="number" min="1" max="128" placeholder="e.g. 2"/></div>
              <div class="field"><label>B port</label><input id="merge-in-b" type="number" min="1" max="128" placeholder="e.g. 3"/></div>
            </div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--muted);margin-bottom:6px">Output physical ports (R, G, B)</div>
            <div class="row3">
              <div class="field"><label>R port</label><input id="merge-out-r" type="number" min="1" max="128" placeholder="e.g. 1"/></div>
              <div class="field"><label>G port</label><input id="merge-out-g" type="number" min="1" max="128" placeholder="e.g. 2"/></div>
              <div class="field"><label>B port</label><input id="merge-out-b" type="number" min="1" max="128" placeholder="e.g. 3"/></div>
            </div>
          </div>
        </div>
        <label class="check-row" style="margin-bottom:12px">
          <input type="checkbox" id="merge-no-out"/> No output side (input only merge)
        </label>
        <button class="btn btn-warn" onclick="doMerge()">⚠ Merge → RGB</button>
      </div>
    </div>
  </div>
</main>

<!-- Template modal -->
<div class="overlay" id="tpl-modal">
  <div class="modal">
    <h3>New from Template</h3>
    <div class="row2">
      <div class="field"><label>Inputs</label>
        <input id="tpl-in" type="number" value="69" min="1" max="256"/></div>
      <div class="field"><label>Outputs</label>
        <input id="tpl-out" type="number" value="69" min="1" max="256"/></div>
    </div>
    <div class="row2">
      <div class="field"><label>Plane type</label>
        <select id="tpl-plane">
          <option value="LC">LC (composite / S-Video)</option>
          <option value="RGB">RGB</option>
        </select></div>
      <div class="field"><label>Default signal type</label>
        <select id="tpl-signal">
          <option value="1">Composite (1-wide)</option>
          <option value="2">S-Video (2-wide)</option>
          <option value="3" selected>RGB (3-wide)</option>
        </select></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeTpl()">Cancel</button>
      <button class="btn btn-blue" onclick="createTemplate()">Create</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let model = null;
let filename = 'output.MTX';
let delSelIn = new Set();
let delSelOut = new Set();
let reorderIn = [];   // list of {i, code, label, phys}
let reorderOut = [];
let reorderSelIn = null;
let reorderSelOut = null;

// ── helpers ──────────────────────────────────────────────────────────────────
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function sigColor(code, idx){
  if(code==='01') return ['#ef4444','#22c55e','#3b82f6'][idx]||'#3b82f6';
  if(code==='05') return ['#6b7280','#a855f7'][idx]||'#a855f7';
  return '#6b7280';
}
function sigLabel(code){ return {01:'RGB',05:'S-Vid',04:'Comp'}[code]||code; }

let _toastT;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  clearTimeout(_toastT); _toastT=setTimeout(()=>el.classList.remove('show'),2400);
}

// ── model load / update ──────────────────────────────────────────────────────
function setModel(m, fname){
  model = m;
  if(fname) filename = fname;
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('editor').style.display = 'block';
  document.getElementById('info-bar').style.display = 'flex';
  document.getElementById('btn-download').disabled = false;
  document.getElementById('btn-preview').disabled = false;
  document.getElementById('file-name').textContent = filename;
  updateInfoBar();
  drawMaps();
  populateRemapIndex();
  populateDeleteLists();
  populateReorderLists();
}

function updateInfoBar(){
  if(!model) return;
  document.getElementById('info-size').textContent =
    (model.size||[0,0]).join('×');
  document.getElementById('info-plane').textContent = model.plane_type||'—';
  document.getElementById('info-vin').textContent   = (model.vin||[]).length;
  document.getElementById('info-vout').textContent  = (model.vout||[]).length;
  document.getElementById('info-hi').textContent    = model.highest_in||0;
  document.getElementById('info-ho').textContent    = model.highest_out||0;
}

// ── file open ────────────────────────────────────────────────────────────────
document.getElementById('file-input').addEventListener('change', async e => {
  const file = e.target.files[0]; if(!file) return;
  const text = await file.text();
  const r = await fetch('/api/mtx/parse', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({text})});
  const j = await r.json();
  if(!j.ok){ toast('Parse error: '+j.error); return; }
  setModel(j.model, file.name);
  toast('Loaded: '+file.name);
});

// ── download ─────────────────────────────────────────────────────────────────
async function downloadMtx(){
  if(!model) return;
  const r = await fetch('/api/mtx/serialize', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model})});
  const text = await r.text();
  const blob = new Blob([text], {type:'text/plain'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename.replace(/\.mtx$/i,'')+'_modified.MTX';
  a.click();
  toast('Downloaded!');
}

// ── preview ──────────────────────────────────────────────────────────────────
let previewVisible = false;
async function togglePreview(){
  const box = document.getElementById('preview-box');
  previewVisible = !previewVisible;
  if(previewVisible && model){
    const r = await fetch('/api/mtx/serialize', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({model})});
    box.textContent = await r.text();
  }
  box.style.display = previewVisible ? 'block' : 'none';
}

// ── template modal ───────────────────────────────────────────────────────────
function openTemplate(){ document.getElementById('tpl-modal').classList.add('open'); }
function closeTpl()    { document.getElementById('tpl-modal').classList.remove('open'); }
document.getElementById('tpl-modal').addEventListener('click', e => {
  if(e.target===document.getElementById('tpl-modal')) closeTpl();
});
async function createTemplate(){
  const body = {
    size_in:  parseInt(document.getElementById('tpl-in').value)||8,
    size_out: parseInt(document.getElementById('tpl-out').value)||8,
    plane:    document.getElementById('tpl-plane').value,
    width:    parseInt(document.getElementById('tpl-signal').value)||1,
  };
  const r = await fetch('/api/mtx/template', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  closeTpl();
  setModel(j.model, 'template.MTX');
  toast('Template created');
}

// ── tabs ─────────────────────────────────────────────────────────────────────
function showTab(id){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+id).classList.add('active');
  event.target.classList.add('active');
  if(id==='port-map') drawMaps();
}

// ── port map canvas ──────────────────────────────────────────────────────────
function buildUsage(items, kind){
  const usage = {};
  for(const rec of items){
    for(let ci=0; ci<rec.phys.length; ci++){
      usage[rec.phys[ci]] = {code: rec.code, ci, vi: rec.i, label: rec.label};
    }
  }
  return usage;
}

function drawMap(canvasId, usage){
  const canvas = document.getElementById(canvasId);
  const W = canvas.offsetWidth || 640;
  canvas.width  = W * devicePixelRatio;
  canvas.height = Math.round(W * 0.35) * devicePixelRatio;
  canvas.style.height = Math.round(W * 0.35) + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(devicePixelRatio, devicePixelRatio);
  const cols=16, rows=8, total=128;
  const cw = W/cols, ch = (W*0.35)/rows;
  ctx.fillStyle = '#0c0e12';
  ctx.fillRect(0,0,W,W);
  for(let n=1; n<=total; n++){
    const r=(n-1)>>4, c=(n-1)&15;
    const x=c*cw+1, y=r*ch+1, w=cw-2, h=ch-2;
    const u = usage[n];
    ctx.fillStyle = u ? sigColor(u.code, u.ci) : '#1b1f28';
    ctx.fillRect(x,y,w,h);
    ctx.fillStyle = u ? 'rgba(0,0,0,.5)' : '#262b36';
    ctx.font = `${Math.min(10, cw*0.42)}px monospace`;
    ctx.textAlign='center'; ctx.textBaseline='middle';
    ctx.fillText(n, x+w/2, y+h/2);
  }
}

function drawMaps(){
  if(!model) return;
  drawMap('canvas-in',  buildUsage(model.vin||[],  'Input'));
  drawMap('canvas-out', buildUsage(model.vout||[], 'Output'));
}
window.addEventListener('resize', ()=>{ if(model) drawMaps(); });

// ── remap tab ────────────────────────────────────────────────────────────────
function populateRemapIndex(){
  if(!model) return;
  const kind = document.getElementById('remap-kind').value;
  const items = kind==='Input' ? model.vin : model.vout;
  const sel = document.getElementById('remap-virt');
  sel.innerHTML = items.map(r=>
    `<option value="${r.i}">${r.i} — ${kind} ${String(r.label).padStart(3,'0')} (${sigLabel(r.code)}) phys:[${r.phys.join(',')}]</option>`
  ).join('');
  showCurrentRemap();
}

function showCurrentRemap(){
  if(!model) return;
  const kind = document.getElementById('remap-kind').value;
  const vi = parseInt(document.getElementById('remap-virt').value);
  const items = kind==='Input' ? model.vin : model.vout;
  const rec = items.find(r=>r.i===vi);
  const box = document.getElementById('remap-current');
  if(!rec){ box.textContent='(not found)'; return; }
  const pfx = kind==='Input'?'0i':'0o';
  box.textContent = `Current: ${kind} ${String(rec.label).padStart(3,'0')} (V-${rec.i})\n`+
    `Signal: ${sigLabel(rec.code)}  code=${rec.code}\n`+
    `Physical: ${rec.phys.map(p=>pfx+String(p).padStart(3,'0')).join(', ')||'(none)'}`;
  document.getElementById('remap-signal').value = rec.code;
}

document.getElementById('remap-virt').addEventListener('change', showCurrentRemap);
document.getElementById('remap-kind').addEventListener('change', populateRemapIndex);

function updateRemapSignal(){}

async function doRemap(){
  if(!model) return;
  const kind   = document.getElementById('remap-kind').value;
  const vi     = parseInt(document.getElementById('remap-virt').value);
  const code   = document.getElementById('remap-signal').value;
  const w      = {01:3, '01':3, '05':2, 05:2}[code] || 1;
  const physRaw= document.getElementById('remap-phys').value.trim();
  let phys;
  if(physRaw){
    phys = physRaw.split(/[\s,]+/).map(Number).filter(Boolean);
    if(phys.length !== w){ toast(`Need exactly ${w} port(s) for ${sigLabel(code)}`); return; }
  } else {
    // auto-tail: take highest used port and increment
    const hi = kind==='Input' ? (model.highest_in||0) : (model.highest_out||0);
    phys = Array.from({length:w}, (_,i)=>hi+i+1);
  }
  const r = await fetch('/api/mtx/remap', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, kind, virt_i: vi, code, phys})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast(`Remapped ${kind} ${vi} → ${sigLabel(code)}`);
  populateRemapIndex();
}

// ── add tab ──────────────────────────────────────────────────────────────────
async function doAdd(){
  if(!model) return;
  const addIn  = document.getElementById('add-in-en').checked;
  const addOut = document.getElementById('add-out-en').checked;
  if(!addIn && !addOut){ toast('Nothing to add — check at least one side'); return; }
  const block = {
    add_in:    addIn,
    count_in:  parseInt(document.getElementById('add-in-count').value)||1,
    win:       parseInt(document.getElementById('add-in-signal').value==='01'?3:
                        document.getElementById('add-in-signal').value==='05'?2:1),
    add_out:   addOut,
    count_out: parseInt(document.getElementById('add-out-count').value)||1,
    wout:      parseInt(document.getElementById('add-out-signal').value==='01'?3:
                        document.getElementById('add-out-signal').value==='05'?2:1),
  };
  // signal select returns code string; convert to width
  const inCode  = document.getElementById('add-in-signal').value;
  const outCode = document.getElementById('add-out-signal').value;
  block.win  = {01:3,'01':3,'05':2,05:2,'04':1,04:1}[inCode]  ||1;
  block.wout = {01:3,'01':3,'05':2,05:2,'04':1,04:1}[outCode] ||1;

  const inStart  = parseInt(document.getElementById('add-in-start').value);
  const outStart = parseInt(document.getElementById('add-out-start').value);
  if(!isNaN(inStart) && inStart>0 && addIn){
    block.phys_in = Array.from({length:block.win},(_,i)=>inStart+i);
    block.count_in = 1; // manual port = single at that position
  }
  if(!isNaN(outStart) && outStart>0 && addOut){
    block.phys_out = Array.from({length:block.wout},(_,i)=>outStart+i);
    block.count_out = 1;
  }
  const r = await fetch('/api/mtx/add', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, blocks:[block]})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Added I/O');
}

// ── delete tab ───────────────────────────────────────────────────────────────
function populateDeleteLists(){
  if(!model) return;
  delSelIn.clear(); delSelOut.clear();
  renderDeleteList('del-in-list',  model.vin||[],  'Input',  delSelIn);
  renderDeleteList('del-out-list', model.vout||[], 'Output', delSelOut);
}
function renderDeleteList(id, items, kind, selSet){
  const box = document.getElementById(id);
  box.innerHTML = items.map(r=>{
    const col = sigColor(r.code,0);
    return `<div class="list-item" data-vi="${r.i}" onclick="toggleDelSel(this,'${kind}')">
      <span class="sig-dot" style="background:${col}"></span>
      V${r.i} — ${kind} ${String(r.label).padStart(3,'0')} (${sigLabel(r.code)}) [${r.phys.join(',')}]
    </div>`;
  }).join('');
}
function toggleDelSel(el, kind){
  const vi = parseInt(el.dataset.vi);
  const set = kind==='Input' ? delSelIn : delSelOut;
  if(set.has(vi)){ set.delete(vi); el.classList.remove('selected'); el.style.color=''; }
  else           { set.add(vi);    el.classList.add('selected');    el.style.color='var(--bad)'; }
}
function clearDelSelection(){
  delSelIn.clear(); delSelOut.clear();
  document.querySelectorAll('#del-in-list .list-item, #del-out-list .list-item').forEach(el=>{
    el.classList.remove('selected'); el.style.color='';
  });
}
async function doDelete(){
  if(!model) return;
  if(!delSelIn.size && !delSelOut.size){ toast('Select items to delete first'); return; }
  const compact = document.getElementById('del-compact').checked;
  if(!confirm(`Delete ${delSelIn.size} input(s) + ${delSelOut.size} output(s)?`)) return;
  const r = await fetch('/api/mtx/delete', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, del_vin:[...delSelIn], del_vout:[...delSelOut], compact})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast(`Deleted — ${j.model.vin.length} inputs, ${j.model.vout.length} outputs remain`);
}

// ── reorder tab ──────────────────────────────────────────────────────────────
let smartGroups = true;  // buddy-aware grouping

// Detect groups of consecutive Composite virtuals with sequential physical ports
// (e.g. [phys:1],[phys:2],[phys:3] → one RGB group; [phys:4],[phys:5] → S-Vid group)
function detectGroups(items){
  const groups = [];
  let i = 0;
  while(i < items.length){
    const r = items[i];
    if(smartGroups && r.code === '04' && r.phys.length === 1){
      const p0 = r.phys[0];
      const next1 = items[i+1];
      const next2 = items[i+2];
      // RGB group: 3 consecutive Composite with sequential ports
      if(next2 && next1.code==='04' && next2.code==='04' &&
         next1.phys.length===1 && next2.phys.length===1 &&
         next1.phys[0]===p0+1 && next2.phys[0]===p0+2){
        groups.push({type:'rgb3', items:[items[i],items[i+1],items[i+2]], startIdx:i});
        i+=3; continue;
      }
      // S-Video group: 2 consecutive Composite with sequential ports
      if(next1 && next1.code==='04' && next1.phys.length===1 && next1.phys[0]===p0+1){
        groups.push({type:'svid2', items:[items[i],items[i+1]], startIdx:i});
        i+=2; continue;
      }
    }
    groups.push({type:'single', items:[r], startIdx:i});
    i++;
  }
  return groups;
}

function populateReorderLists(){
  if(!model) return;
  reorderIn  = (model.vin||[]).map(r=>({...r}));
  reorderOut = (model.vout||[]).map(r=>({...r}));
  reorderSelIn = null; reorderSelOut = null;
  renderReorderList('reorder-in-list',  reorderIn,  'in');
  renderReorderList('reorder-out-list', reorderOut, 'out');
}

function renderReorderList(id, items, side){
  const box = document.getElementById(id);
  const groups = detectGroups(items);
  box.innerHTML = groups.map((g, gi)=>{
    const isGroup = g.type !== 'single';
    const typeLabel = g.type==='rgb3' ? '🔴🟢🔵 RGB group' : g.type==='svid2' ? '⬜🟣 S-Vid group' : '';
    const col0 = sigColor(g.items[0].code, 0);
    const physAll = g.items.flatMap(r=>r.phys).join(',');
    const label = isGroup
      ? `${typeLabel} — V${g.items.map(r=>r.i).join('+')} [${physAll}]`
      : `V${g.items[0].i} — ${sigLabel(g.items[0].code)} [${g.items[0].phys.join(',')}]`;
    const border = isGroup ? 'border:1px solid rgba(224,160,64,.3);background:rgba(224,160,64,.05)' : '';
    return `<div class="reorder-item" data-gi="${gi}" data-side="${side}" style="${border}"
      onclick="selectReorderGroup(this,'${side}')" draggable="true"
      ondragstart="rDragStart(event,'${side}')" ondragover="rDragOver(event)"
      ondrop="rDrop(event,'${side}',${gi})" ondragleave="rDragLeave(event)">
      ⠿ <span class="sig-dot" style="background:${col0}"></span>
      ${label}
    </div>`;
  }).join('');
}

// Store groups for drag/move operations
function getGroups(side){ return detectGroups(side==='in' ? reorderIn : reorderOut); }

function selectReorderGroup(el, side){
  document.querySelectorAll(`.reorder-item[data-side="${side}"]`).forEach(e=>e.style.background='');
  el.style.background='rgba(59,130,246,.2)';
  if(side==='in') reorderSelIn=parseInt(el.dataset.gi);
  else reorderSelOut=parseInt(el.dataset.gi);
}

function moveSelected(side, delta){
  const list = side==='in' ? reorderIn : reorderOut;
  const groups = getGroups(side);
  const selGi = side==='in' ? reorderSelIn : reorderSelOut;
  if(selGi===null||selGi===undefined) return;
  const newGi = selGi + delta;
  if(newGi<0 || newGi>=groups.length) return;

  // Swap the group's items with the adjacent group's items in the flat list
  const gA = groups[selGi];
  const gB = groups[newGi];
  const flatA = gA.items; const flatB = gB.items;
  const startA = gA.startIdx; const startB = gB.startIdx;

  // Rebuild list by splicing groups
  const newList = [...list];
  if(delta > 0){
    // A is before B: remove A's items, insert after B
    newList.splice(startA, flatA.length);
    const insertAt = startA + flatB.length;
    newList.splice(insertAt, 0, ...flatA);
  } else {
    // A is after B: remove A, insert before B
    newList.splice(startA, flatA.length);
    newList.splice(startB, 0, ...flatA);
  }
  if(side==='in'){ reorderIn=newList; reorderSelIn=newGi; }
  else           { reorderOut=newList; reorderSelOut=newGi; }
  renderReorderList(side==='in'?'reorder-in-list':'reorder-out-list', newList, side);
  // restore selection highlight
  const el=document.querySelector(`.reorder-item[data-side="${side}"][data-gi="${newGi}"]`);
  if(el) el.style.background='rgba(59,130,246,.2)';
}

// drag-and-drop (group-aware)
let rDragGi=null, rDragSide=null;
function rDragStart(e, side){
  rDragGi=parseInt(e.currentTarget.dataset.gi); rDragSide=side;
  e.dataTransfer.effectAllowed='move';
  setTimeout(()=>e.currentTarget.classList.add('dragging'),0);
}
function rDragOver(e){ e.preventDefault(); e.currentTarget.classList.add('drag-over'); }
function rDragLeave(e){ e.currentTarget.classList.remove('drag-over'); }
function rDrop(e, side, dstGi){
  e.preventDefault(); e.currentTarget.classList.remove('drag-over');
  if(rDragSide!==side || rDragGi===dstGi) return;
  const list = side==='in' ? reorderIn : reorderOut;
  const groups = getGroups(side);
  const gSrc = groups[rDragGi]; const gDst = groups[dstGi];
  const newList = [...list];
  const srcItems = gSrc.items;
  // Remove source group, insert before/after destination
  if(rDragGi < dstGi){
    newList.splice(gSrc.startIdx, srcItems.length);
    const insertAt = gDst.startIdx - srcItems.length + gDst.items.length;
    newList.splice(insertAt, 0, ...srcItems);
  } else {
    newList.splice(gSrc.startIdx, srcItems.length);
    newList.splice(gDst.startIdx, 0, ...srcItems);
  }
  if(side==='in') reorderIn=newList; else reorderOut=newList;
  renderReorderList(side==='in'?'reorder-in-list':'reorder-out-list', newList, side);
  rDragGi=null;
}

async function doReorder(){
  if(!model) return;
  const r = await fetch('/api/mtx/reorder', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model,
      vin_order:  reorderIn.map(r=>r.i),
      vout_order: reorderOut.map(r=>r.i),
    })});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Reorder applied');
}

// ── merge RGB tab ────────────────────────────────────────────────────────────
async function doMerge(){
  if(!model) return;
  const noOut = document.getElementById('merge-no-out').checked;
  const inPhys  = ['r','g','b'].map(c=>parseInt(document.getElementById('merge-in-'+c).value)||0).filter(Boolean);
  const outPhys = noOut ? [] : ['r','g','b'].map(c=>parseInt(document.getElementById('merge-out-'+c).value)||0).filter(Boolean);
  if(inPhys.length!==3 && inPhys.length>0){ toast('Need 3 input ports'); return; }
  if(!noOut && outPhys.length!==3){ toast('Need 3 output ports (or check "no output side")'); return; }
  if(!confirm('This will delete the 3 Composite virtuals and create one RGB virtual. Continue?')) return;
  const r = await fetch('/api/mtx/merge-rgb', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model, in_phys: inPhys, out_phys: outPhys})});
  const j = await r.json();
  if(!j.ok){ toast('Error: '+j.error); return; }
  setModel(j.model);
  toast('Merged to RGB');
}

// ── init ─────────────────────────────────────────────────────────────────────
// Canvas resize observer
new ResizeObserver(()=>{ if(model) drawMaps(); }).observe(document.getElementById('editor'));
</script>
</body></html>"""



