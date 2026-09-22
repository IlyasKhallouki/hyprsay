# Lane: prior art. What already exists, so we do not reinvent it, and so we know the bar

Written 2026-09-22 for the hyprsay vision work. Research lane: recent voice control and
computer control systems, what they do that hyprsay does not, and where the bar actually is.

Evidence labels used throughout:
- **[P]** primary: a repository, the project's own docs, a named author's post, a patent, a real demo
- **[S]** secondary: third-party article, review, forum thread
- **[V]** vendor claim: marketing or a spec sheet, unverified
- **[M]** measured by me on this machine, today
- **[I]** my own inference, explicitly not a finding

Target machine for all cost estimates: i5-8350U (4C/8T, Kaby Lake R, no GPU), 23 GiB RAM,
Arch Linux, Hyprland 0.56.2, French AZERTY, one 1920x1080 display.

**Search hygiene note.** The dictation-tool space is saturated with SEO content farms that
exist to sell a competing product. spokenly.app, getvoibe.com, vmake.ai, softorbits.net,
toksta.com, byteplus.com, novavoice.app, dume.ai, oran.chat and saner.ai all appeared in
results for these queries and all of them are marketing for a rival tool. I treat none of them
as evidence for a number. Where I could not reach a primary source I say **not found**.

---

## 1. Talon Voice, and the news that matters most to hyprsay

### 1.1 Talon 1.0 shipped 2026-09-20, two days ago, and it is abandoning Linux [P]

Source: the official changelog, `https://talonvoice.com/dl/latest/changelog.html`.

Verbatim: *"NOTE: Talon 1.0 (plus any minor fixes) is planned to be the last public release of
Talon for Linux/X11."* The same release adds detection for Wayland sessions, with a warning to
the user that Talon is not compatible with it.

This is the most strategically important fact in this lane. The most capable voice control
system in existence, the one every serious hands-free user runs, has just announced it is
leaving the platform hyprsay lives on, and it never supported Wayland at all. The niche is not
crowded. It has just been vacated, publicly, two days ago.

### 1.2 What Talon 1.0 gained, and what each item means for us [P]

From the same changelog:

- **Three new GPU-accelerated models dated 2026-09-20**: `Hum` (streaming dictation), `Song`
  (command recognition plus rejection), `Tone` (speaker verification).
- **Conformer D2 (2026-05-27)**: *"improves accuracy and has an algorithm for rejecting
  incorrect commands."*
- **`axquery`**: *"New `axquery` system for CSS-style queries against the accessibility
  hierarchy"*, with actions `query("AXButton")` and `query_all("AXButton")` returning Element
  objects.
- Optimisation: *"Optimize grammar compilation performance (significantly)"*, *"Optimize CPU
  and memory usage"*, context-switching performance, script reload latency.

Read those four together and you have Talon's answer to three of the owner's six complaints.
`Song` with explicit *rejection* answers "it acts on things I did not mean". `Tone`, speaker
verification, answers "voice is an unauthenticated channel", which hyprsay currently solves
with tiers and countdowns. `axquery` answers "it cannot click inside things": a queryable
accessibility tree with CSS-like selectors, as a first-class scripting primitive.

What is **not** in the Talon 1.0 changelog: any LLM integration, anywhere. Talon's entire bet
is a compiled grammar plus better acoustic models. That is the ceiling of the non-LLM approach
and it is a high ceiling.

### 1.3 The mechanism that makes Talon good, and that hyprsay does not have [P for mechanism, I for consequences]

Talon's core design, documented across talonvoice.com/docs and the community wiki: the set of
currently active commands is **compiled into the speech engine's language model**. Recognition
and parsing are one step, not two. The recognizer is not free to emit arbitrary English and
leave a parser to guess afterwards; it is constrained to the grammar the current context
allows, and context is itself a function of the focused window (app, title, hostname).

Three consequences that hit hyprsay's complaints directly:

1. **A command is recognised or rejected, never "transcribed then misparsed".** hyprsay's
   grammar sits *after* a free-form recognizer, which is why `normalize` must repair
   "workspace too" into "workspace 2". Talon never has that class of bug because "too" was
   never a legal token at that position.
2. **Commands chain within one utterance by grammar**, not by splitting on the literal word
   "and". A Talon user says four commands in one breath and the parser segments them because
   the grammar says where a command ends. **This is the exact answer to owner complaint 1.**
   The fix is a parser over the whole utterance, not a smarter split.
3. **Context is the grammar.** `app: firefox` and `title: /GitHub/` blocks in `.talon` files
   activate and deactivate whole command sets as focus moves. **Owner complaint 3, that
   hyprsay ignores what is open and where focus is, is something Talon solved in 2019 by
   making the active vocabulary a function of live desktop state.**

Latency: the community wiki says only *"Even lower latency for Talon beta users due to ongoing
performance optimisations"* and gives no number `[P, unquantified]`. The "sub-10 ms" figure
that circulates came from an SEO page (novavoice.app) and I could not trace it to any primary
source. **Do not use it.** `[not found]`

### 1.4 What Talon users complain about [S]

From handsfreecoding.org's in-depth review and community threads: the learning curve dominates,
typically weeks to productivity; the vocabulary is large and must be memorised; the whole thing
is a configuration project rather than an appliance; and the engine is closed source, which is
a standing irritation in Linux circles. **The complaint is never "it is not smart enough". It
is "it is too much work to set up".** That is the opposite failure mode from hyprsay's.

