# Research lane: hypr-ipc (Hyprland 0.56 control surface)

Date: 2026-09-21. Status: COMPLETE. Target: Hyprland 0.56.2 on Arch, hyprlang config provider active. All live interaction with the compositor was read-only (queries only, no dispatch, no keyword).

## 1. Local measurements (this machine, read-only requests only)

Environment verified locally: `hyprctl version` reports Hyprland 0.56.2, tag v0.56.2, commit efb50993780079460b0cbed1363e2166a2de1d9f, built 2026-08-05, Hyprlang 0.6.8, Hyprutils 0.14.0, Aquamarine 0.14.0. Python 3.14.7. i5-8350U.
Socket dir: `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/` contains `.socket.sock`, `.socket2.sock`, `hyprland.lock`, `hyprland.log` (plus `.hyprsunset.sock` from hyprsunset).

Benchmark script: `scratchpad/bench_ipc.py`. Method: for each request open a fresh AF_UNIX SOCK_STREAM connection, sendall, recv until EOF, close. 300 iterations for socket, 100 for subprocess. Times in milliseconds, wall clock via perf_counter_ns. Desktop was live but mostly idle.

| Path | Request | reply bytes | min | p50 | p95 | max |
|---|---|---|---|---|---|---|
| direct socket | `j/activewindow` | 861 | 0.057 | 0.095 | 0.161 | 2.997 |
| direct socket | `j/clients` (approx 10 windows) | 6248 | 0.100 | 0.161 | 0.237 | 0.356 |
| direct socket | `j/monitors` | 1127 | 0.042 | 0.046 | 0.083 | 0.119 |
| direct socket | `j/workspaces` | 1470 | 0.044 | 0.047 | 0.126 | 0.935 |
| direct socket | `[[BATCH]]j/activewindow;j/clients;j/monitors;j/workspaces` | 9715 | 0.111 | 0.151 | 0.307 | 1.142 |
| direct socket | `j/version` | 714 | 0.081 | 0.121 | 0.172 | 0.206 |
| spawn `hyprctl -j activewindow` | | | 5.225 | 5.989 | 9.143 | 13.645 |
| spawn `hyprctl -j clients` | | | 4.956 | 6.044 | 8.032 | 12.726 |
| spawn `hyprctl --batch -j "activewindow;clients;monitors;workspaces"` | | | 5.484 | 6.410 | 9.500 | 16.269 |
| spawn `/usr/bin/true` (fork/exec floor from Python subprocess) | | | 0.689 | 0.946 | 1.327 | 10.122 |

Takeaways (local measurement):
- Direct socket is roughly 40x to 60x faster than spawning hyprctl at p50 (0.1 to 0.16 ms vs about 6 ms). Both are negligible next to a network round trip to Jev, but hyprctl spawn cost adds up when snapshotting state (several queries) and has a fatter tail (13 to 16 ms max).
- One `[[BATCH]]` of four queries (0.151 ms p50) costs less than the four queries issued separately (0.095+0.161+0.046+0.047 = 0.349 ms). Batch a full state snapshot in one connection.
- hyprctl spawn cost is about 5 ms above the bare fork/exec floor of about 1 ms, so most of it is dynamic linking and hyprctl startup, not the IPC itself.
- Caveat: these are idle-compositor numbers. Hyprland serves the request socket from its main event loop, so a request issued while the compositor is busy rendering a heavy frame will wait for that loop iteration. Not measured under load.

Reply format facts observed locally on 0.56.2:
- Each connection is one request then EOF: the server closes after replying. There is no persistent request connection.
- `[[BATCH]]a;b` replies are the individual replies concatenated, separated by `\n\n\n` (observed between two JSON objects). For JSON batches you must split on that separator or use a streaming JSON decoder (`json.JSONDecoder().raw_decode` in a loop); the whole reply is NOT one JSON array.
- Flags prefix the command with a slash: `j/activewindow`. In a batch, each sub-command carries its own flag: `[[BATCH]]j/activewindow;j/clients`.
- Unknown request returns the literal text `unknown request` (not JSON, even with `j/`).
- `locked` exists in 0.56.2: plain reply is `false`, `j/locked` replies `{"locked": false}`.
- `j/getoption animations:enabled` replies `{"option": "animations:enabled", "int": 1, "set": true }`.
- `submap` replies `default\n`. `j/layouts` replies `unknown request` on 0.56.2 (the layouts request does not exist under that name here).

## 2. Dispatcher registry in 0.56.2 (primary source: Hyprland source at tag v0.56.2)

Big architectural fact: in 0.56 the dispatcher implementations no longer live in `KeybindManager.cpp`. `CKeybindManager` populates `m_dispatchers` from a fixed name list, and every entry forwards to `Config::Legacy::translator()->run(name, args)` (src/managers/KeybindManager.cpp line 113 at v0.56.2). The legacy string dispatchers are implemented in `src/config/legacy/DispatcherTranslator.cpp`, which parses the old string argument grammar and calls typed actions in `src/config/shared/actions/ConfigActions.cpp`. The Lua config (`hl.dsp.*`) calls the same typed actions. So legacy string dispatch over IPC is a compatibility layer over a new typed action core, and it is still fully registered in 0.56.2.

Complete legacy dispatcher name list registered in 0.56.2 (DispatcherTranslator.cpp lines 797 to 867, 71 names):
exec, execr, killactive, forcekillactive, closewindow, killwindow, signal, signalwindow, togglefloating, setfloating, settiled, workspace, renameworkspace, fullscreen, fullscreenstate, movetoworkspace, movetoworkspacesilent, pseudo, movefocus, movewindow, swapwindow, centerwindow, togglegroup, changegroupactive, movegroupwindow, focusmonitor, movecursortocorner, movecursor, workspaceopt, exit, movecurrentworkspacetomonitor, focusworkspaceoncurrentmonitor, moveworkspacetomonitor, togglespecialworkspace, forcerendererreload, resizeactive, moveactive, cyclenext, focuswindowbyclass (alias of focuswindow), focuswindow, tagwindow, toggleswallow, submap, pass, sendshortcut, sendkeystate, layoutmsg, dpms, movewindowpixel, resizewindowpixel, swapnext, swapactiveworkspaces, pin, mouse, bringactivetotop, alterzorder, focusurgentorlast, focuscurrentorlast, lockgroups, lockactivegroup, moveintogroup, moveintoorcreategroup, moveoutofgroup, movewindoworgroup, setignoregrouplock (registered as a no-op, source comment says deprecated), denywindowfromgroup, event, global, setprop, forceidle, releaseinputcapture.

NOT registered in 0.56.2 (so `dispatch X` returns an error): togglesplit, swapsplit, splitratio, toggleopaque. The dwindle/master specific ones are reached through `layoutmsg` (see section 4).

Lua-side equivalents (from the locally installed, autogenerated `/usr/share/hypr/stubs/hl.meta.lua`): `hl.dsp.{dpms, event, exec_cmd, exec_raw, exit, focus, force_idle, force_renderer_reload, global, layout, no_op, pass, release_input_capture, send_key_state, send_shortcut, submap}`, `hl.dsp.window.{alter_zorder, bring_to_top, center, clear_tags, close, cycle_next, deny_from_group, drag, float, fullscreen, fullscreen_state, kill, move, pin, pseudo, resize, set_prop, signal, swap, tag, toggle_swallow}`, `hl.dsp.workspace.{change_id, move, rename, swap_monitors, toggle_special}`, `hl.dsp.group.{active, lock, lock_active, move_window, next, prev, toggle}`, `hl.dsp.cursor.{move, move_to_corner}`. These are invoked with `hl.dispatch(hl.dsp.window.close(...))` and are reachable over IPC through the new `eval <lua code>` request (listed in `hyprctl --help` on 0.56.2).

