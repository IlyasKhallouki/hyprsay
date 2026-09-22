# Lane: what people have actually built with Jev, and what they learned

Research date 2026-09-22. Jev launched 2026-09-15, so everything below is at most seven days old.
Target machine for all cost notes: i5-8350U, no GPU, 23 GiB RAM, Arch, Hyprland 0.56.2, AZERTY.

Label key:
- [PRIMARY] the repository, the vendor's own docs, or a named author's own demo/measurement.
- [SECONDARY] third-party writeup about someone else's work.
- [VENDOR] a TypeSafe or Vercel marketing claim, unverified by anyone else.
- [MEASURED] measured by me, here, in this session.
- [INFERENCE] my reasoning. Not a finding.

## 0. Scale of the ecosystem, honestly

There is far more than "a handful". [PRIMARY, gh CLI, 2026-09-22] Verified live on GitHub:

| repo | stars | created | what |
|---|---|---|---|
| browser-use/jev-ultrafast | 16,860 | 2026-09-16 | browser agent, dynamic indexed action space |
| jarrodwatts/jev-trader | 1,918 | 2026-09-16 | one decision per Monad block |
| rmalde/minecraft-agent | 491 | 2026-09-20 | Astra planner + Jev controller |
| AbdelStark/awesome-typesafe-jev | 432 | 2026-09-17 | the catalogue I used as an index |
| droidrun/mobile-jev | 336 | 2026-09-17 | local Android agent |
| jkudish/jev-browser | 235 | 2026-09-17 | Playwright navigator |
| moritzkremb/jev-voice-browser | 224 | 2026-09-17 | **voice, acts mid-sentence** |
| agent-labs-dev/fastbrowse | 94 | 2026-09-17 | Jev picks, LLM reads and plans, quotes cited |
| antiyro/jevdroid | 3 | 2026-09-18 | Android over ADB |

So: one true viral project (jev-ultrafast, ~17k stars in six days), a second tier of a few
hundred stars, and a long tail of several hundred SDK wrappers and agent hooks. The
`awesome-typesafe-jev` catalogue lists roughly 180 entries; I verified nine by API and treat
the rest of that list as [SECONDARY] until opened.

**The single most important finding for hyprsay: the exact thing the owner described as "I have
seen systems that act while the person is still speaking" exists, is open source under MIT, is
224 stars, and is 2,500 lines of readable JavaScript.** It is
`moritzkremb/jev-voice-browser`. Its own one-line description, verbatim: "Control a real browser
by voice. Jev (TypeSafe System One) decides intent + target in ~300 ms per spoken word;
Playwright acts - often before you finish the sentence."

---

## 1. jev-voice-browser (moritzkremb) - the reference implementation of the owner's vision