---

## 2. Cursorless: the best worked example of semantic targets inside an application [P]

Source: `https://www.cursorless.org/docs/`.

Mechanism, stated well enough to rebuild:

1. A VS Code extension parses the visible buffer with **tree-sitter** into a syntax tree.
2. Every token on screen gets a **"hat"**: a small coloured shape drawn over one character of
   the token. The (colour, shape) pairs are the address space, a few hundred simultaneously
   addressable targets on a screen.
3. Talon's grammar knows the hat alphabet, so `"chuck blue air"` parses as action=`chuck`
   (delete), target=the token whose hat is a blue mark on the letter "a".
4. The action applies to a **syntactic scope** resolved from the tree, not to a pixel: "take
   funk" takes the enclosing function, "chuck arg" deletes the enclosing argument including its
   trailing comma.

Three ideas worth stealing, in order of value:

- **Every addressable thing on screen gets a short, speakable, stable name, rendered as an
  overlay, all the time.** hyprsay already has numbered badges, but only as a tie-breaker.
  Cursorless proves the idea generalises from "pick one of two windows" to "pick one of two
  hundred elements", and that users accept it *because it is always on*, so the name is
  available before you need it rather than appearing as an interrogation after you fail.
- **The target is resolved in a structured tree, never from pixels.** No OCR, no screenshots,
  no vision model.
- **Action and target are separate grammar slots.** Action vocabulary is small and closed;
  target vocabulary is generated from live state. That is exactly hyprsay's split between
  `ops.py` and `lexicon.py`, extended to the inside of a window.

Honest limitation: Cursorless works because VS Code exposes the buffer and lets an extension
draw decorations. There is no general equivalent. On Linux the nearest thing is AT-SPI, which
is much weaker. See section 6, where I measured it.

---

## 3. Acting while the user is still speaking: how it is actually done

This is owner complaint 5, and it has a real, documented, buildable answer. Two distinct
techniques get confused under one name, and only the second is interesting.

### 3.1 Streaming partial transcripts [S, industry default]

Production voice stacks in 2026 run a streaming recognizer emitting a partial hypothesis about
every 50 ms, revised as audio arrives. This is the default in LiveKit Agents and Pipecat and
every cloud ASR vendor exposes it `[S]`. Consequence: at the instant the user stops speaking,
the transcript already exists; you do not pay a decode afterwards.

hyprsay today pays 93 ms of local decode *after* key-up `[M, the project's own README]`. Fast,
but it is 93 ms of dead time that a streaming recognizer would have spent during the utterance.

**sherpa-onnx, which hyprsay already links, ships streaming (online) recognizers**, including
streaming zipformer transducers with an endpointing API. Parakeet TDT CTC 110M is an offline
model. Moving to, or adding, a streaming model is a change of model class, not of framework.
`[P]` that sherpa-onnx supports streaming. `[I]` that it is close to a drop-in for hyprsay.

### 3.2 Eager end-of-turn, the Deepgram Flux design [P, vendor docs with named parameters]

Source: `https://developers.deepgram.com/docs/flux/voice-agent-eager-eot` and
`.../flux/configuration`. This is the cleanest published specification of "act before they stop
talking" that I found, and it is a protocol, not a model.

Three events and what the client does with each, quoting the docs:

- **`EagerEndOfTurn`**: a moderately confident transcript. *"Send the transcript downstream to
  your LLM and begin preparing a reply."*
- **`TurnResumed`**: the user kept talking. *"Cancel the in-progress response and wait for the
  next `EagerEndOfTurn` or `EndOfTurn`."*
- **`EndOfTurn`**: *"Finalize and deliver the response you've already started preparing."*

The rule: *"Use `EagerEndOfTurn` outputs to draft, not finalize"* and *"Avoid committing to a
reply until `EndOfTurn`."*

Parameters: `eager_eot_threshold` (default 0.3), `eot_threshold` (valid 0.5 to 0.9, default
0.7), `eot_timeout_ms`.

The guarantee that makes it safe: *"`EndOfTurn` transcript will exactly match the
`EagerEndOfTurn` transcript"* when the eager turn is confirmed, which is what lets you cache
the speculative work rather than redo it.

Reported numbers `[V]`: median end-of-turn detection latency under 300 ms, p95 1.5 s; eager EOT
reduces median response latency by about 150 ms when it fires, with top-5-percent savings around
350 ms. **Stated cost: 50 to 70 percent more LLM calls.**

**Why this matters enormously for hyprsay specifically:** Jev is priced at $0.042 per million
input tokens with output free `[S, widely reported; vendor pricing page not read in this lane]`.
A 50 to 70 percent increase in call volume against that price is, in absolute terms, nothing.
The technique whose cost is prohibitive for a generative LLM is essentially free for Jev. That
is a genuine structural advantage and nobody else has it.

### 3.3 Speculative execution with an invalidation rule: the Voice-Light mechanism [P]

Source: Bertil Braun, *"Voice-Light: A Full-Duplex Cascaded Voice Agent with Causal Turn-Taking
and Speculative Generation"*, arXiv:2609.20995v1, September 2026. **Caveat up front: a
single-author preprint with a small evaluation, 36 measured turns across three unscripted
sessions. Treat the numbers as indicative and the mechanism as well specified.**