## 3. Window selector grammar in 0.56.2 (primary: src/desktop/state/ViewQuery.cpp `CViewQuery::bySelector`, v0.56.2)

All legacy dispatchers that accept a window use one selector parser. Order of checks:
- `active` (prefix match): the focused window.
- `floating` / `tiled` (prefix match): first mapped floating/tiled window on the focused window's workspace.
- `class:<RE2 regex>`, `initialclass:<regex>`, `title:<regex>`, `initialtitle:<regex>`, `tag:<regex>`: regex selectors. They use `RE2::FullMatch`, so the regex must match the WHOLE string. `title:Firefox` does not match "Docs - Mozilla Firefox"; use `title:.*Firefox.*`. RE2 means no lookahead/backreferences.
- `stableid:<hex>`: exact match on the window's stable id formatted as lowercase hex without 0x.
- `address:0x<hex>`: exact match on the window pointer formatted `0x{:x}`. This is the `address` field in `j/clients`.
- `pid:<decimal>`: exact match.
- No prefix: treated as a class regex (default mode is MODE_CLASS_REGEX).
- First match in the compositor's window list wins; there is no "best" or "most recent" ranking. Unmapped windows are skipped.

Design consequence for the voice daemon: resolve the target yourself from a `j/clients` snapshot (Jev `choice` over the window list), then always dispatch with `address:0x...`. Never hand a spoken app name straight to `class:` because of FullMatch and first-match semantics. Addresses are pointers and can in principle be reused after a window closes, so re-validate against a fresh snapshot or use `stableid:` if the clients JSON exposes it (checked in section 1b below).

### 1b. Extra local observations
- `j/clients` objects on 0.56.2 carry these keys: acceptsInput, address, allowedOverFullscreen, at, class, contentType, floating, focusHistoryID, fullscreen, fullscreenClient, fullscreenHandler, grouped, hidden, inhibitingIdle, initialClass, initialTitle, mapped, monitor, pid, pinFullscreened, pinned, size, stableId, swallowing, tags, tearingHint, title, visible, workspace, xdgDescription, xdgTag, xwayland. `stableId` is present, so `stableid:` selectors are usable. `focusHistoryID` (0 = current, 1 = previous, ...) is the cheapest "the last window" / "previous window" signal for Jev state.
- `j/activeworkspace` includes `tiledLayout` (here `dwindle`), so the daemon can know per workspace which layoutmsg vocabulary applies.
- `j/status` replies `{"configProvider": "hyprlang", "backend": "drm"}` on this machine. This is the programmatic way to tell whether the legacy or the Lua config manager is active (matters a lot, see section 5).

## 4. Request socket protocol (primary: src/debug/HyprCtl.cpp at v0.56.2)

Path: `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`, AF_UNIX SOCK_STREAM. One request per connection: connect, write request, read until EOF, server closes.

Wire grammar: `[flags/]command [args]`.
- Flags are single characters before the first `/`: `j` JSON output, `r` reload-all after the command (monitor rules, layouts, keyboard layout), `a` "all" (used by `monitors all` style requests), `c` include config in systeminfo. Flag parsing stops at the first space, so arguments may contain slashes (HyprCtl.cpp `getReply`, lines 2068 to 2097).
- Batch: `[[BATCH]]cmd1;cmd2;...`. The batch splitter ignores `;` inside `[...]` brackets, trims each command, runs each through `getReply` (so each sub-command carries its own flags, e.g. `[[BATCH]]j/clients;j/monitors`), and joins replies with the delimiter `\n\n\n` (three newlines) (HyprCtl.cpp `dispatchBatch`, lines 1308 to 1332). There is no length prefix and no per-command status framing beyond that delimiter.
- A batch is executed synchronously in one main-loop callback, so a batch of N dispatches is applied back to back with no frame rendered in between. This is the right way to do compound voice commands ("move this to workspace 3 and follow") with one visual transition.
- Dispatch reply: `ok` on success, otherwise the dispatcher's error string (examples from source: `Invalid dispatcher`, `No such window found`, `Invalid workspace`, `Window not found`, `Monitor not found`). There is no JSON form for dispatch replies even with `j/`.
- Unknown command: `unknown request`.

Server-side read loop facts that matter to a client author (lines 2236 to 2320):
- The compositor accepts the connection and then `poll()`s it for up to 5000 ms ON THE MAIN THREAD before reading. A client that connects and then stalls before writing freezes the whole compositor for up to 5 seconds. Always build the full request first, then connect and `sendall` immediately. Never hold an idle connection to `.socket.sock`.
- It reads in 1023-byte chunks and stops as soon as a chunk is shorter than 1023 bytes. Inference from that loop: a request whose length is an exact multiple of 1023 bytes makes the server call `read` again and block until the client sends more or half-closes. Mitigation: after `sendall`, call `shutdown(SHUT_WR)`, or pad/split such requests. Requests are otherwise unbounded in length.
- The server records the peer PID via SO_PEERCRED (used for permission/plugin logic and debug logs).
- Replies can be deferred via a promise (used by plugin loading), in which case the connection stays open until the promise resolves.

Full request command list in 0.56.2 (HyprCtl.cpp lines 2004 to 2043): exact-match commands: workspaces, workspacerules, activeworkspace, clients, kill, activewindow, layers, version, devices, splash, cursorpos, binds, globalshortcuts, systeminfo, animations, rollinglog, configerrors, locked, descriptions, submap, status. Prefix-match commands: reloadshaders, monitors, reload, plugin, notify, dismissnotify, getprop, seterror, switchxkblayout, output, dispatch, keyword, setcursor, getoption, decorations, [[BATCH]], eval, repl. (`hyprctl --help` also lists `layouts`, `instances`, `hyprpaper`, `hyprsunset`; `instances` and the hypr* ones are handled client side by hyprctl, and `j/layouts` returned `unknown request` on the live 0.56.2 socket.)

## 5. The Lua config manager changes IPC dispatch syntax (primary: HyprCtl.cpp `dispatchRequest` and `evalRequest`, v0.56.2)

This is the single most important compatibility fact for the daemon.

- 0.56 ships two config providers: `hyprlang` (legacy `hyprland.conf`) and `lua` (`hyprland.lua`). The packaged example config is now `/usr/share/hypr/hyprland.lua` (there is no example `hyprland.conf` in `/usr/share/hypr` on this machine), with autogenerated LuaLS stubs at `/usr/share/hypr/stubs/hl.meta.lua`.
- `dispatchRequest` behaves differently per provider. With `hyprlang`: it splits `dispatch <name> <args>` and runs the legacy dispatcher; reply `ok`, the dispatcher error, or `Invalid dispatcher`. With `lua`: the text after `dispatch ` is wrapped verbatim as `return hl.dispatch(<text>)` and evaluated as Lua. So `dispatch togglefloating` becomes `hl.dispatch(togglefloating)` and fails; the server appends the hint "Note: dispatch in lua is a shorthand for hl.dispatch(...), your syntax might need to be updated." Under Lua you must send e.g. `dispatch hl.dsp.window.float({ action = "toggle" })`.
- `eval <lua>` and `repl <lua>` requests only work with the Lua provider; on hyprlang they reply `eval is only supported with the lua config manager`.
- Detect the provider with `j/status` (`configProvider` is `hyprlang` on this machine, measured locally). The daemon should read this once at startup and on every `configreloaded` event, and keep two renderers for each operation (legacy string and Lua expression).
- The legacy dispatcher table is still constructed unconditionally in `CKeybindManager` (so plugins and hyprlang binds keep working), but the IPC `dispatch` path does not consult it when the Lua provider is active.

Lua dispatcher argument grammar is table based (from `src/config/lua/bindings/LuaBindingsDispatchers.cpp`); details in section 7.

## 6. socket2 event stream in 0.56.2 (primary: grep of every `postEvent` call in the v0.56.2 source tree, plus src/managers/EventManager.cpp)

