# Lane: controlling a browser properly (hyprsay)

Research date 2026-09-22. Target machine: i5-8350U, no GPU, 23 GiB RAM, Arch (EndeavourOS),
Hyprland 0.56.2, AZERTY. Google Chrome **151.0.7922.137** at `/opt/google/chrome/chrome`,
already running under `--ozone-platform=wayland` (pid 1325064, up 8.7 days). Firefox 153.0.4
also installed. No chromium, no brave. node/npm/npx present.

Every number tagged MEASURED was produced on this machine today, against a scratch headless
Chrome 151 on `--remote-debugging-port=9333` with a throwaway `--user-data-dir`, viewport
emulated to 1920x1080, 2.5 s settle after load. The user's own Chrome was never touched and
is still running. Scripts and raw JSON: `cdp_bench.py`, `cdp_out2.json`, `budget.py`,
`click_bench.py` beside this file.

Labels used: **[P]** primary source, **[S]** secondary, **[V]** vendor claim, **[M]** measured
locally today, **[I]** my inference.

---

## 0. The headline, first

**The owner's normal Chrome can never be driven over a debugging port. This is not a
configuration problem; it is a Chrome security decision, and I confirmed it on his machine.**

Chrome 136 (blog post dated 2025-03-17) stopped honouring `--remote-debugging-port` and
`--remote-debugging-pipe` when the profile is the default one. Vendor wording: *"These
switches will no longer be respected if attempting to debug the default Chrome data
directory. These switches must now be accompanied by the `--user-data-dir` switch to point to
a non-standard directory."* Rationale: *"Since App-Bound Encryption was enabled we've seen an
increase in attackers using Chrome Remote Debugging to extract cookies."* **[V]**
(developer.chrome.com/blog/remote-debugging-port)

I verified this is live on Chrome 151 on this machine, with `HOME` redirected to a scratch
directory so the "default" user-data-dir was a throwaway one and the real profile was never
opened: **[M]**

| launch | port 9445/9446 opens? |
|---|---|
| headful, `--remote-debugging-port`, **no** `--user-data-dir` (i.e. default dir) | **NO. Never opens. Nothing in stderr. Chrome starts normally.** |
| headful, `--remote-debugging-port`, `--user-data-dir=<scratch>` | yes, `/json/version` answers |
| `--headless=new`, `--remote-debugging-port`, no `--user-data-dir` | yes (headless is exempt) |

The failure mode is the cruel one: Chrome does not error. It starts, looks healthy, and the
port silently does not exist. A third-party bug report describes exactly this: *"It does not
fail like a bad flag. Chrome starts, the automation client connects, reports healthy, and then
hangs on a blank page waiting for a port that was never opened."* **[S]**
(github.com/terrylica/cc-skills issue 151)

Consequences for hyprsay, in order of how much they hurt:

1. There is no way to turn on a debugging port in an already-running Chrome. The flag is read
   at startup only; no runtime switch, no D-Bus, no signal. **[I]**, and no vendor mechanism
   exists in the CDP `Browser` domain to open one.
2. Restarting Chrome with a non-default `--user-data-dir` gives you a browser with **no
   cookies, no logins, no Spotify session, no YouTube history**. "Pick a song on Spotify" is
   exactly the task that dies there.
3. Copying the real profile to a new directory is not a clean workaround: on Linux the cookie
   DB is encrypted with a key held by the login keyring (`v10`/`v11` records), and the whole
   point of the Chrome 136 change is that a non-standard directory uses a different key. **[I]**
   (vendor states the different-key property **[V]**; whether a Linux profile copy survives
   is untested here.)

So: **CDP over a debugging port is out for the owner's daily browser.** Everything below is
written around that fact.

---

## 1. What hyprsay does inside a browser today, and why it is the wrong shape

`src/hyprsay/recipes.py` (853 lines) is a table of keystroke sequences keyed by app *kind*.
The browser table has 17 entries. The one that matters, `open_url`, is literally:

```
focus -> ctrl+t -> settle 150 ms -> ctrl+l -> settle 150 ms -> type "https://<host>" -> enter
```

`search_youtube` is the same with `SEARCH_URLS["youtube"]`. The file's own docstring is honest
about why: *"the only browser window open reports class `google-chrome`, which publishes no
tree at all without `--force-renderer-accessibility`"*, and *"What every one of those
applications does honour is its keyboard."* **[P]** (the repo)

Two costs the owner is feeling:

- `BROWSER_SETTLE_S = 0.15` twice per URL command is **300 ms of pure sleep** on every
  navigation, and the comment says the value *"is [A] until someone measures the round trip on
  a loaded machine"*. That is a third of a Jev round trip spent on a guess.
- Nothing in the table can name a thing *on* the page. There is no `click the second result`,
  no `play Around the World`, because the table has no idea what the page contains.

The accessibility route hyprsay already built is real but far too slow for this. From
`src/hyprsay/a11ybus.py`, measured by the project: a 48-control KCalc window took **16.2 s**
through the `busctl`-spawning transport; the persistent-connection transport is about 10x
faster on a tree walk (waybar subtree, 91 nodes: 4.8 s -> 0.47 s), so that window *"lands near
1.6 s"*, explicitly extrapolated and not re-measured. **[P]** (the task brief quoted 14.5 s ->
1.8 s; the code says 16.2 s -> ~1.6 s estimated. Same order, different provenance.)

For comparison, on this machine **CDP gets a ranked, deduplicated, click-ready candidate list
out of a real page in 7 to 19 ms. [M]** That is 100x. The accessibility bus is not in the race
for web pages, and Chrome does not populate it at all without the flag anyway.

---

## 2. Chrome DevTools Protocol: what each call actually gives you, and what it costs here

All **[M]**, Chrome 151, 1920x1080, medians.

### Protocol overhead (paid once per session, or per call)

