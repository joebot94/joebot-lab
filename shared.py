import collections
import time

_logs = collections.deque(maxlen=300)

def log(msg):
    _logs.appendleft({"t": time.strftime("%H:%M:%S"), "msg": msg})


# --------------------------------------------------------------------------- #
# Shared stylesheet, served at /static/lab.css
#
# Design tokens + common chrome for all control pages. Pages set their own
# accent by overriding --accent AFTER the <link>:
#     <link rel="stylesheet" href="/static/lab.css">
#     <style>:root{--accent:#7c6af5}  /* page-specific styles below */</style>
# Adopted by: /control/autoswitch. Other pages migrate as they're touched.
# --------------------------------------------------------------------------- #

LAB_CSS = """
:root{
  --bg:#0c0e12;--panel:#15181f;--panel2:#1b1f28;--line:#262b36;
  --ink:#e8ebf0;--muted:#8b93a3;--accent:#e0a040;
  --ok:#34d399;--warn:#f5b942;--bad:#ff5470;--gray:#454b58;
  --blue:#60a5fa;--purple:#a78bfa;--amber:#fcd34d;--green:#34d399;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:var(--mono);font-size:13px;line-height:1.5}
header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:14px 20px;border-bottom:1px solid var(--line);
  background:linear-gradient(180deg,rgba(224,160,64,.05),transparent)}
.brand{font-size:18px;font-weight:700;letter-spacing:.1em}
.brand b{color:var(--accent)}
nav a{color:var(--muted);text-decoration:none;font-size:12px;padding:4px 8px;
  border-radius:5px;border:1px solid transparent}
nav a:hover{color:var(--ink);border-color:var(--line)}
.spacer{flex:1}
button,select,input{font-family:var(--mono);font-size:12px}
.btn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
  border-radius:6px;padding:6px 13px;cursor:pointer}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn-sm{padding:4px 9px;font-size:11px}
.btn-ok{background:rgba(52,211,153,.1);color:var(--ok);border-color:rgba(52,211,153,.35)}
.btn-ok:hover{background:rgba(52,211,153,.2);border-color:var(--ok)}
.btn-blue{background:rgba(96,165,250,.1);color:var(--blue);border-color:rgba(96,165,250,.35)}
.btn-blue:hover{background:rgba(96,165,250,.2);border-color:var(--blue)}
.btn-purple{background:rgba(167,139,250,.1);color:var(--purple);border-color:rgba(167,139,250,.35)}
.btn-purple:hover{background:rgba(167,139,250,.2);border-color:var(--purple)}
.btn-bad{background:rgba(255,84,112,.08);color:var(--bad);border-color:rgba(255,84,112,.3)}
.btn-bad:hover{background:rgba(255,84,112,.15)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.ph{display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--line)}
.ph-lbl{font-size:9px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
.pb{padding:10px 12px}
.toast{position:fixed;bottom:20px;right:20px;background:var(--panel2);
  border:1px solid var(--line);border-radius:7px;padding:8px 14px;
  font-size:12px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:200}
.toast.show{opacity:1}
"""