Path: `.socket2.sock` in the same dir. Connect and read; the server never reads from you. Wire format: `EVENT>>DATA\n`. DATA is truncated to 1024 bytes and any newline inside it is replaced by a space (EventManager.cpp `formatEvent`). Window addresses in events are lowercase hex WITHOUT the `0x` prefix, while `j/clients` and the `address:` selector use `0x...`; prepend `0x` yourself.

Backpressure: each client has a queue capped at 64 pending events (`MAX_QUEUED_EVENTS = 64`); a client that falls behind is disconnected ("overflowed event queue, removing"). The daemon must drain socket2 on a dedicated reader (thread or asyncio task) that does no blocking work, and must reconnect plus resnapshot on EOF.

Complete event name list (45 names) found in the 0.56.2 source, with data layout:
- workspace `NAME`; workspacev2 `ID,NAME`
- focusedmon `MON,WORKSPACENAME`; focusedmonv2 `MON,WORKSPACEID`
- activewindow `CLASS,TITLE` (`,` when nothing focused); activewindowv2 `ADDR` (empty when none)
- fullscreen `0|1`
- monitoradded `NAME`; monitoraddedv2 `ID,NAME,DESCRIPTION`; monitorremoved `NAME`; monitorremovedv2 `ID,NAME,DESCRIPTION`
- createworkspace `NAME`; createworkspacev2 `ID,NAME`; destroyworkspace `NAME`; destroyworkspacev2 `ID,NAME`
- moveworkspace `NAME,MON`; moveworkspacev2 `ID,NAME,MON`
- renameworkspace `ID,NEWNAME`; changeworkspaceid `OLDID,NEWID`
- activespecial `NAME,MON`; activespecialv2 `ID,NAME,MON` (empty id and name when the special closes)
- activelayout `KEYBOARD,LAYOUT`
- openwindow `ADDR,WORKSPACENAME,CLASS,TITLE`; closewindow `ADDR`; kill `ADDR` (hyprctl kill mode click)
- movewindow `ADDR,WORKSPACENAME`; movewindowv2 `ADDR,WORKSPACEID,WORKSPACENAME`
- openlayer `NAMESPACE`; closelayer `NAMESPACE`
- submap `NAME` (empty for default)
- changefloatingmode `ADDR,0|1`
- urgent `ADDR`; minimized `ADDR,0|1`
- screencast `0|1,TYPE`; screencastv2 `0|1,TYPE,NAME`
- windowtitle `ADDR`; windowtitlev2 `ADDR,TITLE`
- togglegroup `0|1,ADDR`; moveintogroup `ADDR`; moveoutofgroup `ADDR`; lockgroups `0|1`
- pin `ADDR,0|1`
- configreloaded (empty); bell `ADDR` or empty; custom `DATA` (emitted by the `event` dispatcher, a free IPC channel from keybinds to the daemon)

Not present in 0.56.2: there is no `ignoregrouplock` event any more, and there is no session lock/unlock event on socket2. Lock state must be polled (`locked` request) or obtained via the Wayland lock notifier protocol (section 9).

Lua-side event names (from the local stubs, for reference only, these are in-process config hooks and not IPC): config.reloaded, hyprland.start, hyprland.shutdown, input.keyboard.key, keybinds.submap, layer.opened/closed, monitor.added/removed/focused/layout_changed, screenshare.state, window.active/class/close/destroy/fullscreen/kill/move_to_workspace/open/open_early/pin/title/update_rules/urgent, workspace.active/created/move_to_monitor/removed/special_active.

Use in the daemon: keep an in-memory desktop model updated from socket2 so the Jev `state` string is ready before the utterance ends (zero IPC on the hot path), and confirm action completion by waiting for the matching event (e.g. `movewindowv2`, `fullscreen`, `closewindow`) instead of re-polling.

## 7. Argument grammar per dispatcher in 0.56.2 (primary: DispatcherTranslator.cpp and LuaBindingsDispatchers.cpp at v0.56.2)

`W` means a window selector from section 3. "focused only" means the legacy string form cannot target another window in 0.56.2, which forces a `focuswindow` first (inside the same `[[BATCH]]`) or the Lua form which accepts `window = W` on nearly everything.

