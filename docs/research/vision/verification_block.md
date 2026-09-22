
## Verification

Skeptic pass on lane "competition", 2026-09-22. Method: for each claim I used a *different*
source or a *different* tool than the researcher. Local re-measurements use
`gi.repository.Atspi` directly, not `pyatspi`. Script:
`/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/vision/skeptic_atspi.py`.

Headline: two claims refuted, three overstated, three confirmed. The single most consequential
error is the claim that no Jev project exists. One does, with 16,938 stars, and it is the exact
mechanism this lane needed.

---

### V1. "I found no project on any platform using Jev for desktop or voice control", **REFUTED**

Checked with the GitHub REST API (`api.github.com/repos/...`), not a web page, so stars, creation
dates and push dates are hard data, 2026-09-22:

| repo | stars | forks | created | last push | lang |
| --- | --- | --- | --- | --- | --- |
| `browser-use/jev-ultrafast` | **16938** | 1069 | 2026-09-16 | 2026-09-18 | Python |
| `moritzkremb/jev-voice-browser` | 224 | 30 | 2026-09-17 | 2026-09-21 | JavaScript |
| `kevinbadi/jev-voice` | 68 | 9 | 2026-09-18 | 2026-09-22 | Python |
| `chris-wozniczek/jev-voice-control` | 3 | 2 | 2026-09-18 | 2026-09-21 | Swift |
| `daneknudsen8-maker/jev-voice-control` | 0 | 0 | 2026-09-19 | 2026-09-20 | JavaScript |
| `mikakostoev/jev-voice-control` | 0 | 0 | 2026-09-21 | 2026-09-21 | Swift |
| `jonatasperaza/jev-voice-windows` | 0 | 0 | 2026-09-20 | 2026-09-20 | JavaScript |
| `dg-coreylweathers/jev-voice-agent` | 0 | 0 | 2026-09-18 | 2026-09-18 | TypeScript |
| `adon68/jev-voice-control` | 0 | 0 | 2026-09-19 | 2026-09-19 | Swift (**a fork**) |

`browser-use/jev-ultrafast` is the viral project the owner asked about. 16,938 stars in six days,
by Browser Use, built jointly with TypeSafe. It is not voice, but it is the mechanism every voice
repo above then reuses. From its README [P]:

- "A browser agent with a dynamic, indexed action space." Every observation produces a new element
  table (`[1] button Change ticket type`, `[2] combobox Where from?`, ...).
- Operations are a closed set: `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`,
  `DONE`, `BLOCKED`. "Only supported operations and targets are offered."
- "Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute.
  Two decisions, **one network round trip**. Each target head contains only compatible elements."
- "There are no site-specific action scripts or prepared field strings in the policy."
- A small LLM writes text only when the operation is `TYPE_TEXT`.
- Vendor claim, not verified by me: "Zürich to London on Google Flights in 7.1 seconds."

That last bullet is the owner's complaint 6 answered by name: no pre-baked routes, and the model
never emits a selector.

`moritzkremb/jev-voice-browser` (224 stars) is complaint 5 already shipped [P, README]: speech is
streamed word by word from the Web Speech API, and "on every partial transcript the server asks Jev
one request with a dozen typed questions - intent, target element, site, 'is the command complete?',
'is this even addressed to me?', 'is it destructive?' - gets typed probabilities back in ~250-350 ms,
and code decides whether to act, wait, ask, or ignore." Debounce 200 ms, page snapshot capped at
100 elements labelled `e01..eNN`, 9 to 11 questions per request, thresholds in `constants.js`,
about $0.0002 per call. It handles "two commands in one breath" through the typed questions rather
than a string split, which is complaint 1. It keeps `previous_page` plus the last three executed
actions in the state because "Jev has no memory between requests, so the memory lives in the state".

`daneknudsen8-maker/jev-voice-control` is complaint 3 solved explicitly [P, README]: "If a named
site is already open in a tab, it switches to that tab instead of reloading it", and a spoken verb
disambiguates ("click on X" acts on the page, "go to X" may prefer an existing tab).

`kevinbadi/jev-voice` (68 stars) ports `jev-ultrafast`'s loop "from the DOM to the macOS
Accessibility tree", with bounded runs (40 actions, 80 Jev calls, three no-op actions stop the run)
and a re-read of role, label, value and enabled state plus a centre hit-test before every input so a
covered control is never clicked.

The shared design rule, stated independently in three of these repos, is worth copying verbatim:
**Jev selects, it never generates.** Code enumerates candidates, Jev picks an index, dictated text
is copied from the transcript and never passes through the model.

Caveat, stated plainly: I confirmed these repositories exist, contain code, and describe these
designs. I did **not** run them, because that needs a paid TypeSafe key. Every latency and success
figure inside those READMEs remains an author claim [V], including the 7.1 seconds and the
250-350 ms.

