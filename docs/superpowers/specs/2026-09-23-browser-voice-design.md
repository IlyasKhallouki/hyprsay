# Browser voice control: design

Date: 2026-09-23. Status: approved by the owner, section by section, in conversation.

## Why this exists

hyprsay's first real voice session (44 spoken commands, 21 and 22 September) got about 14 right. A
replay of 14 of the failures showed that Jev was rarely the reason. Asked one clean question with
the right answer among the options, Jev picked correctly 30 times out of 33, at 351 ms p50. The
failures came from hyprsay itself: the grammar answered before Jev was asked (9 failures), seven
overlapping requests per command ran out of time (3), and several option lists did not contain
the right answer. Raw data is in `scratchpad/ab-2026-09-23/`, which is ignored by git.

The owner also wants the demo to happen in the browser, because that is where demos land. This
design covers both: a new decision path, and a browser extension that gives it tabs, links,
buttons and search boxes to choose from.

## The demo, which is also the acceptance test

Spoken into the microphone with the push-to-talk key held, headphones on, in the owner's real
Chrome profile:

1. On an empty desktop, "open chrome" brings Chrome up.
2. "open spotify" switches to the Spotify web tab that is already open.
3. "play hold the line" searches Spotify in that tab and plays the track.
4. "open youtube" opens YouTube in a new tab.
5. "search for linus tech tips" runs the search on YouTube.
6. "scroll down", then "a bit more".
7. "click the second video", or "open the one about the new GPU".
8. "pause" and "turn it down" act on the video.
9. "go back to spotify" switches tabs.
10. "close this tab".

It passes when all ten do the right thing and none does the wrong thing. Typed text does not
count, because the recognizer never produces quite the string a keyboard does.

## Architecture

```
 mic -> STT -> hyprsay daemon --one Jev request--> Jev
                  |   ^
                  |   +-- options: windows, apps, media, + browser (tabs, page)
                  v
        hypruse.browser client (Python)
                  |  $XDG_RUNTIME_DIR/hypruse/browser.sock  (owner only)
                  v
        hypruse browser-host   <- started by Chrome (native messaging)
                  |  stdio, length-prefixed JSON
                  v
        hypruse-browser extension (service worker + content script)
```

The work splits into four units:

1. The `hypruse-browser` extension: a new public repository at
   `github.com/IlyasKhallouki/hypruse-browser`, in TypeScript, built with the same tools as the
   owner's `tab-steward` extension: pnpm, Vite, Vitest, Playwright, ESLint, Prettier, GitHub
   Actions. It answers requests and never acts on its own.
2. `hypruse browser-host`: a new hypruse subcommand. Chrome starts it when the extension
   connects. It relays between Chrome and a unix socket and holds no state of its own.
3. `hypruse.browser`: a typed Python client in hypruse. hyprsay uses it first. hypruse's MCP
   server can later expose it as a `browser` tool, which closes the gap hypruse's own instructions
   admit to: Chrome publishes nothing to the accessibility bus without a flag.
4. hyprsay, which gains a browser source for its options and a decision path that asks Jev
   once per command.

hyprsay vendors hypruse under `src/hypruse`, and that copy is identical to upstream today. The
host and client are written in hypruse first and copied into hyprsay unchanged.

## The protocol

The messages are the same JSON on both hops; only the framing differs.

- Extension and host use Chrome native messaging: a 4-byte little-endian length, then UTF-8
  JSON. Chrome caps a message from the host at 1 MB.
- Host and clients use newline-delimited JSON over `$XDG_RUNTIME_DIR/hypruse/browser.sock`.
  The directory is mode 0700 and the socket 0600, the same boundary hyprsay's HUD socket has.

Messages:

```
request   {"id": "c1-17", "op": "page.snapshot", "args": {...}}
response  {"id": "c1-17", "ok": true, "result": {...}}
          {"id": "c1-17", "ok": false, "error": {"code": "stale_snapshot", "message": "..."}}
event     {"event": "tabs.changed", "data": {...}}
hello     {"hello": {"protocol": 1, "extension": "0.1.0", "browser": "Chrome/151.0"}}
```