| Legacy dispatcher | Legacy args (hyprlang provider) | Targeting | Lua provider equivalent |
|---|---|---|---|
| focuswindow | `W` | any | `hl.dsp.focus({ window = "W" })` |
| focuswindowbyclass | alias of focuswindow | any | same |
| movefocus | `l|r|u|d` (first char is parsed) | focused | `hl.dsp.focus({ direction = "left" })` |
| focusurgentorlast / focuscurrentorlast | none | n/a | `hl.dsp.focus({ urgent_or_last = true })` / `hl.dsp.focus({ last = true })` |
| cyclenext | words: `prev`/`next`, `tiled`/`floating`. Source comment: `hist` and `visible` modes are NOT mapped in the new API | focused | `hl.dsp.window.cycle_next({...})` |
| killactive | none. Calls closeWindow (polite xdg close, app may prompt) | focused | `hl.dsp.window.close()` |
| closewindow | `W` | any | `hl.dsp.window.close({ window = "W" })` |
| forcekillactive | none. Calls killWindow (kills the process) | focused | `hl.dsp.window.kill()` |
| killwindow | `W` | any | `hl.dsp.window.kill({ window = "W" })` |
| signal / signalwindow | `SIG` / `W,SIG` | focused / any | `hl.dsp.window.signal({ signal = 15, window = "W" })` |
| togglefloating / setfloating / settiled | optional `W` (empty or `active` = focused) | any | `hl.dsp.window.float({ action = "toggle|set|unset", window = "W" })` |
| pseudo | optional `W` | any | `hl.dsp.window.pseudo()` |
| pin | optional `W` (toggle) | any | `hl.dsp.window.pin({...})` |
| fullscreen | `MODE [toggle|set|unset]`, MODE `0` = real fullscreen, `1` = maximize (anything other than "1" is fullscreen) | focused only | `hl.dsp.window.fullscreen({ mode = "fullscreen|maximized", action = "toggle|set|unset", window = "W" })` |
| fullscreenstate | `INTERNAL CLIENT` with -1 keep, 0 none, 1 maximize, 2 fullscreen, 3 both | focused only | `hl.dsp.window.fullscreen_state({ internal = n, client = n })` |
| movetoworkspace / movetoworkspacesilent | `WS` or `WS,W` (selector is whatever follows the LAST comma) | any | `hl.dsp.window.move({ workspace = WS, follow = true|false, window = "W" })` (`follow = false` is the silent variant) |
| workspace | `WS` | n/a | `hl.dsp.focus({ workspace = WS })` |
| focusworkspaceoncurrentmonitor | `WS` | n/a | `hl.dsp.focus({ workspace = WS, on_current_monitor = true })` |
| togglespecialworkspace | optional `NAME` (internally prefixed with `special:`) | n/a | `hl.dsp.workspace.toggle_special("NAME")` |
| renameworkspace | `ID [NAME]` | n/a | `hl.dsp.workspace.rename({ workspace = ID, name = "..." })` |
| movewindow | `l|r|u|d`, or `mon:MONITOR`, optional trailing ` silent` | focused only | `hl.dsp.window.move({ direction = "left" })`, `hl.dsp.window.move({ monitor = M, follow = false })` |
| swapwindow | `l|r|u|d` or `W` (swap focused with W; refuses if focused is fullscreen) | focused + other | `hl.dsp.window.swap({ direction = ... })` / `{ target|with|other = W }` |
| swapnext | empty/`next`, or `prev|last|l|b|back` | focused | `hl.dsp.window.swap({ next = true })` style |
| centerwindow | none | focused (floating) | `hl.dsp.window.center()` |
| resizeactive | `X Y` relative px, `exact X Y`, percentages allowed (`10% 0`, `exact 50% 50%`). Rejects result < 1 px | focused only | `hl.dsp.window.resize({ x = , y = , relative = true })` |
| moveactive | same vector grammar | focused only | `hl.dsp.window.move({ x = , y = , relative = true })` |
| resizewindowpixel / movewindowpixel | `VECTOR,W` | any | same Lua calls with `window = "W"` |
| alterzorder / bringactivetotop | `top|bottom[,W]` / none | any / focused | `hl.dsp.window.alter_zorder(...)`, `hl.dsp.window.bring_to_top()` |
| togglegroup | none | focused | `hl.dsp.group.toggle()` |
| changegroupactive | `f` (default) or `b|prev`, or a 1-based index number | focused group | `hl.dsp.group.next()`, `hl.dsp.group.prev()`, `hl.dsp.group.active({ index = n })` |
| movegroupwindow | `f` / `b|prev` | focused | `hl.dsp.group.move_window({ forward = bool })` |
| moveintogroup / moveintoorcreategroup / movewindoworgroup | `l|r|u|d` | focused | `hl.dsp.window.move({ into_group = "left" })`, `{ into_or_create_group = ... }` |
| moveoutofgroup | none/`active` or `W` | any | `hl.dsp.window.move({ out_of_group = true })` |
| lockgroups / lockactivegroup | `lock|unlock|toggle` | global / focused | `hl.dsp.group.lock(...)`, `hl.dsp.group.lock_active(...)` |
| denywindowfromgroup | `on|off|toggle` | focused | `hl.dsp.window.deny_from_group(...)` |
| setignoregrouplock | registered NO-OP, deprecated | n/a | none |
| workspaceopt | always fails with `workspaceopt is deprecated` | n/a | none |
| focusmonitor | monitor config string: name, id, `l|r|u|d`, `+1`/`-1`, `desc:...`, `current` | n/a | `hl.dsp.focus({ monitor = M })` |
| movecurrentworkspacetomonitor | `MON` | n/a | `hl.dsp.workspace.move({ monitor = M })` |
| moveworkspacetomonitor | `WS MON` | n/a | `hl.dsp.workspace.move({ workspace = WS, monitor = M })` |
| swapactiveworkspaces | `MON1 MON2` | n/a | `hl.dsp.workspace.swap_monitors(...)` |
| layoutmsg | free string handed to the workspace's tiled algorithm | focused workspace | `hl.dsp.layout("togglesplit")` |
| dpms | `on|off|toggle [MON]` (anything not starting with on/toggle means OFF) | n/a | `hl.dsp.dpms(...)` |
| exit | none | n/a | `hl.dsp.exit()` |
| sendshortcut | `MODS,KEY,W` exactly 3 comma fields (W may be empty for focused). KEY is an xkb keysym name, `code:N`, a number > 9 as raw keycode, or `mouse:N` (N >= 272) | any | `hl.dsp.send_shortcut({ mods = "CTRL", key = "w", window = "W" })` |
| sendkeystate | `MODS,KEY,down|repeat|up,W` exactly 4 fields | any | `hl.dsp.send_key_state({ mods, key, state, window })` |
| pass | `W` | any | `hl.dsp.pass(...)` |
| tagwindow | `TAG [W]` (`+tag`, `-tag`, `tag` toggles) | any | `hl.dsp.window.tag(...)`, `hl.dsp.window.clear_tags()` |
| setprop | `W PROP VALUE...` | any | `hl.dsp.window.set_prop({ prop, value, window })` |
| movecursor / movecursortocorner | `X Y` / `0..3` | n/a | `hl.dsp.cursor.move({ x, y })`, `hl.dsp.cursor.move_to_corner({ corner })` |
| submap | `NAME` or `reset` | n/a | `hl.dsp.submap("NAME")` |
| exec / execr | shell command, `exec` supports `[rules]` prefix | n/a | `hl.dsp.exec_cmd("cmd", {rules})`, `hl.dsp.exec_raw("cmd")` |
| event | `DATA` emits socket2 `custom>>DATA` | n/a | `hl.dsp.event("DATA")` |
| global | `APPID:NAME` | n/a | `hl.dsp.global(...)` |
| forceidle | seconds (plus/minus grammar) | n/a | `hl.dsp.force_idle(...)` |
| forcerendererreload, toggleswallow, releaseinputcapture, mouse | niche | | matching `hl.dsp.*` |

Exact Lua table field names above for fullscreen, float, focus, and window.move (workspace, monitor, follow, into_group, out_of_group) were read from the 0.56.2 source. For the rows where I only list the function name with `(...)`, the function exists in the stubs but I did not read its field names; check `LuaBindingsDispatchers.cpp` before relying on them.

Workspace selector grammar (src/helpers/MiscFunctions.cpp `getWorkspaceIDNameFromString`, v0.56.2): `N`, `+N`/`-N` relative, `r+N`/`r-N`/`r~N` relative including empty, `m+N`/`m-N`/`m~N` on monitor, `e+N`/`e-N`/`e~N` existing only, `name:NAME`, `special[:NAME]`, `empty` (with optional suffix flags), `prev`/`previous`, `next`.

### layoutmsg vocabulary in 0.56.2 (primary: src/layout/algorithm/tiled/*)
Layouts built in: `dwindle` (default), `master`, `scrolling`, `monocle`, plus `lua:<name>` user layouts registered via `hl.layout.register` (config value `general:layout` description in ConfigValues.cpp line 179). Layout can differ per workspace (`tiledLayout` in workspace JSON).
- dwindle: `togglesplit`, `swapsplit`, `rotatesplit [angle]`, `movetoroot [W] [unstable]`, `preselect <dir>`, `splitratio <+-delta|value> [exact]`.
- master: `swapwithmaster`, `focusmaster`, `cyclenext`, `cycleprev`, `swapnext`, `swapprev`, `addmaster`, `removemaster`, `rollnext`, `rollprev`, `mfact <value>`, `orientationleft|right|top|bottom|center|next|prev|cycle`.
- monocle: `cyclenext`, `cycleprev`.
- scrolling: `move`, `colresize`, `fit`, `fit_into_view`, `focus`, `center`, `promote`, `consume`, `expel`, `consume_or_expel`, `swapcol`, `inhibit_scroll`.

## 8. Renames, removals and deprecations relevant to a fork of hypruse (0.50 to 0.56)

Verified in the 0.56.2 source:
- `togglesplit`, `swapsplit`, `splitratio` are NOT top-level dispatchers in 0.56.2. They are `layoutmsg` messages now (`dispatch layoutmsg togglesplit`). The packaged example config confirms it: `hl.dsp.layout("togglesplit") -- dwindle only`.
- `workspaceopt` is registered but always returns the error `workspaceopt is deprecated`.
- `setignoregrouplock` is registered as a silent no-op (source comment: deprecated). The `ignoregrouplock` socket2 event no longer exists.
- `toggleopaque` is not registered (opacity toggling goes through `setprop`).
- `cyclenext`: the `hist` and `visible` arguments are accepted but ignored by the translator (source comment says they are not mapped to the new API).
- `fullscreen` and `fullscreenstate` legacy forms only act on the focused window; `movewindow`, `resizeactive`, `moveactive` likewise.
- New in this generation: `stableid:` and `pid:` window selectors, `j/status` request, `eval`/`repl` requests, `changeworkspaceid` event, `forceidle`, `releaseinputcapture`, `moveintoorcreategroup`, `monocle` and `scrolling` built-in layouts, Lua user layouts.
- The layout system was rewritten: layouts are per workspace "algorithms" under `src/layout/algorithm/tiled/`, not a single global IHyprLayout. Third-party layout plugins written for 0.50-era APIs (hy3 and similar) need ports; the daemon must not assume a plugin's dispatchers exist.