Mechanism, which transfers to a desktop controller almost unchanged:

- A small adapter (~183K parameters) taps a **frozen streaming ASR encoder** at layers 6, 12,
  18 and 24, every 80 ms, through causal depthwise-separable convolutions and a unidirectional
  GRU. All features are causal; the model may not see future audio.
- Speculation **starts after 80 ms of low speech probability**, roughly 160 ms in.
- Speculated output **stays private** until final ASR and a transcript-revision check permit
  promotion.
- **The invalidation rule is the valuable part: revisions limited to case, punctuation,
  whitespace or apostrophes preserve the speculated work; any lexical change invalidates it
  entirely.**
- Speculation is discarded if (1) the transcript changes lexically, (2) turn commitment fails,
  or (3) new user speech triggers a floor-take.
- Commitment: an 800 ms timeout, or floor-take probability above 0.82.

Reported latencies: final VAD to rendered output 836 ms median overall; **turns with promoted
speculation 667 ms median (n=9) against 1,513 ms without (n=4)**. The author notes the causality
is confounded with transcript stability, which is honest: easy utterances both stabilise early
and get promoted.

Cost in that deployment: two A10 GPUs, cold readiness 32 to 55 seconds. Irrelevant to an
i5-8350U, but note that **the control logic itself costs nothing**: it is a state machine over
partial transcripts.

**The most useful sentence in the paper is a negative result.** The learned turn-taking adapter
achieved **12.53 percent end-of-turn recall against 95.60 percent for a plain Silero VAD timing
baseline** on 1,673 real-conversation test cases, so the shipped system keeps the simple VAD as
the authority and treats the learned signal as optional evidence. **Do not build a learned
end-of-turn model for hyprsay.** Someone tried both and the dumb one won by 7x.

### 3.4 Prior art in patents [P]

US12046234B1, *"Predicting on-device command execution"*, assignee Amazon Technologies, filed
2021-06-28, granted 2024-07-23. An arbitration component predicts, after initial speech
processing but before skill execution, whether the device can handle the command locally, and
**stores an execution plan as a backup so that a wrong prediction is recovered from a stored
plan rather than by reprocessing the input**. An arbitration monitor observes outcomes and
updates the policy when predictions diverge from results.

Relevance: it is prior art for the general shape of "decide early, keep a fallback plan, learn
from divergence", in a voice assistant, held by Amazon. Worth knowing it exists. It is about
local-versus-remote routing rather than about acting mid-utterance, so I do not read it as
blocking anything hyprsay would do `[I, and I am not a lawyer]`.

### 3.5 What all of this means concretely for hyprsay [I, clearly labelled]

hyprsay is push-to-talk. **That is a gift, not a limitation.** The key release *is* the
end-of-turn signal, so hyprsay never has to solve the hard half of the problem, the half that
beat the Voice-Light author. What it can take is the other half:

- Run a streaming recognizer **while the key is held**.
- On each stable prefix, run the grammar. If the prefix already determines the action and the
  remaining audio cannot change the verb, **warm the path**: build candidates, refresh desktop
  state, open the Jev connection, read the accessibility tree of the focused window (section 6
  says that costs about 90 ms for a large tree, so it must happen during the utterance, not
  after).
- Apply the Voice-Light invalidation rule verbatim: a lexical change to the prefix throws the
  speculation away; case and punctuation do not.
- Use the Deepgram split: **draft on eager, commit on final.** Never dispatch above tier 0 on a
  speculative prefix. hyprsay's existing tier table already draws that line.
- Expected saving: the 93 ms decode plus the 315 ms Jev round trip, both moved under the
  utterance. On a two-second command that is roughly 400 ms of the felt latency gone, which is
  most of it, since the README already notes window animations of 500 ms or more are often the
  largest single delay.

---

## 4. Dictation tools: Wispr Flow, Aqua, Superwhisper, Willow, Handy

These matter less than their visibility suggests. **None of them does semantic in-application
work.** Every one is "hold a key, speak, get text injected at the cursor", with an LLM cleanup
pass. hyprsay already does strictly more. Where they are ahead is polish, streaming, and using
screen context for *understanding* rather than for *action*.

### 4.1 Wispr Flow [S and P]

- Latency: *"under 700 ms at p99"* attributed to Baseten, the model host Wispr uses on AWS
  us-east-1 `[V relayed through S]`. Reviewers report the felt round trip nearer 1 to 2 seconds
  `[S]`.
- Wispr's own claims: 99.9 percent dictation uptime "over the past few weeks", latency down 30
  percent since the start of 2026 `[V]`.
- Documented outage: sustained dictation-latency incidents and intermittent outages from
  2026-05-27 to 2026-06-03, all platforms and all regions simultaneously. Source: Wispr's own
  status page at `statuspage.incident.io/wispr-flow` `[P]`.
- Complaints: Digital Trends reported that Wispr asked its critics what was wrong and more than
  700 people answered `[S]`. Recurring themes: cloud dependency, idle resource cost (a Reddit
  benchmark on a 2021 MacBook Pro reported roughly 800 MB RAM and 8 percent CPU **while idle**),
  and the Electron Windows build freezing target applications including VS Code.

