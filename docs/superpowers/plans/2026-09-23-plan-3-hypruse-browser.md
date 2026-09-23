# Plan 3: the hypruse-browser extension

**Goal:** a Manifest V3 Chrome extension, in its own public repository, that lets local programs
list and switch tabs, open and close them, read a page's clickable and typeable things, click,
type, and scroll, through Chrome native messaging and hypruse's `browser-host`.

**Spec:** `docs/superpowers/specs/2026-09-23-browser-voice-design.md` (sections "The protocol" and
"The extension"). This plan pins the details the spec leaves open.

## Repository

- Path `~/projects/hypruse-browser`, a new git repository, branch `main`. GitHub remote
  `github.com/IlyasKhallouki/hypruse-browser`, public. The owner asked for public. The push
  happens after review.
- Tooling copied from `~/projects/tab-steward`: pnpm 10.33, Node 22+ in CI, TypeScript strict,
  Vite, Vitest, Playwright, ESLint (flat config), Prettier, `pnpm check` running format, lint,
  typecheck, unit tests, build. No React: the extension has no UI.
- License MIT, "Copyright (c) 2026 Ilyas Khallouki", as hypruse.
- Files a well kept repository has: README.md, docs/PROTOCOL.md, CONTRIBUTING.md, SECURITY.md,
  CHANGELOG.md (0.1.0), CODE_OF_CONDUCT.md, LICENSE, .github/workflows/ci.yml (quality job plus
  a Chromium end to end job), .gitignore, .prettierrc.json, .npmrc.
- Every document goes through the `humanizer` skill before it is committed. No em dashes and no
  en dashes anywhere, in docs, code comments or commit messages.
- Commits are small and professional, conventional style (`feat:`, `test:`, `docs:`,
  `chore:`), and carry NO attribution or co-author lines.

## Identity

- Extension ID `pcfjlemgcbbpppclachnobhnnnahpgoj`, pinned by the public key in
  `~/.config/hypruse-browser/manifest-key`, which goes into `manifest.json` as `"key"`. The
  private key never enters the repository.
- Native host name `dev.hypruse.browser`.

## Manifest

`manifest_version` 3, name "hypruse browser", version 0.1.0, `minimum_chrome_version` "116",
`incognito` "not_allowed", permissions `nativeMessaging`, `tabs`, `scripting`, `topSites`,
`alarms`, host permissions `<all_urls>`, background service worker `background.js` as a module,
icons 16/32/48/128 (simple generated PNGs are fine). No popup, no options page.

## Build

`dist/` holds `manifest.json`, `background.js` (ES module) and `content.js` (a classic script,
IIFE, because it is injected with `chrome.scripting.executeScript({files})`). Load unpacked from
`dist/`.

## Protocol v1

Native messaging frames between the host and the extension. JSON messages:

```
extension -> host, once on connect
  {"hello": {"protocol": 1, "extension": "0.1.0", "browser": "Chrome/151.0.7922.137"}}
host -> extension
  {"id": "h17", "op": "page.snapshot", "args": {...}}
extension -> host
  {"id": "h17", "ok": true, "result": {...}}
  {"id": "h17", "ok": false, "error": {"code": "stale_snapshot", "message": "..."}}
  {"event": "tabs.changed", "data": {"reason": "activated", "tab_id": 12}}
```

Every request gets exactly one response. Unknown op or bad args: `bad_request`. An exception
inside a handler: `internal` with the message. Error codes: `bad_request`, `not_found`,
`stale_snapshot`, `refused`, `restricted_page`, `timeout`, `internal` (`no_extension` is the
host's, never the extension's).

| op | args | result |
|---|---|---|
| `ping` | none | `{extension}` |
| `tabs.list` | none | `{tabs: [{id, window_id, index, active, audible, title, url}]}`, every normal window, focused window first |
| `tabs.activate` | `tab_id` | `{}`; also focuses the tab's window |
| `tabs.open` | `url`, `where`: `new` or `current` | `{tab_id}`; `new` opens in the last focused normal window, active |
| `tabs.close` | `tab_id` | `{}` |
| `tabs.history` | `tab_id`, `direction`: `back` or `forward` | `{}` |
| `page.snapshot` | `tab_id` (default: active tab of the last focused window), `max` (default 120), `settle_ms` (default 0) | see below |
| `page.click` | `snapshot_id`, `element_id` | `{}` |
| `page.type` | `snapshot_id`, `element_id`, `text`, `submit` (bool) | `{}` |
| `page.scroll` | `tab_id` (default active), `direction`: `up`, `down`, `top`, `bottom` | `{scroll_y, scroll_height}` |
| `sites.top` | none | `{sites: [{url, host, title}]}` from `chrome.topSites` |

`tabs.open` accepts only `http:` and `https:` URLs; anything else is `refused`.

### page.snapshot result

```
{snapshot_id: "s7", tab_id, url, title,
 headings: "up to 400 characters of h1..h3 text in document order",
 viewport: {width, height, dpr, scroll_y, scroll_height},
 elements: [{id: "e3", role, name, kind, rect: [x, y, w, h], in_view,
             list?: {group: "g2", index: 1, size: 20}}]}
```

- `settle_ms` > 0: before reading, wait until the DOM has had no mutation for 250 ms, or until
  `settle_ms` passes, whichever is first (MutationObserver). This is what a caller uses right
  after typing a search.