Which release introduced each change was NOT pinned down release by release (see not_found). What is certain is the end state in v0.56.2.

### 8b. Release by release timeline, 0.50 to 0.56 (primary: GitHub release notes of hyprwm/Hyprland, fetched 2026-09-21 via api.github.com/repos/hyprwm/Hyprland/releases)

| Release | Date | IPC / dispatcher relevant changes quoted from the release notes |
|---|---|---|
| v0.50.0 | 2025-07-16 | baseline of this comparison |
| v0.51.0 | 2025-09-10 | "dispatchers: allow window address in swapwindow (#11518)", "hyprctl: add getprop (#11394)", configurable trackpad gestures, physical monitor size in monitors JSON |
| v0.52.0 | 2025-11-07 | "dispatchers: add forceidle (#11922)", "dispatchers: add set, unset and toggle to fullscreen (#11893)", submap auto closing |
| v0.53.0 | 2025-12-29 | Breaking: "Windowrule syntax has been completely overhauled"; `misc:on_focus_under_fullscreen` replaces `misc:new_window_takes_over_fullscreen`; `contentType` in activewindow |
| v0.54.0 | 2026-02-27 | Breaking: "`togglesplit` and `swapsplit` have been removed after being long deprecated. Use `layoutmsg` with the same params instead." Also "layout: rethonk layouts from the ground up (#12890)", "event: refactor HookSystem into a typed event bus (#13333)", "desktop/window: add stable id and use it for foreign" |
| v0.55.0 | 2026-05-09 | Lua lands: "config/lua: init lua config manager, use lua if available (#13817)", "config: use lua by default, generate lua if no config present", "example: remove old .conf file". Breaking: "`misc:vfr` moved to `debug:`", `dwindle:pseudotile` removed. "animations: add springs (#14171)", "dispatchers: add moveintoorcreategroup", "dwindle: add rotatesplit layoutmsg", "socket2: emit `kill` event", "layout/dwindle,master: return invalid layoutmsg errors", "config/legacy: default to active window for movetoworkspace dispatchers" |
| v0.56.0 | 2026-07-20 | "No breaking changes! :)". "desktop/windowRule: add `stableid:` window selector (#14984)", "hyprctl: add interactive Lua REPL mode (#15043)", "hyprctl: add config full-reload for performing a ground-up reload (#14748)", "socket2: add back changefloatingmode and togglegroup events (#14089)" (so they were missing in the 0.54/0.55 window), "plugins: add api for registering and dispatching events (#14734)", "eventmanager: handle partial IPC writes (#15262)", "hyprctl: handle partial IPC transfers (#15408)", scrolling layout `inhibit_scroll`, `fit_into_view` |
| v0.56.1 | 2026-07-27 | "compositor: add a deprecation notice to .conf configs (#15538)", "workspace: add changeworkspaceid to socket2 (#15571)", "hyprctl: fix json output for binds" |
| v0.56.2 | 2026-08-05 | patch fixes; "ipc/socket1: don't round/truncate `hyprctl monitors` scale (#15628)" |

The deprecation notice text, verbatim from src/i18n/Engine.cpp line 254 at v0.56.2: "You are using the .conf config format, support for which will be removed in Hyprland 0.57." This machine runs the hyprlang provider today, so the legacy dispatch strings work now, but the daemon must ship the Lua renderer from day one because the next minor release is announced to drop `.conf` and with it, per the `dispatchRequest` code, the legacy `dispatch name args` IPC syntax.

Related request differences under the Lua provider (HyprCtl.cpp): `keyword` replies `keyword can't work with non-legacy parsers. Use eval.`; `reload full-reset` recreates the config context and can switch provider at runtime (wiki using-hyprctl page), so re-read `j/status` after every `configreloaded` event.

Wiki caveat: wiki.hypr.land (repo hyprwm/hyprland-wiki, main branch) is now Lua-first and tracks git, not 0.56.2. Example: the wiki batch section says semicolons inside a batched command "must be backslash-escaped", but the 0.56.2 `dispatchBatch` source only protects semicolons inside `[...]` brackets and has no backslash handling. Trust the tagged source over the wiki for 0.56.2. For the daemon: never put a raw `;` inside a batched Lua expression on 0.56.2 (Lua tables can use `,` as the separator, so this is easy to avoid).

## 9. Session lock: `locked` request and the lock notifier protocol

- `locked` request: exists in 0.56.2 (registered in HyprCtl.cpp line 2021, handler `getIsLocked` returns `g_pSessionLockManager->isSessionLocked()`). Plain reply `true`/`false`, JSON reply `{"locked": bool}`. It is NOT listed in `hyprctl --help` on 0.56.2, which is probably why it is easy to miss. It was added upstream by commit 064bdb06f "hyprctl: Add locked cmd to requests (#6042)" dated 2024-05-13, so it predates 0.50. Measured locally: 0.091 ms p50 over the direct socket.
- There is NO lock or unlock event on socket2 in 0.56.2 (full event list in section 6).
- Push notification of lock state is available as a Wayland protocol: `hyprland-lock-notify-v1` (XML installed locally at /usr/share/hyprland-protocols/protocols/hyprland-lock-notify-v1.xml). Interfaces: `hyprland_lock_notifier_v1` (request `get_lock_notification`) and `hyprland_lock_notification_v1` (events `locked`, `unlocked`), version 1. Hyprland implements it in src/protocols/LockNotify.cpp. Using it from Python needs a Wayland client (pywayland plus generated bindings); a simpler path for a Python daemon is polling `locked` right before each action, which costs about 0.1 ms.
- Security finding from source: only KEYBINDS are gated on the session lock (KeybindManager.cpp line 649: binds without the `locked` flag are skipped while `isSessionLocked()`). The IPC `dispatch` path has no such check. A voice daemon that keeps listening while the screen is locked could therefore close windows, exec commands, or `exit` the compositor from behind the lock screen. The daemon MUST gate every action on `locked` itself (put `locked` as the first sub-request of the same `[[BATCH]]` snapshot, and refuse on `true`).
- hypruse today: `src/hypruse/trust.py` `session_locked()` scans /proc for locker process names (hyprlock, swaylock, gtklock, waylock) and its comment states "Hyprland exposes no lock state over hyprctl either", with a documented cost of about 8 ms. That comment is outdated for this target: the fork should use the `locked` request (about 80x cheaper, and correct for any ext-session-lock-v1 client, not only the four named lockers) and may keep the /proc scan as a fallback.
- 0.55 note: "sessionLock: send locked instead of denied when missing a lock frame for 5 seconds (#14271)"; 0.56 Lua adds `hl.clear_crashed_lockscreen()`.

## 10. Animation settings and perceived speed