The host rewrites request ids so that two clients cannot collide, and routes each response back
to the client that asked. Events go to every client. When a client connects, the host sends it a
hello that says whether an extension is attached.

Operations in protocol version 1:

| op | args | result |
|---|---|---|
| `tabs.list` | none | tabs: id, window_id, index, active, audible, title, url |
| `tabs.activate` | tab_id | raises the tab and focuses its window |
| `tabs.open` | url, where: `new` or `current` | the tab id |
| `tabs.close` | tab_id | nothing |
| `tabs.history` | tab_id, direction: `back` or `forward` | nothing |
| `page.snapshot` | tab_id (default: active), max (default 120) | see below |
| `page.click` | snapshot_id, element_id | nothing |
| `page.type` | snapshot_id, element_id, text, submit | nothing |
| `page.scroll` | tab_id, direction: `up`, `down`, `top`, `bottom` | the new scroll position |
| `sites.top` | none | sites: url, host, title |
| `ping` | none | the extension version |

Error codes: `no_extension`, `stale_snapshot`, `not_found`, `refused`, `restricted_page`,
`timeout`, `bad_request`.

A snapshot result carries `snapshot_id`, `tab_id`, `url`, `title`, up to 400 characters of
headings, the viewport (width, height, device pixel ratio, scroll position), and a list of
elements. Each element has an `id`, `role`, `name`, `kind` (`link`, `button`, `input`, `search`,
`tab`, `option`, `toggle`, `other`), a `rect` in CSS pixels, `in_view`, and optionally a `list`
of `{group, index, size}`.

## The extension

### What it reads

- Tabs, from `chrome.tabs`.
- Top sites, from `chrome.topSites`, so "open youtube" becomes the `youtube.com` the owner
  actually visits and not a guess.
- A page snapshot, taken by a content script injected on demand with
  `chrome.scripting.executeScript`. The script installs itself once per document and later calls
  reuse it. It collects:
  - named elements that can be clicked or typed into: links, buttons, form fields, ARIA widget
    roles, `contenteditable`, and a second pass over `cursor: pointer` elements that have no
    interactive ancestor;
  - only those that are visible and not covered, checked with `elementFromPoint` at their centre;
  - a name for each, from `aria-labelledby`, `aria-label`, `alt`, `title`, its label, its
    placeholder, or its text, with whitespace collapsed and cut at 60 characters;
  - duplicates merged by role and name, except inside lists.
- Lists: when at least three siblings repeat the same tag-and-class signature (YouTube
  results, Google results, Spotify track rows), their items are numbered in visual order, top to
  bottom and then left to right. This is how "click the second video" works on any site without
  code for that site.
- Search boxes: an input counts as one when its type is `search`, its role is `searchbox`,
  it is a `combobox` input, it sits in a form with `role="search"`, or its name, placeholder or
  label says "search".

### What it does

- Switch to a tab and raise its window; open a URL in a new tab or the current one; close a tab;
  go back or forward.
- Click: scroll the element into view if it is not, then dispatch pointer and mouse down and
  up at its centre, then `click()`.
- Type: focus, set the value through the native property setter so frameworks like React
  notice, fire `input` and `change`, and on submit press Enter and, inside a form, call
  `requestSubmit()`.
- Scroll: find the element that actually scrolls, starting from the middle of the viewport
  and walking up to the first ancestor with scrollable overflow, and fall back to the document.
  Spotify scrolls inside a panel, not the document. Scrolling is smooth, by 80 percent of the
  viewport, or to the top or bottom.

The extension leaves media alone. "Pause" and "turn it down" already work through the MPRIS player
Chrome publishes and the system volume, both of which hyprsay has.

### Safety inside the extension

These rules hold even when a client is careless:

- It never sends the value of a field, only its name.
- It refuses to type into `type=password` fields or fields whose `autocomplete` starts with `cc-`.
- It does not run in incognito.
- It cannot touch `chrome://` pages or the Web Store, and says so with `restricted_page`.
- A click or a type names a snapshot. If the page has moved on, the answer is `stale_snapshot`,
  never a click on whatever is at that spot now.