Consequence for the plan: the lane's conclusion, "there is no Jev project to copy, only the design
to exploit", is wrong in the most expensive direction. There are several, they are one week old,
and between them they have already solved complaints 1, 2, 3, 5 and 6 on other platforms.

---

### V2. "AT-SPI costs 0.22 to 0.24 ms per element, so a 400 element page costs about 90 ms", **REFUTED**

Three separate problems.

**(a) The stated tool was not available.** The claim cites "pyatspi walk". `pyatspi` does not import
on this machine, neither under system `python3` nor under `/home/ilyask/projects/hypruse/.venv`
(`ModuleNotFoundError: No module named 'pyatspi'`) [M]. The project's own `src/hypruse/a11y.py`
uses the `gi` bindings. The source attribution on this measured claim is wrong.

**(b) My re-measurement is 25 to 40 percent slower.** Using `gi.repository.Atspi`, three runs per
app, fetching name and role per node exactly as the claim specifies [M]:

```
top-level apps: 13, enumerate cost 18.76 ms        (claim said 2.3 ms)
waybar: 88 elements, 28.3 / 28.9 / 29.4 ms         (claim said 87 elements, 19.6 / 20.7 ms)
        per element 0.322 / 0.328 / 0.334 ms       (claim said 0.22 to 0.24 ms)
google-chrome: 1 element, 0.7 ms
```

Element count matches within one. Per-element cost does not: 0.27 to 0.34 ms across all runs, never
0.22. Top-level enumeration is 8x the quoted figure.

**(c) The extrapolation is invalid, and the researcher's own data in this same directory disproves
it.** The number is measured on waybar, an in-process GTK layer-shell bar, and then applied to a
web page reached over a different transport. `scratchpad/vision/cdp_out2.json`, written by the
browser lane four hours earlier, measures actual pages [M, theirs]:

| page | AX nodes | `Accessibility.getFullAXTree` | `DOM.getDocument` | JS extract (median) |
| --- | --- | --- | --- | --- |
| Wikipedia "Hyprland" | 1826 | **172.1 ms** | 77.0 ms / 2851 nodes | 19.3 ms |
| YouTube search results | 1554 | **185.7 ms** | 243.0 ms / 4509 nodes | 121.3 ms |
| DuckDuckGo results | - | - | - | (same harness) |

Real pages are 1554 to 1826 accessible nodes, not 400. At my measured 0.33 ms per element an
AT-SPI walk of 1826 nodes would be roughly 600 ms, and that is before Chromium's cross-process
AT-SPI bridge, which is slower than CDP, not faster.

**Correction to use in the plan.** Do not budget 90 ms and do not read pages over AT-SPI. Budget
**170 to 190 ms** for `Accessibility.getFullAXTree` over CDP, or **19 to 121 ms** for the
JS extraction path the browser lane already wrote, which returns 32 to 62 deduplicated viewport
elements, close to `jev-voice-browser`'s 100-element cap. AT-SPI stays the right tool for GTK and
Qt application chrome, where 88 elements in 29 ms is genuinely cheap.

---

### V3. Chrome 151 needs `--force-renderer-accessibility`, **CONFIRMED, with a correction to the detector**

Issue `trycua/cua#2915` exists and says what was quoted. Title: "a11y.rs: the Chromium
ScreenReaderEnabled premise no longer holds on current Chrome", opened 2026-08-05 by `f-trycua` [P].
Quoted comparison: no flags gives "1 element (bare frame)", with the flag gives "413 elements, incl.
document web, 44 links". Suggested signature: "a Chromium-family process whose AT-SPI application
has exactly one child and no descendants".

Independently reproduced here, by a different route (`dbus-send` to `org.a11y.Status`, `/proc`
cmdline inspection, `gi` walk) [M]:

- `IsEnabled` is `true`, `ScreenReaderEnabled` is `false`.
- Google Chrome 151.0.7922.137, running `--ozone-platform=wayland`, with no accessibility flag on
  any renderer cmdline.
- Its AT-SPI application node answers in 0.7 ms and exposes nothing.

**Correction.** On this machine the `google-chrome` application node reports **zero** children, not
one. Five of the thirteen registered applications (`nm-applet`, `udiskie`, `blueman-applet`,
`blueman-tray`, `xdg-desktop-portal-gtk`) also report zero children, so a detector keyed on the
trycua signature of "exactly one child" would both miss Chrome here and cannot be disambiguated by
child count alone. Key it on the process (`/proc/<pid>/exe` or the cmdline naming a Chromium binary)
plus an empty tree, not on the child count.

The 413 versus 1 numbers themselves remain one author's measurement in an open issue. I did not
reproduce them, because that needs launching a second browser.