- Semantics (wiki, configuring/core/animations.md, main branch): "`speed` is the number of deciseconds (100ms each) the animation will take. For example, `speed = 1` = 100ms." Curves are beziers or, since 0.55, springs (`hl.curve(NAME, { type = "spring", mass, stiffness, damping })`; hyprlang `bezier = ...` still works on the legacy provider).
- Animation tree names on this 0.56.2 machine (from `j/animations`): global, windows (windowsIn, windowsOut, windowsMove), layers (layersIn, layersOut), fade (fadeIn, fadeOut, fadeSwitch, fadeShadow, fadeGlow, fadeDim, fadeLayers (fadeLayersIn, fadeLayersOut), fadePopups (fadePopupsIn, fadePopupsOut), fadeDpms), border, borderangle, shadowangle, glowangle, workspaces (workspacesIn, workspacesOut, specialWorkspace (specialWorkspaceIn, specialWorkspaceOut)), zoomFactor, monitorAdded.
- This machine's current values (local measurement via `j/animations`): windows 6 ds (600 ms, curve `wind`, slide), windowsIn 6 ds, windowsOut 5 ds, windowsMove 5 ds, workspaces 5 ds (500 ms), fade 10 ds (1 s), global 8 ds. `animations:enabled` = 1, blur enabled, shadows disabled. With a voice pipeline where the whole budget from end of speech to visible effect is a few hundred ms, a 500 to 600 ms workspace or window move animation is the single largest component of perceived latency, larger than IPC by more than three orders of magnitude.
- Animations do not delay the logical state change. The dispatch reply and socket2 events are sent when the action is applied, and `clients` JSON reports goal geometry (`position(GEOMETRIC_GOAL)` in HyprCtl.cpp), not the mid-animation position. So the daemon can chain the next action immediately; only the human sees the tween.
- Options for the fork, least invasive first: (1) leave user config alone and only document it; (2) offer an opt-in "snappy" profile that sets `windows`/`windowsMove`/`workspaces` to 2 to 3 ds with a fast-out curve (front-loaded curves such as the default `0, 0.75, 0.15, 1` read as faster than linear at equal duration); (3) wrap voice-triggered actions in a batch that disables animations for that action only: `[[BATCH]]keyword animations:enabled 0;dispatch ...;keyword animations:enabled 1` on hyprlang. Caveat for (3): `keyword` does not exist under the Lua provider ("Use eval"), and re-enabling in the same batch may or may not suppress the tween since the animation starts on the dispatch and config is re-read afterwards; this was NOT tested (read-only constraint) and needs a live experiment before relying on it.
- Related knobs: `misc:vfr` moved to `debug:vfr` in 0.55 (confirmed locally: `getoption misc:vfr` replies `no such option`); `cursor:no_warps` (0 here) controls whether focus changes warp the cursor, which matters for voice focus changes because a warped cursor plus `input:follow_mouse` can re-steal focus; `animations:workspace_wraparound`; `misc:focus_on_activate`.

## 11. Plugins

- `hyprctl plugin list` on this machine: `no plugins loaded`. Design for the stock dispatcher set.
- Plugin API surface in 0.56.2 (src/plugins/PluginAPI.hpp): `addDispatcherV2` (the V1 `addDispatcher` is marked deprecated), `registerHyprCtlCommand` (plugins can add request-socket commands), `invokeHyprctlCommand`, `addNotificationV2`, `addLuaFunction(handle, namespace, name, fn)` (plugins extend the `hl.plugin.*` Lua namespace), `addEvent`/`removeEvent` (custom event bus events, new in 0.56.0), and tiled/floating algorithm registration (`addTiledAlgo`, `addFloatingAlgo` replacing the old layout API).
- Plugin dispatchers land in `g_pKeybindManager->m_dispatchers`, which the IPC `dispatch` path only consults under the hyprlang provider. Under Lua they are reached through whatever Lua functions the plugin registers. A voice daemon should discover capabilities at runtime (`j/status`, `plugin list`, `j/activeworkspace.tiledLayout`) and not hardcode plugin verbs.
- A compositor plugin is not needed for this app and would be a liability: plugins are ABI-locked to the exact Hyprland commit (the version ABI string in `hyprctl version`), run on the compositor main thread, and a crash takes the session down. Everything the voice app needs is available out of process at about 0.1 ms per call.
- The `event` dispatcher plus socket2 `custom>>DATA` gives a zero-dependency push channel from a user keybind to the daemon (push-to-talk: `bind = SUPER, V, event, voice:start` on hyprlang, `hl.bind("SUPER + V", hl.dsp.event("voice:start"))` on Lua; release binds give `voice:stop`). That avoids a global-shortcuts portal round trip entirely.

## 12. Reply semantics gotchas (primary: ConfigActions.cpp at v0.56.2)

- `xtract(std::optional<PHLWINDOW>)` is `window.value_or(focused)`. A legacy call such as `closewindow address:0xSTALE` passes an optional that HOLDS a null pointer, so there is no fallback to the focused window (good: a stale address never closes the wrong window) but `closeWindow` then hits `if (!window) return {};` and the IPC reply is `ok` even though nothing was closed. Same for `killwindow`. So `ok` does not prove an effect. Confirm destructive actions from socket2 (`closewindow>>ADDR`) or a follow-up snapshot.
- `killactive` and `closewindow` are the polite path: `window->sendClose()` (xdg_toplevel close; the app may show an "unsaved changes" dialog or ignore it). They also refuse while a `noclosefor` rule timer is active. `forcekillactive` and `killwindow` do `kill(pid, SIGKILL)` on the window's PID: unsaved data is lost, and for multi-window single-process apps (browsers, Electron, terminals with a server process) it kills ALL of that app's windows.
- `exit` calls `stopCompositor()` immediately. No confirmation. Ends the whole session and every app in it.
- `dpms off` blanks the display; with voice as the only input path that is recoverable (`dpms on`), but treat it as "needs confirmation" because the user loses visual feedback.
- `fullscreen` legacy: any first arg other than the literal `1` means real fullscreen, so a typo silently fullscreens instead of maximizing.
- `dpms`: any arg not starting with `on` or `toggle` means OFF. `dpms garbage` turns the screen off.
- `movetoworkspace` creates the workspace if it does not exist (`resolveWorkspace` creates it on the focused monitor).

## 13. Voice-worthy window management operations mapped to exact dispatcher strings

Wire form for the request socket: prefix each legacy string with `dispatch ` (example: `dispatch movetoworkspacesilent 3,address:0x55a4479b2d40`). Under the Lua provider send `dispatch <lua expression>` with the expression from the Lua column. `{A}` = `address:0x...` taken from the snapshot. `{N}` = workspace selector. Target column: "none" = acts globally, "focused" = acts on the focused window and cannot take a selector in legacy form, "optional" = selector accepted and focused used when omitted, "required" = selector mandatory. When a "focused" op must hit another window, send `[[BATCH]]dispatch focuswindow {A};dispatch <op>`.