| thing | cost |
|---|---|
| WebSocket connect to `/devtools/browser/<id>` | 47.7 ms (second run) to 131.3 ms (cold) |
| `Target.createTarget` | 40.4 ms |
| `Target.attachToTarget` (flatten) | 21.8 ms |
| **round-trip floor** (`Runtime.evaluate "1+1"`) | **1.2 to 1.7 ms** |
| `Input.dispatchMouseEvent` mousePressed | 1.07 ms |
| `Input.dispatchMouseEvent` mouseReleased | 1.08 ms |
| `Input.insertText` | 0.95 ms |
| `Page.navigate` (returns; page not loaded) | 211 ms |

The floor is the important one: **a CDP call costs about 1.5 ms.** Anything expensive is
expensive because of what it serialises, not because of the transport.

### Reading the page: five ways, measured on five real pages

`DOM.getDocument {depth:-1, pierce:true}` returns the whole node tree as JSON.

| page | ms | wire bytes | nodes |
|---|---|---|---|
| en.wikipedia.org/wiki/Hyprland | 77 | 653 KB | 2851 |
| youtube.com/results?search_query=daft+punk | 243 | 1.09 MB | 4509 |
| open.spotify.com/search/daft%20punk | 207 | 1.10 MB | 4307 |
| news.ycombinator.com | 31 | 206 KB | 1243 |
| duckduckgo.com/?q=... | 44 | 156 KB | 501 |
| github.com/IlyasKhallouki/hypruse (800x600 run) | 212 | 985 KB | 4031 |

`Accessibility.getFullAXTree` returns the computed accessibility tree.

| page | ms | wire bytes | AX nodes |
|---|---|---|---|
| Wikipedia | 172 | 699 KB | 1826 |
| YouTube search | 186 | 582 KB | 1554 |
| Spotify search | 278 | 902 KB | 2201 |
| Hacker News | 164 | 629 KB | 1624 |
| DuckDuckGo | 82 | 155 KB | 392 |
| GitHub repo (800x600 run) | **537** | **1.61 MB** | 4477 |

`Accessibility.queryAXTree {role:"link"}` on Hacker News: **86 ms, 229 nodes.** Cheaper than
the full tree but still 50x the cost of doing it in page JS, and it filters by one role at a
time, so a real candidate set needs several calls.

`DOM.querySelectorAll` on `a[href],button,input,select,textarea`: **1.8 to 68.7 ms**, returns
only node ids (no names, no rects), so every id then needs `DOM.describeNode` or
`DOM.getBoxModel` at ~1.5 ms each. For Spotify's 554 matches that is **830 ms of round trips**
for information one `Runtime.evaluate` returns in 17 ms. **This is the single biggest
efficiency trap in CDP**, and it is exactly the 1092-calls-per-task figure that jev-ultrafast
reports cutting (see section 5).

`Runtime.evaluate` with a candidate-extraction snippet, `returnByValue:true`:

| page | total ms | in-page JS ms | wire bytes |
|---|---|---|---|
| Wikipedia | 19.3 | 14 | 12.8 KB |
| YouTube search | 121.3 | 8 | 5.2 KB |
| Spotify search | 17.0 | 13 | 11.6 KB |
| Hacker News | 7.1 | 4 | 9.5 KB |
| DuckDuckGo | 7.1 | 3 | 2.2 KB |

(YouTube's 121 ms is the renderer being busy, not the extraction: its own `performance.now()`
delta was 8 ms. Under a busy SPA the wrapper cost is the main thread, and no protocol choice
fixes that.)

`Page.captureScreenshot {format:"jpeg", quality:80}`: 99 to 316 ms, 112 to 288 KB of base64.

**Verdict on reading:** the only sane shape is *one* `Runtime.evaluate` that does all the
filtering, naming, hit-testing and formatting inside the page and returns a small string.
Everything else moves megabytes to do work the page could do in 4 ms.

### Acting: `Input.dispatchMouseEvent` vs `element.click()`

CDP `Input.*` events arrive with `isTrusted === true` and go through Chrome's real input
pipeline: hover, focus, composition, all of it. Synthetic `element.click()` and
`dispatchEvent(new MouseEvent(...))` arrive with `isTrusted === false`, which some sites check
and which never fires native behaviours (a real `<select>` popup, a file picker, drag).
`Input.dispatchMouseEvent` costs 1.07 ms here. **[M]** That is the argument for the
`debugger` permission in section 4, and the reason section 7 proposes a third option.

---

## 3. The realistic options for this user

### A. CDP over a debugging port
**Can:** everything. Read, click with trusted events, navigate, intercept network, emulate.
**Cannot:** run on the owner's real profile (section 0). **Latency:** 1.5 ms per call, 7 to 19
ms per page read. **Demands:** the user must abandon their daily browser or run a second one.
**Fails:** silently, with a port that never opens. Also, once the port is open,
**anything on the machine that can reach 127.0.0.1 gets full control of the browser and can
read every cookie.** There is no authentication on the DevTools endpoint at all: `/json/version`
answers to bare curl, as I did today. A websocket from a malicious page cannot reach it
directly (Chrome checks Host headers) but any local process can. **[M]/[I]**
**Verdict: rejected for the daily browser. Keep it for a dedicated agent browser only.**

### B. A WebExtension the user installs
**Can:** read the page from a content script with full DOM access; enumerate every tab in
every window via `chrome.tabs.query`; run in the user's real profile with their real logins;
talk to a local process via native messaging. **Cannot:** dispatch trusted input without the
`debugger` permission (see D). **Latency:** the same 4 to 14 ms of in-page work, minus the CDP
wire, plus a native-messaging hop. **Demands:** the user installs an extension, and either
loads it unpacked in developer mode or the project publishes it to the Web Store.
**Fails:** extensions cannot run on `chrome://` pages, the Web Store itself, or other
extensions' pages; MV3 service workers are killed after ~30 s idle and must be re-woken.
**Verdict: this is the route.**

Native messaging mechanics, vendor-documented **[V]** and already proven on this machine **[M]**:

- Host manifest for Chrome on Linux, per user:
  `~/.config/google-chrome/NativeMessagingHosts/<name>.json`; system-wide:
  `/etc/opt/chrome/native-messaging-hosts/<name>.json`.