[PRIMARY: repository README, `src/` layout, author's own measurements.]

### What it does
Node server owns a headed Chromium via Playwright. Speech comes from the *control page's* Web
Speech API in ordinary Chrome and is streamed **word by word** over a websocket. On every partial
transcript the server fires one Jev request and a policy in code decides: act, wait, ask, ignore.

### How it uses Jev: one request, 9 to 11 questions, answered in parallel
This is the mechanism hyprsay is missing. Not "ask Jev which action", but ask everything at once
and let code arbitrate:

| id | type | what it answers |
|---|---|---|
| `intent` | Choice | navigate_url, search_web, click_element, type_into_field, select_option, press_enter, scroll_down/up, go_back/forward, reload, open/close/switch tab, confirm, cancel, none. Each option carries `{what, not_for, examples}` |
| `target` | Choice | the element ids present on the page, plus `none` |
| `site` | Choice | google, duckduckgo, the_web, youtube, wikipedia, github, amazon, reddit, twitter_x, hacker_news, example_com, other_named_site, none |
| `complete` | Noul | **has the user finished the command?** This is what licenses acting on partial speech |
| `is_command` | Noul | is the user addressing the browser at all? |
| `destructive` | Noul | would it submit, buy, delete or send? |
| `scroll_amount` | Score | a little, one page, to the end |
| `text_span` | Choice | verbatim candidate spans extracted **by regex in code**, plus `none`. Asked only when the transcript contains any |
| `url_span` | Choice | domain-looking spans, plus `none` |
| `tab_direction` | Choice | next, previous, first, none |
| `is_correction` | Noul | is the user rejecting or redirecting the most recent action? Asked only when there is history |

### What it does NOT use Jev for
Everything that must be exact. Verbatim: "Jev never generates text. Search queries, typed text
and URLs are extracted as candidate spans by code and Jev only *picks* one, which is copied
verbatim." URL templates, the search-box fallback, option-label substring matching and tab
cycling are all code. `src/spans.js` does candidate extraction. This is the same discipline
hyprsay already has, so hyprsay is not wrong here, it is just under-asking.

### The policy, verbatim from the README, in order
0. `is_correction >= 0.6` on a finished phrase: with no confident new command ("no, not that
   one", "undo that") reverse the last action (click/navigate goes back, typing clears, scroll
   reverses); with a new target ("no, the other one") the previously clicked element is
   **excluded from the candidate list**. A confident closed-set command is never a correction.
1. `is_command >= 0.5`, else ignore.
2. `intent.confidence >= 0.55` and not `none`, else wait.
3. `complete >= 0.6`, or 900 ms of silence, or the recognizer's final result, else wait.
4. Free-text intents (search, type) additionally wait for the final result or 600 ms of silence,
   so a query is never truncated.
5. Build the action in code: URL templates, search-box fallback, verbatim span copy.
6. Click/type targets need `target.confidence >= 0.45` and top probability >= 0.35, else the top
   2 to 3 candidates get numbered overlays in the page and a spoken number picks one, **with no
   further model call**.
7. `destructive >= 0.5` on a click means confirm: say "confirm" or "cancel".

Concurrency: up to 2 requests in flight, older ones cancelled with `AbortSignal`. A response to a
partial transcript may still act if the words already commit to a closed-set action ("go back"),
but is never treated as final for free text.

### Context is the memory
Jev has no memory between requests, so the state carries it. Verbatim shape:
`context.previous_page` plus `context.recent_actions`, the last three executed actions as
`{said, action, target, outcome, seconds_ago}`. The README says plainly what this buys: "It is
what makes 'go back to the results', 'no, not that one', 'the other one' and 'open its
documentation' resolvable."

### Numbers the author published [PRIMARY, author's machine, not independently reproduced]
- integration 34/34 (100%), on captured page fixtures, including 7 context/correction cases
- Jev latency avg ~330 ms, p50 ~300 ms, 3,000 to 6,000 input tokens per request
- first request of a process ~700 ms because of the TLS handshake
- last-word to decision ~300 ms including the 200 ms debounce
- whole 16-command demo ~$0.01, so roughly $0.0002 per request
- element snapshot capped at 100 items, viewport first, 60 chars of text each
- confidence gates calibrated on the pinned alias `jev-1.13.0`; the README warns to re-check the
  thresholds if the alias moves

### Stated limitations
One action per utterance. Elements inside iframes are not seen. Deep pages need a scroll before
"click ..." finds below-fold items. Web Speech API is Chrome/Edge only and sends audio to Google,
which for hyprsay is the part to discard: hyprsay already has a better answer (local Parakeet).

---

## 2. browser-use/jev-ultrafast - the viral one, and the fan-out trick

[PRIMARY: repository README and its `docs/performance.md`.] 16,860 stars in six days, MIT.

### Mechanism
Every observation renders the page as an indexed element table:

```
[1] button    Change ticket type . Round trip
[2] combobox  Where from?        . San Francisco
[3] combobox  Where to?          . empty
[4] textbox   Departure          . empty
```

Operations: `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`,
`BLOCKED`. Only supported operations and targets are offered.

**Speculative fan-out, the part worth stealing.** One request contains four heads: `operation`,
`click_target`, `type_text_target`, and `select_target` when present. Each target head contains
only the elements compatible with that operation. The answer to `operation` selects which target
answer is used and the others are discarded. Verbatim: "Target questions are speculative. If the
operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**."

A small LLM is called **only** when the operation is `TYPE_TEXT`, to write the field text. The
demo uses `inception/mercury-2.5` with reasoning disabled.

Explicitly not pre-baked, verbatim: "There are no site-specific action scripts or prepared field
strings in the policy."

### Engineering rules the README lists as the reason it is fast
- one request per decision cycle, operation and target heads share one observed state
- **no screenshots in the agent loop**; Jev consumes structured state only
- one browser call per snapshot, atomic, keeping references to the real DOM nodes
- validate the selected target before acting: check document freshness, form values, target and
  nearby context; resolve current geometry and reject covered controls
- bounded waits, not sleeps: after typing into a combobox wait for visible suggestions capped at
  200 ms; other interactions get two animation frames or 50 ms
- send visible text only; offscreen bodies and footers do not fill the context
- "Model output never becomes selectors, coordinates, shell commands, or executable JavaScript."

### Numbers [PRIMARY, author's]
- Zurich to London on Google Flights: 7,073 ms end to end including text generation and waits
- six alternating runs, both versions 3/3; median task time 9.450 s to 7.092 s, a 25% reduction;
  median browser protocol calls 1,092 to 101
- Wikipedia article opened in 2.798 s; a local hotel search/filter task in 1.896 s
- the README is honest: "three repeats of one task on one browser profile, not a general
  reliability benchmark"

Out of scope in their MVP: shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling,
arbitrary keyboard widgets.

---

## 3. agent-labs-dev/fastbrowse - Jev plus a generative model, with the cost table

[PRIMARY: repository README, `docs/evals.md` referenced.] Tagline: "A browser agent that picks
instead of generating." Division of labour, verbatim: "Jev chooses each action, an LLM plans and
reads, and code owns verification, safety and secrets."

### Their head-to-head, dated 2026-09-22, build 0.5.2, 14 tasks, 3 passes each
| | passed | cost per task | median time |
|---|---|---|---|
| fastbrowse | 41/42 | $0.0041 median, $0.0086 mean | 20.6 s |
| Browser Use agent (LLM generates actions) | 39/42 | $0.3668 median, $0.6193 mean | 21.6 s |

Whole suite: $0.36 versus $26.01. Median cost ratios by category: 65x for lookups, 173x for
sign-ins, 195x for checkout. Note that **time is about the same**; the win is cost and the
elimination of invented selectors. [INFERENCE] For hyprsay the time axis matters more than cost,
so the honest read is that picking beats generating on reliability and money, and on latency only
when the alternative is a big model in the loop.

They also measured against jev-ultrafast: "On the six navigation tasks both can run, fastbrowse
passed 18/18 against 12/18, and jev-ultrafast is cheaper on every task both finish."

Their three-way table is the clearest statement of the design space:

| | Browser Use agent | jev-ultrafast | fastbrowse |
|---|---|---|---|
| Choosing an action | LLM generates from a screenshot | Jev picks from indexed controls | Jev picks from indexed controls |
| Returns | an answer | `DONE` or `BLOCKED` | an answer with quotes, or why it stopped |
| Reads pages | yes | no | yes, every claim cited |
| Signing in | yes | password fields excluded | scoped secrets, models see names only |

Gates in code: irreversible actions stop without `--authorize`; secrets reach models by name
only; cookie banners refused through autoconsent.

(continues below as research proceeds)

---

## 4. Desktop and OS control: three ports already exist, one of them on Hyprland

[PRIMARY: repositories, verified by gh on 2026-09-22.]

### 4a. devfros/omause - Jev voice control on Omarchy, which is Hyprland

0 stars, created 2026-09-20, TypeScript. Tagline: "Silent Omarchy voice control powered by
TypeSafe Jev. Omause is not a conversational assistant. It is an input layer: voice command in,
visible Omarchy action out."

Its loop, verbatim from the README:
1. Gather live affordances from the machine.
2. Rank and budget candidates per kind; **risky affordances are hidden from Jev**.
3. Resolve obvious single actions locally, without an API call. For ambiguous or compound
   requests, **ask Jev for one bounded ordered plan in a single request**.
4. If the affordance needs a value (a plugin, device, sink, monitor), resolve it: auto-bind when
   the transcript uniquely names one, otherwise ask Jev a second typed parameter choice.
5. Execute the chosen affordance through its motor primitive.
6. Observe exit status, window changes, and state; stop immediately on failure.

Affordance sources: open Hyprland windows (focus, close), workspaces, `omarchy commands --json`
including enum and provider-bound parameters, Omarchy menu JSONC, `.desktop` apps, PATH
executables, Omarchy keybinding combos as aliases, `~/.config/omause/aliases.toml`. System
sources: Bluetooth, Wi-Fi, audio, power profiles, brightness, media, bar panels.

Motor primitives: `command`, `terminal_command`, `desktop_app`, `focus_window`, `window_action`,
`focus_workspace`, `move_window`, `open_menu`, `type_text`, `wait`.

Compound commands, verbatim: "`npm run dev -- run --execute "open the browser and two terminals,
set DND on"` ... Jev returns the bounded sequence in one request. For example, the command above
resolves to browser, terminal, terminal, then DND." **This is the direct answer to complaint 1.**
No splitting on "and": the plan is an ordered choice over affordances, returned in one request.

Context following, verbatim: "Requesting to move an app, such as 'move my browser to workspace
3', focuses that window first and then moves it." And "'open two terminals in workspace three'
focuses workspace 3 first, then launches two terminals there." **This is the shape that fixes
complaint 3**: the affordance list contains the existing window, so "focus_window" is a
candidate competing with "desktop_app", and Jev picks between them from live state.

Handoffs: interactive pickers and panels are marked as handoffs; Omause opens them, records the
handoff, and stops the loop. Menu actions Omarchy composes in a shell are handed to
`omarchy menu summon <entry>` rather than run verbatim, and fragments of those actions are
withheld from Jev so a bare fragment is never run alone.

Verification: ten-second deadline per command step with final exit status; the runner diffs
Hyprland clients before and after each action including workspace moves; probes state for
nightlight, idle, DND, Wi-Fi, Bluetooth, audio mute, bar, screensaver, suspend, touchpad,
touchscreen; launch actions poll for up to two seconds.

Voice transport: it does not build one. It hooks Omarchy's existing F9-hold `voxtype` as a
post-processing command. Wake phrase "hey omarchy"; anything else passes through as dictation.

Misheard names, three mechanisms: whisper `initial_prompt` biased with app names discovered from
`.desktop` entries and PATH; fuzzy matching in the affordance ranker so `yazy`, `neo-vim`,
`obsidion` still surface `yazi`, `nvim`, `obsidian` **to Jev**; and persistent corrections via
`omause teach "niolvim" path-exec:nvim`, optionally writing a voxtype `text.replacements` entry.

[INFERENCE] hyprsay is ahead of omause on safety, speech (local Parakeet vs cloud Whisper),
testing and the HUD, and behind it on exactly the three things the owner complained about:
affordance harvesting, single-request ordered plans, and parameter resolution.

### 4b. kevinbadi/jev-voice - the jev-ultrafast loop ported to an accessibility tree

68 stars, created 2026-09-18, Python, macOS. This is the most important one for hyprsay's
"click inside things" problem, because it proves the browser technique transfers to a desktop
accessibility tree. Verbatim:

```
AX tree of the frontmost window -> indexed element table -> one Jev request -> executor
                                   [1] button   Back                | operation
                                   [2] textfield Address and search | click_target
                                   [3] link     Home                | type_text_target
                                   ...                              | open_app_target
                                                                    | press_key_target
                                                            use the matching head only
```

Operations: `CLICK`, `TYPE_TEXT`, `OPEN_APP`, `PRESS_KEY`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`,
`DONE`, `BLOCKED`. Verbatim on safety: "Every target is an observed `AXUIElement` held by code;
the model never emits selectors, coordinates, scripts, or shell. Before input the executor
re-reads the target's role, label, value and enabled state, re-resolves its geometry, and
hit-tests its centre so a covered control is never clicked."

Bounds: 40 actions, 80 Jev calls, and three actions in a row that change nothing stop the run as
`BLOCKED`. "A `DONE` choice is the model's claim, not proof."

Extra code-owned guards worth copying verbatim: "controls behind an open modal are withdrawn; an
action repeated twice without leaving the page is withdrawn; a BLOCKED under 50% executes the
runner-up operation from the same request; a DONE chosen twice stands on live pages; an unchanged
page reuses the decision for free; three rejections of one choice stop the run."

Its pipeline: `mic -> energy VAD -> whisper.cpp (Metal, ~100 ms) -> Jev (1 request, ~250 ms) ->
macOS actions`. Three activation modes: wake word hands-free with an 8 second follow-up window
where no wake word is needed, hold-to-talk on a remapped Caps Lock, and always-on.

Without a text model key, `TYPE_TEXT` values are Jev choices over spans cut from the goal:
"sentences, comma clauses, labelled values like 'postal code M5V 3L9', numbers as
'$15,000'/'15000'". Published numbers: a typical AutoTrader run ~13 s, ~30 Jev calls, about one
cent. Chess on chess.com: Stockfish returns its top five moves, Jev selects one, code clicks,
about 1.3 s per move.

### 4c. mikakostoev/jev-voice-control - two-stage clicking, and commands as data

0 stars, created 2026-09-21, Swift, macOS, always-listening. Two findings matter.

**Two-stage clicking, verbatim:** "When an utterance starts, Jev reads what is visible in the
frontmost app, the Accessibility tree, or OCR of the window when the app exposes almost nothing.
The first decision ('which action?') sees that list, so 'shuffle' or 'allow' are understood as
buttons rather than chatter. A second decision picks the item, matching by meaning across
languages ('перешли' -> `Forward`)." Note the ordering: on-screen controls are in state for the
*action* question too, not only the target question.

**Commands are data, not code.** `defaults.json` plus `~/.config/jev/config.json`. A command is
`{id, say, do, app, title, confirm}` where `say` is "a description of the intent **plus example
phrases**", `app` scopes it to a frontmost app, and `do` is a list of single-key steps
(`keys`, `open`, `type`, `click`, `shell`, `applescript`, `media`, `wait`, `builtin`). `confirm`
defaults to true when there is a `shell` or `applescript` step.

Its evaluation discipline is worth copying: `--eval Tests/cases.tsv` with format
`phrase<TAB>expected id[<TAB>previous command[<TAB>frontmost app]]`, `a|b` for either, `none`
for "must stay quiet", and the rule "Give each command of yours at least three lines: two
phrases that must trigger it and one similar phrase that must not." Also honest about jitter:
"a single miss with a confidence near the threshold is noise; a repeating one means the `say`
text needs sharpening."

Cost claim: "about half a second and costs about $0.00002" per decision (via OpenRouter).

### 4d. The rest of the desktop cluster (verified to exist, all 0 to 3 stars)

`chris-wozniczek/jev-voice-control` (macOS menu bar, Swift, 3 stars),
`jonatasperaza/jev-voice-windows` (Windows, local faster-whisper),
`moamenFathy/Jev_voice_computer_use`, `beejsbj/voice-gate` (self-hosted HTTP/CLI/MCP decision
engine), `antiyro/jevdroid` (Android over ADB, 3 stars), `droidrun/mobile-jev` (336 stars),
`wbuecksler/jev-voice-browser-chrome-extension` (MV3 port of moritzkremb's),
`dg-coreylweathers/jev-voice-agent` (no LLM in the loop: Deepgram Flux ends the turn, Jev picks
the reply, Flux TTS speaks).

[PRIMARY, gh search, 2026-09-22] `gh search repos "jev hyprland"` and `gh search repos "jev
wayland"` return **zero results**. Omause reaches Hyprland only through Omarchy's CLI. So: there
is no Jev-on-Hyprland project competing with hyprsay on its own ground, and hyprsay's
compositor-IPC core has no peer in this ecosystem.

---

## 5. Real-time loops: how fast people actually run Jev

### jarrodwatts/jev-trader [PRIMARY, 1,918 stars]
"One decision every Monad block ... answers buy or sell every ~300 ms." The README's own budget
section is the useful part: "A decision and an order have to fit in one block, so the hot loop
makes exactly two RPC round trips", and everything else (receipts, fee estimates, vault checks)
is moved **off the hot path** onto later blocks. Measured, their words: "read p50 18 ms, whole
loop p50 100 ms (80 ms of it the mock's inference stand-in)."

A sample event in the README carries `"decision": {"action":"buy", "probabilities":
{"buy":0.77,"sell":0.23,"hold":0}, "upIn10":0.77, "latencyMs":81, "late":false}` and
`"totals": {..., "decisions":3, "jevUsd":0.000004, ...}`. [INFERENCE] $0.000004 over 3 decisions
at the published $0.042 per Mtok is roughly 30 input tokens per call, so that is a minuscule
state; and 81 ms is a US-datacenter round trip. The honest reading is that **Jev's wire latency
is dominated by geography and TLS, not by how much you ask it.** hyprsay's measured 315 ms
median from Europe and jev-voice-browser's 300 ms p50 with 3,000 to 6,000 input tokens are the
same number. That means hyprsay can ask five times as many questions for free.

`hold` appears only when `decision.late: true`, meaning the model missed the block. That is the
pattern: **a deadline, and a defined thing to do when the answer arrives too late.**

### rmalde/minecraft-agent [PRIMARY, 491 stars] - generative planner plus Jev controller
GPT-6 Astra or GPT-5.6 Sol plans; `typesafe/jev-1.13` selects one available action from current
game observations. Verbatim: "The planner sets the current objective, item targets, and a travel
waypoint. JEV selects one available action from current game observations ... This is
structured-state control, not control from screenshots or individual key presses."

Published run: 8 minutes 43.300 seconds to kill the ender dragon from an empty inventory,
**131 JEV decisions and 35 Astra calls**. That is a ratio of about 3.7 fast typed decisions per
slow generative call. Their sources section also points at TypeSafe's own launch blog for a
**Doom demo** described as "structured game state, typed decisions, and a real-time action loop."

### Others in the same shape
`sorrycc/typesafe-snake` (per-tick decisions), `fhshaik/typesafe-mario`, `phyous/tsai-sc`
(StarCraft, "bounded actions, recorded model probabilities, and explicit victory checks"),
`TianyuCodings/NanoJev` (open 0.6B for Maze/Snake/ViZDoom). [SECONDARY, via awesome list.]

---

## 6. What TypeSafe itself says, with the numbers that bound a design

[PRIMARY, docs.typesafe.ai, fetched 2026-09-22.]

### Hard limits and prices, verbatim from `/models`
- Model `jev-1.13.0`. Price **$0.042 per Mtok input, output tokens free**.
- Rate limits **250,000 tokens per second / 1,200 requests per minute**, with a warning that
  they "can change without notice".
- Context **64k tokens per request; 32k tokens for `state` plus the longest question**.
- **Text only.** No image, audio or video input. Pre-process to text.
- A Choice takes **a maximum of 255 options** (`/api`).
- "Jev ingests the `state` once and evaluates every question against it in parallel."
- Language: "English is the primary training language and where accuracy is currently best.
  Other languages, including CJK scripts, are handled but not equally well."
- Aliases move without notice: "If you have tuned confidence thresholds against a specific
  version, pin that version's ID instead of the alias."

Cost for hyprsay [INFERENCE from the published price]: a 4,000-token request costs
4000 / 1e6 * $0.042 = **$0.000168**. At 200 model-touching commands a day that is about
$0.034 a day, roughly **$1 a month**. Asking twelve questions instead of three changes nothing
that matters, because the state is paid once.

### The confidence formula, which the docs ship as running code
`/confidence` embeds its explorer's own function:
`confidence = clamp((count * peak - 1) / (count - 1), 0, 1)` where `count` is the number of
options and `peak` is the largest probability.

[INFERENCE, but arithmetic on a primary formula] This is decisive for hyprsay. With 2 options,
confidence is `2*peak - 1`, so a 0.70 winner gives 0.40. With 40 options, the same 0.70 winner
gives `(40*0.7 - 1)/39 = 0.69`. **A confidence threshold tuned against a short candidate list
becomes far more permissive when the list grows, and far stricter when it shrinks.** Any
threshold hyprsay hard-codes must either be expressed on the peak probability or be a function
of the option count.

### The vendor's own list of what Jev is bad at, `/model-jaggedness/jev-1.13`, reviewed 2026-09-17
Nine failure modes, each with the vendor's own remedy. The ones that bite a desktop controller:
1. **Literal reading.** "answers the question you wrote, not the one you meant"; put boundary
   cases in the criteria; "Where interpretation is unavoidable, split it into two literal
   questions and combine them in code."
2. **Math and numbers.** "Jev is not a calculator." Explicitly: "`jev-1.13` does not count
   reliably ... items in a long list." Do not ask it how many terminals to open. Do not ask it
   to interpolate a score into a magnitude: "score levels are weak in numerical calibration."
3. **Dates and times** read as text, not ordered quantities. Extract parts as Choices over
   closed sets with an explicit "not stated" option, assemble in code.
4. **Indirection.** "A question about a property of a property ... costs accuracy." Name the
   relevant state fields.
5. **Large state full of irrelevant detail.** "Accuracy falls as the state grows with content
   unrelated to the decision." Filter in code first, or use a Noul to filter for relevance.
6. **Adversarial content.** "State is data, and `jev-1.13` does not treat it as hostile by
   default." Injected instructions in state **can move the answer**. This is the vendor
   confirming hyprsay's window-title threat model in writing.
7. **Contradictory instructions and criteria.** A Noul whose `true` means no performs worse.
8. **No structural invariants.** The same question as a Noul and as a yes/no Choice gave
   `noul 0.22` against `Choice yes 0.01, no 0.99, confidence 0.97` on one ticket. "Ask each
   decision one way; enforce identities in code." [This undercuts a naive two-formulation
   agreement check unless both formulations are the same primitive.]
9. **Generation.** Use a generative model.

### Patterns and cookbooks that map straight onto the owner's complaints
| Vendor page | Mechanism | What it unblocks |
|---|---|---|
| `/patterns/fan-out` speculative fan-out | many questions in one call, including ones whose relevance depends on another answer; "All questions are evaluated in parallel, so adding more questions usually has little effect on response time" | the whole design |
| `/cookbooks/parallel_questions` | 13 questions on a 54,000-character document: "batching every question into one TypeSafe call is **12.2x cheaper and 10.0x faster** with no change in answers"; answers are scored independently, batching adds no noise | proof the fan-out is free |
| `/cookbooks/semantic_find` line-by-line search | tag every line with an id, one **Choice over 218 line ids** ranks them, plus a Noul asking whether the document contains an answer at all | one Choice can address 218 on-screen things, with an abstention |
| `/cookbooks/hierarchical_classification` | **beam search over Choice probabilities**: every node is a Choice whose distribution is its edges; keep K paths, score them `product(edge_probabilities) ** (1 / decisions)`; `separation = top_path_score / second_path_score` | how to exceed 255 options, and how to route app then window then control |
| `/cookbooks/function_calling` | function name as a Choice, every closed-set argument as its own Choice, each with a confidence; "whatever reaches the function is a value the function accepts" | typed actions with parameters, no free text |
| `/cookbooks/pre_parsed_value_extraction_cookbook` | regex finds candidate spans, Jev **selects** the requested span, code normalises the verbatim value | dictation and URLs without generation |
| `/cookbooks/classification_using_confidence` | 75 industry groups with one Choice, then read the answer's own confidence to decide whether to report that group or **the broader division above it** | graceful degradation instead of asking |
| `/cookbooks/skill_suggestion` | at most one skill out of 182, in **two** requests: rank, then re-check the top candidates | the shortlist-then-verify shape |
| `/cookbooks/llm_guardrails` | one request screens every message, thresholding hazard probabilities and severity to pass, review, block, or route | the safety tier ladder as probabilities |
| `/patterns/confidence-routing` | "The answer tells you what; confidence tells you whether to act" | hyprsay already does this, one axis at a time |
| `/patterns/composite-scoring` | atomic scores combined with weights **you** control in code | |
| `/demos/smart-home` | the vendor's own device-control demo | the intended shape for a controller |


### The vendor's smart-home demo, `/demos/smart-home` [PRIMARY]
Worth reading because it is the vendor's own answer to the owner's complaint 1. On
"Turn off all of the lights in the house" it asks, in one request: what category of request is
this; what domain is it targeting; what type of device; and **"What action should be taken on
the lights?" asked before anyone knows the request is about lights**. Verbatim: "This is what we
call a 'speculative question' - we ask it before we even know if it's relevant, allowing us to
evaluate all questions in parallel and rely on code to filter out the irrelevant results after
the fact."

Its compound handling, verbatim: "One of the questions in this demo is a Noul question
identifying if the user request is asking for more than one distinct action. If this is true,
the system uses an LLM to split the request into a list of atomic commands. The split requests
are then evaluated by TypeSafe individually."

[INFERENCE] The vendor's own answer reaches for an LLM here; omause's answer (Jev returns one
bounded ordered plan over live affordances, in a single request) is strictly better for a
latency-bound desktop controller, because it stays inside the one round trip.

Also verbatim, on mixing the two: "The initial TypeSafe response is so fast compared to the LLM
response that it adds negligible latency to the overall system."

---

## 7. Independent evaluations: the numbers that should scare and reassure

### nikkoxgonzales/jev-certify [PRIMARY, 0 stars, created 2026-09-21, MIT]
Zero stars, and the single most useful document I found. 2,412 journalled Jev 1.13 decisions on
CLINC150 (150 intents, human-labelled), total cost $0.23, with conformal risk control and
prediction-powered inference. Its headline results, verbatim from the README table:

| Question | Their answer |
|---|---|
| What does a certified threshold buy? | "At a **5% risk target**: Jev settles **84.75%** of traffic itself with a **2.65%** error rate among settled queries, and the certificate **holds** on held-out data (measured 0.0225 vs certified 0.0499)" |
| Can it do better? | "**No.** alpha = 1% is *infeasible*: Jev returns exactly 1.0 on **56.4%** of answers and **9 of those are wrong**, so the attainable bound floors out at **1.95%**" |
| Does the scope gate work? | "**Yes, at the right budget.** At a 1% target the `noul` gate passes **82%** of legitimate traffic while accepting only **1.67%** of human out-of-scope queries" |
| Where does it break? | "Prevalence shift (**3.6x** over the certified bound), out-of-scope traffic arriving mid-flight (**0.23** loss/query), and **a 20-intent deployment meeting the other 130 intents (1.0000 loss/query)**" |
| Cost and latency | "**$0.1455 per 1,000 queries**, p50 latency **0.37 s**, and **81%** cheaper than escalating everything to an LLM" |

Three consequences for hyprsay, and they are large.

**(a) A probability of exactly 1.0 is not a guarantee.** 56.4% of answers come back at exactly
1.0 and some of those are wrong. hyprsay's tier-2 and tier-3 guards cannot be replaced by a
confidence threshold, at any threshold. Keeping close-a-window and lock-the-screen in code is
vindicated, not paranoid.

**(b) The out-of-vocabulary failure is total, not graceful.** "a 20-intent deployment meeting
the other 130 intents (1.0000 loss/query)" means: when the thing the user wants is not among
the options you enumerated, a forced Choice picks one of yours, confidently, essentially always.
**This is precisely the owner's complaint 4, "nowhere near my intent", stated as a measured
number.** The only defences are an explicit abstention option, a separate scope Noul, and making
the option list actually contain what the user might want. hyprsay already has the first two
(`unsupported_kind` with a `none`, and `addressed`); what it lacks is the third.

**(c) 84.75% auto-settled at a 5% risk target is roughly where a well-built system lands.**
hyprsay's own 91% correct over 348 live evaluations is in the same neighbourhood and is not
embarrassing. The headroom is in coverage, not in accuracy.

Their own survey note is also a useful fact about the ecosystem: they read "all 268 entries in
`v-modal/awesome-jev-tools` across its 13 categories" and found conformal prediction nowhere,
which means there is a second large community catalogue besides AbdelStark's.

### Others, with their published headline numbers [SECONDARY, via the awesome catalogue, repos not individually opened]
- `jujumilk3/jev-calibration-audit`: "95% chose 'unknown' on 300 ambiguous items" (KoBBQ).
  Abstention works when you give it an abstention option.
- `FirasSX914/Janus`: "Jev to DeepSeek improved Banking77 over either alone but Web of Science
  matched Jev at 3.6x cost." The cascade helps sometimes, not always.
- `yodablocks/jev-orderby-bench`: "Passes on 20 Newsgroups, fails four of six on Amazon ESCI."
  Pairwise ordering is not reliable.
- `eugeniughelbur/jev-engineering`: "10% false denials on safe ones" over 300 measured calls,
  for an adversarial permission gate.
- `valentynkit/jev-skip`: "77% of SponsorBlock's sponsor seconds" over 23 videos.
- `jaredpalmer/kev`: "Kev-9B 0.822 vs hosted Jev 0.857 on development", so a local 9B
  reproduction gets within about 4 points. [INFERENCE] 9B at useful latency is out of reach on
  an i5-8350U with no GPU, so this is a note for later hardware, not a plan.

---

## 8. Measured here, on the target machine, 2026-09-22 [MEASURED]

Network floor from this laptop to `api.typesafe.ai`, using `curl` against `/v1/models` with no
API key (the request is rejected with a 401, so nothing was spent and no paid API was called):

| | cold, first request | warm, connection reused |
|---|---|---|
| DNS lookup | **584 ms** | 0 |
| TCP connect | 180 ms | 0 |
| TLS handshake | 191 ms | 0 |
| server round trip | 185 ms | **195 to 198 ms** |
| **total** | **1,140 ms** | **~196 ms** |

Three readings follow.

1. **Roughly 196 ms of hyprsay's measured 315 ms Jev median is wire, not thinking.** The model
   itself is contributing something like 120 ms. That number will not improve by asking less.
2. **A cold connection costs 1,140 ms, and 584 ms of it is DNS** against the router at
   192.168.11.1. If the daemon does not hold a warm HTTP/2 connection and a pinned resolved
   address, the first command after any idle period feels broken. This matches
   jev-voice-browser's "the first request of a process is ~700 ms for the TLS handshake".
3. **Therefore asking twelve questions instead of three is free.** hyprsay's own
   `nlu/requests.py` already says so in its docstring: "Question count is free (1, 5, 15 and 40
   questions cost the same 320 ms)."