| # | Spoken intent | Legacy string (hyprlang provider) | Lua expression (lua provider) | Target | Destructive |
|---|---|---|---|---|---|
| 1 | focus / switch to APP | `focuswindow {A}` | `hl.dsp.focus({ window = "{A}" })` | required | no |
| 2 | focus left/right/up/down | `movefocus l` (r, u, d) | `hl.dsp.focus({ direction = "left" })` | focused | no |
| 3 | previous window / go back | `focuscurrentorlast` | `hl.dsp.focus({ last = true })` | none | no |
| 4 | focus the urgent window | `focusurgentorlast` | `hl.dsp.focus({ urgent_or_last = true })` | none | no |
| 5 | next / previous window | `cyclenext` / `cyclenext prev` | `hl.dsp.window.cycle_next()` (prev variant fields not read) | focused | no |
| 6 | go to workspace N | `workspace {N}` | `hl.dsp.focus({ workspace = {N} })` | none | no |
| 7 | next / previous workspace | `workspace e+1` / `workspace e-1` | `hl.dsp.focus({ workspace = "e+1" })` | none | no |
| 8 | last workspace | `workspace previous` | `hl.dsp.focus({ workspace = "previous" })` | none | no |
| 9 | move this to workspace N and follow | `movetoworkspace {N}` or `movetoworkspace {N},{A}` | `hl.dsp.window.move({ workspace = {N}, window = "{A}" })` | optional | no |
| 10 | send this to workspace N (stay here) | `movetoworkspacesilent {N}` or `movetoworkspacesilent {N},{A}` | `hl.dsp.window.move({ workspace = {N}, follow = false, window = "{A}" })` | optional | no |
| 11 | float / tile / toggle floating | `setfloating {A}` / `settiled {A}` / `togglefloating {A}` (selector optional) | `hl.dsp.window.float({ action = "set" })` / `"unset"` / `"toggle"`, plus `window = "{A}"` | optional | no |
| 12 | fullscreen (toggle / on / off) | `fullscreen 0` / `fullscreen 0 set` / `fullscreen 0 unset` | `hl.dsp.window.fullscreen({ mode = "fullscreen", action = "toggle" })` (set, unset; `window` accepted) | focused in legacy | no |
| 13 | maximize (toggle / on / off) | `fullscreen 1` / `fullscreen 1 set` / `fullscreen 1 unset` | `hl.dsp.window.fullscreen({ mode = "maximized", action = "toggle" })` | focused in legacy | no |
| 14 | fake fullscreen (app thinks fullscreen, stays tiled) | `fullscreenstate 0 2` (undo: `fullscreenstate 0 0`) | `hl.dsp.window.fullscreen_state({ internal = 0, client = 2 })` | focused in legacy | no |
| 15 | pin / unpin (floating only) | `pin` or `pin {A}` | `hl.dsp.window.pin()` | optional | no |
| 16 | center window (floating) | `centerwindow` | `hl.dsp.window.center()` | focused | no |
| 17 | make it bigger / smaller | `resizeactive 80 0`, `resizeactive -80 0`, `resizeactive 0 80`, percent form `resizeactive 10% 0` | `hl.dsp.window.resize({ x = 80, y = 0, relative = true })` | focused; other window via `resizewindowpixel 80 0,{A}` | no |
| 18 | resize to exact size | `resizeactive exact 1280 720` or `resizeactive exact 50% 50%` | `hl.dsp.window.resize({ x = 1280, y = 720 })` | focused; `resizewindowpixel exact 1280 720,{A}` | no |
| 19 | nudge floating window | `moveactive 50 0`; `moveactive exact 100 100` | `hl.dsp.window.move({ x = 50, y = 0, relative = true })` | focused; `movewindowpixel 50 0,{A}` | no |
| 20 | move window left/right/up/down in layout | `movewindow l` (r, u, d) | `hl.dsp.window.move({ direction = "left" })` | focused | no |
| 21 | swap with neighbour | `swapwindow l` (r, u, d) | `hl.dsp.window.swap({ direction = "left" })` | focused | no |
| 22 | swap this with APP | `swapwindow {A}` | `hl.dsp.window.swap({ target = "{A}" })` | required (other) | no |
| 23 | swap with next / previous in layout order | `swapnext` / `swapnext prev` | `hl.dsp.window.swap({ next = true })` / `{ prev = true }` | focused | no |
| 24 | move window to other monitor | `movewindow mon:+1` (add ` silent` to stay) | `hl.dsp.window.move({ monitor = "+1" })` | focused | no (single monitor here: returns "Monitor ... not found") |
| 25 | focus other monitor | `focusmonitor +1` / `focusmonitor eDP-1` | `hl.dsp.focus({ monitor = "+1" })` | none | no |
| 26 | toggle scratchpad | `togglespecialworkspace` / `togglespecialworkspace NAME` | `hl.dsp.workspace.toggle_special("NAME")` | none | no |
| 27 | send this to scratchpad | `movetoworkspacesilent special:NAME` (optionally `,{A}`) | `hl.dsp.window.move({ workspace = "special:NAME", follow = false })` | optional | no |
| 28 | group / ungroup (tabs) | `togglegroup` | `hl.dsp.group.toggle()` | focused | mild: ungroup dissolves the group arrangement |
| 29 | next / previous tab in group | `changegroupactive f` / `changegroupactive b` | `hl.dsp.group.next()` / `hl.dsp.group.prev()` | focused group | no |
| 30 | tab number K | `changegroupactive K` (1-based) | `hl.dsp.group.active({ index = K })` | focused group | no |
| 31 | pull window into neighbouring group / out of group | `moveintogroup l` / `moveoutofgroup` | `hl.dsp.window.move({ into_group = "left" })` / `hl.dsp.window.move({ out_of_group = true })` | focused (`moveoutofgroup {A}` accepted) | no |
| 32 | lock groups | `lockgroups lock` / `unlock` / `toggle` | `hl.dsp.group.lock(...)` (fields not read) | none | no |
| 33 | dwindle: toggle split direction | `layoutmsg togglesplit` | `hl.dsp.layout("togglesplit")` | focused; dwindle workspaces only | no |
| 34 | dwindle: swap the two halves | `layoutmsg swapsplit` | `hl.dsp.layout("swapsplit")` | focused; dwindle | no |
| 35 | dwindle: split ratio | `layoutmsg splitratio +0.1` / `layoutmsg splitratio 0.6 exact` | `hl.dsp.layout("splitratio +0.1")` | focused; dwindle | no |
| 36 | dwindle: rotate split / move to root | `layoutmsg rotatesplit` / `layoutmsg movetoroot` | `hl.dsp.layout("rotatesplit")` | focused; dwindle | no |
| 37 | master: make this the master | `layoutmsg swapwithmaster` | `hl.dsp.layout("swapwithmaster")` | focused; master workspaces only | no |
| 38 | master: focus master / cycle | `layoutmsg focusmaster`, `layoutmsg cyclenext`, `layoutmsg cycleprev` | `hl.dsp.layout("focusmaster")` | focused; master | no |
| 39 | master: more / fewer masters, ratio, orientation | `layoutmsg addmaster`, `layoutmsg removemaster`, `layoutmsg mfact 0.6`, `layoutmsg orientationleft` (right, top, bottom, center, next, prev, cycle) | `hl.dsp.layout("addmaster")` etc | focused; master | no |
| 40 | bring to front (floating) | `bringactivetotop` or `alterzorder top,{A}` | `hl.dsp.window.bring_to_top()` | optional | no |
| 41 | close this / close APP | `killactive` / `closewindow {A}` | `hl.dsp.window.close()` / `hl.dsp.window.close({ window = "{A}" })` | optional | YES (polite close; app may prompt; unsaved work at risk) |
| 42 | force kill this / APP | `forcekillactive` / `killwindow {A}` | `hl.dsp.window.kill()` / `hl.dsp.window.kill({ window = "{A}" })` | optional | YES, severe (SIGKILL to the PID, takes all windows of that process, no save prompt) |
| 43 | press a shortcut in APP (e.g. new tab) | `sendshortcut CTRL,t,{A}` (exactly three comma fields; empty third field = focused) | `hl.dsp.send_shortcut({ mods = "CTRL", key = "t", window = "{A}" })` | optional | depends on the shortcut; treat as potentially destructive (CTRL+W, CTRL+Q) |
| 44 | screen off / on | `dpms off` / `dpms on` / `dpms toggle` | `hl.dsp.dpms(...)` (fields not read) | none | YES, mild (blind until `dpms on`; any unknown arg means off) |
| 45 | log out / exit Hyprland | `exit` | `hl.dsp.exit()` | none | YES, catastrophic (ends the session and all apps immediately, no confirmation) |
| 46 | launch APP | `exec firefox` or `exec [workspace 3 silent] firefox` | `hl.dsp.exec_cmd("firefox")`; an optional second argument is a rules table (stubs type it as `table<string, string|number|boolean>`), exact rule keys NOT verified | none | arbitrary command execution; restrict to an allowlist |
| 47 | rename workspace | `renameworkspace 3 mail` | `hl.dsp.workspace.rename({ workspace = 3, name = "mail" })` | none | no |
| 48 | move workspace to monitor | `movecurrentworkspacetomonitor +1` | `hl.dsp.workspace.move({ monitor = "+1" })` | none | no |

Recommended confirmation policy for the Jev layer: rows 41 to 46 are the only ones that can lose work. Gate them on a Jev `boolean` or `score` confidence threshold plus an explicit confirm step for 42 and 45. Everything else is reversible with one more command, so it can fire at a lower confidence for speed.

## 14. Recommendations for the daemon's Hyprland layer

