# Plans 2 and 4: the browser host, the browser adapter, the session

Written as built, on 2026-09-23, because the owner asked for the build to run without stops
once the checkpoint was reported. The spec is
`docs/superpowers/specs/2026-09-23-browser-voice-design.md`; plan 3 is the extension.

## Plan 2: hypruse (branch `browser-bridge`)

| Unit | File | Job |
|---|---|---|
| framing | `src/hypruse/browser/framing.py` | native messaging frames: 32-bit little-endian length, UTF-8 JSON, Chrome's 1 MB and 64 MiB limits |
| host | `src/hypruse/browser/host.py` | `hypruse browser-host`: relay between Chrome's pipe and `$XDG_RUNTIME_DIR/hypruse/browser.sock` (0600 in 0700); binds only after the extension's hello; rewrites request ids; broadcasts events; one browser at a time; removes its socket when Chrome closes the pipe |
| client | `src/hypruse/browser/client.py` | `BrowserClient`: one call at a time with a deadline, events kept, a timed-out call drops the connection so a late answer cannot be misread |
| install | `src/hypruse/browser/install.py` | `hypruse browser install / uninstall / status`, a launcher that runs the host with the installing Python, the doctor line (never fails the report) |

Tests: `tests/test_browser.py`, 24 cases, the extension side played through real pipes.
The vendored copy in hyprsay is identical (`diff -rq`).

## Plan 4: hyprsay (branch `browser-voice`)

**The adapter** (`src/hyprsay/adapters/browser.py`). Capabilities: switch tab (tier 0), open a
top site (1), open an address from the spoken words (1, `wants_words`, `words_kind="address"`),
and with a browser in front: click (1), search the site with its own search box (1,
`wants_words`), find by searching then choose among the results (1, `needs_query`, a longer
fetch), scroll (0), back and forward (1), close the tab (2, needs the spoken verb "close").
Readings are cached; the engine warms them at key down, on focus and after every act.

**The framework, extended rather than bypassed.** `Capability` gained `wants_words`,
`words_kind`, `verbs` and `fetch_s`; `Context` gained `words`; `Service` gained `text`
(journaled as a digest). The harvest gives the browser its own source and quota, and the
keystroke recipes for a browser window stand down while the extension serves.

**Privacy.** Tabs are named by their site; a title goes only to tell apart two tabs of one site,
cut and redacted. Page names go only while a browser is in front. `privacy.titles = "never"`
withholds all of it. `docs/PRIVACY.md` says so.

**The session** (`daemon.py`). While the key is held, 800 ms of quiet after speech ends a
command, which is finished as key up would finish it (`_finish`); the next command starts from
an empty buffer, re-pinned to the window in front. `ptt.max_hold_s` is 180.

**Found while walking the demo against the live desktop, fixed with tests:** a bare
`playerctl play-pause` reached a paused episode in another tab, so "pause" now pauses what is
playing, by name; every link inside a search result was numbered, so only an item's main link
is "result N".

**The demo driver** (`evals/demo.py`). The ten lines, in order, in one pipeline, typed or
spoken by a synthetic Piper voice through the engine's own recognizer; each checked by what
changed (active tab, audible tabs, scroll position, volume); run in a Chrome window of its own
in the profile the extension is loaded in; tabs, players, volume and workspace restored.
