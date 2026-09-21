# hypruse codebase map for an in-process voice daemon

Lane: hypruse-map. First pass 2026-09-20, independent verification 2026-09-20, second full pass 2026-09-21 (this revision). Source tree: `/home/ilyask/projects/hypruse` at commit `9a84bdb` (version 0.11.0, 150 commits, 7,733 lines in `src/hypruse` plus ARCHITECTURE.md, 652 unit tests green in 9.3 s on the second pass, 13 e2e deselected).

Method: every claim below is from reading the source (cited `file:line`, paths relative to the repo root) or from a local read-only measurement. Nothing in this lane dispatched, clicked, typed, focused, captured, or created a virtual input device. No web was used. No Jev call was made and no Jev latency is claimed anywhere.

Second pass (2026-09-21): I re-read every module in `src/hypruse` end to end without relying on the first pass, re-ran the measurements with fresh scripts at a lower load (`/proc/loadavg` about 11, versus 22 to 28 on the first pass and 11 to 18 on the verification pass), corrected two fork counts, and closed five items the first pass left open by reading the Hyprland 0.56.2 source tree that another lane had unpacked at `scratchpad/Hyprland-0.56.2` (a primary source, local). Where numbers differ between passes, both are shown and the lower-load number is the better estimate. Bench scripts next to this file: `bench_import.py`, `bench_live2.py` (second pass), `bench_inproc.py` (first pass), `verify-hypruse-map/` (verification pass), `sigterm_glib.py`.

Machine facts confirmed locally: Hyprland 0.56.2, config provider `hyprlang` (`hyprctl -j status` returned `{"configProvider":"hyprlang","backend":"drm"}`), repo venv is Python 3.13.13 with `mcp` 1.28.1, system Python is 3.14.7, one monitor eDP-1 1920x1080 scale 1.

---

## 0. The ten things that matter most

1. All 15 tools are plain synchronous module-level functions in `src/hypruse/server.py`. Importing them does not import `mcp`. A daemon can call `server.hypr(...)` directly. Second-pass cold-process wall clock (loadavg about 11, n=7): bare interpreter 32 ms, `import hypruse.server` 97 ms median on venv 3.13 and 93 ms on system 3.14.7 with `PYTHONPATH=src` (so about 65 ms of actual import work), versus 704 ms for `server.app()` (FastMCP build) and 933 ms for `import mcp.types`. First pass under heavier load saw 0.3 to 0.6 s and 2.6 to 3.8 s. The ratio (mcp-free is 7x to 10x cheaper) held on all three passes.
2. Every Hyprland query and dispatch forks the `hyprctl` binary (`hyprctl.py:35-47`). Second pass: 20 to 30 ms median per call (min 11 ms); `hyprctl.snapshot()` 19.7 ms median. The same six-query snapshot over a direct UNIX socket to `.socket.sock` measured 0.3 ms median (min 0.2 ms, n=30), and a single direct query 0.1 ms. It parses with the existing pure `snapshot_from` and equals `hyprctl.snapshot()` once volatile fields (cursor) are masked. Replacing `hyprctl._run` with a socket client is the single largest latency win available in the fork, roughly 60x for the snapshot and 200x for a single query at today's load.
3. Lock detection is a `/proc` comm scan for four process names (`trust.py:352-391`), costs 13 ms here (369 processes; 24 to 28 ms on the first pass), and fails open. `hyprctl -j locked` exists on this 0.56.2 session and answers `{"locked": false}` in about 0.1 ms over the socket. The code comment saying Hyprland exposes no lock state (`trust.py:336-339`, `ARCHITECTURE.md:172-176`) is stale. New on the second pass, from the Hyprland 0.56.2 source: the compositor's locked flag stays TRUE when the lock client dies without `unlock_and_destroy` (section 6), so the `/proc` scan is wrong in exactly the crash case, and `hyprctl locked` is right.
4. The lock gate only covers `pointer` (non-move), `keyboard`, and `click_ui`. `hypr`, `launch`, `use_bind`, and `sequence` steps of kind `hypr` have no lock check at all, and Hyprland itself does not refuse `hyprctl dispatch` while locked (`HyprCtl.cpp:1126-1158` has no lock check; only the keybind path does, `KeybindManager.cpp:649`). A voice daemon must add its own global "locked means deaf and inert" gate.
5. `HYPRUSE_READONLY` and `HYPRUSE_CLIPBOARD` are NOT enforced inside the tool functions. They are enforced only at MCP registration (`server.py:1672-1676`) and in the shell wrapper (`verbs.py:351-363`). A library caller bypasses both unless it re-implements the check.
6. `HYPRUSE_STRICT` (seat-moved guard) is structurally hostile to voice use: the human keeps using mouse and keyboard while speaking, so the seat always moves between commands. Leave it off, or re-baseline with `server.desktop()` immediately before each action (which makes it vacuous).
7. The a11y path spawns one `busctl` process per D-Bus call (`a11y.py:84-100`), at least two per visited node and up to about nine per reported control. A bare busctl spawn measured 8.5 ms median on the second pass (39 ms under first-pass load); `a11y.connect()` (one real call) took 28 ms. The AT-SPI registry on this machine is still broken on 2026-09-21 (same error as 2026-09-13 and 2026-09-20), so no live tree walk could be timed on any pass. Expect seconds, not milliseconds, until `a11y.py` is rewritten over a persistent D-Bus connection. The `Atspi-2.0` GI typelib is installed (`/usr/lib/girepository-1.0/Atspi-2.0.typelib`), so that rewrite needs no new dependency on the system interpreter.
8. Tools have hard latency floors from `time.sleep`: 50 ms focus settle in `keyboard(window=...)` and `click_ui`, 20 + 20 ms per click, 200 ms between every `sequence` step plus a 200 ms tail drain, and `close_window` blocks until the compositor confirms (up to 1 s).
9. Process-global state is everywhere (env flags read per call, `_last_marks`, `_vp`, `_provider`, `_seat`, `_owned`, the beacon file, one-way `use_plain_blocks()`). Only raw input delivery is serialized (`input._seat_lock`). Run all acting calls through a single worker queue.
10. Runtime files live under `$XDG_RUNTIME_DIR/hypruse/` with fixed names. Four hypruse MCP server processes were running on this machine during the survey. A fork that keeps the directory name will fight them over `state.json`, `state.tmp`, `cli.lock`, and the 20-file screenshot prune.

---

## 1. Every tool function

All live in `src/hypruse/server.py`. All are `def`, not `async def` (verified with `inspect.iscoroutinefunction` on Python 3.14; there is no `async`, `await`, `asyncio`, or `anyio` anywhere in `src/hypruse`). All are wrapped by `@journal.journaled(kind)` (`journal.py:399-451`), which is a passthrough when `HYPRUSE_JOURNAL` is unset (`journal.py:421-422`). Signatures below are copied from `inspect.signature` output.

Return conventions: a tool returns a `dict`, a `list[dict]`, a plain `str`, or a `list` of content blocks. After `server.use_plain_blocks()` the blocks are `server.Block` objects with `.type`, `.text`, `.data`, `.mimeType` (`server.py:137-172`). "Nothing happened" outcomes are usually prose strings, not exceptions. The canonical prose-to-status parser is `verbs.exit_for(tool, result)` (`verbs.py:462-488`): reuse it rather than re-deriving the string prefixes.

### Observation tools (registered in every mode, `server.py:1657`)

| Tool | def line | Signature | Returns | Cost and notes |
|---|---|---|---|---|
| `desktop` | 190 | `() -> dict` | snapshot dict (section 3) | one `hyprctl --batch` fork; re-baselines strict seat (`:203`). Measured 26.6 ms median, 17.5 ms min on the second pass (34 / 21 ms on the first). README claims about 20 ms (`README.md:255`). |
| `screenshot` | 268 | `(window='', region='', scale=0, stable=False, lossless=False) -> list` | file mode (default): `[Block(text="screenshot saved..."), Block(text=json meta with "path")]`; image mode: `[image block, meta block]` (`:232-249`) | grim spawn; `stable=True` loops captures every 150 ms up to 2 s (`screenshot.py:291-318`). Not useful to Jev (no image input). |
| `zoom` | 290 | `(x, y, size='', window='', stable=False, lossless=False) -> list` | same packaging as screenshot, meta gains `target:"zoom"`, `point` | vision loop only. |
| `ui` | 400 | `(window='', name='', actionable=True) -> list[dict] | str` | `[{role, name, x, y, clickable, value?, percent?, checked?}]` with GLOBAL click points (`:381-391`), or a prose string when there is no tree, no match, or the read failed (`:363, 369, 395`) | busctl storm, see section 5. Never raises `A11yError` (caught at `:368`). Raises `ValueError` for a bad or vanished window (`:323, 326`). |
| `marks` | 502 | `(window='', name='') -> list | str` | numbered screenshot plus `{"legend":[...], "hint":...}`; stores numbering in the `_last_marks` global (`:533-539`) | a `ui` read plus a stable capture plus an ImageMagick spawn. Image is useless to Jev; the legend duplicates `ui`. |
| `binds` | 1149 | `() -> list[dict]` | `[{combo, action, arg?, description?, submap?}]` from `hyprctl.parse_binds` (`hyprctl.py:594-623`) | one fork. This machine: 125 binds, 116 with a description, which fits a single Jev `choice` question (255 option cap). |
| `wait_for` | 1287 | `(event, match='', timeout_s=10) -> dict | str` | event payload dict, `{"already": True, ...}` for level-triggered pre-checks, or a `timeout:` / `event socket unavailable:` string | blocks the calling thread 1 to 60 s (`:1306`). Events: `window_open, window_close, workspace, title_change, layer_open, layer_close, urgent, screencast` (`:1226-1238`). |

### Acting tools (not registered under `HYPRUSE_READONLY`, `server.py:1672-1676`)

| Tool | def line | Signature | Returns | Guards run, in order |
|---|---|---|---|---|
| `pointer` | 635 | `(action, x=None, y=None, button='left', to_x=None, to_y=None, scroll_dy=0, scroll_dx=0, double=False, then='none', allow_auth=False)` | `"click ok; cursor now at (x, y)"` plus optional layer or lock NOTE, fused with `then` | `guard_seat` (`:657`), `guard_session_lock` for non-move (`:671`), arg checks, `guard_pointer` (`:687, 696-697, 705`), `_layer_note` warning (`:688`). actions: `move|click|drag|scroll`. |
| `keyboard` | 719 | `(action, text='', keys='', window='', then='none', allow_auth=False)` | `"typed N characters[ into 0x..]"` or `"pressed ctrl+t"` | `guard_seat` (`:740`), arg and combo validation BEFORE any focus change (`:744-755`), `guard_session_lock` (`:762`), `guard_keyboard_layer` (`:764`), then if any window guard is on: resolve target, `guard_client`, `guard_auth_client`, `guard_password_field` (`:772-781`). `window=` dispatches `focuswindow` then sleeps 50 ms (`:788-790`). actions: `type|key`. Cannot trigger compositor binds. |
| `click_ui` | 801 | `(name='', mark=0, window='', index=-1, button='left', double=False, then='none', allow_auth=False)` | `"clicked push button 'Save' at (x, y) in class"`; an ambiguity returns `[Block(text="... is ambiguous (N candidates)..."), Block(json candidates)]` (`:861-867`); no tree returns the `ui` prose string (`:853-854`) | `guard_seat`, exactly one of name or mark, marks TTL 600 s and class check (`:829-846`), `guard_client`, `guard_auth_client`, `guard_session_lock(True, ...)` always refuses when locked, `guard_covering_layer` (`:871-879`). Then `focuswindow`, 50 ms sleep, real pointer click (`:886-888`). |
| `hypr` | 980 | `(action, target='', workspace='', then='none')` | `"on workspace 3"`, `"focused 0x.."`, `"moved .. to workspace .."`, `"closed 0x.."` or `"asked .. to close, but it is still open after 1s"`, `"fullscreen toggled"`, `"floating toggled"` | arg validation (`:991-1005`), `guard_window(target)` only when confinement is on (`:1012-1013`), `guard_seat` only for target-less fullscreen or floating (`:1014-1018`). NO lock guard. actions are exactly six: `workspace, focus_window, move_window, close_window, fullscreen, toggle_floating` (`:969-976`). |
| `launch` | 1093 | `(command, workspace='', wait_s=8.0) -> dict | str` | `{address, class, title, workspace, note?}` or a `"launched, but no new window appeared within Ns"` string | No trust guard at all (by design, `ARCHITECTURE.md:157-159`). Subscribes to socket2 first, dispatches `exec`, blocks on `openwindow` up to `wait_s` clamped 1 to 30 (`:1066-1089, 1100`). `command` is an arbitrary shell line handed to Hyprland `exec`. No `.desktop` index exists anywhere in hypruse (grep for `.desktop`, `gtk-launch`, `gio` found nothing), so "open Firefox" to command mapping is the fork's job. |
| `use_bind` | 1191 | `(combo, then='none')` | `"ran SUPER+F: fullscreen 0"` | `guard_seat`, `guard_use_bind` (refused wholesale under confinement), bind lookup, Lua-closure refusal (`:1200-1217`). Dispatches the bind's own dispatcher and arg verbatim (`:1221`). Works on this machine because the provider is hyprlang. |
| `sequence` | 1481 | `(steps, stop_on_change=True, then='desktop')` | `"sequence: all N/N steps ran\n[0] ..."` or `"... stopped after k/N steps, <reason>"`, fused with `then` (default `desktop`) | steps dispatch through the same tool functions so they inherit guards (`:1328-1334, 1413-1423`). Max 20 steps, 30 s budget, 200 ms settle drain between steps and after the last (`:1356-1358, 1519, 1567`). Ops allowed: `pointer, keyboard, hypr, wait_for, click_ui` only (no `launch`, no `use_bind`). |
| `clipboard` | 1166 | `(action, text='') -> str` | text, `"(clipboard is empty)"`, or `"copied N characters to the clipboard"` | opt-in tool. The function itself does NOT check `HYPRUSE_CLIPBOARD`; only registration and `verbs.run` do. `wl-paste` / `wl-copy` spawns (`clipboard.py`). |