**Lesson: a cloud-only voice tool is one outage away from useless, and users measure idle
resource cost.** hyprsay's local-first default is a competitive advantage, not merely a privacy
nicety, and its two-process design should be checked for idle cost before release.

### 4.2 Aqua Voice [S; vendor site unreachable in this session]

Aqua's differentiator is **natural-language editing of what you just dictated**: "make this a
list", "rephrase that", "redo the second sentence", with no command syntax to memorise, plus a
streaming display so words appear as recognised rather than pasting in one chunk. Their model
is called Avalon, launched August 2025, cloud only, no on-device mode on any plan. Latency
figures seen in reviews: sub-50 ms startup, 450 ms to 1 s to text insertion. All `[S]` from
competitor-owned blogs. `withaqua.com` 308-redirects to `aquavoice.com`, which I did not reach,
so **none of this is confirmed from the vendor** `[not found]`.

What is worth stealing regardless of the numbers: **the edit-the-last-thing loop.** "no, the
other one", "undo that", "bigger instead", applied to hyprsay's last *action* rather than to
text. hyprsay has a journal, and `ops.py` already records how to undo each operation. A
correction grammar is the cheapest possible way to feel intelligent, because the user's
correction carries almost all of the disambiguating information, and because a system that
recovers gracefully from a wrong guess is allowed to guess more often.

### 4.3 Superwhisper, Willow Voice, Handy [S]

- **Handy** is the open-source local-first cross-platform one (Rust plus Tauri, whisper.cpp or
  Parakeet via ONNX). It is the closest standalone equivalent of hyprsay's STT layer. Push to
  talk, local models, paste at cursor. No command understanding.
- **Superwhisper** (macOS) and **Willow Voice** are the same category with hybrid
  transcription. Superwhisper's **"modes"**, where the focused application selects a different
  post-processing prompt, is Talon's `app:` context idea applied to dictation, and it is the
  one feature in this group hyprsay should copy: per-application behaviour selected by what is
  in front of you.
- I found no evidence that any of the three performs in-application semantic action.
  `[not found]`

---

## 5. Linux-specific prior art

### 5.1 numen [P, partially]

`https://git.sr.ht/~geb/numen`, author `~geb`, site numenvoice.org. Vosk-based, fully local,
default model `/usr/share/vosk-models/small-en-us`, overridable with `NUMEN_MODEL`. The design
idea: you control the machine by saying **syllables and short literal words**, so the vocabulary
is tiny and phonetically distinct rather than natural. Phrase files map a spoken phrase to a
command or a synthetic input event. Explicitly aimed at people who cannot use their hands.
The sourcehut repository returned HTTP 502 during this session, so I could not read the README
directly `[partially not found]`.

Position relative to hyprsay: numen is the "no model, no network, no ambiguity" extreme. It is
fast and reliable and nothing like natural language. It has no window awareness, no app
awareness, and no notion of what is on screen. **It solves the reliability problem by deleting
the intelligence problem.** It is the honest floor of this category and it is worth respecting:
for its users it works every single time, which is not something hyprsay can say yet.

### 5.2 hyprwhspr and the Hyprland dictation cluster [P]

`https://github.com/goodroot/hyprwhspr` (upstream; many forks). Pure dictation for Wayland and
X11. Backends: Cohere Transcribe, Parakeet TDT V3, Whisper, Qwen3-ASR, ElevenLabs, or a REST or
realtime WebSocket endpoint. Injection via `wl-clipboard` plus `wtype` on Wayland,
`xclip`/`xdotool` on X11. Ships an animated microphone OSD as a layer-shell overlay on Hyprland,
Sway, niri and KDE. Claims *"nearly instant and accurate performance via in-memory models"* and
points at `onnx-asr` for *"wild CPU speeds"* on GPU-less machines. **No latency numbers given**
`[P, unquantified]`. **No command interpretation at all.**

Also in this cluster: `hyprvoice` (leonardotrapani), `hyprwhspr-rs` (better-slop, describing
itself as a "Wispr Flow alternative for hyprland"), `vocalinux`, `nerd-dictation`. All
dictation. There is an open Omarchy issue (`omacom/omarchy#2467`) asking to integrate hyprwhspr,
which tells you where the demand is concentrated.

**The competitive picture on Hyprland: several good dictation tools, zero controllers.** hyprsay
is not competing with hyprwhspr. It is in a category with no other entrant, on a platform the
incumbent just announced it is leaving.

### 5.3 Speech Note, and the Home Assistant satellite [S]

Speech Note (Nomad, Flatpak) is an offline STT/TTS/translation app, not a controller.
`OHF-Voice/linux-voice-assistant` turns a Linux box into a Home Assistant voice satellite over
the ESPHome protocol: wake word, STT, TTS, timers, LEDs. It controls *the house*, not the
desktop. Neither is prior art for what hyprsay does. `[S]`

### 5.4 oc-voice [not found]

No project by that name surfaced in any search. Either very new, very small, or the name is
slightly off. Recording as not found rather than inventing something.

---

## 6. The real state of accessibility trees on Linux, measured here today

This is owner complaint 2, "it cannot click inside web pages", and it is the section where
measurement changes the answer.

### 6.1 What I measured on this machine [M]