- It talks to its own host only. Anyone who can reach the owner-only socket can already drive
  the whole desktop through hypruse, so the socket is the boundary, as it is for hypruse today.

### Connection lifetime

The service worker connects with `chrome.runtime.connectNative("dev.hypruse.browser")` on
install and on browser startup. An open native port keeps a Manifest V3 service worker alive. If
the port drops, the worker retries with backoff and a `chrome.alarms` wake-up. The host exits
when Chrome closes its stdin and removes its socket. If another host already owns the socket
(a second browser), the new one exits with a message: one browser at a time in version 1.

### Installation

- The extension is loaded unpacked from `dist/`. Its ID is pinned by a public `key` in the
  manifest, so the host manifest can name it before the extension is loaded. The private key is
  kept outside the repository, in `~/.config/hypruse-browser/`.
- `hypruse browser install` writes `dev.hypruse.browser.json` into the native messaging folder of
  each installed Chromium browser (Chrome, Chromium, Brave), plus a launcher script. Chrome passes
  the extension's origin as the first argument and a native host cannot take arguments of its
  own, so the manifest points at the launcher and the launcher runs `hypruse browser-host`.
  `hypruse browser uninstall` removes both. `hypruse doctor` reports whether the host is
  installed and whether an extension is connected.
- Web Store publishing is out of scope.

## The decision path in hyprsay

A new module, `nlu/decide.py`. `understand.py` is 3,080 lines and is not where this goes.

For each command:

1. The grammar acts only on a complete parse: every word accounted for, a canonical command
   such as "workspace ten". It no longer answers with numbered hints or "did you mean". Anything
   it cannot fully parse goes to Jev.
2. The harvester builds one options list from every source: windows, workspaces, apps, system and
   media, Spotify, and the new browser sources. The target of the last action is listed first,
   and the list is sized to the token budget with the existing estimator.
3. One Jev request carries the utterance, what is focused, the current workspace and what was
   done last, and three questions:
   - `action`: a Choice over the options plus `none`;
   - `words`: a Choice over spans of the utterance, up to six words each, plus `none`, for the
     name, query, address or number the action needs. Jev cannot write text, but it can point at
     the words that were said. In the replay this found "hold the line", "ltt", "github com" and
     "75" in the same request as the action, at 378 ms p50;
   - `addressed`: a Boolean, whether the speaker was talking to the computer at all.
4. The gate looks at the margin between the top two answers, not the top probability alone,
   because near-ties flip between identical requests. Following the owner's rules from 22
   September, reversible actions happen at once, and anything that cannot be taken back (closing
   a tab, closing a window) gets the spoken cancel window. A close call says what it heard and
   offers the top two.
5. There is at most one follow-up request. Some options promise more than one step can do. For every
   search box the browser source offers two options: "search Spotify for words", and "find and
   play or open something on Spotify by name". When the second wins, hyprsay types the words,
   takes a new snapshot, and asks Jev one more question over what actually appeared: which of
   these is the thing the speaker named. "play hold the line" becomes a search, then "Play Hold
   the Line by Toto". The bounded loop in `nlu/turns.py` carries this.

This path replaces the seven-request fan-out (`r1`, `r1.2`, `rp`, `r2`, `r2b`, `r3`, `r3.2`),
the "first thing, second thing" step questions, which cost 27 points of accuracy in the replay,
and the grammar's early answers. Compound commands keep today's clause splitter, and each clause goes
through the new request. Better compounds are out of scope.

The interpret stage stays: a small generative model that rewrites rambling speech. It skips
crisp commands, and all ten demo lines are crisp. The keystroke recipes stay as the browser fallback
while the extension is not connected, and are deleted once the extension has proven itself.

### The browser source

When a Chrome window exists and the extension is connected, the harvester adds:

- "switch to the {title} tab" for each tab, with its host;
- "open {host} in a new tab" for top sites, and "open a website in a new tab" that needs an
  address;