ICMP to `api.typesafe.ai` is dropped (4 packets, 100% loss), so the handshake timings above are
the only usable measure.

### What hyprsay asks Jev today, read from the source [PRIMARY, local]
`src/hyprsay/nlu/bank.py::utterance_questions` builds R1 with exactly ten questions, and every
one of them is about **the utterance alone**: `addressed` (Boolean), `intent` (Choice),
`names_window`, `names_app`, `deictic` (Booleans), `workspace`, `direction` (Choices), `amount`
(Score), `verb`, `unsupported_kind` (Choices). Then R2 crowns a window, R2b corroborates it with
one absolute Boolean per window, R2t breaks same-app ties on titles, R3 shards the app list, and
Rc asks one Boolean per candidate "and" seam.

The architecture is already the right one. What is missing from the state is everything the
rest of the field puts in it:
- no indexed list of the controls on screen (so complaint 2 is structural, not a bug)
- no `context.recent_actions` and no previous window (so no "the other one", no "undo that")
- no `complete` Noul and no partial transcript (so complaint 5 is structural)
- no ordered plan question (so complaint 1 is handled by splitting on the literal word "and")
- no parameter-binding second choice (so complaint 6 falls back to `recipes.py`, 853 lines of
  hardcoded key sequences)

### One tension worth re-measuring
hyprsay's `requests.py` docstring records its own measurements: "Latency is flat to about 2.2k
input tokens and then climbs, and the HTTP 503 rate climbs with it (13 of 30 at 11.5k)."
jev-voice-browser runs 3,000 to 6,000 input tokens per request at a 300 ms p50 and reports no
503s. The `/models` page carries a warning that rate limits "can change without notice" because
TypeSafe is "serving a very large volume of demand". [INFERENCE] hyprsay's 503 wall is plausibly
a launch-week capacity artefact rather than a property of the model, and the token budget should
be re-measured before it is treated as a hard design constraint. Until then, 6k tokens is the
figure another working system uses in production.