`then` values: `none|desktop|screenshot|ui` (`server.py:559-595`). `then='desktop'` appends `json.dumps(hyprctl.snapshot())` as a text block, which costs one more fork.

Exceptions a library caller must catch (the canonical mapping is `verbs.run`, `verbs.py:391-398`): `trust.TrustError` (a refusal, not a bug), `ValueError` / `TypeError` (bad args, unknown window), `hyprctl.HyprctlError`, `input.InputError`, `wire.WireError`, `screenshot.ScreenshotError`, `clipboard.ClipboardError`, `journal.DryRunError` (should never fire). `a11y.A11yError` and `events.EventError` are mostly converted to prose inside the tools.

### Lower-level functions worth calling directly

- `hyprctl.snapshot() -> dict` (`hyprctl.py:550-557`), `hyprctl.snapshot_from(monitors, workspaces, clients, active_window, cursor, layers)` pure (`:518-547`), `hyprctl.query(cmd)` (`:50-56`), `hyprctl.batch_query(cmds)` (`:59-85`), `hyprctl.dispatch(name, *args)` (`:291-319`), `hyprctl.binds()` / `find_bind(combo)` (`:626-639`), `hyprctl.cursor_pos()` (`:372-374`), `hyprctl.notify(message, ms, icon, color)` (`:365-369`), `hyprctl.layer_kind(ns)` (`:462-467`), `hyprctl.provider()` (`:122-137`).
- `hyprctl.dispatch` is generic under the hyprlang provider: any dispatcher name and args pass straight to `hyprctl dispatch` (`:284-288`). Under the Lua provider only nine dispatchers are mapped (`_LUA_DISPATCH`, `:240-250`) and anything else raises. This machine is hyprlang, so a voice fork can reach `movefocus`, `resizeactive`, `swapwindow`, `togglespecialworkspace`, `pin`, `togglegroup`, `layoutmsg`, and so on through `hyprctl.dispatch` today. Doing so skips `safety.touch`, the journal, and every trust guard (only the dry-run barrier at `:300` still applies), so the fork should wrap new verbs in its own guarded, journaled tool function rather than calling `dispatch` raw.
- `events.EventStream` and `events.parse_event` (section 4).
- `trust.session_locked()`, `trust.covering_layer(x, y)`, `trust.guard_*` (section 6).

---

## 2. The in-process call path (no MCP, no `hypruse` subprocess)

### What the repo already proves

`verbs.run` (`verbs.py:348-403`) is the reference for calling tools without MCP. `hypruse replay` does the same. The essential sequence, minus the one-shot-process baggage:

```python
import os
os.environ.setdefault("HYPRUSE_SCREENSHOT_MODE", "file")   # only matters if you capture
from hypruse import server, session, safety, journal, trust
from hypruse import input as hinput

session.ensure_session_env()          # session.py:47-58, fills HYPRLAND_INSTANCE_SIGNATURE / WAYLAND_DISPLAY if stripped
server.use_plain_blocks()             # server.py:153-156, ONE-WAY process-global switch; never import mcp after this
safety.init()                         # safety.py:87-93, beacon + atexit + SIGTERM handler; MAIN THREAD ONLY (signal.signal, :83-84)
safety.on_shutdown(hinput.release_held)   # server.py:1712, releases a held drag button on the kill path
journal.start(version)                # optional, server.py:1713
safety.on_shutdown(journal.stop)
trust.init_marking()                  # optional, HYPRUSE_MARK border rule

snap = server.desktop()
msg = server.hypr("workspace", workspace="3")
```

Do not call `verbs.run(...)` itself from a long-lived process: it mutates `os.environ` (`HYPRUSE_SCREENSHOT_MODE` always, `HYPRUSE_DRYRUN=1` when `dry_run=True`, `verbs.py:366-368`) and never unsets it, takes a cross-process flock per call (`:332-345`), and saves and restores `cli-state.json` around each call (`:387, 400`). None of that is needed when state lives in memory.

`cli_state.py` exists only because a verb is a fresh process (`cli_state.py:1-21`). A daemon is the "one long-lived process" case the module docstring describes, so marks numbering, the launched set, and the strict baseline already persist in module globals.

### Why import is cheap

`server.py` never names `mcp` at module level. `_text()` / `_image()` import `mcp.types` lazily only when `_plain_blocks` is False (`server.py:159-172`). `build_app()` imports FastMCP on first use (`:1665-1677`), `app()` memoizes it (`:1683-1691`), and the module `__getattr__` exposes it as `server.mcp` (`:1694-1697`). `server.main()` is the only caller in the server path (`:1716`).

Dependency surface of the library path: zero third-party packages. Under system Python 3.14 with `PYTHONPATH=src`, importing `server, hyprctl, events, trust, journal, safety, session, a11y` loaded no non-stdlib module other than an unrelated `sphinxcontrib` namespace stub from site-packages, and `'mcp' in sys.modules` was False. The fork can therefore run on the system interpreter (needed for the system `python-gobject` / GTK4 layer-shell bindings) without a venv for the control library. `pyproject.toml:23` lists `mcp>=1.2,<2` as the only dependency; drop it if the fork does not serve MCP.

### Measured startup

Second pass, cold process wall clock, n=7, loadavg about 11 (`bench_import.py`). This is the table to plan with:

| What | median | min | max |
|---|---|---|---|
| venv 3.13 bare interpreter | 32 ms | 28 ms | 39 ms |
| venv 3.13 `import hypruse.hyprctl` | 87 ms | 85 ms | 112 ms |
| venv 3.13 `import hypruse.server` (mcp not loaded) | 97 ms | 86 ms | 105 ms |
| venv 3.13 `import hypruse.server, verbs, cli_state, cli` | 103 ms | 75 ms | 112 ms |
| venv 3.13 `server.app()` (FastMCP build) | 704 ms | 632 ms | 997 ms |
| venv 3.13 `import mcp.types` alone | 933 ms | 724 ms | 1081 ms |
| system 3.14.7 bare interpreter | 26 ms | 22 ms | 28 ms |
| system 3.14.7 `import hypruse.server`, `PYTHONPATH=src` | 93 ms | 78 ms | 127 ms |

After `import hypruse.server` on the venv interpreter, `sys.modules` holds 137 modules and none of `mcp`, `pydantic`, `anyio`, `httpx`. Most of the 65 ms import cost is stdlib (`subprocess`, `socket`, `json`, `inspect`, `hashlib`, `fcntl`, `re`): `import hypruse.hyprctl` alone is already 87 ms.

First pass, same measurement at loadavg 22 to 28, venv Python 3.13 (kept for the record; load-inflated by 3x to 5x):

| What | median | min |
|---|---|---|
| bare interpreter | 169 ms | 58 ms |
| `import hypruse.server` (mcp not loaded, asserted) | 303 to 569 ms | 196 to 205 ms |
| `import hypruse.server; server.app()` (FastMCP build) | 3671 ms | 3363 ms |
| `from mcp.server.fastmcp import FastMCP` alone | 3817 ms | 2636 ms |

In-process, interpreter already up: importing `a11y, events, hyprctl, server, session, trust` took 86 ms on 3.13 (warm pyc) and 418 ms on system 3.14 with `PYTHONDONTWRITEBYTECODE=1` (no pyc cache, so this is a worst case). The repo's own figure is 0.38 s verb startup versus 2.6 s with MCP (memory note from the 0.11.0 work; `ARCHITECTURE.md:52-55` says "a few hundred milliseconds" versus "about two seconds"). For a daemon this is paid once at boot and is irrelevant to per-command latency.

Import-time side effects to know about: `server.py:124-129` reads `HYPRUSE_READONLY`, `HYPRUSE_CLIPBOARD`, and dry-run into module constants at import; `server.py:1658-1662` rewrites docstrings when read-only; `trust.py:364-366` runs an assert. Nothing touches the compositor at import.

### Threading reality

With the installed `mcp` 1.28.1, FastMCP calls sync tools inline on the event loop (`.venv/lib/python3.13/site-packages/mcp/server/fastmcp/utilities/func_metadata.py:93-96`: `return fn(**arguments_parsed_dict)`), so under MCP the tools are effectively serialized. The comment at `input.py:118-124` ("sync tool functions run on worker threads") does not describe this version. A daemon that calls tools from a thread pool will be the first caller to exercise real concurrency. See section 7.

---

## 3. The desktop snapshot structure (future Jev state and candidate lists)

Built by `hyprctl.snapshot_from` (`hyprctl.py:518-547`) from six queries issued as one batch: `monitors, workspaces, clients, activewindow, cursorpos, layers` (`:553-555`). Live shape on this machine (titles redacted), 2,269 JSON characters for 7 windows, 5 workspaces, 1 layer:

```json
{
  "monitors":   [{"name": "eDP-1", "geometry": [0, 0, 1920, 1080], "scale": 1, "focused": true, "active_workspace": 10}],
  "workspaces": [{"id": 6, "name": "6", "monitor": "eDP-1", "windows": 2, "visible": false}],
  "windows":    [{"address": "0x55a4471ec9b0", "workspace": 8, "class": "kitty", "title": "...",
                  "at": [7, 7], "size": [949, 1066], "floating": false, "pid": 2312889}],
  "active_window": "0x55a4480166f0",
  "cursor": [1736, 620],
  "layers": [{"namespace": "waybar", "kind": "bar", "level": "bottom", "monitor": "eDP-1", "geometry": [0, 0, 1920, 38]}]
}
```

Field by field:

- `monitors[]` (`_monitor`, `hyprctl.py:421-435`): `name`, `geometry [x, y, w, h]` in global LOGICAL pixels (physical size divided by scale, axes swapped for odd transforms, `logical_rect` `:377-387`), `scale`, `focused`, `active_workspace` (id), optional `transform` when nonzero.
- `workspaces[]` (`:530-539`): `id`, `name`, `monitor`, `windows` (count), `visible` (id is some monitor's active workspace, `:527`). Sorted by id. Special workspaces appear with negative ids and `special:name` names (fixture `tests/fixtures/desktop.json`). Note `visible` ignores a pulled-up special workspace; `trust._visible_workspaces` handles that separately (`trust.py:209-220`).
- `windows[]` (`_window`, `:401-418`): `address` (hex string, a heap pointer, valid only within one compositor instance, can be reused later), `workspace` (id only, not name), `class`, `title`, `at [x, y]`, `size [w, h]`, `floating`, `pid`, plus `fullscreen: true` and `hidden: true` only when set. Unmapped clients are dropped (`:540`). Order is hyprctl's client order, not z-order and not focus order.
- `active_window`: address string or None. This is the only focus field.
- `cursor`: `[x, y]` global logical, or None.
- `layers[]` (`parse_layers`, `:484-515`): `namespace`, `kind` (prefix heuristic: `launcher|bar|notifications|lock|osk|unknown`, `:453-467`), `level` (`bottom|top|overlay`; background level 0 is dropped), `monitor`, `geometry`. The key is absent when nothing but wallpaper exists (`:545-546`). A listed layer is tracked, not necessarily visible (`:490-497`).

What `_window` throws away that a voice resolver will want. Raw `hyprctl -j clients` keys on 0.56.2 (local measurement): `acceptsInput, address, allowedOverFullscreen, at, class, contentType, floating, focusHistoryID, fullscreen, fullscreenClient, fullscreenHandler, grouped, hidden, inhibitingIdle, initialClass, initialTitle, mapped, monitor, pid, pinFullscreened, pinned, size, stableId, swallowing, tags, tearingHint, title, visible, workspace, xdgDescription, xdgTag, xwayland`. The valuable drops:

- `focusHistoryID`: 0 is the focused window, 1 the previous one. This is what resolves "the last window", "go back", "the other terminal". Not in the snapshot.
- `workspace.name`: needed for named and special workspaces.
- `initialClass` / `initialTitle`: stable app identity when the title is a document name.
- `pinned`, `grouped`, `tags`, `xwayland`, `monitor`, `visible`.
- `stableId`: new in recent Hyprland; could be a stable key across address reuse (not used anywhere in hypruse; semantics not verified here).

Raw workspace keys also include `lastwindow`, `lastwindowtitle`, `hasfullscreen`, `ispersistent`, `tiledLayout`. Raw monitor keys include `specialWorkspace`, `reserved`, `dpmsStatus`, `description`. Raw layer keys include `address`, `alpha`, `pid`.

Recommendation: keep `snapshot_from` for compatibility with the tools and tests, and add a second pure builder in the fork (for example `voice_state_from(...)`) that takes the same six raw replies and emits the richer, Jev-oriented record: windows sorted by `focusHistoryID`, short stable option labels, workspace names, and no pixel geometry unless a command needs it. Both builders can share one socket round trip.

Second-pass size check: 7 windows, 5 workspaces, 1 monitor, 1 layer serialized to 2,187 chars with `json.dumps` defaults and 1,975 chars compact (`separators=(",", ":")`), roughly 500 tokens at 4 chars per token (an estimate, not a Jev tokenizer count). Raw `focusHistoryID` values on this session were exactly `[0, 1, 2, 3, 4, 5, 6]` for the 7 windows, so it is a dense recency rank and a ready-made ordering key for a window candidate list.

Untrusted text warning: `class`, `title`, layer `namespace`, and a11y names are written by whatever application is on screen. The dict `desktop()` returns is raw; control characters are stripped only on the CLI rendering path (`verbs._safe`, `verbs.py:408-420`) and in journal display (`cli._safe`). README's threat model already flags window titles as a prompt-injection channel for LLM agents. Jev does not generate text, but a title is still attacker-influenced input to a classifier, so the fork should sanitize and length-cap titles before they become state or option labels, and should never let a title select a destructive option on its own.

Jev sizing note (inference from the numbers above): about 320 JSON chars per window. Even 40 windows plus 125 binds is far below a 32k token state. Candidate lists map cleanly: windows to a `choice` over addresses (cap 255), workspaces to a `choice`, binds to a `choice` over combos with their descriptions as option text.

---

## 4. How hypruse talks to Hyprland

### Requests: subprocess per call

`hyprctl._run` (`hyprctl.py:35-47`) does `shutil.which("hyprctl")` and then `subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)` for every query, batch, dispatch, keyword, eval, and notify. The module docstring says this is deliberate: "It shells out to the hyprctl binary rather than opening the .socket directly so behaviour always matches what the user's own shell would do" (`hyprctl.py:3-6`). `batch_query` exists specifically to amortize the fork (`:59-85`).

Fork counts on the default configuration (auth guard on, no confine, no strict, no journal), derived from the code:

| Call | hyprctl forks | other cost |
|---|---|---|
| `desktop()` | 1 | |
| `hypr("workspace"|"focus_window"|"move_window")` | 1 (plus 1 one-time `-j status` provider probe, cached, `:122-137`) | |
| `hypr("fullscreen")` with no target | 3 (`clients`, `activewindow`, dispatch; `server.py:1016, 316-327`) | |
| `hypr("close_window")` | 1 | socket2 subscribe, blocks until `closewindow` or 1 s (`server.py:936-966`) |
| `use_bind` | 2 (`binds` query, dispatch) | |
| `pointer("click", x, y)` | 4 (`guard_pointer` batch, `covering_layer` layers, `movecursor` dispatch, `cursor_pos` for the result string) | `/proc` lock scan, 20 ms + 20 ms sleeps, 2 Wayland roundtrips |
| `keyboard("type")` no window | 3 (`layers`, `clients`, `activewindow`) + 1 `wtype` spawn | `/proc` lock scan; first call also opens a Wayland connection and creates a virtual pointer just to ask `on_named_seat` (`input.py:141, 194-203`) |
| `keyboard(..., window=addr)` | 3 (`layers`, `clients`, `focuswindow` dispatch; `_resolve_window` with an explicit address skips the `activewindow` query, `server.py:316-327`) + `wtype` | plus 50 ms sleep. Corrected on the second pass (first pass said 4) |
| `click_ui(name)` | 6 with no `window=` (`_resolve_window("")` = 2, `_ui_read(address)` re-resolves = 1, layers, focuswindow, movecursor), 5 with `window=` | busctl storm, `/proc` scan, 50 + 40 ms sleeps. Corrected on the second pass (first pass said 7) |
| `launch` | 2 (`exec`, `clients`) | blocks until `openwindow` |

Every `_run` also calls `shutil.which("hyprctl")` first (`hyprctl.py:36`), a PATH walk per call, and the first `dispatch` of the process adds one `-j status` provider probe.

Second pass, measured here (`bench_live2.py`, n=15 unless noted, loadavg about 11). Plan with these:

| Operation | median | min | p90 |
|---|---|---|---|
| `hyprctl.snapshot()` via subprocess (1 fork, 6-query batch) | 19.7 ms | 11.1 ms | 29.7 ms |
| `server.desktop()` in-process | 26.6 ms | 17.5 ms | 28.3 ms |
| `hyprctl.query("activewindow")` | 24.3 ms | 19.3 ms | 30.8 ms |
| `hyprctl.query("clients")` | 24.0 ms | 11.5 ms | 40.0 ms |
| `hyprctl.query("layers")` | 30.4 ms | 11.4 ms | 38.4 ms |
| `hyprctl.cursor_pos()` | 20.5 ms | 11.0 ms | 28.7 ms |
| `hyprctl.binds()` (n=7, 125 binds) | 32.5 ms | 19.5 ms | 34.6 ms |
| `server._resolve_window("active")` (2 forks) | 48.8 ms | 23.5 ms | 66.3 ms |
| `trust.guard_pointer(cursor)` (auth guard default on, 1 batch fork) | 26.3 ms | 22.3 ms | 38.7 ms |
| `trust.guard_keyboard_layer(False, False)` (1 fork) | 16.6 ms | 12.2 ms | 25.7 ms |
| `trust.covering_layer(cursor)` (1 fork) | 21.1 ms | 11.5 ms | 32.2 ms |
| `trust.session_locked()` (`/proc` scan, 369 processes) | 13.0 ms | 12.3 ms | 13.6 ms |
| bare `hyprctl --help` spawn, no IPC (fork+exec+link floor) | 13.2 ms | 10.5 ms | 21.0 ms |
| bare `busctl --version` spawn | 8.5 ms | 3.9 ms | 10.7 ms |
| DIRECT socket, same 6-query batch (n=30) | 0.3 ms | 0.2 ms | 0.5 ms |
| DIRECT socket `j/activewindow` (n=30) | 0.1 ms | 0.1 ms | 0.2 ms |
| `events.EventStream()` connect + close (n=30) | 0.1 ms | 0.1 ms | 0.1 ms |

Reading the table: the `hyprctl --help` row shows that 10 to 13 ms of every call is process spawn and dynamic linking, not IPC. The compositor answers in a fraction of a millisecond. A default-config `pointer("click", x, y)` therefore pays about 4 x 20 ms of forks plus 13 ms of `/proc` scan plus 40 ms of sleeps, call it 130 ms, of which under 1 ms is Hyprland doing work (derived from the rows above, not measured end to end, since this lane may not click).

First pass, same operations at loadavg about 22 (load-inflated, kept for the record):

| Operation | median | min |
|---|---|---|
| `hyprctl.snapshot()` via subprocess | 35.6 ms | 24.9 ms |
| `server.desktop()` | 34.5 ms | 21.2 ms |
| `hyprctl.query("activewindow")` | 33.9 ms | 15.6 ms |
| `hyprctl.cursor_pos()` | 26.0 ms | 16.1 ms |
| raw `hyprctl -j activewindow` spawn | 28.7 ms | 14.8 ms |
| `/bin/true` spawn (fork+exec floor) | 4.0 ms | 1.7 ms |
| DIRECT socket, same 6-query batch, parsed by `snapshot_from` | 3.53 ms | 1.35 ms |
| DIRECT socket `j/activewindow` | 0.16 ms | 0.14 ms |
| DIRECT socket `j/locked` | 0.14 ms | 0.10 ms |

The direct-socket snapshot compares equal to `hyprctl.snapshot()` once volatile fields are masked (the verification pass caught a cursor difference between the two calls; a live parity test in the fork must mask `cursor`, and in principle `active_window` and titles).

Direct protocol, verified read-only against `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`: connect, send one request, read until EOF, one request per connection. `j/clients` is the JSON form, `/version` and bare `version` both work, a batch is `[[BATCH]]j/monitors;j/workspaces;...` and returns the JSON documents concatenated (which `batch_query`'s `raw_decode` loop already handles, `hyprctl.py:66-80`), an unknown command returns the literal string `unknown request`. There is no exit code on the socket: errors are in-band strings. hypruse already treats `out != "ok"` as a dispatch failure (`:286-288`) and JSON decode failure as a query failure (`:53-56`), so the semantics carry over. I did NOT send any `dispatch` over the raw socket (that would act). The request spelling is now verified from the hyprctl client source instead (second pass, `Hyprland-0.56.2/hyprctl/src/main.cpp:398-480`): flags are collected into `fullArgs` (`-j` adds `j`, `:413-415`), every non-flag argv element is appended to `fullRequest` followed by one space (`:470`), the trailing space is popped (`:478`), and the wire request is `fullArgs + "/" + fullRequest` (`:480`). So `hyprctl dispatch workspace 3` sends the bytes `/dispatch workspace 3`, `hyprctl -j clients` sends `j/clients`, and a batch is `[[BATCH]]` plus the semicolon-joined commands (`batchRequest`, `:339-349`; hypruse already prefixes each command with `j/` itself, `hyprctl.py:64`, so it never needs the client's `-j` rewrite). On the compositor side `dispatchRequest` strips the first word and, for hyprlang, splits dispatcher name from argument at the first space (`src/debug/HyprCtl.cpp:1126-1158`); for Lua it evaluates `return hl.dispatch(<rest>)` (`:1130-1141`). It answers `ok`, `Invalid dispatcher`, or the dispatcher's error string, in-band.

Fork plan: replace the body of `_run(*args)` with a socket client that maps `("-j", cmd)` to `j/cmd`, `("--batch", spec)` to `[[BATCH]]spec`, and everything else to `/` + space-joined args, keeping the subprocess path as a fallback. Every caller upstack, every guard, and all 477 lines of `tests/test_hyprctl.py` (which monkeypatch `_run`) keep working. The first pass worried that `lua_dispatch` relies on the expression being "a single argv element" (`hyprctl.py:253-256`). The client source settles it: hyprctl joins argv with single spaces before sending, so a one-element argv and the same string written to the socket are byte-identical, and the Lua path carries over unchanged. One real difference to handle: `hyprctl` exits non-zero on a connection failure and hypruse maps that to `HyprctlError` (`hyprctl.py:45-46`); the socket client must raise the same error on `ConnectionRefusedError` / `FileNotFoundError` / timeout so the fail-closed guards (`trust.guard_pointer`, `guard_seat`, `_guarded_active_client`) keep failing closed.

### Config provider

`provider()` probes `hyprctl -j status` once and caches in the `_provider` global (`:119-137`); `dispatch` re-probes after a failure (`:301-319`). Lua sessions lose `use_bind` entirely (`server.py:1208-1217`) and every dispatcher outside the nine mapped ones. `hyprctl keyword` is refused by Lua; `border_rule` hides that (`:341-362`).

### Events: socket2

`events.py` is 157 lines and has no dependency on the rest of the package.

- Path: `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock` (`:70-75`); raises `EventError` when the signature env is missing.
- `EventStream()` opens a blocking `AF_UNIX` stream socket (`:82-90`). Measured connect plus close: 0.09 ms median.
- `parse_event(line)` is pure (`:51-67`): splits `NAME>>DATA`, applies a per-event `maxsplit` so titles containing commas survive, and adds the `0x` prefix that socket2 omits from addresses (`:64-65`). Schemas exist for 11 events (`:31-46`): `openwindow(address, workspace, class, title)`, `closewindow(address)`, `movewindow(address, workspace)`, `workspace(name)`, `activewindowv2(address)`, `windowtitlev2(address, title)`, `windowtitle(address)`, `openlayer(namespace)`, `closelayer(namespace)`, `urgent(address)`, `screencast(state, owner)`. Any other event passes through as `(name, {"data": raw})` (`:57-58`), so `focusedmon`, `fullscreen`, `activespecial`, `submap`, and the rest are still delivered, just unparsed.
- `wait_for(names, matcher, timeout)` (`:92-121`) blocks with a 1 s `settimeout` poll and DISCARDS every non-matching event it reads. `drain(settle)` (`:123-148`) returns everything that arrives within a window.
- There is no iterator, callback, or asyncio API, and an instance is not thread-safe (shared `_buf`).
- Tools open a fresh connection per use: `wait_for` (`server.py:1319`), `launch` (`:1074`), `close_window` (`:945`), `sequence` (`:1509`). "Subscribe before you act" is the repeated correctness rule (`events.py:79-80`, `server.py:941-942, 1069-1071`).

Daemon plan: run one persistent reader (a thread looping on `drain(1.0)`, or an asyncio `open_unix_connection` reader that reuses `parse_event`) that keeps a live world model: on `openwindow / closewindow / movewindow / windowtitlev2 / activewindowv2 / workspace / openlayer / closelayer`, either patch the cached snapshot or mark it dirty and re-pull it over the socket in about 2 to 4 ms. The Jev state is then always already built when speech ends, and the per-command "observe" cost is zero. The tools keep opening their own short-lived streams, which is fine at 0.09 ms.

---

## 5. a11y.py, marks, click_ui

### Mechanism

AT-SPI over D-Bus, reached by spawning `busctl --json=short` once per method call or property read (`a11y.py:84-100`, 10 s timeout each, `shutil.which` on every call). `connect()` asks the session bus for the private a11y bus address (`:103-110, 135-136`); `Bus.call` / `Bus.prop` wrap one spawn each (`:120-126`).

Resolution chain used by `server._ui_read` (`server.py:352-396`): `_resolve_window` (2 hyprctl forks) then `a11y.connect()` (1 spawn) then `app_for_pid` (`a11y.py:145-158`: 1 spawn for the registry children, 1 `GetConnectionUnixProcessID` spawn per registered app until the pid matches, then a title-match fallback that costs a `GetChildren` plus a `Name` per frame per app) then `window_frame` (`:165-189`: picks the toplevel by exact title, then by extent size) then `find_elements` (`:309-360`).

`find_elements` is a DFS capped at `max_nodes=400`, `max_results=60`. Per visited node it always spawns twice (`Name` property, `GetChildren`, `:332-333`). A node passing the name filter costs a `GetRole` (`:337`). A node that is reported costs `GetExtents`, `GetState`, `GetRoleName` (`:344-350`) and, for value-bearing roles, 1 to 4 more (`GetInterfaces` plus `CharacterCount` + `GetText`, or plus three `Value` properties, `:260-296`). Roles are matched by AT-SPI enum number because role names differ across GTK and Qt (`:59-81`). Extents are window-relative (`COORD_WINDOW`), sanity-filtered (`:220-235`), and the server adds the hyprctl window origin and drops anything outside the window rect (`server.py:370-380`). Password text (role 40) is listed as actionable but its contents are never read (`a11y.py:44, 49-51`).

Capabilities: list named controls with exact global click points and current values (`value`, `percent`, `checked`); filter by substring; report `clickable` from SHOWING + VISIBLE + (SENSITIVE or ENABLED) (`:299-306`); `focused_role` for the password-field guard (`:363-379`); `do_action` (`:382-388`) can invoke a control with no pointer at all, but NOTHING in `server.py` calls it (grep confirms the only reference is its definition). For a voice app that acts without looking, `do_action` is attractive: no focus change, no cursor motion, works on hidden windows. It is unused and therefore untested against real apps.

Known coverage limits stated by the project (`README.md:340`, `server.py:415-417`): terminals, canvas UIs, games, and Electron or Chrome without `--force-renderer-accessibility` expose little or nothing; GTK4 combo boxes expose no value.

### marks and click_ui

`marks` (`server.py:502-556`) is `ui` plus a stable window capture plus an ImageMagick spawn that draws numbered dots, and it stores `{window, class, ts, items: {n: {dx, dy, label}}}` in the single `_last_marks` global with WINDOW-RELATIVE offsets so a moved window still resolves (`:463-466, 527-539`). `click_ui(mark=N)` enforces a 600 s TTL and a class match (`:835-846`). For Jev the image is dead weight; the numbering could be repurposed as a voice "show numbers" overlay ("click 7"), but then the fork should draw the numbers in its own layer-shell HUD from the `ui` list and skip grim and ImageMagick entirely.

`click_ui(name=...)` (`:850-870`): exact case-insensitive name match preferred, else substring pool; more than one candidate returns the candidate list instead of guessing; `index` disambiguates. That ambiguity list is a ready-made Jev `choice` question.

### Latency

Not measurable today on this machine: `a11y.connect()` succeeded in 17 ms, then `a11y.apps()` failed with `Could not activate remote peer 'org.a11y.atspi.Registry': unit failed`. Diagnosis (read-only): four `at-spi-bus-launcher` processes are alive (pids 2990, 708130, 1122520, 1144134); `org.a11y.Bus` on the session bus is still owned by the original pid 2990 and hands out `unix:path=/run/user/1000/at-spi/bus_1`, but that socket file was re-created on Sep 10 16:32 by a stray launcher whose broker has no registry. The project memory recorded the same breakage on 2026-09-13. Until the user restarts `at-spi-dbus-bus.service`, kills the stray launchers, and restarts the GTK/Qt apps, `ui`, `marks`, `click_ui`, `then='ui'`, and the strict password-field guard all degrade to "accessibility read failed" strings. `hypruse doctor` has no a11y check (`cli.py:132-141` lists deps, session, events, pointer, screenshot, mode, skill).

Second pass, 2026-09-21: unchanged. `a11y.connect()` succeeded in 28 ms (one busctl call to the session bus), then `a11y.apps()` failed with the identical `Could not activate remote peer 'org.a11y.atspi.Registry': unit failed`. `systemctl --user status at-spi-dbus-bus.service` reports active since 2026-09-01 with main pid 2990, and `pgrep` still shows the three stray launchers (708130, 1122520, 1144134) beside it and one `at-spi2-registryd` (pid 3184). I did not touch the service (that would change the machine).

What can be said with evidence: one bare `busctl` spawn measured 8.5 ms median, 3.9 ms min on the second pass (39.1 / 11.0 ms under first-pass load), and one real busctl D-Bus call (`connect()`) 17 to 28 ms. Cost model (inference, not measurement): a window with 100 accessible nodes and 20 reported controls needs roughly 2x100 + 100 + 3x20 + value reads, about 370 spawns, which is about 3 s at the 8.5 ms bare-spawn median, 6 to 10 s at the 17 to 28 ms real-call figure, and worse under load. The 400-node cap bounds the worst case at roughly 800 to 1,300 spawns. The project documents no a11y latency figure anywhere (searched README and CHANGELOG on both passes); its own code comments call the walk "too costly for every keystroke" (`trust.py:557-559`). For a voice command that must feel instant, `click_ui` as shipped is not usable on the hot path.

Fork plan: keep the a11y module's logic and role tables, replace `_busctl` with a persistent D-Bus connection. `python-gobject` 3.56 is installed system-wide, so `Gio.DBusConnection.new_for_address_sync(address, ...)` plus `call_sync` is available with no new dependency, and the `Atspi` GI typelib IS installed (`/usr/lib/girepository-1.0/Atspi-2.0.typelib`, checked on the second pass; `Gtk-4.0` and `Gtk4LayerShell-1.0` typelibs sit beside it), which would be simpler still. Per-call cost drops from about 11 to 39 ms to well under 1 ms, which turns a multi-second walk into tens of milliseconds. Add a per-window cache invalidated by `windowtitlev2` and focus events if "click X" must feel instant.

---

## 6. Gates a voice front end must respect (or that will block a voice action)

### Summary table

| Gate | Env / trigger | Where enforced | Enforced for library callers? | Voice impact |
|---|---|---|---|---|
| Read-only | `HYPRUSE_READONLY` | MCP registration `server.py:1672`; `verbs.run` `verbs.py:351-357` | NO. `server.READONLY` is an import-time constant (`:124`) that no tool function consults | The fork must check it itself or a "read-only" user setting is silently ignored |
| Clipboard opt-in | `HYPRUSE_CLIPBOARD` | `server.py:1675`; `verbs.py:358-363` | NO | same |
| Dry run | `HYPRUSE_DRYRUN` | every acting tool returns a `DRY RUN, nothing was delivered: would ...` plan (`server.py:598-600, 712, 785, 880, 1019, 1102, 1183, 1219`); hard barrier `journal.refuse_if_dry` in `input.py:134,148,244,266,277,306`, `hyprctl.py:300`, `clipboard.py:47` | YES, read per call from `os.environ` (`journal.py:92-93`) | Excellent for a "rehearse what you heard" mode and for tests. It is process-global, so it cannot be toggled per command safely while another command runs |
| Strict seat | `HYPRUSE_STRICT` | `trust.guard_seat` (`trust.py:617-639`) called by `pointer`, `keyboard`, `click_ui`, `use_bind`, and target-less `hypr fullscreen/toggle_floating`; baseline set by `remember_seat` after acting and after `desktop/screenshot/zoom/ui/marks` | YES | Will refuse almost every voice command because the human moved the mouse while talking. Leave unset, or call `server.desktop()` immediately before acting. Address-targeted `hypr` actions skip it by design (`server.py:1006-1018`) |
| Confinement | `HYPRUSE_CONFINE=launched|class:..|workspace:..` | `guard_client`, `guard_window`, `guard_pointer`, `guard_use_bind`, lock and layer guards (`trust.py:122-206, 237-281`) | YES, malformed value refuses everything (`:136-139`) | `use_bind` is refused wholesale; `hypr workspace` and `launch` are deliberately NOT confined (`server.py:910-917, 1012`) |
| Auth interlock | `HYPRUSE_AUTH_GUARD` default ON; `strict` adds the password-field a11y walk | `guard_auth_client` (`trust.py:562-573`), `guard_pointer` (`:274-281`), `guard_password_field` (`:576-595`); class list `_AUTH_CLASSES` (`:535-547`) | YES | Costs a `clients` + `activewindow` lookup on every keyboard action and a batch on every pointer action (section 4 table). A voice path must never pass `allow_auth=True`. Blind to Quickshell-based polkit agents (Omarchy 4), per project memory |
| Session lock | always on | `guard_session_lock` (`trust.py:394-437`) called from `pointer` non-move (`server.py:671`), `keyboard` (`:762`), `click_ui` (`:878`) | YES for those three; ABSENT for `hypr`, `launch`, `use_bind`, `clipboard` | see below |
| Keyboard-grabbing layer | always on | `guard_keyboard_layer` (`trust.py:440-495`): launcher or lock layer present and `window=` given means refuse | YES | see "HUD namespace" below |
| Covering layer | always on | `guard_covering_layer` refuses `click_ui` (`trust.py:513-527`); `pointer` only appends a NOTE (`server.py:613-631`) | YES | same |
| Marking | `HYPRUSE_MARK` | `note_launched` tags windows and notifies; `notify_capture` rate-limited (`trust.py:63-73, 685-697`) | YES | harmless; `hyprctl.notify` is a free on-screen toast the HUD could reuse |
| Journal | `HYPRUSE_JOURNAL`, `_TEXT`, `_MAX_BYTES` | decorator at each tool's definition (`journal.py:399-451`) | YES | A recorder, never a guard: fails toward the action (`journal.py:43-46`). Typed text is stored as length + sha256 prefix unless `_TEXT` is set (`:271-300`). Dictation is therefore not written to disk by default. The fork's own transcript log should follow the same rule |
| Beacon / kill switch | `safety.init()` | `safety.touch` inside every tool is a NO-OP until `init()` ran (`safety.py:96-99`) | only if the daemon calls `init()` | see below |

### The lock problem in detail

`trust.session_locked()` (`trust.py:372-391`) lists `/proc`, reads every `/proc/PID/comm`, and returns the first match in `{"hyprlock", "swaylock", "gtklock", "waylock"}` (`:352-359`). Measured: 13.0 ms median, 12.3 ms min on the second pass with 369 processes; 27.9 ms on the first pass under load. The docstring's "about 8 ms" is optimistic for this machine either way.

It fails OPEN in at least four ways:

1. Unreadable `/proc` returns None by design (`:377-380`, "best-effort ... never blocks anything").
2. Any locker not in the four-name list is invisible. Project memory records that Omarchy 4 replaced hyprlock with an in-process Quickshell lock screen, which this scan cannot see.
3. A crashed locker. The code's premise is "the protocol hands the session back the instant the locking client exits, so a live locker means a locked session" (`trust.py:339-341`, `ARCHITECTURE.md:176-178`). That premise is FALSE on Hyprland 0.56.2, verified on the second pass from the compositor source (local tree `scratchpad/Hyprland-0.56.2`):
   - The protocol-level flag `CSessionLockProtocol::m_locked` is set true when a lock is created (`src/protocols/SessionLock.cpp:200`) and is cleared in exactly two places: the client's explicit `unlock_and_destroy` request (`:122-139`, `m_locked = false` at `:128`) and `forceUnlock()` (`:242-250`).
   - When the lock client dies or merely destroys the object, the `CSessionLock` destructor only emits `destroyed` (`:143-145`). `CSessionLockManager`'s `destroy` listener resets its own `m_sessionLock` pointer and drops surface focus (`src/managers/SessionLockManager.cpp:90-96`) but never touches `m_locked`.
   - `CSessionLockManager::isSessionLocked()` returns `PROTO::sessionLock->isLocked()`, which is `m_locked` (`SessionLockManager.cpp:143-145`, `SessionLock.cpp:238-240`).
   - So after a locker crash the compositor remains locked (and `misc:allow_session_lock_restore` decides whether a new locker may take over, `SessionLockManager.cpp:53-59`), `hyprctl locked` keeps answering true, and hypruse's `/proc` scan answers "unlocked" because the process is gone. This is the known fail-open, now confirmed against the compositor rather than taken from project memory.
4. The guard is only wired into three tools. `hypr` (workspace switch, focus, move, CLOSE window, fullscreen, float), `launch` (arbitrary `exec`), `use_bind` (arbitrary dispatcher), and `clipboard` run with no lock check. Under MCP that is a modest risk because an agent rarely acts on a locked desk. For an always-listening microphone it is the main risk: anyone in the room, or a video playing, can say "close window" or "open terminal" to a locked machine. Hyprland does not save you here: the IPC `dispatch` handler looks up the dispatcher and calls it with no lock check at all (`src/debug/HyprCtl.cpp:1126-1158`); the only `isSessionLocked()` test near dispatchers is on the KEYBIND path, which skips binds lacking the `locked` flag (`src/managers/KeybindManager.cpp:649`). The only other use in `HyprCtl.cpp` is the `locked` query itself (`:1967-1976`, registered at `:2021`). Individual dispatchers were not audited one by one, but nothing generic stops `exec`, `workspace`, or `closewindow` issued over IPC on a locked session.

No push notification of lock state exists on socket2 in 0.56.2: I enumerated every `postEvent` name in the source tree and the list is `activewindow(v2)`, `workspace(v2)`, `moveworkspace(v2)`, `activespecial(v2)`, `activelayout`, `submap`, `screencast(v2)`, `minimized`, `configreloaded`, `openwindow`, `closewindow`, `movewindow(v2)`, `windowtitle(v2)`, `urgent`, `pin`, `openlayer`, `closelayer`, `moveintogroup`, `moveoutofgroup`, `lockgroups` (window groups, not the session), `fullscreen`, `focusedmon(v2)`, `monitoradded(v2)`, `monitorremoved(v2)`, `custom`. Lock transitions are announced only through the Wayland protocol `hyprland_lock_notifier_v1` (`PROTO::lockNotify->onLocked()` / `onUnlocked()`, `SessionLock.cpp:130, 149, 249, 259`). For a Python daemon the practical choice is polling `j/locked` over the socket (about 0.1 ms per poll).

The fix is available and cheap. On this machine `hyprctl -j locked` returns `{"locked": false}` (exit 0) and the direct socket request `j/locked` costs 0.10 to 0.14 ms. The code comment "Hyprland exposes no lock state over hyprctl either" (`trust.py:337-338`) is stale; project memory dates `hyprctl locked` to Hyprland 0.41. Fork plan:

- Replace the body of `session_locked()` with the compositor query, keep the `/proc` scan only as a fallback for old compositors, and fail CLOSED when the compositor cannot be asked (a voice daemon has no reason to act on an unreadable desktop).
- Add one daemon-level gate ahead of intent resolution: if locked, drop the utterance (better: stop capturing audio while locked, and resume on unlock). socket2 carries no lock event at all in 0.56.2 (enumerated above); `hyprland_lock_notifier_v1` (a Wayland protocol, which would need more raw-wire code in `wire.py`) is the only push source. Polling `j/locked` at about 0.1 ms is affordable at any rate, for example once per audio frame batch and once more immediately before every dispatch.
- Keep the seam the tests rely on: `tests/conftest.py:50-58` monkeypatches `trust.session_locked`, and `tests/test_e2e.py:115` has a live "matches reality" check.

### The HUD namespace trap

The fork will add a layer-shell HUD. hypruse classifies layer surfaces by namespace PREFIX (`hyprctl.py:453-467`): `wofi, rofi, fuzzel, tofi, anyrun, walker, launcher` are `launcher`; `waybar, hyprpanel, ags-, bar` are `bar`; `mako, dunst, swaync, notification` are `notifications`; `hyprlock, swaylock, lockscreen` are `lock`; `wvkbd, squeekboard, osk` are `osk`. `launcher`, `lock`, and `osk` are focus-stealing (`:477`); `launcher` and `lock` are keyboard-grabbing (`:481`). Consequences if the HUD's namespace starts with one of those prefixes:

- `keyboard(window=...)` is refused outright while the HUD exists (`trust.py:473-477`), and window-less typing gets a false "keys went to the launcher" note.
- `click_ui` is refused for any point the HUD rect covers (`trust.py:513-527`); `pointer` results get a false warning.
- `sequence` aborts when the HUD opens or closes mid-run (`server.py:1351-1354`).

Pick a namespace that classifies as `unknown`, for example one starting with the fork's own name, and avoid the prefixes `bar`, `osk`, `launcher`, `notification`. Separately from hypruse's heuristics, the HUD must use keyboard-interactivity none and an empty input region, or it will really swallow the clicks and keys the daemon sends. Remember also that a layer stays "listed" while merely tracked (`hyprctl.py:490-497`), so a hidden-but-alive HUD surface with a bad namespace would still trip the guards.

### Beacon and kill switch

`safety.init()` writes `$XDG_RUNTIME_DIR/hypruse/state.json` with pid and last action and arms `atexit` plus SIGTERM cleanup (`safety.py:87-93, 64-84`). `signal.signal` only works on the main thread and the failure is swallowed (`:83-84`), so call it from the main thread before starting a GLib or asyncio loop. Second pass, tested (`sigterm_glib.py`, no display involved): on system Python 3.14.7 with pygobject 3.56.3, a handler installed with `signal.signal(SIGTERM, ...)` DID run while an idle `GLib.MainLoop().run()` was blocking, and the process exited within about 120 ms of the signal (my polling granularity was 100 ms). So `safety.arm()`'s kill path works under a GLib main loop as long as it is installed from the main thread first. Not tested: the same under a busy GTK4 application loop or under asyncio. The documented panic actions are `hypruse stop` (signals the beacon pid, `cli.py:254-299`) and `pkill -f hypruse` (`safety.py:13`, waybar module). `pkill -f` matches on the command line, so a renamed fork binary will NOT be matched unless its path contains `hypruse`. The fork needs its own stop verb, its own bindable kill key, and should register `hinput.release_held` on shutdown exactly as `server.main` does (`server.py:1712`). Voice adds a second panic need that hypruse has no concept of: a hard mute for the microphone.

---

## 7. Global state and singletons: what makes concurrent use risky

| State | Location | Risk in a daemon |
|---|---|---|
| Env-driven flags read on every call | `trust._flag` `:52-53`, `_confine_scope` `:122-139`, `_auth_guard_on` `:550-553`, `journal.dry_run` `:92-93`, `journal.path` `:117-128`, `server._image_mode` `:207-208` | Configuration is `os.environ`, process-global. Per-command overrides (dry run for one utterance) race with any concurrent command. `verbs.run` sets `HYPRUSE_DRYRUN=1` and never clears it (`verbs.py:367-368`) |
| Import-time constants | `server.READONLY`, `server.CLIPBOARD`, `_instructions` (`server.py:124-129`) | Changing the env later does nothing; and tools never check them anyway |
| `server._plain_blocks` | `server.py:150-156` | One-way switch. A process cannot both serve MCP and use plain blocks |
| `server._last_marks` | `server.py:466, 533-539` | Single slot, last writer wins, no lock. Two `marks` calls race; a `click_ui(mark=N)` can resolve against another window's numbering (mitigated by the class check, not by address) |
| `server._app` | `server.py:1680-1691` | FastMCP singleton, irrelevant if MCP is dropped |
| `hyprctl._provider` | `hyprctl.py:119-146` | Cached probe; `dispatch` clears and re-probes on any failure (`:315-319`), which doubles the cost of every failed dispatch |
| `input._seat_lock` (RLock) | `input.py:125` | Serializes raw delivery ONLY (`type_text, key_combo, move, click, drag, scroll`). The guard-then-focus-then-sleep-then-act sequence in `keyboard` and `click_ui` is outside the lock, so two concurrent tool calls can interleave focus changes and keystrokes. `release_held` deliberately does not take the lock (`:118-124`) |
| `input._vp`, `_vk`, `_held_button` | `input.py:161-163` | One lazily created Wayland connection plus virtual pointer device for the life of the process, recreated once on `WireError` (`:206-217`). Never closed on shutdown, so the device lingers in `hyprctl devices` until the process exits. 3 s socket timeout (`wire.py:160`). A keyboard-only daemon still creates it because `_on_named_seat()` asks the pointer (`input.py:194-203`) |
| `trust._owned`, `_seat`, `_last_notify` | `trust.py:58, 600, 682` | Unsynchronized; benign under the GIL. `_owned` grows forever (addresses are never pruned in-process; only `cli_state._prune_owned` prunes, and only for verbs), and addresses can be reused by unrelated windows, which matters only under `HYPRUSE_CONFINE=launched` |
| `journal._seq`, `_origin`, `_source`, `_broken`, `_local.parent` | `journal.py:147-153` | Write and seq are locked; seq is also coordinated across processes through a flock'd `.seq` file (`:188-211`), one extra open/flock/write per recorded call. `parent` is thread-local, so sequence steps run on another thread lose attribution |
| `safety._state_path`, `_cleanups`, `_armed` | `safety.py:33-36, 61` | The beacon is ONE file per user. `_write` locks in-process only; the temp file name `state.tmp` is fixed (`:56-58`), so two hypruse processes can clobber each other's temp file. Last `init()` wins the pid; `shutdown` only unlinks its own pid (`:123-128`). Observed live on both passes: four `hypruse` MCP server processes running (pids 1191421, 1484816, 1864740, 2313959, still alive on 2026-09-21) and no `state.json` present; the directory held `cli.lock`, `cli-state.json`, `cli-state.lock`, and 14 or more `shot-*.jpeg` files of 300 to 520 KB each on tmpfs |
| Runtime dir files | `$XDG_RUNTIME_DIR/hypruse/`: `state.json`, `cli.lock`, `cli-state.json`, `cli-state.lock`, `shot-*.jpeg` pruned to newest 20 (`server.py:175-186`), `marks-*` temps | A fork that keeps the directory shares all of it with any hypruse MCP server the user also runs (this user does). Rename the directory in the fork |
| Blocking calls | everything | `wait_for` up to 60 s, `launch` up to 30 s, `sequence` up to about 30 s, `capture_stable` 2 s plus, `close_window` up to 1 s, busctl 10 s per call, hyprctl 5 s, wtype 15 s, grim 10 s, magick 15 s. Never call a tool on the audio thread, the UI thread, or the asyncio loop |

Recommended concurrency shape: one dedicated "hands" worker thread that owns all acting calls, fed by a queue with cancel-on-new-utterance semantics; one reader thread (or asyncio task) on socket2 that owns the world model; read-only queries may run anywhere once `_run` is a socket call. Treat `os.environ` flags as boot-time configuration only. If per-command dry run is wanted, thread an explicit parameter through the fork's own wrappers instead of flipping the env.

---

## 8. Dead weight and what to drop in the fork

### HYPRUSE_SEAT named seat (dead on stock Hyprland)

The code's own comment: "on stock Hyprland this finds nothing and we fall back to the default seat" (`wire.py:41-45`). Project memory: Hyprland advertises one `wl_seat`, has no `ext_transient_seat`, and upstream is against multi-seat, so the feature can never engage. Everything below is unreachable unless a compositor advertises a second seat:

- `wire.py:38-45` (`SEAT_INTERFACE`, `SEAT_EV_NAME`, `SEAT_ENV`), `:185-186` (`_globals_cache`, `_seat`), `:194-245` (`_resolve_seat`, including a `warnings.warn` fallback), `:279-282` (seat-name event handling inside `_roundtrip`), `:288-291` (`on_named_seat`), `:293-318` (`move`, `move_to`: absolute positioning built from ten `-4000,-4000` relative sweeps), `:356-526` (the keymap builders and the whole `VirtualKeyboard` class).
- `input.py:25` (the `VirtualKeyboard` import), `:138-143` and `:151-155` (named-seat branches in `type_text` and `key_combo`), `:162` (`_vk`), `:180-203` (`_with_keyboard`, `_on_named_seat`), `:246-256` (named-seat branch in `move`).
- Docs and tests: `ARCHITECTURE.md:120-121`, `CHANGELOG.md`, `tests/test_input.py:48-50`. Commits: `777af5d`, `b1a5dd9`, `3390215`, `8551694`.

It is not merely inert. On the default seat every `type_text`, `key_combo`, and `move` first calls `_with_pointer(lambda p: p.on_named_seat)`, which forces creation of the Wayland connection and a virtual pointer device even for keyboard-only work. Removing the feature removes that side effect and about 250 lines.

One salvage idea, with a warning. `VirtualKeyboard.type_text` generates a keymap in which each needed character is its own keycode (`wire.py:369-389, 462-477`), which is a spawn-free alternative to `wtype` for dictation. As written it is not reusable: it subclasses `VirtualPointer` and so creates an unused pointer, it binds a new manager and creates a new keyboard object on every call and never destroys either (`:479-496`), it sends a sync roundtrip per character (`:477`), and it has never run on the default seat. If the fork wants wire-level typing, write it fresh and test it under supervision; do not inherit this class.

### MCP and agent-distribution surface (drop if the fork is an app, not a server)

- `server.py`: `INSTRUCTIONS`, `READONLY_INSTRUCTIONS`, `DRYRUN_NOTE` (`:29-122`), `_READONLY_DOCS` and the docstring swap (`:1594-1662`), `build_app`, `app`, module `__getattr__`, `main`, `_INTERACTIVE_HELP` (`:1132-1145, 1665-1720`), and the lazy `mcp.types` branches in `_text` / `_image`.
- `pyproject.toml:23` dependency `mcp>=1.2,<2` (the only dependency), `server.json` (MCP registry manifest), `tests/test_mcp_roundtrip.py`, `tests/test_readonly.py` (registration-level read-only).
- `cli.py` `init` and the Claude Desktop / Claude Code registration (`:163-248`), `skill.py` plus `skills/hypruse/` (SKILL.md 167 lines, `references/verbs.md` 653, `references/recipes.md` 381, `agents/openai.yaml`), and the hatch `force-include` that ships the skill in the wheel (`pyproject.toml:41-42`).
- `packaging/aur/hypruse`, `packaging/aur/hypruse-git`, `RELEASING.md`, `.github/workflows/release.yml`. Per project memory a version tag triggers a PyPI publish of `hypruse`; the fork must not inherit that workflow under the same project name.
- `dist/` (built 0.11.0 wheel and sdist), `assets/demo.gif`, `scratchpad/article-there-is-only-one-cursor.md`, the untracked `cliphist` file, `skills-lock.json`, `.agents/`.

### Vision path (Jev takes text state, not images)

`screenshot.py` (318 lines), `server.screenshot`, `zoom`, `marks` image drawing, `_grab_env`, `_package`, `_deliver_capture`, `_draw_marks`, `_prune_shots`, the `then='screenshot'` branch, and the grim and ImageMagick dependencies. Keep only if the fork wants a debug capture or a "numbers overlay" rendered by its own HUD instead.

### Process-per-call scaffolding

`cli_state.py` (168 lines), `verbs._lock`, the `cli._take_beacon` dance, the journal's cross-process `.seq` counter and header dedupe (`journal.py:188-211, 525-571`), and `replay`. A daemon holds all of this in memory. The verbs CLI is still handy as a debugging surface; keeping it costs nothing at runtime.

### Stale or wrong statements to fix rather than copy

- `trust.py:336-339` and `ARCHITECTURE.md:172-176`: "Hyprland exposes no lock state over IPC". False on this 0.56.2 session (`hyprctl -j locked` works).
- `trust.py:374-375`: "about 8 ms" for the `/proc` scan; measured 13 ms (second pass) to 28 ms (first pass, loaded) here.
- `trust.py:339-341` and `ARCHITECTURE.md:176-178`: "the protocol returns the session the instant that client exits". Hyprland 0.56.2 keeps the session locked when the lock client dies (section 6, source-verified).
- `input.py:118-124`: "sync tool functions run on worker threads"; mcp 1.28.1 calls them inline.
- `pyproject.toml:13`: classifier `Environment :: X11 Applications` on a Wayland-only project.
- `input.py:239-240`: `_check_button` / `_check_xy` private aliases with a comment saying they only predate the dry run.

### Keep, nearly as is

`hyprctl.py` (swap `_run` for a socket client), `events.py`, `trust.py` (fix the lock source, keep the guard shapes), `safety.py` (rename paths), `journal.py`, `session.py`, the pointer half of `wire.py` and `input.py`, `a11y.py` logic with a new transport, and the tool functions `desktop, hypr, launch, use_bind, binds, keyboard, pointer, click_ui, ui, wait_for, sequence`. The test suite is an asset: 652 tests, all hermetic by construction (`tests/conftest.py:1-8` forbids reading live state; three autouse fixtures neutralize the journal env, the provider probe, and the lock scan). Tests patch at `hyprctl._run`, `trust.session_locked`, and `a11y._busctl`, which are exactly the three seams the fork wants to re-implement, so the new transports can land under the existing tests.

---

## 9. Gaps between what hypruse offers and what voice control needs

Facts from the code, framed for the planner:

- `hypr` has six actions. There is no move-focus-by-direction, resize, swap, split, pin, group, special-workspace toggle, monitor focus, kill, or layout message. `use_bind` covers whatever the user already bound (125 binds here, 116 described), and raw `hyprctl.dispatch` covers the rest on hyprlang. A guarded, journaled `hypr`-style wrapper for a whitelisted dispatcher table is the natural extension and keeps Lua users working only if each new dispatcher also gets a `_lua_*` builder (`hyprctl.py:240-250`).
- The workspace argument is validated against an injection blacklist because on Lua sessions it is evaluated inside the compositor (`server.py:910-930`). Any new free-text dispatcher argument sourced from speech needs the same treatment, and `lua_str` (`hyprctl.py:160-174`) is the only sanctioned way to embed text on the Lua path.
- There is no app catalog. `launch` takes a shell command string.
- There is no "previous window" notion in the snapshot (no `focusHistoryID`).
- There is no persistent world model; each tool re-queries. socket2 plus the socket query path make one cheap to add.
- There is no text selection, no window-content reading besides a11y values, and no OCR (project memory: OCR was evaluated and dropped).
- Output is prose aimed at an LLM. A voice HUD wants structured outcomes: reuse `verbs.exit_for` and the exception classes to derive `delivered | refused | no_result | error`.

---

## 10. Not measured, not verified

- Any a11y tree walk latency (registry broken on this machine, see section 5).
- `VirtualPointer` creation cost. The docstring says about 1 ms (`wire.py:148`). Creating one registers a device with the compositor, which I treated as out of bounds for a do-not-touch-the-desktop lane. The repo's own seat-safe e2e test does exactly this (`tests/test_e2e.py:96`). All three passes left it alone.
- Any acting latency: dispatch, click, wtype, launch. README claims dispatch about 10 to 20 ms and a full-monitor JPEG capture about 65 ms (`README.md:255-256`); both are project claims, not re-measured.
- The ext-session-lock-v1 protocol TEXT on lock-client death was not re-read (no web). What was verified instead is Hyprland 0.56.2's actual behavior from its source: the locked flag survives lock-client death (section 6).
- Whether individual dispatchers (`exec`, `closewindow`, and so on) contain their own lock checks. The generic IPC dispatch path has none (`HyprCtl.cpp:1126-1158`); dispatchers were not audited one by one.
- A dispatch over the raw socket was never SENT (by design). Its spelling is verified from the hyprctl client source (`/dispatch name args`, section 4), which is primary but not an end-to-end test.
- Python SIGTERM handling under a busy GTK4 loop or under asyncio (the idle `GLib.MainLoop` case was tested and works, section 6).
- Truly idle-machine timings. The three passes ran at loadavg 22 to 28, 11 to 18, and about 11 (other research agents share the 4C/8T box). Subprocess-bound numbers roughly halved between the first and the later passes; expect the observed minimums (about 11 ms per hyprctl fork) on an idle machine, and the socket-bound numbers to stay where they are.
- The first Hyprland version that shipped `hyprctl locked` (project memory says 0.41; only 0.56.2 was checked here).

---

## Verification

Independent skeptical pass, 2026-09-20, against the same tree (`9a84bdb`, clean apart from untracked files). Method: re-read the cited code myself, wrote my own read-only scripts (`scratchpad/verify-hypruse-map/v_ipc.py`, `v_import.py`) without reading the researcher's bench scripts, and cross-checked the lock claim against the Hyprland 0.56.2 source tarball in `scratchpad/Hyprland-0.56.2`. Nothing dispatched, typed, clicked, focused or captured. No Jev call. Loadavg during my runs was 11 to 18 (lower than the researcher's 22 to 28), so my wall-clock numbers are lower across the board; that is the main source of disagreement below.