```
at-spi2-core 2.60.6-1, at-spi-bus-launcher and at-spi2-registryd both running
org.a11y.Status.IsEnabled            = true
org.a11y.Status.ScreenReaderEnabled  = false
Google Chrome 151.0.7922.137, launched with no flags at all
```

AT-SPI registry contents, walked with pyatspi:

```
apps on AT-SPI registry: 13
  xdg-desktop-portal-gtk  children=0
  udiskie                 children=0
  nm-applet               children=0
  blueman-applet          children=0
  waybar                  children=1
  blueman-tray            children=0
  google-chrome           children=0      <-- the entire browser, zero children
```

Five `kitty` terminal windows are open and **none of them appear on the registry at all**.

hypruse's own `ui` call on the focused Chrome window returns, verbatim:
`"chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2 exposes no accessibility tree; use
screenshot + zoom instead"`.

**Traversal cost, measured**: walking waybar's tree, fetching name and role for each node,
87 elements in 19.6 ms and 20.7 ms on two runs, **0.22 to 0.24 ms per element**. Enumerating the
13 top-level apps: 2.3 ms.

That per-element figure is the number the whole "click inside a page" plan rests on. A
400-element web page tree costs roughly **90 ms** to traverse fully. Affordable, but only if it
happens **while the key is held**, in parallel with speech, never after.

### 6.2 Why Chrome is empty, and why the usual fix no longer works [P]

Documented in `xa11y.dev/explanation/accessibility-quirks/` and, more sharply, in
`trycua/cua` issue #2915, opened 2026-08-05, titled *"a11y.rs: the Chromium ScreenReaderEnabled
premise no longer holds on current Chrome"*.

The old premise: Chromium kept its accessibility tree disabled until it detected an assistive
technology listening, via the freedesktop `ScreenReaderEnabled` property on the session bus.
**As of Chrome 151 that detection stopped working.** The reporter's controlled test: two Chrome
processes in the same session, minutes apart. Without the flag, *"1 element (bare frame)"*. With
`--force-renderer-accessibility`, *"413 elements, incl. `document web`, 44 links"*.

**This machine runs Chrome 151.0.7922.137.** The issue applies here exactly, and my measurement
above is an independent confirmation of it.

Other quirks from the same source that any implementation will hit:

- **Chromium on Linux exposes no `EditableText` interface anywhere in the tree**, so you cannot
  set a text field's value through accessibility; keyboard synthesis is the only option.
- Chromium uses non-standard action names, `"doDefault"` and `"showContextMenu"`, where GTK uses
  `"click"` and Qt uses `"Press"`. A client needs an alias table.
- **Firefox needs `MOZ_ACCESSIBILITY_ATK2=1`** in its environment.
- GTK4 `CheckButton` reports zero actions despite being toggleable; you must use the press path.
- `GtkMenuButton` and `AdwMenuButton` advertise `NActions = 0` on the outer button; the real
  click action is on an inner toggle button.
- WebKitGTK 2.52 changed which AT-SPI role it reports for an HTML `<textarea>`, breaking
  selectors keyed to the old role. Roles are not a stable contract.
- Qt: `QFormLayout` does not propagate row labels into accessible names, so fields are often
  unnamed; spin box values cannot be set reliably and must be stepped with
  `increment()`/`decrement()` and read back.

### 6.3 The honest conclusion for hyprsay [I, but grounded in the measurement above]

1. **Today, on this machine, hyprsay is structurally incapable of seeing anything inside any
   Chrome tab or any Electron app.** That is not a bug in hyprsay's code; it is Chrome 151's
   behaviour. The owner's complaint 2 is therefore correct and currently unfixable *by hyprsay
   alone*.
2. The fix exists and is one flag: `--force-renderer-accessibility`, or
   `ACCESSIBILITY_ENABLED=1` on some builds. It must be applied at browser launch, which means
   either hyprsay tells the user to add it to their `.desktop` file or launch command, or
   hyprsay launches browsers itself. The cua issue's own recommendation is *"surface the issue
   to users by having applications suggest the `--force-renderer-accessibility` flag"* and
   *"detect the condition directly: a Chromium process with exactly one AT-SPI child and no
   descendants signals the problem"*. That detection is four lines of code and hyprsay's
   `doctor` command is the obvious place for it.
3. Cost of the flag: Chromium's accessibility tree is not free. It is maintained per-frame for
   every tab. I have no measurement of that overhead on this CPU and the vendor publishes none
   `[not found]`. This needs measuring before it is recommended to users, on a machine that is
   already an i5-8350U with no GPU.
4. **Terminals expose nothing, and never will.** Five kitty windows, zero registry entries. For
   terminals the only routes are screenshot plus zoom, or a shell integration. Do not pretend
   otherwise.
5. A serious browser story probably wants a second route as well: the Chrome DevTools Protocol
   or a small extension, which gives the DOM, the real accessibility tree, and, crucially,
   *semantic* elements rather than AT-SPI's lossy projection of them. That is a bigger project
   than the flag but it is the only route that makes "click the second search result" reliable.

---

## 7. Computer-use agents, and why their design cannot be copied for voice

Source: Anthropic's own docs, `platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool` `[P]`.

The current tool is `computer_toolset_20260801`, 17 member tools: `screenshot`, `zoom`,
`left_click`, `right_click`, `middle_click`, `double_click`, `triple_click`, `left_click_drag`,
`mouse_move`, `left_mouse_down`, `left_mouse_up`, `cursor_position`, `type`, `key`, `hold_key`,
`scroll`, `wait`.