---

## 9. Not found, stated plainly

- **No Jev project on Hyprland or Wayland.** `gh search repos "jev hyprland"` and
  `"jev wayland"` both return zero. Omause reaches Hyprland only through Omarchy's CLI.
- **No published Jev latency measured from Europe** other than hyprsay's own 315 ms and
  jev-voice-browser's 300 ms p50 (location not stated by its author).
- **No independent reproduction** of jev-ultrafast's 7.1 s Google Flights run, jev-voice-
  browser's 34/34, or fastbrowse's 41/42. Every number in sections 1 to 3 is the author's own.
- **No documented limit on the number of questions per request**, only the 64k combined token
  budget and the 255 options per Choice. `/patterns/fan-out` shows five questions; jev-voice-
  browser ships eleven; hyprsay's own note says forty behaves like one.
- **No Jev support for audio or images.** `/models`: "Text only ... No image, audio, or video
  input." So there is no path where Jev hears the speech itself or looks at a screenshot; the
  transcript and a text rendering of the screen are the only interface.
- **No French-language accuracy figure.** `/models` says only that English is "where accuracy is
  currently best" and other languages are "handled but not equally well". Nobody has published
  numbers. For an AZERTY owner who may speak French, this is an untested axis.
- **I did not open the source of** minecraft-agent, jev-trader, fastbrowse, jev-ultrafast or
  droidrun/mobile-jev beyond their READMEs, nor verify any of the ~180 catalogue entries I did
  not query by API.