Lane trust: high. Every structural and file:line claim I checked held. The only corrections are to absolute timings (load-inflated by roughly 2x to 3x, as the researcher warned) and to one over-strong equality statement.

### V1. All 15 tools are sync, journaled, no async in the package; FastMCP 1.28.1 runs sync tools inline. CONFIRMED

- `inspect.iscoroutinefunction` is False for all 15 on system Python 3.14.7 and venv 3.13; `inspect.unwrap` def lines are exactly 190, 268, 290, 400, 502, 635, 719, 801, 980, 1093, 1149, 1166, 1191, 1287, 1481. Each is preceded by `@journal.journaled(...)` on the line above.
- `grep -rn "async \|await \|asyncio\|anyio" src/hypruse` returns one hit, a comment in `safety.py:78`. No `Thread(`, no executor anywhere in `src/hypruse`; only locks (`journal.py:147-151`, `input.py:125`, `safety.py:36`).
- `func_metadata.py:93-96` is `if fn_is_async: return await fn(...) else: return fn(...)`. I additionally grepped all of `mcp/server/` for `to_thread|run_sync|run_in_executor`: the only hits are file resources in `fastmcp/resources/types.py`, none on the tool path (`tools/base.py:93-106` awaits `call_fn_with_arg_validation` directly). So sync tools do block the event loop, and the comment at `input.py:118-124` is stale for this mcp version.