- Fields: `name`, `description`, `path` (absolute), `type: "stdio"`, `allowed_origins`
  (explicit extension IDs, no wildcards).
- Wire: each message prefixed by a **32-bit length in native byte order**, JSON, UTF-8.
  Max **1 MB** host -> extension, **64 MiB** extension -> host.
- Extension declares `"nativeMessaging"`; connects with `chrome.runtime.connectNative(name)`
  from the service worker (not from a content script).

**This machine already has three such hosts installed**, including
`com.anthropic.claude_code_browser_extension` pointing at
`/home/ilyask/.claude/chrome/chrome-native-host` with
`allowed_origins: ["chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"]`. **[M]** So the
pattern works here, under Hyprland, with the owner's Chrome, today. hyprsay would install a
fourth.

### C. Playwright or Puppeteer driving a separate browser
**Can:** everything CDP can, with a nicer API and ARIA snapshots. **Cannot:** use the owner's
profile (same Chrome 136 wall, plus Playwright deliberately uses its own browser build).
`jev-browser` downloads Playwright's Chromium on install unless
`JEV_BROWSER_SKIP_BROWSER_DOWNLOAD=1`. **[P]** **Latency:** adds a Node process and a browser
launch (seconds). **Demands:** ~150 MB download, node runtime, a second browser window.
**Fails:** logged-out. For "pick a song on Spotify" it fails at the login screen.
**Verdict: wrong tool. It is for automating tasks, not for driving the browser a human is
looking at.**

### D. `chrome.debugger` from an extension (the hybrid)
An extension may attach CDP to a tab via `chrome.debugger.attach`. This is how
`chy4pro/jev-for-chrome` sends clicks: *"Clicks and keystrokes are dispatched through Chrome's
DevTools protocol for genuine browser events. This requires the `debugger` permission (Chrome
mandates it, cannot be optional)."* **[P]**
**Can:** trusted input, on the real profile, with no command-line flag and no restart. This is
the only way to get `isTrusted` events into the owner's own Chrome. **Cannot:** be quiet.
Chrome shows an infobar on every tab (*"X started debugging this browser"*) that appears on
first attach and stays until detach; it can be closed but returns on the next interaction.
Suppression requires either the `--silent-debugger-extension-api` command-line flag (restart
needed, back to square one) or force-install by enterprise policy. **[S]**
There is a live Claude Code issue about exactly this banner (anthropics/claude-code #69287),
which means the owner has probably already seen it on this machine. **[S]**
**Verdict: keep it as an opt-in escalation, never the default.**

### E. Accessibility tree (AT-SPI, the current route)
**Can:** nothing useful in Chrome without `--force-renderer-accessibility`, which again means
a restart. **Latency:** 0.47 s for a 91-node waybar subtree on the fast transport; ~1.6 s
estimated for a 48-control window. **[P]** A web page has 1500 to 4500 AX nodes, an order of
magnitude more. **Fails:** Chrome publishes nothing by default; even when it does, this is the
slowest option by 100x. **Verdict: dead for the web.**

Note also, from a primary source, that AT-SPI/AX is not merely slow but *wrong* here:
`jev-browser`'s README says *"Elements come from the DOM directly, not the accessibility tree,
because accessibility trees under-report inputs"*, and that the agent only found DuckDuckGo's
search box after the switch. **[P]** My own DOM extractor did find it
(`1 combobox "Search privately"`). **[M]**

### F. Screenshot plus a vision model
**Can:** see anything, including canvas and video. **Cannot:** be cheap. 99 to 316 ms to
capture, 112 to 288 KB of base64, then a vision model round trip measured in seconds, on a
machine with no GPU. And Jev does not take images: it takes typed questions over options, so
vision would need a *different* model in the loop, breaking the sub-second budget the whole
project is built on. **Verdict: last resort for canvas UIs only, and probably not at all.**

---

## 4. What the viral Jev browser projects actually do

Jev launched 2026-09-15, so everything here is under a week old. There is a lot of noise: I
found at least nine different `awesome-jev*` repositories with near-identical descriptions,
plus several blog posts whose only content is a rewrite of a README. **I am treating all of
those as spam and citing only repositories whose READMEs contain specific, falsifiable
mechanics.** Three survive.

### `browser-use/jev-ultrafast` [P]
The upstream, from the browser-use team (it is on Trendshift, repo 242003). Mechanics from its
README:

- **Element table**: *"native controls (`a[href]`, `button`, `input`, `textarea`, `select`,
  `summary`, `[contenteditable="true"]`) plus elements carrying an explicit ARIA role from a
  fixed list"*, numbered, with index/type/label/current-value columns. Visible only:
  *"Offscreen article bodies and footers do not fill the model context."*