- **The TypeSafe launch blog's Doom demo** is cited by rmalde/minecraft-agent as "structured
  game state, typed decisions, and a real-time action loop", but typesafe.ai is a
  JavaScript-rendered Framer site and I could not extract the text to confirm it first-hand.
  [SECONDARY at best.]
- **jevtypesafeai.com and the requesty.ai blog post** surfaced in the first search and are not
  cited anywhere above: neither is a primary source and I did not use them.

---

## 10. What hyprsay should do, complaint by complaint

Each row names the mechanism, where it is proven, and what it costs here.

### Complaint 1: compound commands split on the literal word "and"
**Mechanism: ordered plan over live affordances, one request.** Stop asking "does this 'and'
separate two commands" (Rc, one Boolean per seam). Instead harvest affordances, then ask
`step_1`, `step_2`, `step_3` as three Choices over the same affordance list, each with a `none`
option, plus a Score `step_count` over "one, two, three or more". Code takes the prefix up to
the first `none`. Proven by devfros/omause: "Jev returns the bounded sequence in one request ...
resolves to browser, terminal, terminal, then DND."
**Cost:** zero extra round trips; three more Choices in the fan-out that hyprsay already sends.
Complexity: moderate, because the affordance list has to exist first (see complaint 6).
**Warning:** do not ask Jev *how many* steps as a number. `/model-jaggedness`: "`jev-1.13` does
not count reliably ... items in a long list." Use a Score with named levels, or read the `none`.

### Complaint 2: cannot click inside web pages, no idea what is on a page
**Mechanism: indexed element table in the state, one Choice over element ids, code holds the
real handles.** hyprsay already has the hard half: hypruse's `a11y.py`, `click_ui`, `ui` and
`marks` read AT-SPI and click by control, and the README already says "Clicking a control by
name also works, but only for applications that publish an accessibility tree." What is missing
is putting that list into the Jev state and letting one Choice rank it.

Proven three times over: jev-ultrafast on the DOM (16.8k stars), kevinbadi/jev-voice on the
macOS AX tree ("AX tree of the frontmost window -> indexed element table -> one Jev request"),
mikakostoev on the AX tree with OCR fallback. The vendor's own `/cookbooks/semantic_find` shows
one Choice ranking **218 ids** with a companion Noul asking whether an answer exists at all.