### V2. Importing `hypruse.server` does not import mcp; import timings. CONFIRMED (structure), numbers are load-inflated

- System Python 3.14.7, `PYTHONPATH=src`, `PYTHONDONTWRITEBYTECODE=1`: after importing `server, hyprctl, events, trust, journal, safety, session, a11y, input, wire, verbs`, `mcp`, `pydantic` and `anyio` are all absent from `sys.modules`, and zero newly loaded modules come from site-packages. The control library genuinely runs on the system interpreter with no third-party dependency. `pyproject.toml:6` is `requires-python >=3.11`, `:23` is the single `mcp>=1.2,<2` dependency.
- `server.py` imports of mcp are at lines 162, 170, 1667 only, all inside function bodies.
- My timings at loadavg 11 to 13: in-process import 94 to 143 ms on 3.14 without pyc (researcher: 418 ms), 68 to 74 ms on venv 3.13 (researcher: 86 ms). Cold process `import hypruse.server` 99 to 165 ms (researcher: 303 to 569 ms median). Cold `server.app()` FastMCP build 1.05 to 1.62 s (researcher: 3.4 to 3.8 s). The ratio (mcp-free import is about 10x cheaper) holds. Correction for the plan: quote "about 0.1 to 0.2 s cold, versus 1 to 1.6 s with FastMCP at moderate load", which also matches ARCHITECTURE.md's "a few hundred milliseconds versus about two seconds".