The facts that settle the question:

- **It operates on screenshots only. No accessibility tree, no DOM.** Coordinates are pixels in
  screenshot space.
- Recommended resolution 1024x768 or 1280x720; avoid above 1920x1080 "to prevent performance
  issues". Limits: 2576 px long edge, 4784 visual tokens.
- **Each screenshot costs roughly 1,000 to 1,800 input tokens.**
- Guidance says to add **about 0.5 s of delay after actions** for the UI to update, and to prune
  history to 20 images or fewer per request.

So one step of a computer-use loop is: screenshot, upload roughly 1.5k image tokens, wait for a
generative model to produce coordinates, click, wait 0.5 s, screenshot again. Even at a very
fast time-to-first-token that is comfortably over a second per step, plus real money per step.

**A voice command must resolve in well under a second or it stops feeling like control and
starts feeling like a request.** hyprsay's measured budget is 93 ms decode plus 315 ms median
Jev plus compositor animation. A screenshot loop does not fit in that budget and never will.

Benchmarks for context `[S]`: Claude 3.5 Sonnet scored 14.9 percent on OSWorld in the
screenshot-only category when computer use launched; reported figures for Sonnet 4.6 are around
72.5 percent. OSWorld itself offers agents a choice of modalities: full-resolution screenshots,
**accessibility trees**, **Set-of-Marks overlays**, or combinations. Note that last one:
Set-of-Marks, numbered badges drawn over candidate elements, is the same idea as Cursorless hats
and as hyprsay's existing numbered tie-break badges. hypruse's `marks` tool already implements
it. **The research consensus is that marks plus a tree beat raw pixels, and hyprsay already owns
both primitives.**

Google's Gemini computer use, grown from Project Mariner, is explicitly optimised for browser
workflows where DOM awareness beats generic screen scraping `[S]`. OpenAI's CUA/Operator
processes raw pixels `[P, openai.com/index/computer-using-agent/]`. I found no published
per-step latency figures for either `[not found]`.

**Conclusion: the computer-use agents are prior art for what NOT to do in a voice loop.** Their
one transferable lesson is the modality result: tree plus marks, not pixels.

---

## 8. Screen-context products: Raycast, Highlight, Rewind and Limitless

### 8.1 Raycast AI: the closest thing to what the owner is asking for [P]

Two Raycast features matter, both documented in the Raycast manual.

**Screen Awareness** (`manual.raycast.com/ai/screen-awareness`, shipped in the v0.71 macOS beta
around 2026-07/08). What it bundles from the focused window:

- frontmost app name and window title
- **"the readable text in the window, taken from the system accessibility layer"**
- the current text selection
- the focused control, cursor position and the field's value
- a screenshot of the focused window
- browser tab title, URL and page content, via a Browser Companion extension
- selected file paths when Finder is frontmost

It requires the Accessibility permission (captures fail without it) and optionally Screen
Recording. It is **read-only** and explicitly *"doesn't steal focus from the app you're in and
doesn't touch your clipboard."* No latency figures published `[not found]`.

That list is almost exactly the context payload hyprsay needs and does not yet assemble. And
Raycast's non-negotiable property, not stealing focus and not touching the clipboard, is
already hyprsay's overlay design principle.

**AI Extensions** (`manual.raycast.com/ai/ai-extensions`) is the more important one, because it
is the answer to "deep in-app ability without pre-baked routes":

- An extension **exposes its commands as tools**. Quoting: *"Once an extension exposes its
  commands as tools, you can mention it from Quick AI, AI Chat, or Root Search and the AI
  figures out which tool to call, with what arguments, and runs it for you."*
- Tools **chain in a single prompt**: their example is
  `@calendar @things any free slots tomorrow that I could use to clear overdue tasks?`, and the
  AI consults both before answering.
- There is an approval gate: *"It always asks before reading credentials such as `.env` files or
  private keys, and before any destructive action"*, and when approval is needed *"the chat
  pauses and shows a card that describes the action."*
- **Spotify is a built-in extension, and the manual's own example prompt is
  `@spotify play something mellow but not sad`.**

That last line is worth sitting with. The owner's exact example, "pick a song on Spotify", is a
solved problem in a shipping product, and the way it is solved is **not** UI automation and
**not** a hardcoded key sequence. It is a typed tool surface per application, plus a model that
picks the tool and fills the arguments. The route is not pre-baked; the *capability* is
declared, and the binding from speech to capability is computed.

**This is the single most directly applicable design in this entire lane.** It is also
architecturally compatible with Jev in a way it is not with a generative model: "which of these
N declared capabilities" and "which of these N candidate values for argument X" are exactly the
typed choice questions Jev answers, in parallel, in one round trip. Raycast needs a generative
model to emit a JSON tool call. hyprsay would not.

Note the honest gap: Raycast extensions are written by humans, one per application. The owner
said "without pre-baked routes", and a per-app extension is a kind of pre-baking. The
difference that matters is **granularity**: hyprsay's `recipes.py` bakes *sequences* ("ctrl+L,
type URL, Enter"), which break when the UI changes and cannot recombine. A capability manifest
bakes *verbs with typed arguments* ("search(query)", "play(track_uri)"), which recombine freely
and which the model composes. The first is a script; the second is an API. That distinction is
the whole design.

