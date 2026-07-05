# Joebot Lab — UI Polish Handoff (for Claude Opus)

## Mission
Visual polish and consistency ONLY. No behavior changes, no new features, no
endpoint changes, no protocol/SIS changes. If a change would alter what gets
sent to a device or what an API returns, stop — that work belongs in a
different session.

## The codebase, 60 seconds
- FastAPI app on a Synology NAS. Every page is an HTML string embedded in a
  Python module (`FRONTEND_HTML` in app.py, `MTPX_HTML` in routes_mtpx.py,
  `_AS_HTML` in routes_autoswitch.py, etc.). There is no template engine and
  no build step — that's intentional, keep it that way.
- Shared stylesheet exists: **`/static/lab.css`** (source: `LAB_CSS` string in
  `shared.py`). It holds design tokens (`--bg --panel --panel2 --line --ink
  --muted --accent --ok --warn --bad`), header chrome, `.btn*` variants,
  `.panel/.ph/.pb`, `.toast`.
- Adoption pattern (already done in routes_autoswitch.py — copy that page's
  approach):
  1. `<link rel="stylesheet" href="/static/lab.css"/>` in `<head>`
  2. Override the page accent AFTER the link: `:root{--accent:#7c6af5}`
  3. Delete only the CSS blocks that lab.css now covers; keep page-specific CSS

## ⚠ The one bug class that will bite you
Embedded JS inside a **non-raw** Python `"""string"""`: Python converts `\'`
to a bare quote and silently kills the entire script (this shipped broken once
— see commit c4d566d). Rules:
- Prefer `r"""raw strings"""` for any HTML block containing JavaScript.
- If the string is not raw, JS escapes need doubling (`\\'`).
- `WELCOME_HTML` in app.py is a SINGLE-QUOTED one-line string where `\'` and
  `\n` are intentional Python escapes. Edits there must use `\n` not real
  newlines. Do not convert it to a triple-quoted string.

**Mandatory verification after every page edit** (no exceptions):
```bash
python3 -c "import ast; ast.parse(open('FILE.py').read())"
python3 - <<'EOF'   # extract embedded JS and parse it
import ast, re
src = open('FILE.py').read()
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Assign) and getattr(node.targets[0],'id','').endswith('HTML'):
        m = re.search(r'<script>(.*)</script>', ast.literal_eval(node.value), re.S)
        if m: open('/tmp/x.js','w').write(m.group(1))
EOF
node --check /tmp/x.js
```

## Page inventory (accent = wayfinding, KEEP these colors)
| Page | File | Accent | lab.css? |
|---|---|---|---|
| Dashboard `/` | app.py `FRONTEND_HTML` | amber #e0a040 | no (self-contained, fine) |
| Setup `/welcome` | app.py `WELCOME_HTML` | green/purple | no — DO NOT restructure |
| Auto-switch `/control/autoswitch` | routes_autoswitch.py | amber | ✅ adopted |
| SMX `/control/smx` | routes_smx.py | purple #a78bfa | migrate |
| DMS `/control/dms` | routes_dms.py | amber | migrate |
| Matrix 12800 `/control/matrix12800` | routes_matrix12800.py | green | migrate |
| MTX editor `/config/mtx` | routes_mtx_config.py | blue | migrate |
| IPCP hub `/control/ipcp505` | routes_ipcp505.py | amber #f5b942 | migrate |
| VTG/USP/VSC pages | routes_ipcp505.py / routes_vsc.py | amber/teal/varied | migrate |
| MTPX `/control/mtpx` | routes_mtpx.py | amber #d97706 | ❌ skip — different design language (slate bg, Segoe UI), leave as-is |
| DSC 401 `/control/dsc401` | routes_dsc401.py | blue | migrate |
| IR remotes `/control/ir/*` | routes_ir.py | varied | judgment call |

## Priorities (in order)
1. Migrate control pages to lab.css (one page per commit, verify each).
   Expect small visual diffs — aligning to shared chrome is the point — but
   layout and functionality must not change.
2. Consistency sweep: same border-radius scale, same button hierarchy, same
   panel header style, same toast, same mono font stack everywhere.
3. Mobile: every control page usable at 390px wide. Dashboard already has an
   icon-only header pattern (`.hdr-actions`, `.tl` label spans) — reuse it.
4. Polish only after 1–3: hover states, focus rings, transitions.

## Hard constraints
- Do NOT touch: sis.py, smx_control.py, any `_send`/socket code, any
  `/api/*` route signatures or response shapes, poll intervals, SIS strings.
- Do NOT rename element IDs that JS references (grep the page's own `<script>`
  before renaming anything).
- Do NOT add frameworks, CDNs, fonts, or external requests. Everything is
  LAN-local and must work with no internet.
- Keep per-device accent colors. Keep the terminal/mono aesthetic. Dark only.
- Emoji icons are the icon system. Don't introduce SVG icon sets.

## Build / deploy / verify loop
- Repo: `/Users/joe/Downloads/joebot2` (git, GitHub: joebot94/joebot-lab).
- Bump `VERSION` in app.py on every deploy.
- Deploy = copy changed files to NAS and rebuild (ask Joe, or:
  `sshpass … ssh joe@10.0.0.2`, files at `/volume1/docker/joebot2/`,
  `sudo docker compose up -d --build` — credentials from Joe).
- Verify live: `curl -s http://10.0.0.2:8080/api/status | jq .meta.version`,
  page loads, extracted served JS passes `node --check`, and
  `curl http://10.0.0.2:8080/static/lab.css` returns CSS.
- Commit per page with a one-line summary; push to main.

## Definition of done
Every control page (except MTPX and WELCOME_HTML) links lab.css, keeps its
accent, renders correctly on desktop + 390px mobile, all JS parses, no API or
behavior diffs, dashboard unaffected, versions bumped, commits pushed.