### V3. hyprctl is a subprocess per call; a direct socket batch is an order of magnitude faster and parses with the existing `snapshot_from`. PARTIALLY CORRECT (mechanism confirmed, numbers lower, equality is conditional)

- Mechanism confirmed by my own client: one AF_UNIX stream connection to `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`, send `[[BATCH]]j/monitors;j/workspaces;j/clients;j/activewindow;j/cursorpos;j/layers`, read to EOF, get six concatenated JSON documents (list, list, list, dict, dict, dict) that feed `hyprctl.snapshot_from(*docs)` positionally. An unknown command returns the literal `unknown request`.
- My timings at loadavg about 17: subprocess `hyprctl.snapshot()` 15.1 ms median, 11.3 min (researcher 35.6 / 24.9). Subprocess `query("activewindow")` 10.8 / 9.1 ms (researcher 26 to 34 / 15 to 16). Direct socket snapshot 1.00 ms median, 0.66 min, n=50 (researcher 3.53 / 1.35). Direct `j/activewindow` 0.09 / 0.064 ms, n=100 (researcher 0.14 to 0.16). So the speedup is real: about 15x for the snapshot and about 100x for a single query. The researcher's absolute figures are pessimistic upper bounds, not typical values.
- Correction on "compared equal": in my run `direct_snapshot() == hyprctl.snapshot()` was False, because the human moved the mouse between the two calls and `cursor` differed. With `cursor` masked the dicts were equal. The statement should read "equal except for fields that change between two calls (cursor, and in principle focus or titles)". This matters for the fork's test design: a live parity test must mask volatile fields or it will flake.
- Still unverified by either of us: the socket spelling of a dispatch request (not sent, by design).

