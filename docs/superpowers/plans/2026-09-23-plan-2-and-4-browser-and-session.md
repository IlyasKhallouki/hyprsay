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

Later runs found more, in roughly this order:

- The browser client held its connection for the whole of a call. A harvest that could
  afford 0.6 s waited behind a warm's 3 s snapshot of a page that was still loading, and
  the whole harvest was dropped. The deadline now covers the wait.
- A snapshot that failed named no tab, so every capability asked for it again at 0.6 s
  each. The reading now remembers which tab it asked. A page read just after an act is
  read again a second later, because the first read of a new YouTube page had its
  buttons and not its search box. A site whose search box is missing is searched by its
  own search address when recipes know it.
- "search for linus tech tips" split Jev 0.58 to 0.31 between finding and searching.
  They are one vote now, and finding needs a playing verb.
- "a bit more" after a page scroll was chosen at 0.99 and then refused, because nothing
  in it names a scroll. The words of the last command now vouch for a bare "more of it".
- On YouTube's results page the largest list is the filter tabs, and on Spotify's it is
  the footer. The numbered list is the one on screen whose items hold more than one
  thing. An item's title is its first link: by length, Spotify's "Natural" row was named
  after its artist.
- A find on Spotify offered five "Hold the Line" and the rows' menu buttons, and turn two
  chose none. Each result is offered once, named with its other links, and with "play"
  said, its play button is what gets clicked.
- Jev was slower on the day than the client was tuned for: 536 ms p50 and 2.1 s p90 over
  138 real calls. A duplicate request now races the first after 0.8 s, and the deadline
  is 2.5 s.

The driver changed too. One run went on while the owner was using the machine, and its
clean-up closed a Firefox window and an extension's page that the owner had opened, since
it closed anything that appeared during the run. It now closes only its own window and
windows a line launched. It starts only after a stretch with no keyboard or touchpad
interrupts, and stops at the first key.

**The demo driver** (`evals/demo.py`). The ten lines, in order, in one pipeline, typed or
spoken by a synthetic Piper voice through the engine's own recognizer; each checked by what
changed (active tab, audible tabs, scroll position, volume); run in a Chrome window of its own
in the profile the extension is loaded in; tabs, players, volume and workspace restored.