1. Talk to `.socket.sock` directly from Python with the stdlib `socket` module. Local measurement: 0.05 to 0.16 ms per query versus about 6 ms (p95 9 ms, max 13 to 16 ms) for a `hyprctl` spawn. hypruse currently shells out on purpose (`src/hypruse/hyprctl.py` docstring) and already has `batch_query`, a provider probe via `hyprctl -j status`, and Lua renderers for a handful of dispatchers; the fork should keep that logic and swap the transport.
2. Build the request string fully, then connect, `sendall`, `shutdown(SHUT_WR)`, read to EOF. Never hold an idle connection: the compositor main thread polls an accepted connection for up to 5000 ms.
3. One `[[BATCH]]` per snapshot: `[[BATCH]]locked;j/status;j/clients;j/workspaces;j/monitors;j/activewindow;j/activeworkspace;j/layers` measured at 0.44 ms p50 for about 11 KB. Split on `\n\n\n`.
4. Maintain the desktop model from `.socket2.sock` on a dedicated reader so the Jev state string is ready when speech ends; resnapshot on reconnect, on `configreloaded`, and on any parse surprise. Drain fast: 64 queued events and the compositor drops you.
5. Always target by `address:0x...` (or `stableid:`), never by class or title regex. Event addresses lack the `0x` prefix.
6. Two renderers per operation (legacy string, Lua expression), chosen by `j/status.configProvider`. 0.56.2 prints a notice that `.conf` support will be removed in 0.57, so the Lua renderer is not optional.
7. Gate every action on `locked` (IPC dispatch is not blocked by the lock screen).
8. Treat `ok` as "accepted", not "done". Confirm via the matching socket2 event with a short timeout.
9. Compound commands go in one batch so they apply within one main-loop turn with a single visual transition.
10. Perceived speed is dominated by this machine's 500 to 600 ms window and workspace animations, not by IPC. Offer an opt-in snappy animation profile.

## 15. Sources

Primary:
- Hyprland source at tag v0.56.2 (https://github.com/hyprwm/Hyprland/tree/v0.56.2), downloaded as tarball to scratchpad `src/Hyprland-0.56.2/`. Files cited: src/debug/HyprCtl.cpp, src/managers/EventManager.cpp, src/managers/KeybindManager.cpp, src/config/legacy/DispatcherTranslator.cpp, src/config/shared/actions/ConfigActions.{hpp,cpp}, src/config/lua/bindings/LuaBindingsDispatchers.cpp, src/desktop/state/ViewQuery.cpp, src/helpers/MiscFunctions.cpp, src/layout/algorithm/tiled/{dwindle,master,monocle,scrolling}/*, src/layout/supplementary/WorkspaceAlgoMatcher.cpp, src/config/values/ConfigValues.cpp, src/plugins/PluginAPI.hpp, src/i18n/Engine.cpp, src/protocols/LockNotify.cpp.
- GitHub release notes v0.50.0 to v0.56.2 via https://api.github.com/repos/hyprwm/Hyprland/releases
- Commit 064bdb06f "hyprctl: Add locked cmd to requests (#6042)", 2024-05-13 (GitHub commit search API).
- Hyprland wiki repo main branch (https://github.com/hyprwm/hyprland-wiki): content/configuring/core/animations.md, content/configuring/core/dispatchers.md, content/configuring/core/advanced-configuration/using-hyprctl.md, content/ipc/_index.md. Note: tracks git, Lua-first.
- Locally installed files: /usr/share/hypr/hyprland.lua, /usr/share/hypr/stubs/hl.meta.lua (autogenerated Lua API stubs), /usr/share/hyprland-protocols/protocols/hyprland-lock-notify-v1.xml, `hyprctl --help`.
Local measurement: scratchpad `bench_ipc.py` and inline scripts, read-only requests only, against the live 0.56.2 session.

## Verification

Independent skeptical pass, 2026-09-21. Method: my own benchmark script (not bench_ipc.py) against the live socket, read-only requests only, plus fresh greps of the v0.56.2 tarball tree in scratchpad/src/Hyprland-0.56.2. No dispatch was sent, the desktop was not changed.

1. Direct socket vs hyprctl latency: PARTIALLY CORRECT. Re-measured direct socket p50 0.113 ms (j/activewindow, n=300) and 0.267 ms (j/clients, 6247 bytes, n=100); p95 0.18 and 0.43 ms. Same order of magnitude as claimed. Spawning `hyprctl -j activewindow` measured 15.3 ms p50, 22.4 ms p95 (n=50) in my run, about 2.5x the claimed 6 ms; other benchmarks may have been loading the machine. Correction: plan on "direct socket well under 1 ms, hyprctl spawn 6 to 20 ms depending on load"; the conclusion (use the socket directly) is strengthened.
2. [[BATCH]] shape and cost: CONFIRMED. Same 8-command batch returned 11030 bytes, p50 0.457 ms, p95 0.846 ms (n=100). Reply contains exactly 7 "\n\n\n" delimiters for 8 commands. dispatchBatch in HyprCtl.cpp (line 1308 onward) uses DELIMITER "\n\n\n", tracks [] depth, has no backslash handling. Caveat for the parser: JSON replies begin with a leading "\n", so the raw stream shows "false\n\n\n\n{"; split on "\n\n\n" then strip each part.
3. One request per connection, 5000 ms poll on accept, 1023-byte reads: CONFIRMED in source (HyprCtl.cpp:2264 poll(pollfds, 1, 5000); :2274 read(..., 1023)). The "freezes the compositor" consequence was not tested live (would disturb the desktop); it follows from the code running in the main event loop.
4. Lua vs hyprlang dispatch path: CONFIRMED. HyprCtl.cpp:1132 formats "return hl.dispatch({})" when Config::mgr()->type() == CONFIG_LUA; :1163 "keyword can't work with non-legacy parsers. Use eval."; evalRequest returns "eval is only supported with the lua config manager" otherwise. Live j/status returns {"configProvider": "hyprlang", "backend": "drm"}. The exact Lua example string hl.dsp.window.float({ action = "toggle" }) was not executed or checked here.
5. 71 legacy dispatchers, dwindle ops via layoutmsg: CONFIRMED. 71 unique names in KeybindManager.cpp:40-113 and 71 m_dispMap entries in DispatcherTranslator.cpp; none of togglesplit, swapsplit, splitratio, toggleopaque is among them; DwindleAlgorithm.cpp:679-713 handles togglesplit, swapsplit, rotatesplit, movetoroot, preselect; workspaceopt returns "workspaceopt is deprecated" (:356); setignoregrouplock is an empty lambda (:861).
6. `locked` request plus no lock gate on IPC dispatch: CONFIRMED. Live: "locked" returns "false", "j/locked" returns {"locked": false}, p50 0.097 ms; `hyprctl --help | grep -ci locked` is 0. Source: HyprCtl.cpp:1968 and :2021. dispatchRequest has no isSessionLocked check; the only isSessionLocked users are Renderer, Monitor, SessionLockManager, KeybindManager (:649 bind skip), InputManager, Touch, FocusState, WorkspaceSwipeGesture, HyprCtl (the locked command) and LuaBindingsToplevel; none in src/config/shared/actions. Nuance: FocusState does consult the lock, so focus-changing dispatches may behave differently while locked, but close/kill/exec/exit are ungated. Not tested live. hypruse trust.py:336-340 does still say Hyprland exposes no lock state and scans /proc.

Also spot-checked: socket2 event count is 45 distinct names by my own grep, format "{}>>{}\n" with data.substr(0, 1024), MAX_QUEUED_EVENTS = 64, the only "lock" event is lockgroups (group lock, not session lock). ViewQuery.cpp selector prefixes and RE2::FullMatch confirmed; j/clients carries both address and stableId.

Not verified: upstream dates and PR numbers (#6042, #15538), release-note claims, animation numbers, LockNotify protocol.