### V4. Lock detection is a /proc comm scan that fails open and gates only pointer (non-move), keyboard and click_ui. CONFIRMED (timing lower here)

- `trust.py:352-359` is the four-name frozenset; `:372-391` lists `/proc`, returns None on `OSError` from `scandir`, and skips unreadable `comm` files. It cannot see any other locker.
- `grep -n "session_locked\|guard_session_lock" src/hypruse/*.py` finds exactly three call sites outside trust.py: `server.py:671` (inside `if action != "move":`), `:762` (keyboard), `:878` (click_ui). `hypr` (980 to 1048), `launch` (1093 to 1129), `use_bind` (1191 to 1223) and `clipboard` (1166 to 1187) contain no lock check. `sequence` steps inherit the gate only when the step op is pointer, keyboard or click_ui.
- My timing: 11.8 ms median, 10.9 min at loadavg about 17 with about 1,550 tasks (researcher 27.9 ms). Either way it is roughly 200x the cost of the socket `j/locked` query.

### V5. `hyprctl -j locked` exists on 0.56.2; the "no lock state over IPC" statements are stale. CONFIRMED, with an independent primary source

- Live: `hyprctl -j locked` printed `{"locked": false}` with exit 0; plain `hyprctl locked` printed `false`. Over the raw socket `j/locked` measured 0.056 ms median, 0.046 min (researcher 0.10 to 0.14).
- Source, which the researcher did not cite: `Hyprland-0.56.2/src/debug/HyprCtl.cpp:1967-1976` defines `getIsLocked`, returning `g_pSessionLockManager->isSessionLocked()`, and `:2021` registers it as `registerCommand(SHyprCtlCommand{"locked", true, getIsLocked})`. So the answer comes from the compositor's session-lock manager, which covers any ext-session-lock-v1 client (including lockers outside the four-name list) rather than a process name.
- The stale text is where the researcher says: `trust.py:336-339` ("Hyprland exposes no lock state over hyprctl either") and `ARCHITECTURE.md:175-176` ("Hyprland exposes no lock state over IPC").
- Not verified: the first Hyprland version that shipped `locked` (the report's "0.41" comes from project memory), and whether `isSessionLocked()` stays true after a locker crash. A published fork should keep the /proc scan as a fallback for old compositors and treat an unanswerable query as locked.

### V6. HYPRUSE_READONLY and HYPRUSE_CLIPBOARD are not enforced inside the tool functions. CONFIRMED

- Every reference to `READONLY` / `CLIPBOARD` in `server.py`: the two import-time constants (`:124-125`), the instructions choice (`:127-128`), a hint string inside `marks` (`:542`, cosmetic), the docstring swap (`:1658`), and registration in `build_app` (`:1672`, `:1675`). No acting tool body reads either one. The `clipboard` body (`:1166-1187`) goes straight from `safety.touch` to `clip.read()` / `clip.write()`.
- The only runtime refusals are in `verbs.run` (`verbs.py:351-363`) and in `cli.py` (`:115`, `:530`, `:674`, doctor and replay). A daemon calling `server.clipboard("read")` or `server.pointer(...)` directly gets no refusal. The fork must carry its own gate.

### Bonus checks (beyond the six)

- verbs.run env mutation: CONFIRMED. `verbs.py:366` sets `HYPRUSE_SCREENSHOT_MODE=file` unconditionally and `:367-368` sets `HYPRUSE_DRYRUN=1` when `dry_run`, neither is ever unset. Small precision fix: the flock and beacon are taken only when the call is acting (`:376-384`); `cli_state.restore()` / `save()` run on every call (`:387, 400`).
- HUD namespace trap: CONFIRMED with one precision fix. `_LAYER_KINDS` is at `hyprctl.py:453-459`, matched with `ns.startswith(p)` on the lowercased namespace. `KEYBOARD_GRABBING_KINDS` is `{launcher, lock}` only, so an `osk...` namespace would NOT make `keyboard(window=...)` refuse; it would only trip `guard_covering_layer` (click_ui refused where the HUD rect covers the point, `trust.py:305-324`) and `sequence`'s `_structural` abort (`server.py:1351-1354`). `launcher`, `rofi`, `wofi`, `fuzzel`, `tofi`, `anyrun`, `walker`, `hyprlock`, `swaylock`, `lockscreen` trip all three. A namespace beginning with the fork's own name is safe as long as it does not begin with `bar`, `osk`, `ags-`, `notification`, or any name above. `hypr` alone is not a listed prefix.
- Snapshot shape: CONFIRMED. Live keys match the claim exactly (windows: address, at, class, floating, pid, size, title, workspace; monitors: active_workspace, focused, geometry, name, scale; workspaces: id, monitor, name, visible, windows; layers: geometry, kind, level, monitor, namespace). Raw `j/clients` on 0.56.2 carries `focusHistoryID, initialClass, initialTitle, pinned, grouped, tags, monitor, stableId, xwayland, visible` and the rest of the listed set. Size now: 2,162 JSON chars for 7 windows, 5 workspaces, 1 layer (researcher 2,269; titles changed).

### Not independently verifiable in this pass

- The AT-SPI registry breakage and the busctl spawn timing (I did not re-probe the a11y bus; it is a transient machine state and the claim is labeled local measurement).
- The dead-code line ranges for HYPRUSE_SEAT in `wire.py` and `input.py` (not re-read line by line).
- Anything involving acting latency, since neither pass was allowed to act.

---

## Second pass (2026-09-21): what changed and how far to trust this file

Scope of the second pass: a full independent re-read of `ARCHITECTURE.md`, `pyproject.toml`, `server.py` (all 1,720 lines), `hyprctl.py`, `events.py`, `a11y.py`, `trust.py`, `safety.py`, `session.py`, `journal.py`, `input.py`, `verbs.py`, `cli_state.py`, `wire.py` (pointer half and `VirtualKeyboard`), the relevant parts of `screenshot.py`, `clipboard.py`, `cli.py`, and `tests/conftest.py`; fresh measurement scripts; the unit suite (652 passed, 13 deselected, 9.25 s); and targeted reading of the Hyprland 0.56.2 source for the questions the first pass could not answer. Nothing dispatched, typed, clicked, focused, captured, or created an input device.

Every structural claim and file:line reference I spot-checked against my own reading held, including all 15 tool signatures and def lines, the three lock call sites (`server.py:671, 762, 878`), the READONLY/CLIPBOARD non-enforcement, the dry-run barrier sites (`input.py:134, 148, 244, 266, 277, 306`, `hyprctl.py:300`, `clipboard.py:47`), the `HYPRUSE_SEAT` line ranges in `wire.py` and `input.py` (which the verification pass had not re-read, now confirmed line by line), the `_LAYER_KINDS` prefixes, and the snapshot field list.

Corrections made in this revision:

1. Fork counts: `keyboard(..., window=addr)` is 3 hyprctl forks, not 4; `click_ui(name)` is 6 (5 with `window=`), not 7. Cause: `_resolve_window` with an explicit address issues only the `clients` query (`server.py:316-327`).
2. Test line references: `tests/test_e2e.py:96` (virtual pointer handshake) and `:115` (live lock check), not `:93` and `:103`.
3. Timings restated from a lower-load run; first-pass figures kept and labeled.

Open items closed from a local primary source (Hyprland 0.56.2 source tree):

1. Lock survives locker death: `m_locked` is cleared only by `unlock_and_destroy` or `forceUnlock` (`src/protocols/SessionLock.cpp:122-139, 242-250`); the destroy path leaves it set (`:143-145`, `src/managers/SessionLockManager.cpp:90-96, 143-145`). hypruse's `/proc` scan therefore fails open on a crashed locker while `hyprctl locked` stays correct.
2. IPC dispatch is not lock-gated by the compositor (`src/debug/HyprCtl.cpp:1126-1158`; keybind-only check at `src/managers/KeybindManager.cpp:649`).
3. Raw-socket request spelling, including dispatch: `flags + "/" + space-joined args` (`hyprctl/src/main.cpp:398-480`), batch `[[BATCH]]...` (`:339-349`).
4. No socket2 lock event exists (full `postEvent` name enumeration, section 6).
5. `Atspi-2.0.typelib` is installed; a Python `SIGTERM` handler fires under an idle `GLib.MainLoop` with pygobject 3.56.3.

Still open after three passes: any a11y walk timing (bus broken on all three dates), `VirtualPointer` creation cost, and every acting latency (dispatch, click, `wtype`, `launch`). None can be closed without either repairing the AT-SPI bus or acting on the desktop, both out of bounds for this lane.

---

## Verification (third independent pass, 2026-09-21, against the second-pass key facts)

Scope: the six key facts a build plan leans on hardest, each attacked with a check the researcher did not use. Method: my own read-only scripts in `scratchpad/verify/` (`v_sock.py`, `v_import.py`, `v_cpu.py`, `v_binds.py`), a fresh re-read of the cited hypruse lines, and a fresh re-read of the Hyprland 0.56.2 source tree in `scratchpad/Hyprland-0.56.2`. I did not read the researcher's bench scripts. Only `j/` queries went over the socket. Nothing dispatched, typed, clicked, focused or captured. No Jev call. Loadavg during my runs was 13.5 to 18.6 (other lanes were benchmarking), which is higher than the researcher's "about 11", so my wall-clock figures are slower than theirs.

Lane trust: high. All six structural claims held. The only corrections are to absolute timings (load dependent, not reproducible to within 2x on this shared machine) and a few details the plan should add.

### T1. 15 sync tools at the stated def lines, journaled passthrough, FastMCP inline call. CONFIRMED

- `grep -n "^def \|^@" server.py`: def lines are exactly 190, 268, 290, 400, 502, 635, 719, 801, 980, 1093, 1149, 1166, 1191, 1287, 1481, each with `@journal.journaled(...)` on the line above.
- `grep -rn "async \|await \|asyncio" src/hypruse/` returns nothing at all in code position.
- `journal.py:421-422` is `if not enabled(): return fn(*args, **kwargs)`; `enabled()` (`:131-132`) is `path() is not None`, evaluated per call, so the passthrough is per call, not per import.
- `mcp-1.28.1` `func_metadata.py:93-96` is `if fn_is_async: return await fn(...) else: return fn(...)`. Sync tools run inline on the loop.

### T2. Import cost and Python 3.14 viability. PARTIALLY CORRECT (structure confirmed, absolute ms not reproduced)

- Confirmed on system Python 3.14.7 with `PYTHONPATH=src`: after `import hypruse.server`, no module from `mcp, pydantic, pydantic_core, anyio, httpx, starlette` is in `sys.modules`, and the only site-packages module loaded is `_distutils_hack` (a setuptools .pth artifact, not a hypruse dependency). `pyproject.toml:23` is `dependencies = ["mcp>=1.2,<2"]`, `:6` is `requires-python = ">=3.11"`.
- Not reproduced: the 93 to 97 ms figure. At loadavg 17 to 18.6 I measured cold-process wall clock, n=9: bare interpreter 67 ms (3.14) and 57 ms (3.13); `import hypruse.server` 239 ms median, 213 min (3.14) and 183 ms median, 172 min (3.13 venv); `server.app()` 1,923 ms; `import mcp.types` 2,875 ms. Child CPU time (user+sys via `wait4`, less load sensitive) told the same story: 219 ms for the 3.14 import, 1,659 ms for `server.app()`.
- Correction for the plan: quote the import as "0.1 to 0.25 s cold depending on load, and 8x to 10x cheaper than building FastMCP". The ratio is stable across all three passes; the absolute number is not. Python 3.14 was slower than 3.13 in my run (239 vs 183 ms), the opposite of the researcher's 93 vs 97; treat the two interpreters as equivalent.

### T3. hyprctl forks per call; raw socket is sub-millisecond; reply parses with `snapshot_from`. CONFIRMED (numbers same order, load dependent)

- Code: `hyprctl.py:35-47` is `shutil.which` then `subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=5)`. Every other entry point goes through `_run`: `query` (:52), `batch_query` (:65), provider probe (:134), `eval` (:276), `dispatch` (:286), `keyword` (:326), `notify` (:369). No socket code in the file.
- My numbers at loadavg 13.5: `hyprctl.snapshot()` 16.2 ms median, 13.8 min (researcher 19.7 / 11.1); `hyprctl.query("activewindow")` 16.4 ms; bare `hyprctl --help` spawn 14.9 ms (researcher 13). Raw socket six-query batch 0.74 ms median, 0.43 min, n=50 (researcher 0.3); `j/activewindow` 0.18 ms; `j/locked` 0.12 ms. So the speedup I can vouch for is about 20x for the snapshot and about 90x for a single query. The researcher's 0.3 ms batch figure is a quiet-machine value; budget 1 ms.
- The batch reply decoded into six JSON documents and fed `hyprctl.snapshot_from` unchanged: keys `monitors, workspaces, windows, active_window, cursor, layers`, 1,976 compact chars for 7 windows, 5 workspaces, 1 layer (researcher: 1,975). Raw `j/clients` carries `focusHistoryID` (observed exactly 0..6), `initialClass`, `initialTitle`, `pinned`, `grouped`, `tags`, `stableId`, all absent from the snapshot's window dicts.

### T4. Raw socket request format. CONFIRMED from source, with three details to add

- Client: `hyprctl/src/main.cpp:398-480` builds `fullArgs + "/" + space-joined args` after popping the trailing space; `:339-349` builds `"[[BATCH]]" + commands` and, under `-j`, rewrites `;\s*` to `;j/`. Server: `src/debug/HyprCtl.cpp:1126-1158` strips the first word, looks the dispatcher up in `m_dispatchers`, returns `"Invalid dispatcher"`, `"ok"`, or the dispatcher's error string; `:2124` returns `"unknown request"` (I also got that literal live for `j/definitelynotacommand`). One request per connection: `hyprCtlFDTick` (`:2232-2319`) accepts, reads, replies, closes.
- Add 1: batch replies are joined with the delimiter `"\n\n\n"` (`dispatchBatch`), and the splitter ignores `;` inside `[...]`. A daemon batching dispatches should split on that delimiter; JSON batches can keep using `raw_decode`.
- Add 2: the compositor reads the request in 1,023-byte chunks and stops at the first short read, on its main event loop, after a blocking `poll(..., 5000)`. So send the whole request in one `sendall` immediately after `connect`, keep it small, and never hold an idle connection open: a client that connects and stalls freezes the compositor for up to 5 s.
- Add 3: under a Lua config `dispatchRequest` wraps the argument as `return hl.dispatch(<arg>)` (`:1130-1141`), so the legacy `/dispatch workspace 3` spelling is hyprlang only. This machine reports `"configProvider": "hyprlang"` over `j/status`.
- Still true: no dispatch was sent over the raw socket by any pass. The spelling is source-derived.

### T5. Compositor lock flag survives locker death; `j/locked` works. CONFIRMED

- Every write to `CSessionLockProtocol::m_locked` in the tree: set true at `SessionLock.cpp:200` (`onLock`) and `:253` (`forceLock`); set false only at `:128` (inside the `unlock_and_destroy` handler) and `:243` (`forceUnlock`). `grep -rn m_locked src/` finds no other writer (the other hits are `LockNotify` and `PointerConstraints`, different members). The destructor (`:143-145`) only emits `destroyed`, and the manager's listener for it (`SessionLockManager.cpp:90-96`) only resets `m_sessionLock`. `isSessionLocked()` (`:143-145`) returns `PROTO::sessionLock->isLocked()`, which is `m_locked`.
- `getIsLocked` at `HyprCtl.cpp:1967-1976`, registered at `:2021`. Live over the raw socket: `j/locked` returned `{"locked": false}`.
- Extra: the only caller of `CSessionLockManager::forceUnlock()` is a Lua binding (`src/config/lua/bindings/LuaBindingsToplevel.cpp:347`). On this hyprlang session a crashed locker leaves the flag set until a new locker takes over and unlocks.
- `trust.py:336-341` and `ARCHITECTURE.md:172-178` say what the researcher says they say. `trust.session_locked()` measured 36 ms median, 20.6 min here with 379 processes at loadavg 18 (researcher 13.0 ms); either way 100x to 300x the socket query.
- Also confirmed while here (claim 8): the only `isSessionLocked()` uses in the dispatch and bind paths are `HyprCtl.cpp:1968` and `KeybindManager.cpp:649`; my own enumeration of `postEvent` names found no session lock event (`lockgroups` is about window groups).

### T6. READONLY and CLIPBOARD are registration-only. CONFIRMED

- All uses of the bare names in `server.py`: `:124`, `:125`, `:127`, `:128`, `:542` (a hint string in `marks`), `:1658`, `:1672`, `:1675`. The `clipboard` body (`:1166-1187`) goes from `safety.touch` straight to `clip.read()` / `clip.write()`. Runtime refusals exist only in `verbs.run` (`verbs.py:351-363`) and `cli.py` (`:115`, `:530`, `:674`).
- The per-call flags are per call as claimed: `journal.dry_run()` (`journal.py:92-93`), `HYPRUSE_CONFINE` (`trust.py:126`), `HYPRUSE_AUTH_GUARD` (`trust.py:553`, `:559`), and the generic `_flag` (`trust.py:52-53`) all read `os.environ` inside the function.

### Bonus (beyond the six)

- Claim 15 (dispatchers and binds): CONFIRMED. `_HYPR_PLANS` has six actions (`server.py:969-976`), `_LUA_DISPATCH` has nine entries (`hyprctl.py:240-250`). `hyprctl.binds()` returns 125 entries, 116 with descriptions, 0 Lua. Two details to add: raw `j/binds` has 131 entries (120 described); `parse_binds` drops the 6 mouse binds. And 4 combos are duplicated (`XF86AudioPlay`, `XF86AudioNext`, `XF86AudioPrev`, `SUPER+SHIFT+P`), while `find_bind` returns the first match, so a Jev choice question over binds must key options by index or by combo plus arg, not by combo alone.
- Claim 14 (layer namespace trap): CONFIRMED. `_LAYER_KINDS` at `hyprctl.py:453-459`, `startswith` on the lowercased namespace. The osk kind's prefixes are `wvkbd`, `squeekboard` and `osk` (the key fact lists only `osk`). `bar`, `waybar`, `hyprpanel`, `ags-`, `mako`, `dunst`, `swaync`, `notification` classify as bar or notifications, which no guard acts on, but the HUD should still avoid them so `desktop` does not mislabel it.

### Not checked in this pass

- a11y/busctl claims (bus is broken, transient machine state), the fork-count table (inference, needs tracing), the HYPRUSE_SEAT line ranges, and the concurrency inventory. No acting latency of any kind.