---

### V4. Deepgram Flux eager end-of-turn, **CONFIRMED on mechanism, OVERSTATED on numbers**

Event names confirmed verbatim on Deepgram's own page: `EagerEndOfTurn`, `TurnResumed`,
`EndOfTurn` [P]. The consistency guarantee is confirmed verbatim: "`EndOfTurn` transcript will
exactly match the `EagerEndOfTurn` transcript" [P].

Parameters, checked against `developers.deepgram.com/docs/flux/configuration` [P]:

| parameter | report said | documentation says |
| --- | --- | --- |
| `eager_eot_threshold` | "default 0.3" | range 0.3 to 0.9, **default None**, eager disabled unless set |
| `eot_threshold` | "0.5 to 0.9 default 0.7" | range **0.5 to 1.0**, default 0.7 |
| `eot_timeout_ms` | named, no value | range 500 to 60000, **default 5000** |

So 0.3 is the floor of the range, not a default, and eager mode is off out of the box. There is also
a constraint the report omits: "eager_eot_threshold must be less than or equal to eot_threshold (if
both are set)."

**The latency numbers do not come from Deepgram.** Deepgram's own eager-EOT page says only
"hundreds of milliseconds" and "100-200ms of end-to-end latency", alongside the honest
"50-70% more LLM calls" that the report quotes correctly. The exact figures "~150 ms median" and
"~350 ms top 5 percent" appear on **Telnyx's** release note for their Flux integration
(`telnyx.com/release-notes/automatic-eager-end-of-turn-deepgram-flux`), which states no
methodology, no sample size, no environment and does not attribute them to Deepgram [V]. Treat them
as an unattributed third-party vendor figure, not a Deepgram specification.

Also worth noting for the plan: hyprsay is push-to-talk, so the key release already supplies a
hard turn boundary. Flux's value here is the *eager* half, not the EOT detector.

---

### V5. Voice-Light arXiv 2609.20995, **CONFIRMED on the rules, OVERSTATED on the result**

The paper is real: "Voice-Light: A Full-Duplex Cascaded Voice Agent with Causal Turn-Taking and
Speculative Generation", Bertil Braun, submitted 2026-09-17 [P].

Confirmed verbatim, and these are the parts worth copying:
- "After 80 ms of low Silero speech probability, the adapter receives another 80 ms opportunity to
  open a speculative response candidate."
- "Revisions limited to case, punctuation, whitespace, or apostrophes preserve work; lexical change
  invalidates it."
- The negative result: learned adapter 12.53 percent end-of-turn recall, 205 of 1636 EOT cases,
  against Silero VAD at 95.60 percent; adapter false cutoffs 2.70 percent; "most turns therefore
  used the 800 ms timeout". The report said "1673 test cases"; 1,673 is the candidate count and
  1,636 is the EOT-case count.

**The overstatement.** "Promoted turns reached 667 ms median against 1513 ms without" is presented
as the payoff of speculation. In the paper those medians cover **9 turns and 4 turns** out of a
13-turn subset, and the paper explicitly says the difference reflects correlation with "transcript
stability and turn difficulty" rather than causal isolation. The paper's own headline figure is
**758 ms median across 36 measured turns**. The evaluation is one operator, three sessions,
unscripted and unblinded, and the paper says so itself.

Net: take the 80 ms trigger and the invalidation rule, which are free and mechanical. Do not quote
667 against 1513 to anyone as evidence that speculation halves latency.

---

### V6. Jev's own specification, **CONFIRMED, with two corrections**

Checked against two sources the researcher did not use: TypeSafe's launch post and Cloudflare's
model page [P/V].

Confirmed: choice cardinality up to 255; all questions answered in one query, in parallel; 70 to
500 ms end to end; $0.042 per million input tokens with output "FREE (too cheap to meter)"; no
free-form text generation; launched 2026-09-15.

Corrections:
1. The yes/no type is not called "boolean". Cloudflare's model page names the three types
   `noul` (boolean, returns one probability 0 to 1), `choice`, and `score`. The voice repos above
   all use the word `noul`. Use the real name in any design document.
2. "Score over 2 to 10 levels" is **unverified**. Cloudflare says "customizable criteria levels"
   with no numeric range, and the launch post gives none. Do not design against a 10-level cap
   until it is checked in the API reference.

New fact the report missed and the plan needs: Cloudflare states Jev's **context window is 32,000
tokens** [P]. That is the hard ceiling on how large an element table can be, and it is the reason
`jev-voice-browser` caps its page snapshot at 100 elements.

The adoption claim is confirmed and stronger than the report's secondary sourcing: Vercel's own blog
post, "Jev is the fastest-adopted model in AI Gateway history", plus Vercel's post on X: "In the
first day, @typesafeai reached ~13% of teams, 2x the GPT-5.6 family and 6x Fable 5.1" [P].