### 8.2 Highlight [S]

Positions itself against Raycast on "universal context awareness": auto-detecting action items
across apps, meeting transcripts and summaries across platforms. I read only its own comparison
page, which is marketing, so I have no independent architecture detail `[V]`. Not load-bearing
for this lane.

### 8.3 Rewind and Limitless: the category died [S]

Meta acquired Limitless on 2025-12-05, pulled the $99 Pendant from sale, and absorbed the team
into Reality Labs. **The Rewind app shut down on 2025-12-19**; desktop and web recording
stopped. Existing users keep a free unlimited plan through 2026; service ended entirely in the
EU, Brazil, South Korea, Israel, Turkey and the UK.

Conclusion: "record everything and let an LLM recall it" is not a live competitor, and the most
funded attempt at it was absorbed. **The lesson for hyprsay is about scope, not features:
ambient total capture was the expensive, invasive, legally fraught path, and it lost to
push-to-talk with explicit intent.** hyprsay's privacy posture is on the right side of that.

---

## 9. The Spotify question, concretely, because it is the owner's chosen example

"Pick a song on Spotify" decomposes into three problems and only one of them is hard.

1. **Know that Spotify is the target and that it is already running.** Solved. MPRIS over D-Bus.
   Measured here today: `playerctl -p chromium.instance1325064 metadata` returns
   `xesam:title = "(502) Dino Crisis Series Retrospective ... - YouTube"` and
   `mpris:length = 10711521000` `[M]`. hyprsay can already know what is playing, in which
   player, with zero latency and no model. **It does not do this.**