Concrete budget: cap at 100 elements, viewport first, 60 characters of label each
(jev-voice-browser's `MAX_ELEMENTS = 100`, `MAX_ELEMENT_TEXT = 60`, `MAX_STATE_CHARS = 24_000`,
"~6k tokens, far below the 32k-token state limit"). 255 is the Choice ceiling, but the reason to
stay near 100 is accuracy, not the limit: `/model-jaggedness` mode 5, "Accuracy falls as the
state grows with content unrelated to the decision."
**Cost:** no extra network round trip. Reading an AT-SPI tree is local and fast; hypruse already
does it. The real cost is Chromium and Electron, which expose nothing over AT-SPI unless started
with `--force-renderer-accessibility` (hyprsay's MCP guidance already notes "Electron/Chrome
without a flag"). For those, either set the flag or fall back to `screenshot` + `zoom`, which on
an i5-8350U with no GPU is the expensive path and should be last.
**For the browser specifically:** the cheapest honest answer is not OCR. It is to drive Chrome
over CDP the way jev-voice-browser does with `--cdp http://127.0.0.1:9222`, or to ship the MV3
extension port (`wbuecksler/jev-voice-browser-chrome-extension`). That reads the DOM directly,
costs nothing on the CPU, and is the only approach with published success rates.

### Complaint 3: opens a new window instead of going to the one already open
**Mechanism: put both candidates in the same Choice.** Today hyprsay asks `intent` first
(launch versus focus) and then resolves a window or an app separately. Instead the affordance
list should contain `focus_window:<address>` entries for every open window **and**
`launch_app:<desktop id>` entries, and one Choice picks among them. omause does exactly this:
its affordance sources are "open Hyprland windows (focus and close), workspaces, ... `.desktop`
apps, PATH executables", and "Requesting to move an app, such as 'move my browser to workspace
3', focuses that window first and then moves it."
**Cost:** free. It is a restructuring of questions hyprsay already asks, not a new request. It
removes R2, R2b and R3's shard juggling in favour of one list.
**Bias:** put the recency rank and the workspace in each option's description so an already-open
window wins on its own merits, and keep hyprsay's existing "most recently used" tiebreak.

### Complaint 4: nowhere near my intent, not intelligent
**Mechanism: a scope gate and a real abstention, both measured.** jev-certify measured the
failure exactly: "a 20-intent deployment meeting the other 130 intents (1.0000 loss/query)". If
the affordance list does not contain what the user wants, a forced Choice will confidently
choose wrongly, every time. Two defences, both already half-present in hyprsay:
- the `addressed` Boolean is the scope gate. jev-certify measured a `noul` gate at a 1% target
  passing "82% of legitimate traffic while accepting only 1.67% of human out-of-scope queries".
- a `none` option in every Choice, which hyprsay has in `unsupported_kind` and should have in
  every list. `jujumilk3/jev-calibration-audit`: "95% chose 'unknown' on 300 ambiguous items".

The third defence is the one that actually makes it feel intelligent: **make the list bigger and
made of real things.** Intelligence here is not a smarter model, it is a richer candidate set,
harvested live, the way omause harvests `omarchy commands --json`, `.desktop` entries, PATH
executables, keybinding combos and user aliases.

### Complaint 5: systems that act while you are still speaking
**Mechanism: partial transcripts, a `complete` Noul, and a payload rule.** The full recipe is in
jev-voice-browser's `src/constants.js`, which is MIT and 120 lines of thresholds:
`DEBOUNCE_MS = 200`, `MAX_INFLIGHT = 2` with older requests cancelled by `AbortSignal`,
`SILENCE_COMPLETE_MS = 900`, `PAYLOAD_SILENCE_MS = 600`, and
`PAYLOAD_INTENTS = {search_web, type_into_field, select_option}` which may never act
mid-sentence because "the payload may still be growing".

**This is the one item that needs new speech plumbing.** hyprsay decodes after key release. It
needs streaming partial transcripts. sherpa-onnx supports streaming recognisers, and
`docs/research/live/stt_streaming.py` already exists in the repository, so somebody has looked.
Parakeet 110M at 93 ms per one-second clip on this CPU is fast enough to re-decode a growing
buffer several times a second [PRIMARY, hyprsay's own README]. Budget roughly: re-decode every
300 ms, 200 ms debounce, ~196 ms warm wire, so a decision lands about 500 ms after a word, while
the user is still talking.
**Cost:** the biggest engineering item on this list. Also the biggest perceived win, because it
is the one the owner actually named. Note that it multiplies request count: one utterance might
fire four requests instead of one, so ~$0.0008 instead of $0.0002. Still about $1 to $4 a month.
**Keep push-to-talk as the default gate.** Always-on plus an `addressed` Noul is what
mikakostoev ships and it has a "Pause listening" menu item for a reason.

### Complaint 6: deep in-app ability without pre-baked routes, "pick a song on Spotify"
**Mechanism: the affordance table plus typed parameters, which is what replaces `recipes.py`.**
`recipes.py` is 853 lines. The field's answer is to delete that category of code:
- jev-ultrafast: "There are no site-specific action scripts or prepared field strings in the
  policy."
- fastbrowse: "there are no selectors to maintain. The same agent handles a date picker, a
  checkout and a search box it has never seen."
- omause: parameters are resolved by "auto-bind when the transcript uniquely names one,
  otherwise ask Jev a second typed parameter choice."
- the vendor's `/cookbooks/function_calling`: the function name is a Choice, and every
  closed-set argument is its own Choice, so "whatever reaches the function is a value the
  function accepts".

For "pick a song on Spotify" concretely, and honestly: the Spotify desktop client is Electron
and will publish nothing over AT-SPI without the accessibility flag. The three realistic paths,
cheapest first, are (a) the web player over CDP, where it is an ordinary indexed DOM and the
jev-ultrafast loop applies unchanged; (b) the Spotify Web API, where "search then pick" is a
list of real track ids and Jev's job is one Choice over up to 255 of them, which is precisely
the shape `/cookbooks/semantic_find` proves; (c) the desktop client with
`--force-renderer-accessibility`, then the same AT-SPI loop as any other app. None of the three
is a pre-baked route. All three keep hyprsay's rule that the model never emits a selector.
**Cost:** (b) is nearly free in latency and is the only one that can rank the user's own library
semantically. (a) costs a Chrome with a debugging port. (c) costs a relaunch flag and is the
most fragile.

### Cross-cutting, and cheap
- **Pin `jev-1.13.0`, never `jev-latest`.** `/models`: "If you have tuned confidence thresholds
  against a specific version, pin that version's ID." jev-voice-browser pins with the comment
  "aliases move on release, thresholds below were tuned on this version".
- **Hold a warm connection and pre-resolve DNS.** Measured here: 1,140 ms cold against 196 ms
  warm, 584 ms of the difference being DNS.
- **Express confidence thresholds against the peak probability, or make them a function of the
  option count.** From the docs' own formula, `(count * peak - 1) / (count - 1)`, a fixed
  confidence gate silently loosens as the candidate list grows.
- **Do not use a Noul and a yes/no Choice as two independent formulations of the same
  question.** `/model-jaggedness` mode 8 shows them disagreeing 0.22 against 0.01 on one ticket
  and says "Ask each decision one way; enforce identities in code". hyprsay's R2/R2b agreement
  check uses a Choice and a Boolean; that is exactly the pairing the vendor warns about. Two
  Booleans, or two differently worded Choices, are safer.
- **Carry `recent_actions`.** Three entries of `{said, action, target, outcome, seconds_ago}`,
  which is what makes "no, not that one" and "the other one" work, and costs almost no tokens.
- **Adopt mikakostoev's eval discipline:** per command, two phrases that must trigger it and one
  similar phrase that must not, plus `none` rows for chatter. hyprsay's evals are already
  labelled; the missing axis is a negative-per-command rule.

---

# Verification

Adversarial pass, 2026-09-22, by a second agent whose job was to refute. Method: never re-read the
source the researcher read where a different one exists. Repos checked by `gh api` against the git
tree and the actual source files, not the README. Vendor claims checked against the raw `.md` the
docs site serves, not a rendered page. Latency re-measured on this laptop.

Twelve verdicts follow: 3 confirmed, 3 refuted, 1 partly refuted, 4 overstated, 1 unverifiable in use.

## Method

Nine repositories re-checked through `gh api` against the git tree and the source blobs, never the
README alone. Vendor pages re-fetched as the raw `.md` the site serves, so the formula text is the
served text and not a summariser's paraphrase. Network timings re-run here with a different tool
configuration than the researcher used. One claim (the confidence formula) was tested against 2,412
real API responses committed in a third party's repository, which is stronger evidence than the
vendor's own documentation.

All nine repositories exist and are what they say they are. Star counts as of 2026-09-22:
jev-ultrafast 16,878 (researcher said 16,860), jev-trader 1,919 (said 1,918), minecraft-agent 491,
jev-voice-browser 224, fastbrowse 94, kevinbadi/jev-voice 68, and **omause 0, jev-certify 0,
mikakostoev/jev-voice-control 0**. Nothing was fabricated. What follows is where the reading of
those repositories does not survive contact with their code.

## V1. CONFIRMED: jev-voice-browser's thresholds and question set are real code

`src/constants.js` was fetched and read directly (347 lines). Every number the researcher quoted is
verbatim in it: `MODEL = "jev-1.13.0"` with the comment "pinned: aliases move on release, thresholds
below were tuned on this version" (line 15), `MAX_ELEMENTS = 100` (22), `MAX_ELEMENT_TEXT = 60` (23),
`MAX_STATE_CHARS = 24_000` (24), `DEBOUNCE_MS = 200` (35), `MAX_INFLIGHT = 2` (36),
`SILENCE_COMPLETE_MS = 900` (37), `PAYLOAD_SILENCE_MS = 600` (41), and the gate block at 50 to 59:
`intentConfidence 0.55`, `complete 0.6`, `isCommand 0.5`, `destructive 0.5`, `targetConfidence 0.45`,
`targetTopProb 0.35`, `correction 0.6`. One constant the researcher omitted and the plan will want:
`destructiveIntentConfidence: 0.9` (line 54), the override that lets a high-confidence intent through
a destructive gate when the user has already said "confirm".

No correction. This finding is as solid as a one-week-old repository gets.

## V2. REFUTED (as a porting claim): "swap Web Speech for local Parakeet, which is strictly better"

This is the researcher's own inference, printed under a [PRIMARY] finding, and it is wrong on this
machine for a mechanical reason.

jev-voice-browser's whole act-early design rests on the Web Speech API emitting **interim results**
word by word for free: the recognizer is remote, streaming, and costs the laptop nothing per partial.
hyprsay's recogniser is `sherpa_onnx.OfflineRecognizer` (`src/hyprsay/stt/local.py:88-90`), which has
no interim result. Acting mid-sentence means re-decoding a growing prefix on the CPU every step.

Measured on this laptop by a sibling lane (`scratchpad/vision/prefix_parakeet.json`, Parakeet TDT
110M int8, 1 thread, 200 ms step): a 0.2 s prefix decodes in 52.6 ms, 1.0 s in 121.8 ms, 1.4 s in
225.4 ms, 1.6 s in 256.6 ms. By roughly 1.2 to 1.4 s of speech the decode of one prefix already
exceeds the 200 ms step, so on one thread the partial pipeline falls behind the speaker inside two
seconds. Worse for the policy: `is_word_prefix_of_final` flips to **false** at the 1.4 s rung
("Well, I don't wish to say" against a final "wish to see"), so an early decision can be taken on a
prefix the final transcript then contradicts, a failure mode Web Speech's own interim results share
but which the jev-voice-browser policy handles only through `complete` and the recogniser's final.

Correction for the plan: local Parakeet is better on privacy, offline operation and cost, and worse
on exactly the axis this design needs. Budget a CPU cost per partial and a prefix-instability rule,
or evaluate a streaming recogniser, before promising act-while-speaking on an i5-8350U.

## V3. OVERSTATED: the "34/34, 330 ms, $0.01" measurements

The numbers are quoted correctly from the README (lines 163 to 165). What they can carry is smaller
than the researcher's use of them.

- The suite does not run without a key and nothing runs it but the author. `test/integration/jev-decisions.test.js`
  line 154: `test(..., { skip: !hasApiKey() }, ...)`, and line 150 prints "SKIP: no TYPESAFE_API_KEY".
  `gh api repos/moritzkremb/jev-voice-browser/contents/.github` returns **404**: there is no CI. No
  third party has reproduced 34/34.
- The npm script's own bar is lower than the README's headline: `npm run test:integration` is
  documented as "prints pass rate (expects >= 90%)" while the README reports 100%.
- Part of the fixture is hand-written, and the file says so: "result links written by hand because
  results render client-side and don't appear in headless captures". Seven of the 34 cases are the
  context/correction cases added in the second and final commit.
- Machine and location for the latency figures are given only as "from this machine".

The researcher then concluded that hyprsay's own 503s past 11.5k tokens are "probably a launch-week
capacity artefact". That does not follow from a 3k-to-6k-token workload succeeding. Re-measuring
hyprsay's token ceiling is the right action; this finding is not evidence about it.

## V4. CONFIRMED with a material omission: jev-ultrafast's four heads

Verified in `jev_ultrafast/model.py`, not the README. Line 91 builds `questions` with an `operation`
Choice; the loop at 94 adds one `<operation>_target` Choice per operation present, each carrying only
compatible elements; line 120 validates the `operation` answer, 125 to 130 validate only the target
head the operation selected, and the comment at 126 reads "Unused target heads cannot cause an
action." The mechanism is exactly as described, in one request.

Two things the researcher did not report, both of which change plan decisions:

1. **jev-ultrafast does not pin the model.** Line 108: `"model": os.environ.get("TYPESAFE_MODEL", "jev-latest")`.
   The viral repository does the opposite of what finding 8 recommends. Follow jev-voice-browser here,
   not this one.
2. **The TYPE_TEXT helper is a paid third-party LLM, named.** `docs/performance.md`: "Mercury
   generates the city strings when TYPE_TEXT is selected", model `inception/mercury-2.5`, billed
   through OpenRouter at $0.00006272 for two calls in the recorded run, 581 ms for "Zurich" and
   346 ms for "London". So the free-text path costs a second network round trip of roughly 350 to
   600 ms. hyprsay's equivalent is span selection from the transcript (jev-voice-browser's approach),
   which is free and avoids that hop.

Also worth carrying: `docs/performance.md` is unusually honest and its honesty limits the claim. The
headline 7.073 s is **one recording**. The matched comparison is **three pairs**, and the author
writes "two-sided sign-test p = 0.25 ... This is a small controlled-input comparison, not a broad
agent benchmark." Median Jev latency in that run was **178 ms**, which is a second independent data
point that Jev's own service time is well under 200 ms.

## V5. REFUTED: "omause reaches Hyprland only through the Omarchy CLI, not through the compositor IPC"

It calls `hyprctl` directly, and in the batched form. `src/state.ts` line 48:

```ts
const { stdout } = await execFileAsync("hyprctl", [
  ...
  "activewindow;activeworkspace;clients",
]);
```

with single-shot fallbacks at lines 122, 131 and 138 (`hyprctl activewindow -j`, `activeworkspace -j`,
`clients -j`). A repository-wide grep finds `hyprctl` in `src/state.ts`, `src/registry.ts`,
`src/providers.ts`, `src/registry.test.ts` and `spec/project-idea.md`. `src/affordances.ts` iterates
`desktop.clients` and formats each as "<class>: <title> on workspace <n>", which is the compositor's
client list, not an Omarchy route.

Two consequences, in opposite directions. Against the researcher: hyprsay's compositor-IPC core is
**not** unpeered, and omause is a closer competitor than the report says. For the researcher: the
mechanism that fixes complaints 1 and 3 is confirmed in code, not just README prose. `src/jev.ts`
lines 98 to 130 really do build `step_1 ... step_N` Choices in **one** request over the same
affordance list, with the instruction "Build the complete ordered plan in one response. Each plan
slot invokes at most one affordance" and "After stop or any fail choice, every later slot must also
be stop." The compound command is solved by N parallel ordered Choices, with no LLM and no split on
the word "and". That part stands.

## V6. UNVERIFIABLE IN USE: omause as evidence that any of this works on a desktop

Evidence quality on this repository is the weakest in the report and the report treats it as
[PRIMARY] on equal footing with 16,878-star code.

0 stars, 0 forks, 0 watchers, 0 open issues. All 17 commits fall inside one 28-hour window, 2026-09-19
13:22 to 2026-09-20 10:02. Eleven of them are authored by **`omause-agent`**, an agent identity, and
the remaining six by "Afros Rajabov". The repository was created at 10:02:55Z and last pushed at
10:03:03Z, **eight seconds later**, then never touched again. There is no CI, no release, no demo
video, no issue, and nobody but the author. The unit tests (`*.test.ts`, about ten files) exercise the
TypeScript in isolation; nothing demonstrates a live Hyprland session ever executed a plan.

Read omause as a design sketch that compiles, which is genuinely useful, and not as a working peer.

## V7. CONFIRMED, and independently strengthened: the confidence formula

The served page confirms it verbatim. `curl https://docs.typesafe.ai/confidence.md`, line 29 to 31:

```js
const count = values.length;
const peak = Math.max(...values) / 100;
return Math.max(0, Math.min(1, (count * peak - 1) / (count - 1)));
```

Caveat the researcher should have raised: this is a React explorer component (`export function
ConfidenceExplorer()`, line 9, rendered at line 151). It is what the documentation's widget computes,
which is not by itself proof of what the API returns.

So I tested it against the API's own output. `results/journal.jsonl` in nikkoxgonzales/jev-certify
(4.5 MB, fetched raw from `raw.githubusercontent.com` because the contents API truncates it) holds
2,412 real responses from `typesafe/jev-1.13-20260917`, each with the full `probabilities` map and
the returned `confidence`. Recomputing the formula from the probabilities:

**2,412 of 2,412 Choice answers match, worst absolute difference 0.0000.**

The formula is exact, and it is the API's, not just the documentation's. Everything the researcher
concluded from it about thresholds loosening as candidate lists grow holds, and holds harder.

## V8. OVERSTATED: jev-certify's headline rates

The arithmetic checks out against `results/results.json`, which I parsed rather than reading the
README: `routing.primary.measured_on_holdout` gives coverage 0.8475, selective_error 0.0265,
per_query_risk 0.0225 against certified 0.0499, `min_attainable_bound` 0.0195, and
`quantization.share_at_1.0` 0.564 with `errors_at_1.0` 9. CI is green on the last four pushes and
`tests/test_readme_numbers.py` asserts the README against the results dict. This is careful work.

What the report should not do is quote "2.65%" as a rate.

- It is **339 routed queries out of an n=400 holdout**, and the repository itself publishes the
  interval: `selective_error_wilson95 [0.0140, 0.0497]` and `selective_error_cp_ucb95 0.0459`. The
  95% upper bound is nearly double the point estimate. "About 3%, and possibly 5%" is the honest form.
- The quantization figure is over **860 answers**, not 2,412.
- The cost figures disagree with each other inside the repository: the headline says "$0.23 in total"
  for 2,412 decisions, `results.json` `cost` covers 1,160 decisions at $0.168776, and the repro
  section says "~$0.35 for 2,412 decisions". The per-1k figure of $0.1455 is the one to carry.
- Journal latency recomputed by me across all 2,412 records: **p50 0.357 s, p95 0.558 s, min 0.255 s,
  max 20.041 s**. The README's p95 of 0.744 s is from the 1,160-decision subset. Note the 20 s
  outlier: whatever the median is, the tail on a public endpoint is seconds long, and a push-to-talk
  daemon needs a timeout and a local fallback for it.

The conclusions the researcher drew, that 1.0 is not a guarantee and that a forced Choice over a
candidate list missing the user's intent fails at 1.0000 loss per query, survive all of this intact.

## V9. OVERSTATED: kevinbadi/jev-voice as proof the loop ports to a desktop a11y tree

The accessibility half is real and better than described. `jev_voice/desktop.py` (35.8 KB) imports
`ApplicationServices` and `Quartz` through PyObjC, batches attribute reads with
`AXUIElementCopyMultipleAttributeValues` (line 142), keeps a `GUARD_ATTRS` list of
`AXRole, AXTitle, AXDescription, AXValue, AXEnabled, AXSelected, AXPosition, AXSize` (line 63) that
is re-read before acting, and the module docstring states "``Desktop.act()`` re-resolves geometry,
hit-tests for occlusion, and only then posts real events". The safety model the researcher quoted is
implemented, not aspirational. That is the single best piece of evidence in the lane for hyprsay's
AT-SPI move.

But "one Jev call per command" is not this project's loop any more.

- `jev_voice/escalate.py` exists and calls **Anthropic**: `import anthropic`, `MODEL = os.environ.get("ESCALATE_MODEL", "claude-fable-5-1")`,
  `FAST_MODEL = "claude-haiku-4-5")`, described as "Escalation to a fast Claude model, only where a
  Jev choice cannot be made from the screen" and used for "first arbitration, text values".
- `jev_voice/web.py` is **62.3 KB, larger than desktop.py**: much of the project is browser control,
  not desktop a11y.
- Since the researcher read it (the README claim is dated 2026-09-22 03:24), the repository has grown
  a chess mode driving Stockfish, a teaching mode with Haiku narration and Kokoro TTS. It is moving
  fast and away from the clean shape quoted.
- The latency line is **Mac mini M4** (README section "Latency (Mac mini M4, measured)") with
  whisper.cpp on **Metal**. None of "whisper.cpp about 100 ms" transfers to an i5-8350U with no GPU.
  The README's own table gives "Jev fan-out 170-420 ms", a range, not "about 250 ms".

## V10. REFUTED, with a better number: the 584 ms DNS attribution

The magnitudes reproduce. Cold, three fresh curl processes: total 1.111 s, 1.105 s, 1.185 s. Warm on
a reused connection: 0.198 s, 0.184 s, 0.199 s, 0.224 s. So "about 1.14 s cold, about 196 ms warm" is
right, and the endpoint answers 403 (`{"detail":{"error_type":"authentication_error", ...}}`), not the
401 the researcher reported. Nothing was spent.

The cause is not the resolver, and the recommended fix is therefore wrong.

```
dig api.typesafe.ai            Query time: 10 ms, then 2 ms, then 2 ms   (server 192.168.11.1)
curl -4  ... /v1/models        total=0.561  dns=0.0029  tls=0.367
curl -4  ... /v1/models        total=0.577  dns=0.0027  tls=0.368
curl --resolve (DNS bypassed)  total=0.576  dns=0.000024
getent hosts api.typesafe.ai   real 0m0.602s   -> 100.20.85.248 and 44.227.31.201
dig api.typesafe.ai.Home       Query time: 237 ms
dig api.typesafe.ai.home       Query time: 286 ms
```

The home router resolves the real name in 2 ms. The 580 ms is the glibc stub resolver walking
`search Home home` from `/etc/resolv.conf` and asking for both A and AAAA, four lookups that fail
slowly, before it ever asks the right question. `curl -4` removes it entirely, and `--resolve`
confirms that pinning the address buys **nothing beyond what an A-only lookup already gives**:
0.576 s pinned against 0.561 s with a live IPv4 lookup.

Corrected numbers for the plan, measured here:
- Cold connection to api.typesafe.ai: **about 560 ms**, of which TLS completes at about 367 ms and
  the server round trip is about 190 ms. Not 1,140 ms.
- Warm on a held connection: **183 to 224 ms**.
- Of the researcher's "584 ms DNS", roughly **580 ms is a local resolv.conf pathology**, fixable by a
  one-line config change or by forcing IPv4, and about 3 ms is the actual lookup.

The advice that survives: hold a warm HTTP/2 connection, because 190 ms of transport per command is
unavoidable and a fresh TLS handshake triples it. The advice to drop: "pin a resolved address".

One limit on all of this: these timings are a 403 rejection at the edge, not an inference. They bound
transport, not Jev's service time. The best independent figures for service time are jev-certify's
journal (p50 0.357 s through OpenRouter) and jev-ultrafast's recorded run (median 178 ms direct).

## V11. OVERSTATED: fastbrowse's head-to-head

Every number is quoted correctly from the README, and the README links `docs/evals.md`, which is more
candid than the table. The problem is what kind of evidence it is.

- **It is a self-benchmark by the winning arm.** agent-labs-dev wrote fastbrowse, chose the 14 tasks,
  ran all three arms and graded them.
- **The arms are not graded the same way, and the author says so.** `docs/evals.md`: "Where an arm
  exposes none of these (the Browser Use agent has no final URL or quotes), its answer text is checked
  against the truth." fastbrowse is graded on recorded requests and verbatim quotes; the competitor on
  its prose.
- **The cost ratios include estimates.** "Cost includes reported model charges, estimates where only
  token usage is available, and the browser and proxy cost returned when the cloud browser stops."
  The 65x to 195x ratios are not all measured dollars.
- **n is 14 tasks by 3 passes**, and step limits differ ("fastbrowse and jev-ultrafast share a 50-step
  limit, and the Browser Use agent exposes none").
- The 18/18 against 12/18 comparison is against a **pinned jev-ultrafast commit**
  (`1231850a0bf1a0c0341fe408ef1668dbbfdfac46`, `main` as of 2026-09-18) of a four-day-old project.

The researcher's own reading, "the honest claim for hyprsay is reliability and money, not speed", is
the right conclusion and is if anything better supported than the numbers behind it.

## V12. PARTLY REFUTED: "there is no Jev project competing with hyprsay on its own ground"

The negative search reproduces: `gh search repos "jev hyprland"`, `"jev wayland"` and
`"typesafe hyprland"` all return zero rows here today. But the search is the wrong shape, because
nobody names the compositor. `gh search repos "omarchy voice"` returns **ten** voice-control projects
for a Hyprland distribution, including `wombatoperator/omarchy-voice` (created 2026-09-21),
`Cloud-First-Consulting/omarchy-voice` (2026-09-20), `John-Dennehy/omarchy-voice`,
`azzenabidi/omarchy-voice` (local STT plus Piper TTS, push-to-talk) and
`damain/omarchy-voice-dictation`. I checked the two newest: neither README mentions TypeSafe or Jev,
so the **Jev**-specific claim holds, but the field hyprsay is in is crowded with LLM-driven Hyprland
voice controllers appearing at a rate of roughly one a week. Combined with V5, hyprsay's real
differentiators are the guarded execution model and AT-SPI clicking, not being alone on the platform.

## What a plan should carry after this pass

1. Port jev-voice-browser's question set and gate table. The constants are verified. Add
   `destructiveIntentConfidence`, which the report missed.
2. Do not promise act-while-speaking on Parakeet offline decoding without first paying for prefix
   re-decodes on this CPU. Numbers above; this is the largest unresolved risk in the vision.
3. Port jev-ultrafast's speculative operation-plus-targets fan-out. It is verified in code. Pin the
   model as jev-voice-browser does, not as jev-ultrafast does.
4. Take omause's one-request ordered plan for compounds, and treat omause as a sketch, not a proof.
   It already uses `hyprctl`, so the plan should assume a competitor could do this too.
5. Express every threshold on the peak probability. The confidence formula is now confirmed exactly
   against 2,412 real responses.
6. Budget 190 ms of unavoidable transport per command on a warm connection, about 560 ms if cold, and
   a seconds-long tail that needs a timeout and a local fallback. Fix `/etc/resolv.conf` or force
   IPv4; do not bother pinning an address.
7. Expect an LLM hop for free text unless spans are cut from the transcript. Both jev-ultrafast
   (Mercury, 346 to 581 ms) and kevinbadi/jev-voice (Claude) pay it. jev-voice-browser does not.