---

### V7. MPRIS "capabilities cannot be discovered by introspection", **OVERSTATED**

The empty node is real. `gdbus introspect` against
`org.mpris.MediaPlayer2.chromium.instance1325064` returns `node /org/mpris/MediaPlayer2 { };` [M].
But the conclusion does not follow, and two checks on the same bus disprove it [M]:

1. `org.mpris.MediaPlayer2.plasma-browser-integration`, present on this machine right now,
   introspects **in full**, listing `Next`, `Previous`, `Pause`, `PlayPause`, `Stop`, `Play`,
   `Seek`, `SetPosition` and `OpenUri`, with identity "Google Chrome".
2. For the object that introspects empty, `org.freedesktop.DBus.Properties.GetAll` returns the whole
   capability set anyway: `CanControl true, CanGoNext false, CanGoPrevious false, CanPause true,
   CanPlay true, CanSeek true`, plus live `Metadata` and `PlaybackStatus Playing`.

So capabilities are discoverable; `Introspect` is simply the wrong call. Query `GetAll` on
`org.mpris.MediaPlayer2` and `org.mpris.MediaPlayer2.Player` and read `Can*`. That is a better
finding than the one in the report, because it means hyprsay can build an MPRIS capability manifest
today with two D-Bus calls.

Two cautions the report should have carried. Both browser players here report
`SupportedUriSchemes: []` and `SupportedMimeTypes: []`, so `OpenUri` is not advertised as usable
even where the method exists. And the Spotify `OpenUri` claim is **unverifiable on this machine**:
no Spotify client is installed or running, the only players present are two Chromium-backed ones.

---

### V8. Talon 1.0, **CONFIRMED**

Fetched the changelog directly. All four parts check out verbatim [P]:
- "NOTE: Talon 1.0 (plus any minor fixes) is planned to be the last public release of Talon for
  Linux/X11." (Talon 1.0, 2026-09-20)
- "New `axquery` system for CSS-style queries against the accessibility hierarchy."
- "Introducing three new GPU-accelerated speech models (2026-09-20): Hum (streaming dictation),
  Song (command recognition + rejection), Tone (speaker verification)".
- No mention of LLM, language model, GPT, Claude or any AI integration anywhere in the changelog.

The previous release entry is Talon 0.4.0, 2023-07-24, which makes 1.0 a three-year jump and its
Linux farewell deliberate rather than incidental.

One label correction: "hyprsay has no direct competitor" is **the researcher's inference [I]**, not
a finding, and V1 partly undercuts it. A macOS and a Windows Jev voice controller appeared this
week, and `jev-voice-browser` runs on Linux today through Playwright. The platform gap is narrower
than the report implies and is closing weekly.

---

### V9. Raycast, **CONFIRMED on mechanism, OVERSTATED on two details**

AI Extensions confirmed verbatim [P]: "the AI figures out which tool to call, with what arguments,
and runs it for you"; tools chain in a single prompt
(`@calendar @things any free slots tomorrow...`); the system "always asks before reading credentials
such as `.env` files or private keys, and before any destructive action". The
`@spotify play something mellow but not sad` example is on the page.

Screen Awareness confirmed almost item for item, including the constraints that matter:
"It doesn't steal focus from the app you're in and it doesn't touch your clipboard", and the payload
covering frontmost app and window title, readable text "taken from the system accessibility layer",
the selection, the focused control "including its value", a screenshot, and for a browser "the tab's
title, URL, and content" [P].

Overstated:
1. "Spotify is a built-in extension" is not supported by the manual, which presents `@spotify` as an
   example prompt and does not list Spotify among built-in extensions. The distinction matters: if
   it is a store extension, the transferable lesson is the manifest format, not a vendor
   partnership.
2. "macOS v0.71 beta" is not on the page. It is presented as a Pro feature on macOS with no version
   number and no beta label. Minor, but it is a fabricated specific.

---

### Verdict on this lane

The quoting discipline is genuinely good. Every verbatim quotation I checked (Talon, trycua, Flux
events, Voice-Light's two rules, Raycast's constraints) came back exact, which is rare.

The failures cluster in one place: claims the researcher generated themselves rather than quoted.
The AT-SPI measurement names a tool that is not installed, is 25 to 40 percent optimistic where I
could re-measure it, and is extrapolated to a case that their own benchmark file in the same
directory measures at 2x the estimate. The 150/350 ms Flux figures and the Voice-Light 667/1513
comparison are both real numbers lifted out of the context that limits them. And the "not found"
conclusion missed a 16,938-star repository published six days ago that is the direct answer to the
owner's actual question.

Use the primary quotations. Re-derive every number.