2. **Control transport.** Solved. `org.mpris.MediaPlayer2.Player` exposes Play, Pause, Next,
   Previous, Seek, and `OpenUri`. `OpenUri` accepts a `spotify:track:...` URI `[S, Spotify
   community threads and spotifyd docs]`. Caveat from those same threads: Spotify's Linux client
   MPRIS implementation has been historically unreliable, `OpenUri` was reported broken and
   fixed around client 1.0.37, and it *"does not autoplay unless it is a track and the client is
   paused"*. So this route works but needs verification against the current client `[not found:
   I did not test it, Spotify is not installed here]`.
   Also measured: `gdbus introspect` against the running Chromium MPRIS object returns an **empty
   node**, so **you cannot discover MPRIS capabilities by introspection**; you must attempt the
   call or consult `CanPlay`/`CanGoNext` properties `[M]`.
3. **Turn "something mellow but not sad" into a track URI.** This is the only hard part, and it
   is not a desktop problem at all. It is a search problem. Routes: the Spotify Web API search
   endpoint (OAuth, free tier), or the user's own library via a local index, or the UI.

**What this shows.** The owner's example does not actually require in-page clicking. It requires
(a) a capability manifest for the media domain, (b) MPRIS as the transport, and (c) one provider
search call. Two of the three are D-Bus calls hyprsay could make today. The pre-baked keystroke
recipe in `recipes.py` is the wrong tool for a problem that has a real API sitting right there.

**And the generalisation matters more than the example.** The same shape, "declared capabilities
plus a provider query", covers media, browser tabs, files, notifications, clipboard history and
window management. The UI-automation route (accessibility tree, clicking) is the *fallback* for
applications that offer no API, not the primary mechanism. hyprsay has been treating the
fallback as the plan.

---

## 10. An honest read of the bar

What "as good as the best of these on a Linux desktop" concretely requires. Ordered by how much
each closes the gap between what hyprsay is and what the owner wants.

1. **Parse the whole utterance, do not split it.** Talon has done multi-command utterances by
   grammar since 2019. Splitting on the literal token "and" is the single most visible sign of
   unintelligence in hyprsay today and it is the complaint the owner listed first. The fix is a
   real parser over the transcript with the command grammar as its language, not a better split
   heuristic and not a model call to arbitrate a split.
2. **Make the active vocabulary a function of desktop state.** Talon's `app:` and `title:`
   context blocks. hyprsay knows the focused window already; it does not let that knowledge
   change what commands exist. This is also the fix for complaint 3: if the grammar for "notes"
   is generated from windows that are actually open, "bring up my notes" cannot resolve to
   "launch a new one" unless nothing is open.
3. **Focus and existing windows must beat launching, always.** Complaint 3 is a policy bug, not
   an intelligence bug, and it needs no model at all. If a window of class X exists anywhere,
   the default for a bare app name is focus-and-follow, never spawn. Launching is what you do
   when the search returns nothing.
4. **Streaming recognition plus eager speculation.** Section 3. Push-to-talk makes this easier
   for hyprsay than for anyone building a conversational agent, and Jev's pricing makes the
   speculative-waste cost negligible where it is prohibitive for everyone else.
5. **A capability manifest per application, not a recipe table.** Section 8.1 and 9. Verbs with
   typed arguments that the model composes, replacing sequences that the model selects.
6. **Real in-application targets.** Section 6. Start with the flag detection and the `doctor`
   warning, because right now the honest answer to "why can it not click in my page" is "your
   browser is not exposing anything, and here is the one-line fix". Then always-on marks on
   addressable elements, in the Cursorless and Set-of-Marks tradition, resolved in a tree, never
   from pixels.
7. **A correction loop.** "no, the other one", "undo that". This is what lets the system guess
   more aggressively without becoming dangerous, and it is cheap.

And the thing none of them have that hyprsay must keep: **the safety model**. Talon shipped
speaker verification only two days ago; nobody else in this lane has a tier system, a
corroboration requirement, or a rule that a window title can never authorise an action. That is
genuinely ahead of the field and it is the reason hyprsay can afford to get more aggressive
about acting early, because the blast radius is already bounded.

## 11. What is genuinely novel about the Jev-based approach

Being careful here, because Jev is one week old (launched 2026-09-15) and most writing about it
is not primary.

What I could establish `[S, multiple independent outlets including Tom's Hardware, plus Vercel's
own adoption statement]`: Jev is the first of a class TypeSafe AI calls "System One Models".
You supply program state plus typed questions and it answers **all of them in one parallel
pass**, in roughly 70 to 500 ms, returning typed values with **calibrated confidence**, at
$0.042 per million input tokens with output free. It gives up free-form generation in exchange
for parallel sampling, schema conformance, and a probability on every answer. Vercel said it
became the fastest-adopted model in AI Gateway history, with nearly 13 percent of paid teams
using it within 24 hours; the waitlist was removed on 2026-09-20.

Three things follow that no system in this lane can do:

1. **Speculation is nearly free.** Deepgram's eager end-of-turn costs 50 to 70 percent more LLM
   calls, which is why it is a premium feature everywhere else. Against Jev's pricing and
   parallelism that cost rounds to nothing. hyprsay can afford to re-ask the entire question set
   on every stable prefix of the utterance, throw away almost all of it, and still be cheaper
   than one generative call.
2. **Calibrated confidence is a safety primitive, not a metric.** Every other system in this
   lane decides by parsing a model's text output, which gives no number to threshold. hyprsay
   already gates on margins in `config.Gates`. Tie that to tiers and to speculative commitment
   and you get something no competitor has: *a principled rule for how sure the system must be
   before it acts early, that differs by how reversible the action is.*
3. **Parallel typed questions match the structure of desktop intent exactly.** "Which verb",
   "which of your open windows", "which installed app", "does this 'and' separate two commands",
   "which of these 44 links" are all choices over enumerated candidates that code built from
   real state. A generative model has to serialise them into one JSON object and one
   autoregressive pass. Jev answers them simultaneously. **That is the architecture hyprsay
   should lean into, and it is the reason the Raycast AI Extensions design is a better fit for
   hyprsay than it is for Raycast.**

The caveat the README already records and that must not be lost: Jev is not deterministic;
thirty identical requests on a near-tie flipped four times `[M, hyprsay's own measurement]`.
Speculation amplifies that exposure, because you are asking more often. The mitigation is the
one already in the codebase, two independent formulations that must agree, plus the Voice-Light
invalidation rule so that a changed prefix throws the answer away rather than averaging it.

## 12. Not found, explicitly

- Any primary, numerical latency figure for Talon command recognition. The circulating
  "sub-10 ms" traces only to an SEO page.
- Any vendor-confirmed Aqua Voice latency number; aquavoice.com was not reachable in this
  session.
- The numen README; `git.sr.ht/~geb/numen` returned HTTP 502.
- Any project called `oc-voice`.
- Per-step latency figures for OpenAI Operator/CUA or Google Gemini computer use.
- The measured CPU and memory cost of running Chrome with `--force-renderer-accessibility` on a
  GPU-less i5-8350U. Nobody publishes this and I did not measure it, because it requires
  restarting the owner's browser.
- Any viral 2026 demo of a desktop voice agent acting mid-utterance that I could trace to a
  named author or a real video. The *technique* is well documented (Deepgram Flux, Voice-Light,
  the Amazon patent); the specific demo the owner remembers, I could not identify. If they can
  name it, that is worth five minutes.
- Any project, on any platform, using Jev for desktop or voice control. Jev is seven days old.

## Verification

Skeptic pass on lane "competition", 2026-09-22. Method: for each claim I used a *different*
source or a *different* tool than the researcher. Local re-measurements use
`gi.repository.Atspi` directly, not `pyatspi`. Script:
`/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/vision/skeptic_atspi.py`.

Headline: two claims refuted, three overstated, three confirmed. The single most consequential
error is the claim that no Jev project exists. One does, with 16,938 stars, and it is the exact
mechanism this lane needed.

---

### V1. "I found no project on any platform using Jev for desktop or voice control": **REFUTED**

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

### V2. "AT-SPI costs 0.22 to 0.24 ms per element, so a 400 element page costs about 90 ms": **REFUTED**

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

### V3. Chrome 151 needs `--force-renderer-accessibility`: **CONFIRMED, with a correction to the detector**

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

### V4. Deepgram Flux eager end-of-turn: **CONFIRMED on mechanism, OVERSTATED on numbers**

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

### V5. Voice-Light arXiv 2609.20995: **CONFIRMED on the rules, OVERSTATED on the result**

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

### V6. Jev's own specification: **CONFIRMED, with two corrections**

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

### V7. MPRIS "capabilities cannot be discovered by introspection": **OVERSTATED**

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

### V8. Talon 1.0: **CONFIRMED**

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

### V9. Raycast: **CONFIRMED on mechanism, OVERSTATED on two details**

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