- Candidates: `a[href]`, `button`, `input:not([type=hidden])`, `textarea`, `select`,
  `summary`, `[contenteditable=true]`, `[onclick]`, and the ARIA roles `button link tab
  menuitem option checkbox radio switch searchbox combobox textbox`, plus a second pass over
  elements whose computed `cursor` is `pointer` and that have no candidate ancestor (bounded
  to 400 checked elements).
- Visible: a rect with width and height above 0, not `visibility: hidden`, not
  `display: none`, not inside `aria-hidden="true"`. `in_view` when the rect intersects the
  viewport. Keep in-view elements first, then elements within one viewport below. Hit test
  in-view elements with `document.elementFromPoint` at their centre: keep the element when
  the hit is the element, inside it, or an ancestor that has no other candidate in between.
- Name: `aria-labelledby` text, `aria-label`, `alt`, `title`, the associated `<label>`, the
  `placeholder`, then visible text; whitespace collapsed, cut at 60 characters. No name, not a
  candidate.
- Kind: `link`, `button`, `input`, `search`, `tab`, `option`, `toggle` (checkbox, radio,
  switch), `other`.
- `search`: an input or textarea whose type is `search`, whose role is `searchbox` or
  `combobox`, that sits in `form[role=search]`, or whose `name`, `id`, `placeholder`,
  `aria-label` or `data-testid` contains "search" (case insensitive).
- Values are never read. Not for inputs, not for textareas, not for contenteditable.
- Lists: for each candidate, walk up to 8 ancestors to the first one whose parent has at
  least 3 element children with the same signature (tag name plus sorted class list). That
  ancestor is the item and its parent the group. Items are numbered from 1 in visual order
  (top, then left) among the group's visible items. Every candidate inside an item carries
  `list: {group, index, size}`. Groups are ids `g1`, `g2` per snapshot.
- Duplicates (same role and name) outside lists are merged, keeping the first in visual order.
- Order: in-view elements by visual order, then the rest. Cut to `max`.
- Element ids `e1`, `e2` per snapshot. The content script keeps, per document, only the latest
  snapshot id and a map from element id to a `WeakRef` of the element.

### page.click and page.type

- The snapshot id must be the latest of that document, and the element must still be
  connected; otherwise `stale_snapshot`.
- Click: `scrollIntoView({block: "center"})` when not in view, then dispatch `pointerover`,
  `pointerenter`, `mouseover`, `pointerdown`, `mousedown`, focus, `pointerup`, `mouseup` at the
  element's centre with bubbling and composed set, then `element.click()`.
- Type: refuse (`refused`) an `input[type=password]` and any field whose `autocomplete` starts
  with `cc-`. Focus, select the existing text, set the value through the native setter of the
  element's prototype (`HTMLInputElement` or `HTMLTextAreaElement`) so React-style frameworks
  notice, dispatch `input` (an `InputEvent`, `inputType: "insertText"`) and `change`. For
  contenteditable use `document.execCommand("insertText")`. On `submit`: dispatch `keydown`,
  `keypress`, `keyup` for Enter (key "Enter", code "Enter", keyCode 13), and if the Enter
  `keydown` was not default-prevented and the field is in a form, `form.requestSubmit()`.

### page.scroll

The target is the nearest scrollable ancestor (computed `overflow-y` auto, scroll or overlay,
and `scrollHeight > clientHeight + 1`) of the element at the viewport centre, else
`document.scrollingElement`. `down` and `up` scroll by 80 percent of the target's
`clientHeight`, smoothly; `top` and `bottom` go to the ends. The result reports the position the
scroll is heading to.

### Injection

`chrome.scripting.executeScript({target: {tabId}, files: ["content.js"]})` installs
`globalThis.__hypruseBrowser` once per document (guarded); every page op then calls
`executeScript({target: {tabId}, func: (op, args) => globalThis.__hypruseBrowser.handle(op,
args), args: [op, args]})` and returns the result. Top frame only. A tab Chrome will not inject
into (`chrome://`, the Web Store, another extension, a `file:` URL without access) answers
`restricted_page`.

### Events

`tabs.changed` with `reason` one of `activated`, `created`, `removed`, `updated` (only when
`status` becomes complete or the title changes), debounced to one per 200 ms per tab.

### Connection

The service worker connects with `chrome.runtime.connectNative("dev.hypruse.browser")` at top
level, on `onStartup` and on `onInstalled`, and sends the hello. On `onDisconnect` it retries
with backoff (1, 2, 5, 15, 30 s) and a `chrome.alarms` alarm every 30 s reconnects if nothing is
connected. It never throws on a missing host.

## Code layout

Small files, one job each, for example `src/background/{index,port,dispatch,tabs,sites,
inject}.ts`, `src/content/{index,snapshot,names,lists,click,type,scroll,settle}.ts`,
`src/shared/protocol.ts` (types and a runtime validator for requests; `zod` is fine, it is what
tab-steward uses).

## Tests

- Vitest unit tests for names, kinds, search detection, list numbering, duplicate merging,
  stale handles, the password refusal, scroll target choice, request validation, and dispatch
  error mapping. Geometry is injected so unit tests do not depend on a layout engine.
- Playwright end to end: Chromium with the unpacked extension, fixture pages served locally
  (a results list, a Spotify-like track list inside a scrolling panel, a search form), driving
  the dispatcher through the service worker (`serviceWorker.evaluate`) so every op runs for
  real. If a test native host can be registered for that Chromium profile, one test covers the
  full native messaging round trip; if not, say so in the test file.
- `pnpm check` is the gate. It must pass before every commit.

## Done means

`pnpm check` green, e2e green locally, all docs humanized, a clean history of small commits on
`main`, nothing pushed.