- **One Jev request per step**, *"Two decisions, one network round trip"*: which operation
  (`CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, `BLOCKED`) and
  which element, with target heads *"speculative"* and *"only compatible elements"* offered
  per operation.
- **Text is not Jev's job**: an OpenAI-compatible small model writes the string to type, and
  *"No model output becomes selectors, coordinates, shell commands, or executable JavaScript."*
- **Timing discipline**: combobox typing waits up to **200 ms** for suggestions, everything
  else *"at most two animation frames or 50 ms"*.
- **Measured**: Google Flights Zürich to London end to end in **7,073 ms**; median across 6
  runs **9.450 s -> 7.092 s** (25% faster); **median browser protocol calls 1,092 -> 101**.
  Wikipedia article 2.798 s, hotel filter 1.896 s.
- **Limits, stated**: *"Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling,
  and arbitrary keyboard widgets remain outside this MVP."*

The 1,092 -> 101 figure is the lesson. **The speedup did not come from the model. It came from
not making a thousand protocol calls per task.** That is section 2's verdict, independently
arrived at.

### `chy4pro/jev-for-chrome` (and its sibling `JevBrowserExt`) [P]
The MV3 Chrome extension port, explicitly *"not affiliated with TypeSafe"*. This is the closest
existing thing to what hyprsay needs, because it runs **in the tab the user is already looking
at, on their normal profile, with their cookies**.

- Content script reads the page with DOM APIs, gives each control a **code-owned index**, role,
  accessible name, current value. **No screenshots.**
- **It hit-tests**: *"The observer hit-tests every control and drops those covered by other
  blocks."* This is the step my own extractor does not do and should.
- **Password fields are never read.**
- Request payload: goal, tab URL, title, **visible text capped at 6,000 characters**, the
  element table with labels, values, `href` targets and section headings, and the **last ten
  actions with their visible outcomes**.
- Jev answers two things at once: operation+element, and two independent yes/no cross-checks
  (task achieved? recent actions stuck?). **Answers below 50% confidence are asked again once
  after the page settles.**
- Input via `chrome.debugger` CDP, with a synthetic-DOM-event fallback in Options.
- Permissions: `<all_urls>` and `debugger`.
- Measured: Google Flights one-way in **14 steps across 21 seconds**, ~1.5 s per step.
- Stated failures: shadow roots, iframes, canvas, file uploads, drag-and-drop, keyboard-only
  widgets, checkboxes hidden behind styled labels, hover-only menus, content under overlays.

### `jkudish/jev-browser` and the fork `muneeebnaveeed/jev-browser` [P]
A Playwright-driven CLI. Useful for two exact numbers:

- **"Up to 240 elements per step; Jev's Choice supports 255 options. Beyond that the list is
  truncated and the state says so."**
- Fan-out per step: action Choice (clickable/typeable/selectable elements plus scroll/back/done)
  + goal Noul + stuck Noul, with a second-stage Choice for a `SELECT`'s option.
- Thresholds: goal > **0.85**, stuck > **0.85**, max **24** steps, max **180** s, *"starting
  points measured on Wikipedia and DuckDuckGo tasks"*.
- Cost: a Wikipedia run **5,894 ms, 3 Jev calls, $0.0021**; another *"about 4 seconds, for
  $0.0016"*.

### The known break, with a fix [P]
`browser-use/jev-ultrafast` issue #23: a page whose suggestion rows are bare `<li>`/`<div>`/
`<span>` with click handlers and no `role` attribute is **invisible** to the element table. The
reported symptom on a Chinese railway booking site is brutal and exactly the kind of thing that
makes an owner say "it is not intelligent": **60 identical `TYPE_TEXT` actions into the same
field**, because the suggestion the agent needed to click was not in the table.

The proposed fix (PR #24) is a second collection pass: *"cursor:pointer, visible, inside the
viewport, no nested form control, inside a positioned layer or a pointer-cursor sibling list,
innermost candidate only, capped."* hyprsay should ship this from day one rather than
rediscover it.

**Not found:** no project I could verify uses Jev to drive a browser on Linux/Wayland, none
integrates with a compositor, and none takes voice as input. The niche hyprsay is aiming at is
genuinely empty.

---

## 5. Turning a real page into something Jev can choose over

### The funnel, measured on five real pages [M]

Extractor: `a[href],button,input,select,textarea,summary,[contenteditable="true"]` plus 17
explicit ARIA roles plus `[onclick]` plus `[tabindex]:not([tabindex="-1"])`, one level of open
shadow roots; drop rects under 2x2, `visibility:hidden`, `display:none`, `opacity:0`,
`disabled`, `aria-hidden="true"`, and anything with no accessible name; name resolved from
`aria-label` -> `aria-labelledby` -> placeholder/value/name/type for inputs -> `innerText` ->
`title` -> nested `img[alt]`, whitespace collapsed, truncated at 90 chars; role from the
explicit `role` attribute or an implicit map; dedup by (role, name).

| page | all elements | selector match | visible + named | after dedup (viewport) | after dedup (whole page) |
|---|---|---|---|---|---|
| Wikipedia article | 1783 | 474 | 69 | **62** | 322 |
| YouTube search results | 3977 | 192 | 37 | **32** | 68 |
| Spotify web search | 3440 | 637 | 113 | **102** | 207 |
| Hacker News front page | 817 | 230 | 190 | **144** | 153 |
| DuckDuckGo results | 383 | 59 | 20 | **20** | 51 |

The viewport filter is the whole game on content-heavy pages: Wikipedia goes 322 -> 62, an 81%
cut, because a Wikipedia article is mostly a footer of citation links nobody can see.

Cost per candidate line, format `<index> <role> "<name>"`: **20 to 35 characters, median 28.**

Sample, Spotify search for "daft punk", first 12 of 102 **[M]**:

```
0 button "Home"          6 button "Download"
1 button "Search"        7 link "Install App"
2 combobox "What do you want to play?"   8 button "Sign up"
3 button "Browse"        9 button "Log in"
4 button "Premium"      10 button "Create"
5 button "Support"      11 button "Create playlist"
```

Sample, YouTube search for "daft punk" **[M]**: `2 combobox "Search"`, `6 tab "All"`,
`7 tab "Shorts"`, ... The track rows themselves are further down the list. This is the shape
"pick a song" needs, and it arrives in 8 ms of in-page work.

### The budget: it is the token cap, not the 255-option cap

hyprsay's own fitted estimator (`src/hyprsay/jev/tokens.py`, worst error 2.9% over 8 real
requests) **[P]**:

```
tokens = 250 + 0.4241 * state_chars + 0.3386 * question_chars - 11.7 * n_questions
DEFAULT_CAP = 1800
```

and the reason for the cap, from the same file: *"Latency is flat up to roughly 2.2k input
tokens (p50 about 315 ms), then climbs: 448 ms at 6k, 544 ms at 11.5k. Failures climb with it:
interleaved in the same minute, small requests succeeded 25 of 25 while 11.5k-token requests
returned HTTP 503 four times in 25."*

I ran the real candidate lists through that estimator with a small page state (title, URL,
what was said, one recent action) and three questions **[M]**:

| page | viewport list | verdict | whole-page list | verdict |
|---|---|---|---|---|
| Wikipedia (62 / 322) | 882 tok | FITS | 4106 tok | **OVER** |
| YouTube search (32 / 68) | 811 tok | FITS | 1574 tok | fits |
| Spotify search (102 / 207) | 1470 tok | FITS | 3063 tok | **OVER** |
| Hacker News (144 / 153) | **1911 tok** | **OVER** | 1954 tok | **OVER** |
| DuckDuckGo (20 / 51) | 569 tok | FITS | 848 tok | fits |

Floor with an empty option list: **400 tokens.** Ceiling by binary search: **108 to 137
candidates** at 28 chars each, **68** at 35 chars each, **51** at 22 chars each.

**So the binding constraint is hyprsay's 1800-token cap at roughly 120 candidates, not Jev's
255-option limit. The 255 cap is never reached.** Plan for ~100 and the 255 limit is
irrelevant.

And a warning about copying the extension verbatim: **jev-for-chrome's 6,000-char visible-text
block, plus 40 candidates, estimates at 3,372 tokens on hyprsay's fit. That is 1.9x the cap,
past the 2.2k latency knee, and into the region where the service returned 503s.** Cut visible
text to 600 chars and the same request is **1,081 tokens**. **[M]** hyprsay cannot afford the
page-text context that a text-generating agent leans on; it has to lean on the candidate
*names* instead.

### What has to be dropped, in order

1. **Everything outside the viewport.** Biggest single cut (Wikipedia 322 -> 62). Give Jev a
   `SCROLL_DOWN` option so it can ask for the rest, exactly as jev-ultrafast does.
2. **Everything with no accessible name.** Spotify 637 selector matches -> 113 named.
3. **Duplicates by (role, name).** Hacker News 190 -> 144.
4. **Names truncated to ~60 chars**, not 90. At 28 chars/candidate median the tail is cheap,
   but a single 90-char YouTube title costs three candidates' worth of budget.
5. **Rank by local text match to what was said and keep the top N.** This is the lever that
   makes Hacker News fit. A token-overlap score over 400 candidates costs **1.467 ms** in pure
   Python on this machine **[M]**: free, next to a 315 ms Jev round trip. Keep the top 60 by
   score, then unconditionally add every candidate whose role is in a small always-keep set
   (`textbox`, `searchbox`, `combobox`, `button` in the top chrome), so the search box is never
   ranked away.
6. **Do not send page text.** Send the title, the URL host, and up to ~400 chars of the
   nearest heading context instead.

### What to add that nobody in the funnel above does

- **Hit-testing.** `document.elementFromPoint(cx, cy)` at each candidate's centre, keeping the
  candidate only if the hit is the element or a descendant. jev-for-chrome does this
  (*"drops those covered by other blocks"*) and it is what stops the agent clicking through a
  cookie banner. Costs one forced layout per candidate; at 100 candidates that is well inside
  the 4 to 14 ms budget already measured, because the extractor already calls
  `getBoundingClientRect` on all of them. **[I]**
- **The `cursor:pointer` second pass** from issue #24, capped, innermost-only. Without it,
  Spotify's own track rows (which are `div`s with handlers in several of their layouts) may not
  appear at all. **[P] for the technique, [I] for the Spotify specifics.**
- **Section headings as a column.** jev-for-chrome carries them; on a search results page the
  difference between "Songs", "Artists" and "Playlists" is the entire meaning of "pick the
  song". Cheap: nearest preceding `h1..h6` or `[role=heading]`, deduplicated, ~10 chars.

---

## 6. Two things the owner complained about that this lane answers almost for free

**"It opens a new one in my workspace instead of going to the one already open."** For the
browser specifically, `chrome.tabs.query({})` returns every tab in every window with `id`,
`windowId`, `title`, `url`, `active`, `lastAccessed`. That is the browser's analogue of
hyprsay's `desktop` snapshot, it costs no page work at all, and it turns "go to my Spotify"
into a choice over existing tabs before it is ever a navigation. Combined with
`chrome.windows.get(windowId)` and hyprsay's existing hyprctl window list, hyprsay can focus
the right Chrome *window* on the right *workspace* and then activate the right *tab*. **[I]**,
built on vendor-documented APIs **[V]**.

**"It should already be doing stuff while I am talking."** Extraction is 4 to 14 ms of in-page
work **[M]**. An extension content script can maintain a warm element table on a debounced
`MutationObserver` plus `scroll`/`resize`, push it to the native host on change, and the daemon
then holds a current table at the instant the push-to-talk key goes *down*. Combined with
hyprsay's existing key-down prewarm of the Jev connection, the first Jev request can be in
flight before the key comes up. The cost is a few milliseconds of main-thread work per page
mutation, which at a 250 ms debounce is under 6% of one core. **[I]**

---

## 7. Recommendation

**Build a Manifest V3 extension plus a native-messaging host into hyprsay. Make the extension
the eyes and hypruse's existing Wayland pointer the hands.**

The pieces, in the order they should be built:

1. **Native host first, no extension.** A `hyprsay browser-host` subcommand speaking the
   32-bit-length-prefixed stdio protocol, and an installer that writes
   `~/.config/google-chrome/NativeMessagingHosts/dev.hyprsay.browser.json`. This is a known
   quantity: three such hosts already work on this machine.
2. **Content script = one function, `snapshot()`**, returning `{title, url, headings,
   candidates:[{i, role, name, rect, section}], truncated}`. Viewport-first, named-only,
   deduplicated, hit-tested, with the `cursor:pointer` second pass, capped at 120 before
   ranking. Budget: 15 ms.
3. **Ranking and slicing in the daemon**, in Python, against the transcript. 1.5 ms. Cap the
   Choice at whatever the token estimator says fits under 1800 with the state actually being
   sent, not at a fixed number.
4. **Acting, in three tiers, cheapest and quietest first:**
   - **Tier 1, no permission, no banner:** the content script focuses the element and calls
     `.click()` / sets `.value` with proper `input`+`change` events. Works on most of the web.
   - **Tier 2, the interesting one:** the extension returns the element's client rect; the
     daemon converts it to Hyprland global coordinates using the window rect it already has
     from `hyprctl` plus the browser's chrome offset (`outerHeight - innerHeight`,
     `outerWidth - innerWidth`, `devicePixelRatio`, all readable from the page), and clicks
     with **`zwlr_virtual_pointer_v1`, which hyprsay already owns**. The click is a real
     hardware-level click: `isTrusted` is true, no `debugger` permission, no infobar, and every
     one of hyprsay's existing trust guards, the journal, the beacon and the panic key apply
     unchanged. This is the piece nobody in section 4 has, because none of them have a
     compositor.
   - **Tier 3, opt-in:** `chrome.debugger` + `Input.dispatchMouseEvent` for the handful of
     sites that defeat both, with the banner explained honestly in the docs.
5. **Delete the browser half of `recipes.py`** once tiers 1 and 2 work, or reduce it to the two
   entries that are genuinely keyboard-shaped (`ctrl+w`, `ctrl+pgdn`). `open_url` becomes
   `chrome.tabs.create`/`chrome.tabs.update`, which removes 300 ms of blind `BAR_SETTLE_S`
   sleep per navigation and removes the risk of a URL being typed into a page.
6. **Firefox, later.** Firefox still honours `--remote-debugging-port` for WebDriver BiDi and
   has **not** adopted Chrome's default-profile restriction as far as I can tell; CDP support
   has been off by default since Firefox 129 and is being removed. **[S]** Firefox 153 is
   installed here. But it needs a restart with a flag, so the extension route is better there
   too, and a WebExtension written for Chrome MV3 ports with modest changes.

**What this costs on the target machine.** Per spoken in-page command: 15 ms snapshot (already
warm if the observer is running), 1.5 ms ranking, one Jev round trip at the measured 315 ms p50
for a sub-2.2k-token request, 1 to 3 ms to act. **Call it 350 ms from key-up to click**, against
the 300 ms of pure `sleep` that a single `open_url` recipe burns today. Money: the same one Jev
call per command the project already budgets; jev-browser's measured $0.0016 to $0.0021 was for
a 3-call multi-step task, so a single-step voice command is well under a tenth of a cent.
Complexity: an extension (roughly 400 lines of JS), a native host (roughly 200 lines of Python
on top of what exists), an installer, and a real distribution problem, because an unpacked
extension in developer mode is a bad experience and the Chrome Web Store means review and a
developer account. **That distribution problem is the largest real cost in this plan, and it
should be decided before any code is written.**

---

## 8. Not found / not verified

- No primary evidence that any Jev project drives a browser on Linux, Wayland, or from voice.
- The Chrome 136 restriction on **headful** Chrome is confirmed here by my own test; whether
  copying a Linux Chrome profile to a non-default directory preserves logins is **untested**.
- Whether Firefox restricts `--remote-debugging-port` on the default profile: **untested**,
  only inferred from the absence of any such announcement.
- The `--silent-debugger-extension-api` flag and the enterprise-policy exemption for the
  debugger banner come from secondary sources; I did not test either.
- YouTube and Spotify were measured **logged out**, in headless Chrome. A logged-in Spotify
  library page will have more candidates than the 102 measured; the ranking step in section 5
  is what absorbs that, but the exact number is unknown.
- I did not measure `Runtime.evaluate` against the owner's own Chrome, because doing so is
  precisely what section 0 says is impossible.

---

## Verification

Skeptic pass, 2026-09-22, independent of the researcher. Method: for each of the six findings a
plan would lean on hardest, I tried to REFUTE it with a source or a check the researcher did not
use. Work in progress below; verdicts are appended as they are settled.

Method note. Every local re-check below was run with a scratch `HOME` **and** a scratch
`XDG_CONFIG_HOME`, against `/opt/google/chrome/chrome` 151.0.7922.137, with `--no-startup-window`
so no window appeared on the owner's desktop. Scratch profiles under
`scratchpad/verify/`. Scratch Chromes were killed after each test.

### V1. Chrome 136 blocks the debugging port on the default data directory. CONFIRMED, and strengthened.

I did not use the researcher's method. Two independent checks:

- **The binary itself.** `strings /opt/google/chrome/chrome` contains the literal
  `DevTools remote debugging requires a non-default data directory. Specify this using
  --user-data-dir.` and, next to it, `DevTools remote debugging is disallowed by the system
  admin.` and `Web security may only be disabled if '--user-data-dir' is also specified with a
  non-default value.` The check is compiled into the build the owner runs. **[M]**
- **A clean run.** Headful, `--remote-debugging-port=9481`, `HOME` and `XDG_CONFIG_HOME` both
  redirected to a scratch tree: port never opens. Same launch with `--user-data-dir=<scratch>`
  on port 9482: `/json/version` answers `"Browser": "Chrome/151.0.7922.137"`. **[M]**

The lane's headline survives. The plan may build on it.

### V2. "Never opens the port, prints nothing to stderr, and starts normally." REFUTED, twice.

**The failure is not silent.** My clean run printed, on stderr, one line:

```
DevTools remote debugging requires a non-default data directory. Specify this using --user-data-dir.
```

So the `matters_because` on that finding ("the failure is silent, which is how a team wastes a
week debugging it") is wrong, and the cited third-party report of a silent hang describes some
other failure. Chrome names the problem and names the fix.

**The researcher's own test did not measure what they think it measured.** Their run redirected
`HOME` only. On Linux, Chrome resolves its default user data directory through
`XDG_CONFIG_HOME`, which on this machine is set to `/home/ilyask/.config` (verified in the
environment). Three pieces of evidence that the redirect did nothing:

1. `scratchpad/vision/fakehome/` and `fakehome2/` contain only `Documents`. No
   `.config/google-chrome` was ever created in them. My own run, with `XDG_CONFIG_HOME`
   redirected too, **did** create `h1/.config/google-chrome`.
2. `chrome-headful-default.log` reads, in full: `Opening in existing browser session.`
   That is Chrome's process singleton forwarding the command line to an already running
   instance and exiting. The running instance was the owner's own Chrome:
   `~/.config/google-chrome/SingletonLock -> ozzy-1325064`, and pid 1325064 is the owner's
   8-day-old browser.
3. A forwarded launch exits immediately, so port 9445 was never going to open regardless of the
   Chrome 136 rule.

Two consequences. The headful-default row in that table is evidence of the singleton, not of
the restriction. And **"the user's own Chrome was never touched" is false**: a command line was
handed to it at 07:56:36, which normally opens a window or tab.

**"--headless=new is exempt" is also wrong in mechanism.** Headless is not exempt from the
rule; it never uses the default directory. I ran `--headless=new` with no `--user-data-dir` and
inspected the resulting process arguments: Chrome had rewritten them to
`--user-data-dir=<XDG_CONFIG_HOME>/google-chrome-headless/scoped_dirGVz8pY`. There is no
exemption to exploit; there is only a temp directory, which is the opposite of what "run on the
owner's real profile" needs. **[M]**

### V3. One `Runtime.evaluate` beats `DOM.getDocument` and `Accessibility.getFullAXTree`. CONFIRMED independently.

Re-measured today with my own extractor, my own scratch headless Chrome on port 9491, 1920x1080,
3 s settle:

| page | one evaluate | `DOM.getDocument` | `Accessibility.getFullAXTree` |
|---|---|---|---|
| news.ycombinator.com | **5.6 ms, 6.8 KB** (2.9 ms in-page, 190 candidates) | 69.0 ms, 224 KB | 153.0 ms, 674 KB |
| en.wikipedia.org/wiki/Hyprland | **21.5 ms, 2.4 KB** (19.5 ms in-page, 79 candidates) | 58.8 ms, 678 KB | 83.2 ms, 748 KB |

Different absolute numbers from the researcher's (their HN `getDocument` 31 ms against my 69 ms;
their Wikipedia 77 ms against my 58.8 ms), same conclusion: roughly 10x on time and 50x to 100x
on bytes. The read shape in the recommendation is safe.

Two corrections. Their round-trip floor of **1.2 to 1.7 ms is optimistic**; I measured a median
of **2.16 to 2.20 ms** on the same machine a few minutes later, so a 100-call plan should be
budgeted at about 220 ms, not 150 ms. And my average candidate line came out at **24.6 and 18.1
characters**, near their median of 28, on those two pages only. See V6 for why that median does
not hold on the pages the owner cares about.

### V4. `browser-use/jev-ultrafast` and its benchmark. Project CONFIRMED, numbers OVERSTATED.

The repository exists (github.com/browser-use/jev-ultrafast, Trendshift 242003, issues in the
100s). The mechanics quoted in the lane match the README.

The benchmark does not deserve the word "measured" without a caveat. `docs/performance.md`, read
directly, says the comparison is **three pairs of alternating runs, six attempts total**, against
frozen commit `68c077bf79caca4e817b8e8a5854b2efa0c81ff6`, at a **1120x780 viewport**, with **no
CPU, RAM, OS or network specification**, and that *"Google, network responses, routing, and
browser caches remain live."* The authors' own words: *"Three pairs are too few for a strong
statistical claim"* and *"a small controlled-input comparison, not a broad agent benchmark."*
The README adds: *"three repeats of one task on one browser profile, not a general reliability
benchmark."* **[P]**

So 7,073 ms and 1,092 -> 101 are **project self-reports at n=3 on live Google from unstated
hardware**. They are fine as an architectural signal, which is how the lane uses them. They are
not a latency budget, and no hyprsay figure should be derived from them.

**One unresolved tension the plan should not paper over.** The same README says Chrome connects
through *"Browser Harness"*, tells the user to *"Allow remote debugging in Chrome when
prompted"*, and states that *"owned tabs share the existing Chrome profile."* On its face that
contradicts V1. I could not establish from the README how Browser Harness obtains a debugging
port, whether it copies the profile, uses Chrome for Testing, or something else.
**Unverifiable as stated**, and worth thirty minutes of reading `browser_harness` before the
plan declares CDP-on-the-real-profile impossible in absolute terms. My own measurement says the
port refuses on the default directory; it does not say nobody has found a different door.

### V5. `chy4pro/jev-for-chrome`. CONFIRMED on every mechanic I checked.

Read the README directly rather than through the lane's summary. Confirmed verbatim: input
*"dispatched through Chrome's DevTools protocol, so the page receives a real click or keystroke"*
with a synthetic-event fallback in Options; `<all_urls>` and `debugger`, *"cannot be made
optional in Chrome"*; payload of goal, URL, title, **visible text capped at 6,000 characters**,
the element table, and the last ten actions; **no screenshots**; sub-50% confidence re-asked once
after settling; password fields never read; `chrome://` refused; the limitation list matches. It
describes itself as *"a Manifest V3 port of browser-use/jev-ultrafast"* and *"not affiliated with
TypeSafe or Browser Use."*

One correction. The lane's *"Google Flights one-way in 14 steps across 21 seconds"* I could not
find. The README reports **1 to 22.4 seconds across 17 tasks**, self-reported from its own
harness. Treat the per-step figure as approximate.

Issue #23 and PR #24 also check out: opened by **Mind-Hand on 2026-09-18**, site
**kyfw.12306.cn**, 60 repeated text inputs, cause is bare `div`/`span` suggestion rows with no
role, **still open**, PR #24 linked, and PR #24 reports the suggestion list growing **60 -> 72
elements** on that page with no regression on others. Note for the plan: **PR #24 is proposed,
not merged, and was validated on one page.** Shipping it from day one is a good idea, but it is
an untested patch, not a proven technique.

### V6. The 1800-token ceiling at "108 to 137 candidates". OVERSTATED, and optimistic for this owner.

I ran `hyprsay.jev.tokens.estimate` myself with a Spotify-shaped state and three questions
(action Choice, target Choice, done Boolean), binary-searching the option count:

| candidate line length | my ceiling | lane's figure |
|---|---|---|
| 22 chars | **164** | 51 |
| 28 chars | **133** | 108 to 137 |
| 35 chars | **109** | 68 |

The 28-char row agrees. The other two are **internally impossible in the lane's version**: a
shorter candidate line cannot fit fewer candidates, yet the lane reports 51 at 22 chars and 68
at 35 chars. Those two numbers are wrong whatever the state shape.

Confirmed in passing: the 6,000-character warning. My run gives **3,339 tokens** against the
lane's 3,372, and **1,049** against their 1,081 at 600 characters. The conclusion holds. Jev's
255-option cap is also real and vendor-documented (docs.typesafe.ai/api). **[V]**

**The real problem is the 28-character median.** `tokens.py`'s own docstring, which the lane
quotes selectively, says the fit is **eight points for four parameters**, that *"the sample was
synthetic English"*, that *"Paths, Unicode and long identifiers in real window titles tokenize
worse"*, and that the estimator running low is *"the dangerous direction"*. Both of those bite
here. Measured with the same estimator: **[M]**

- 120 candidates at real YouTube result length (71 chars average): **3,446 tokens**, 1.9x the cap.
- 120 candidates with French accented labels (52 chars average): **2,674 tokens**, 1.5x the cap.

The owner is French, on AZERTY, and his stated example is picking a song. Song titles, channel
names and album names are exactly the long, accented, punctuated strings the fit was never
trained on. **Plan for 50 to 60 candidates on the pages he actually named, not 120**, truncate
names hard, and keep `Drift` wired in from the first request so the estimator's error on French
music metadata is measured rather than assumed. The lane's "plan for ~100 and the 255 limit is
irrelevant" is right about 255 and wrong about 100.

### V7. `chrome.debugger` cannot be optional, and the banner. CONFIRMED, and upgraded to vendor-grade.

The lane labelled this secondary. It is now primary: the Chrome extensions `permissions` API
reference states that most permissions can be optional *"with the following exceptions"* and
lists **`debugger`** among them, with `declarativeNetRequest`, `devtools`, `geolocation`, `mdns`,
`proxy`, `tts`, `ttsEngine` and `wallpaper`. **[V]**

The banner is corroborated from four independent directions: Chromium issue **40141220**, titled
*"Chrome debug message 'started debugging this browser' does not disappear after detach"*;
anthropics/claude-code issue **69287**; UiPath's Studio Web documentation; and Leapwork's support
article, which states that force-install by group policy avoids the banner. **[S], four sources,
converging.** Note that the Chromium bug title makes it **worse** than the lane said: the banner
can outlive the detach.

Two corrections and one new risk:

- `--silent-debugger-extension-api` is real and documented, but the Chrome debugger API reference
  cites it for a different purpose: *"Attaching to an extension background page is only possible
  when the --silent-debugger-extension-api command-line switch is used."* That it also suppresses
  the banner comes only from third parties. **[S], not vendor-confirmed for that use.**
- **New, and the lane missed it:** *"Stricter enterprise policy enforcement for chrome.debugger
  in Chrome 155"*, Chrome 155 beta 2026-09-16, **stable 2026-10-06, two weeks from today**.
  `chrome.debugger.attach()` will fail with *"Host access is restricted by policy."* under
  `runtime_blocked_hosts`, and with *"Screenshot capture is restricted by policy."* when
  screenshot policy forbids it. The post is explicit that *"If an extension runs on an unmanaged
  browser ... chrome.debugger continues to operate normally with no changes."* **[V]** So the
  owner's machine is unaffected today. But the direction of travel on the exact escape hatch the
  plan reserves as tier 3 is: tightening. Do not build anything load-bearing on it.

### V8. Native messaging. Contract CONFIRMED verbatim; the host count is wrong.

Checked against developer.chrome.com/docs/extensions/develop/concepts/native-messaging directly.
Every element of the lane's contract is confirmed word for word: **1 MB** host to extension,
**64 MiB** extension to host, *"preceded with 32-bit message length in native byte order"*,
per-user manifest at `~/.config/google-chrome/NativeMessagingHosts/<name>.json`, system-wide at
`/etc/opt/chrome/native-messaging-hosts/`, `allowed_origins` values *"can't contain wildcards"*,
and *"These methods are not available inside content scripts, only inside your extension's pages
and service worker."* **[V]** The service-worker constraint is the one that shapes hyprsay's
code: every content-script snapshot has to hop through the service worker.

Correction. The lane says *"three native messaging hosts installed"* and cites
`~/.config/google-chrome/NativeMessagingHosts/`. That directory holds **two**:
`com.anthropic.claude_browser_extension` and `com.anthropic.claude_code_browser_extension` (whose
path and `allowed_origins` are quoted correctly). The third,
`org.kde.plasma.browser_integration`, is system-wide in `/etc/opt/chrome/native-messaging-hosts/`.
The count is right, the cited source does not support it, and the substance, that the pattern
already works on this machine under Hyprland with this Chrome, stands. **[M]**

One suspicion cleared: `cdp_bench.py` imports `websockets`, which the system Python does not
have. It is present as `websockets 17.1` in `hyprsay/.venv`, so the benchmark could and
presumably did run.

### Not verified, and load-bearing for the recommendation

- **Tier 2 is unverified.** The claim that a `zwlr_virtual_pointer_v1` click lands in Chrome with
  `isTrusted === true`, at coordinates derived from `outerHeight - innerHeight` and
  `devicePixelRatio`, is the linchpin of the whole no-banner plan and **nobody has tested it**.
  It is plausible (the compositor click is a real input event to Chrome) but it is an inference
  presented as a design. Under Wayland, a client that is not focused, or a pointer-constrained
  page, or a fractional-scale output, can each break the coordinate mapping. **Test this before
  anything else in the plan is scheduled.** It is a half-day spike and it decides the architecture.
- Whether a copied Linux Chrome profile keeps its logins under a non-default `--user-data-dir`:
  still untested, by them and by me.
- Firefox's treatment of `--remote-debugging-port` on the default profile: still untested.
- All page measurements, theirs and mine, are logged out and headless. A logged-in Spotify or
  YouTube page will be larger, which pushes V6's ceiling down further, not up.