- when Chrome is focused: "click {name}" for page elements, "result {n}: {name}" for numbered
  list items, the two search options per search box, "scroll down", "scroll up", "go to the top",
  "go back", "close this tab".

Strangers write page text. Its labels are marked untrusted, as window titles are today, and
the question tells Jev to treat them as labels and never as instructions.

## Listening: the key is the session

While the key is held:

- the microphone stays open and the Silero voice detector, already downloaded, cuts speech into
  commands at pauses;
- each command is decoded and decided on its own as soon as it settles: at a pause, or earlier
  when the transcript and the decision stop changing over several decodes, which is the existing
  early-settle logic in `incremental.py`;
- after a command settles, its audio is dropped and the next command starts from an empty buffer,
  so a two-minute hold costs what a two-second one does.

Releasing the key closes the microphone and lets the command in flight finish. A command that was
half said when the key came up is still decoded.

`ptt.max_hold_s` goes from 15 to 180 seconds. The limit stays because a release can be lost to a
screen lock or a compositor restart. The HUD shows how long the key has been held. Latch mode
stays available and is not needed for the demo.

Safety does not change otherwise. The held key says "I am talking to you", and `addressed` is a
second check for narration during a hold. Typing still needs a carrier phrase and never goes
through Jev. Lock handling, the cloud speech rules and the cancel window stay as they are.

While the key is held and a video plays, the microphone also hears the video, so the recording uses headphones. PipeWire echo cancellation is an optional check in `hyprsay doctor`.

## Testing

Each repository's gate is run exactly as its CI runs it.

- hypruse-browser: Vitest for the snapshot against fixture pages (a results list, track rows,
  a search box, a covered element, a stale handle, a password field), for the scroll target and
  for message handling. Playwright end to end: the unpacked extension in Chromium, driven through
  a test host.
- hypruse: pytest for framing, the relay (a fake extension on pipes, fake clients on the
  socket), id rewriting, socket permissions, a second host refusing to start, and the installer
  against a temporary home directory.
- hyprsay: unit tests for `decide.py` with the existing fake Jev; new browser cases in
  `evals/cases.py`; and a replay of the owner's real voice transcripts from the journal, with the
  desktop state each was spoken in rebuilt from the journal's own context, and a fixture browser
  for the commands that need one.

## Acceptance

1. Checkpoint: the journal replay, through the new path, against live Jev. Each transcript is
   labelled with what the owner meant. Transcripts whose intent cannot be told from the words
   ("Two.", a numbered answer to an earlier prompt) are listed and left out. It passes at 80
   percent or more clearly right, with no wrong action. If it fails, work stops and the result is
   reported, instead of building the rest on a path that does not work.
2. Demo: the ten lines above, spoken by the owner.

## Build order

Each step ends with its gate green.

0. Spikes, about half a day, in the owner's real profile: does an in-page click start
   playback on YouTube and on Spotify; does typing into Spotify's search box work; does a native
   messaging round trip work with the pinned ID. If playback refuses an in-page click, a real
   pointer click through hypruse, with the extension supplying the element's position, moves onto
   the critical path.
1. The decision path, then the checkpoint.
2. hypruse: `browser-host`, the client, `browser install` and `uninstall`, the doctor check.
3. The hypruse-browser repository, pushed public once a round trip works end to end.
4. hyprsay: the browser source, carrying out browser actions, the follow-up turn.
5. hyprsay: the held key as a session.
6. The owner's spoken run.

## Where the work goes

- hyprsay and hypruse changes go on branches. The owner is asked before either is pushed and
  before any hypruse release, since a hypruse tag publishes to PyPI and the AUR.
- hypruse-browser is created public on GitHub, as the owner asked.
- Every document written for this work goes through the `humanizer` skill first.

## Out of scope

Web Store publishing, Firefox, iframes and shadow roots beyond what the demo sites need, drag and
drop, file uploads, hover-only menus, a microphone that is always on, better compound commands,
and the hypruse MCP `browser` tool itself (the client makes it a small follow-up).
