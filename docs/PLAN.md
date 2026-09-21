# hyprsay: build plan (v2)

Voice control for Hyprland. You hold a key, say what you want, release, and it happens.
It acts; it does not talk back. Decisions that need understanding are made by Jev
(`typesafe-ai/jev`, TypeSafe AI's System One model, through the Vercel AI Gateway).
Everything else is deterministic code.

hyprsay is a fork of [hypruse](https://github.com/IlyasKhallouki/hypruse) and reuses its
Hyprland control core. This is the plan of record. v1 of this document was reviewed by four
adversarial reviewers on 2026-09-21; they found four blockers, all in safety, and all four
were confirmed against source. Section 14 records what changed and why.

Evidence labels:

- **[M]** measured on the target machine, raw output saved under `docs/research/live/`
- **[O]** observed once on the target machine, raw output **not** saved; treat as a lead
- **[R]** measured on the target machine by a research agent; raw output not kept in this repo
- **[V]** confirmed by an independent checker agent (see the report's `## Verification`)
- **[S]** read from source code, with file and line
- **[D]** documented by a primary source
- **[A]** assumption; section 11 names the task that replaces it

A latency, accuracy or threshold figure without a label is a bug in this document. Plain
design parameters (a 4 s hint timeout, a 200 character cap) are choices and carry no label.

---

## 1. What the research established

The brief called Jev "deterministic" and "hyperfast". Neither survived measurement.

| Finding | Evidence | Consequence |
|---|---|---|
| A Jev round trip is about 315 ms p50 for requests up to about 2.2k tokens (n=30 per series, warm HTTP/2). About 220 ms of it is the gateway's call to TypeSafe (whose API resolves to AWS us-west-2, per the wire report [R]). The vendor's 111 ms does not hold from here. p90 of single small requests fell between 356 and 439 ms across series. | [M] | Jev cannot sit on the path of common commands. |
| Question count is free: 1, 5, 15, 40 questions cost 323, 322, 312, 317 ms p50. | [M] | Ask everything at once. Slice by state, never by question. |
| Payload is not free: 448 ms p50 at 6k tokens, 544 ms at 11.5k. HTTP 503: 1 of 30 at 6k; 13 of 30 then 4 of 25 at 11.5k; small requests failed about 2 times in 400. | [M] | Cap each request near 1.8k tokens. |
| Three concurrent small requests on one connection: 359 ms p50 wall (n=12 rounds of identical 600-token bodies; failed rounds excluded). p90 for real request bodies is unknown. | [M] for p50, [A] beyond | Concurrency is cheap. The real fan-out must be measured in P0 before the budget is trusted. |
| Not deterministic. 30 identical requests on a near-tie: P(top) 0.45 to 0.67, and the pick flipped 4 of 30 times. An answer at P=1.00 never moved; an answer near 0.75 ranged 0.68 to 0.83 across 9 runs (option order was also shuffled in that test). | [M] | Gate on margin and agreement, not on absolute probability. Thin margin means hints. |
| In one test, the same utterance and state went from 0 of 12 correct to 12 of 12 when options were described as `{app, kind, title}` and the instruction was neutral. That test used a different state from the determinism run, so how much wording alone contributes is unknown. | [O] | A strong lead, not a result. P0 reruns it with state held constant on a labelled set. |
| A catch-all option beside real ones absorbed about half the mass: with Firefox focused, "close the terminal" chose `focused` (the wrong window) in 2 of 5 runs. | [O] | Options must be mutually exclusive. Deixis is a separate boolean. |
| Eight identical questions inside one request disagree with each other (median spread 0.08), but averaging them cut stdev only 0.059 to 0.040 (n=19, indicative). Noise is partly shared per request. Averaging **across** requests was never tested. | [M] | In-request ensembling is weak. Cross-request ensembling is a P0 experiment. |
| Tokens are predictable: `250 + 0.424*state_chars + 0.339*question_chars - 11.7*n_questions`, worst error 2.9 percent over 8 requests. | [M] | The client refuses oversized requests before sending. |
| Streaming recognition is not viable on this CPU (i5-8350U, no GPU). One-shot decode with Moonshine Tiny: median 120 ms (researcher) and 166 ms (checker, range 123 to 372) for clips of 0.85 to 1.56 s, real-time factor about 0.12 to 0.17, on a machine loaded to 16 to 27. Parakeet 110M slowed 3.2x under load. | [R][V], 8 synthetic clips | Buffer and decode once. Decode time scales with utterance length. Rerun idle with a real microphone. |
| Best independent figure for a gateway speech model is 458 ms median, measured direct to provider. No live gateway speech call was made. | [V] desk research | Speech to text stays local. |
| `hyprctl` as a subprocess costs 6 to 22 ms; the same request over Hyprland's socket 0.1 to 0.3 ms. | [R][V] | Replace hypruse's transport with a raw socket client. |
| Hyprland does not gate IPC dispatch on the session lock. Ordinary binds are skipped while locked (`KeybindManager.cpp:649`), so a release bind never fires if the lock lands mid-hold. socket2 has no session-lock event. `hyprland_lock_notifier_v1` is registered. | [S] Hyprland 0.56.2 | The lock needs a pushed latch, checked repeatedly, and a maximum hold. |
| All 22 trust-guard call sites, and `launch`, `use_bind`, `keyboard`, `hypr`, live in hypruse's `server.py`. `input.py` calls no guard. `guard_password_field` fails open by design. | [S] fork source | "Drop the MCP surface" must not mean dropping `server.py`. |
| Hyprland 0.57 removes `.conf` support; under the Lua provider legacy dispatch strings fail. No Lua string has ever been executed by this project. | [S] | Either prove the Lua path in a nested session or cut it from v1 honestly. |
| Window and workspace animations on this config last about 500 to 600 ms. | [R], and the checker explicitly did not re-verify it | The largest visible delay. Opt-in snappy profile; never edit user config unasked. |

## 2. Goals and non-goals

Goals for v1:

1. Hold-to-talk control of windows, workspaces, apps, layout, volume and media, with semantic
   references ("the browser", "the music") resolved against the live desktop.
2. Speed, stated honestly: recognition costs about 0.15 times the utterance length [R][V], so a
   one-second command decodes in roughly 150 ms and a three-second one in roughly 450 ms.
   Speculative decoding during the key hold (5.2) is designed to hide most of that. Jev adds
   about 360 ms p50 when it is needed [M]. Targets are set in P0 from an idle-machine run.
3. Wrong actions are rare and cheap. Nothing destructive happens on a model's say-so, and a
   hostile window title cannot steer an action.
4. A HUD that looks native to Hyprland and never takes focus or eats clicks.
5. An eval harness that turns question wording and thresholds into measured quantities.

Non-goals for v1: spoken replies; always-listening or wake word; long dictation; clicking
controls inside apps (needs AT-SPI, broken on the development machine, and `a11y.py` ported off
`busctl`); compound commands ("open X and move it to three"); a compositor plugin.

## 3. Principles

1. **Code calculates, Jev judges.** Numbers, workspace ids and dictated text are parsed by
   code. Jev only chooses among candidates that code built from the live desktop.
2. **Jev proposes, the tier function disposes.** What an action may do is decided in code.
3. **Never hang, never guess at risk.** Every Jev call has a deadline; tier 1 and above fall to
   hints on doubt; tier 0 may act and offer a swap because it is free to reverse.
4. **The attacker controls window titles. Nothing they control may authorize anything.**
5. **The easy 80 percent never touches the network.**
6. **Questions are code**: one reviewed file, a version, regression tests on decisions.
7. **Everything the cloud sees is inspectable**, and by default it sees no window titles.

## 4. Architecture

Two processes, because their dependencies cannot share an interpreter: PyGObject exists only
as a system package, sherpa-onnx only as a pip wheel [V].

```
                 Hyprland  (.socket.sock requests, .socket2.sock events, Wayland protocols)
                    ^   |                                   |
     dispatch/query |   | events                            | hyprland_global_shortcuts_v1 (PTT)
                    |   v                                   v hyprland_lock_notifier_v1 (lock latch)
+-------------------------------------------------------------- engine (venv, asyncio) ------+
|  world      socket2 reader -> DesktopState -> lexicon (trusted / untrusted fields)           |
|  lock       process-wide latch: notifier OR j/locked OR lock layer OR configured locker     |
|  activation PTT down/up, max hold, fresh pair per utterance                                 |
|  audio      capture -> ring buffer -> energy gate -> speculative snapshot                   |
|  stt        Recognizer protocol: sherpa-onnx OfflineRecognizer (Moonshine Tiny), 1 thread   |
|  nlu        normalize -> grammar fast path -> Jev fan-out -> agree + corroborate -> tier     |
|  jev        httpx HTTP/2 client: prewarm, deadline, retry, token cap, cassettes  [built]    |
|  exec       one worker: lock re-check, pinned address, render, dispatch, confirm, inverse   |
|  journal    JSONL, dictated text stored as length + hash                                    |
+------------------------------------------+-------------------------------------------------+
                                           | NDJSON, Unix socket 0600 in a 0700 dir, SO_PEERCRED
                          +----------------v-----------------+
                          | hud (/usr/bin/python3, GTK4 +    |
                          | gtk4-layer-shell, stdlib only)   |
                          +----------------------------------+
```

Runtime directory `$XDG_RUNTIME_DIR/hyprsay` (0700), separate from hypruse so the fork never
shares beacon or lock files with the user's running hypruse MCP servers [V]. Acting calls run
on one worker thread because hypruse's state is process-global [V].

### 4.1 Packaging and lifecycle (decided in P1, before any audio code)

- `hyprsay setup` builds the engine venv under `~/.local/share/hyprsay/venv`, downloads the
  speech model with pinned checksums and visible progress, and prints the two Hyprland bind
  lines in the dialect `j/status` reports. Distro dependencies (python-gobject, gtk4-layer-shell,
  portaudio) are listed by `hyprsay doctor`, not installed silently.
- Two systemd user units, `hyprsay-engine` and `hyprsay-hud` (`Requires`/`After` engine, both
  `PartOf=graphical-session.target`). The HUD is plain files run by `/usr/bin/python3`.
- The HUD's first message is `{t: hello, proto: N}`; a mismatch is refused and reported by
  `doctor`. HUD logic that needs no `gi` (protocol parsing, state machine) is unit tested.
- Config: `~/.config/hyprsay/config.toml`, read with stdlib `tomllib`, validated at start;
  environment variables override the file. Logs go to journald through the unit. The journal has
  a size cap and rotation because it records utterances.
- If the engine cannot start (no key, no model, no audio device) the HUD says so in its
  `refused` state; nothing fails silently.

### 4.2 Repository layout

```
src/hyprsay/
  core/        extracted from hypruse (section 8): hyprctl (socket transport), events, wire,
               input, safety, trust, session, journal, a11y, and ops.py holding the guarded
               tool functions lifted out of server.py
  world.py  lexicon.py  lock.py  activation.py  audio.py
  stt/         Recognizer protocol, sherpa backend, ASR surface normalizer
  nlu/         normalize.py grammar.py bank.py requests.py resolve.py tiers.py
  jev/         client.py types.py tokens.py [built], cassette.py, fake.py
  ops/         operation table: renderers, tier function, target needs, inverse
  exec.py  hudproto.py  daemon.py  cli.py (run|setup|status|inspect|doctor|eval|record|teach)
hud/           the GTK process; imports nothing from src/
evals/         labelled utterances, adversarial titles, ambient negatives, cassettes, reports
docs/          PLAN.md, research/, ARCHITECTURE.md, PRIVACY.md
packaging/     systemd user units, AUR recipe, example binds in both dialects
```

## 5. The pipeline

### 5.1 Activation

Default **transport B**: the engine registers `hyprsay:ptt` through
`hyprland_global_shortcuts_manager_v1` (advertised by the live compositor [O]; hypruse's
`wire.py` already binds registry globals) and the user adds `bind = SUPER, V, global, hyprsay:ptt`.
Press and release arrive in-process: no process spawn (a Python cold start is 95 to 240 ms [V])
and nothing another local client can forge through socket2.

Fallback transport A: `bind ... event, hyprsay:down` and `bindr ... event, hyprsay:up`, read
from socket2 as `custom>>`. Zero dependencies, with two documented weaknesses: any client that
can reach `.socket.sock` can emit the event, and the release bind never fires if the session
locks mid-hold [S]. P0 exercises both live; `bindr` has never been run on this machine.

Both transports enforce a maximum hold (default 15 s) and require a fresh down and up pair per
utterance. Key down prewarms the Jev connection with one unauthenticated GET. Measured on new
connections, interleaved, n=10 each: with no prewarm the first evaluation takes 526 ms p50
(399 to 832); after the GET it takes 325 ms (259 to 605), the same as steady state; adding a
small authenticated call on top buys nothing (338 ms) [M]. The GET itself takes about 250 ms,
less than anyone spends speaking, so no keepalive pings are needed. What is still unknown is
how long an idle connection survives (section 11).

### 5.2 Capture and speech to text

- `sounddevice`, 16 kHz mono. Default: open the stream on key down. P0 measures first-word
  clipping; if it clips, fall back to an always-open stream with a 400 ms pre-roll, which costs a
  lit mic indicator and Bluetooth HFP degradation. `audio.device` is configurable; the stream is
  reopened on a PortAudio error and on source hot-plug.
- **Speculative finalize.** While the key is held, an energy gate (RMS over 20 ms frames, no
  model) watches the ring buffer. On 150 ms of trailing silence it snapshots the PCM, decodes it,
  shows the transcript in the pill, runs normalize and grammar, and on NO MATCH fires the Jev
  fan-out. On key up, if no voiced frame followed the snapshot, the hypothesis stands and its
  answers are reused. People release a push-to-talk key some time after their last word; P0
  measures that lag, which is what this hides decode and part of Jev behind.
- Decode once per snapshot: sherpa-onnx `OfflineRecognizer`, one thread. Default Moonshine Tiny
  (MIT, 182 MB resident [R][V]); Parakeet TDT 110M (CC-BY-4.0) selectable.
- Optional sink ducking while the key is held. No echo cancellation in v1.

### 5.3 Normalize

Lowercase; strip fillers; number words to digits with slot-aware homophone repair; split-word
repair from the lexicon; ASR surface cleanup ("Workspace 3." and "full screen" were real
Moonshine outputs [V]); user aliases. Emits up to three variants by substituting lexicon entries
whose phonetic key is within distance one of a transcript token ("kiddy" to "kitty"), so Jev can
arbitrate mishearings that code proposed. NFKC-normalize and strip format and control code
points from every desktop-derived string before it is matched or serialized.

### 5.4 Fast path: grammar over a live lexicon

60 to 120 patterns with typed captures and a greedy `<text>` tail legal only after a carrier
verb. Entity captures resolve against **trusted** lexicon fields only. Outcomes:

- **EXACT**: one parse, one candidate. Go to the tier function.
- **Several candidates of one app, residual words left over** ("the firefox with youtube"): if
  the residual words overlap the title tokens of exactly one candidate, that is EXACT with no
  network. Titles may break ties among candidates already selected by a trusted field; they
  never select on their own. If they overlap none or several: Jev arbitration over those k.
- **Several candidates, no residual words** ("focus kitty", four kitty windows): hints. No
  model can know.
- **NO MATCH**: semantic path.

### 5.5 Semantic path: how Jev is used

Concurrent small requests on one warm HTTP/2 connection, each under the token cap. State is
always `{utterance, variants, note}` where `note` says the utterance is a speech transcript and
words may be misheard.

- **R1, the utterance.** `addressed` (boolean), `intent` (choice with `none`), `names_window`
  and `names_app` (booleans; "this / it" is `names_window = false`, combined in code with the
  focused window), `amount` (score), `direction` (choice), `reference_specificity` (score: names
  an app, describes a kind, points, says nothing), `unsupported_kind` (choice: click inside app,
  long dictation, screenshot, brightness, file operation, question needing an answer, none).
- **R2, windows, relative.** One choice over live windows described as
  `{app, kind, workspace, recency}`. **No titles.** No catch-all option.
- **R2b, windows, absolute.** One boolean per live window (capped at 20): "Is the speaker
  referring to this window?" A choice always crowns a winner even when the right window does not
  exist; the booleans supply the missing absolute signal. When every boolean is low, the answer
  is "no such window", and the HUD offers to launch the app instead.
- **R2t, titles, only when needed.** Code knows before any call whether two or more candidates
  share the selected app and the utterance has residual words. Only then does a request carry
  titles, only for those candidates, truncated to 60 characters. It fires concurrently with the
  others, so it costs no extra round trip.
- **R3, apps.** Installed apps as `{name, kind}`. With rich descriptions 83 apps estimate at
  about 3.3k tokens [A, from the fitted estimator], over the cap, so R3 is sharded by category
  into parallel requests, each with a `none` option. P0 measures tokens and accuracy.

Rules: options are mutually exclusive. Exit, power and force-kill are never offered to Jev.
Dictated text is never sent: typing is reached only through the carrier-phrase grammar, so the
words you dictate never leave the machine. Command confidence is the minimum over the answers
the chosen intent reads. Service `confidence` is logged only; it cannot be recomputed for Score
(the client formula fails on all 7 live Score answers [M]).

**Agreement.** R2's argmax and R2b's highest boolean are two independent formulations of one
decision, bought in the same round trip. Agreement is the primary confidence signal;
disagreement caps the command at hints. If P0 shows noise is independent across requests, the
R2 choice is sent twice from t=0 and averaged, which also replaces the hedge.

**Transport** (built, `src/hyprsay/jev/`): hard deadline, one retry on 503 or a dropped
connection, no backoff, 4xx fails at once, oversized requests refused before sending. The hedge
point and the deadline are [A] until P0 measures real request bodies with a proper percentile
estimator on persisted samples. A hedge at 450 ms may fire on more than a tenth of calls and
doubles load on a service that already returns 503, so it stays off until that data exists.

**Late answers.** Under push to talk, staleness is observable. At the deadline the pill shows
"still thinking" and the request stays alive until the first of: the key goes down again, a
focus or workspace event arrives, or 2.5 s pass. A late answer is valid for tiers 0 and 1.
Tier 2 never acts late.

**Offline, 503 after retry, or zero balance**: the local fuzzy and phonetic scorer's top three
candidates are shown as numbered hints for the grammar's best-matching intent. The semantic path
degrades to "pick a number"; it does not vanish.

**Cache.** Only decisions where both formulations agree with a wide margin are memoized, keyed
by bank version, for 7 days. Near-ties are never cached: that freezes a coin flip. The gateway
hides the model version, so drift cannot be detected; a weekly sample is re-verified. All cache
thresholds are [A].

### 5.6 Resolve, corroborate, authorize

**Trusted and untrusted anchors.** Trusted: `Name`, `GenericName`, `Categories` (kind) and
`Keywords` from `.desktop` files in root-owned system directories; user aliases; workspace
names. Untrusted: window title, `initialTitle`, and any `.desktop` file in a user-writable
directory (including Flatpak exports) until the user approves it by hash.

**Corroboration** means the chosen entity is anchored in the transcript by a **trusted** field.
A hostile page can set its title to anything, including every command word; it cannot change
what `/usr/share/applications/firefox.desktop` says, and it cannot make the user say its name.
Title tokens may only break a tie among candidates already corroborated, must be discriminating
(absent from every other candidate and from a stoplist of command words and kinds), and never
corroborate a typing target or a tier 2 target. When two candidates share the corroborating
kind and nothing trusted separates them, corroboration fails and hints are shown. Mixed-script
tokens are rejected; phonetic matching runs only against trusted names. Learned patterns never
key on title text.

**Tiers are a function of (operation, target, context), not a constant.** Initial policy, all
thresholds [A] until the P3 reliability diagram:

| Tier | Examples | Rule |
|---|---|---|
| 0 free to reverse | focus, switch workspace | corroborated, R2 and R2b agree: **act, then show swap badges** on the top three for 3 s; a number re-targets and the pick is journalled as a correction. Not corroborated: hints. |
| 1 reversible | launch, move on the same monitor, resize, float, fullscreen, volume | corroborated, formulations agree, wide margin; otherwise blocking hints |
| 2 disruptive | close window, move to another monitor or a special workspace, typing into a non-allowlisted app | never on Jev alone: the verb literally in the transcript, an explicit or deictic target, then a 1.5 s cancellable countdown. Never acts late. |
| 3 session | exit, power, force kill, lock | grammar only. Confirmed by a **physical key event through transport B**, never by a spoken "confirm", which shares the channel it would authenticate. |

**Typing** is the dangerous operation and gets its own rules. Text comes only from the
carrier-phrase grammar. All C0 and C1 control characters and Unicode line and paragraph
separators are stripped, not just Enter; length is capped at 200. The target is pinned to the
window address focused at key **down**; if the focused address or class differs at dispatch,
typing is refused visibly. It is refused by default when the focused class is a terminal, when a
launcher or any keyboard-grabbing layer is open, when the class is in hypruse's `_AUTH_CLASSES`,
or when the kind is unknown; elsewhere it is tier 2 until the user allowlists the app. A launch
suppresses typing until the pinned address is re-confirmed.

**Launch** starts applications by desktop-file ID through a process spawn with an argument
vector, never a shell line built from speech. `doctor` lists entries from user-writable
directories; each needs one approval pinned to the file's hash, and a changed hash or a new entry
shadowing a system ID revokes it with a HUD notice.

**Binds.** An opted-in bind is stored as `(combo, dispatcher, argument)` and refused if the
live bind differs. Its tier comes from its dispatcher: `exec`, `exit`, `kill` and relatives are
tier 3. Every speech-derived or desktop-derived string passes through `lua_str` before it
reaches the Lua evaluator or the batch parser.

### 5.7 The lock

A process-wide latch, set by any of: `hyprland_lock_notifier_v1` (pushed), `j/locked`, a
lock-kind layer surface (hypruse already classifies hyprlock, swaylock and lockscreen
namespaces), a user-configured locker class or namespace for Quickshell lockers that do not use
ext-session-lock, and logind `LockedHint`. An error reading any source means locked. On the
locked edge the engine drops the in-flight utterance, cancels any countdown, closes the audio
stream and zeroes the ring buffer. The latch is checked at key down, at key up, after Jev
returns, at countdown expiry, and immediately before the write; the worker re-checks after
effect confirmation and applies the inverse if the lock flipped in between.

### 5.8 Execute

One worker: lock check, tier function re-evaluated against a fresh focused-window read, render
in the active dialect, target `address:0x...` always, treat `ok` as accepted and confirm from
the matching socket2 event. Each operation declares its inverse; the last action's inverse backs
a grammar-only "undo" and "again" from P3, which is what makes act-then-swap safe.

## 6. The HUD

Separate resident process, Python + GTK4 + gtk4-layer-shell on the system interpreter; the
toolkit combination was run live on this session [V]. Load `libgtk4-layer-shell.so` before
`import gi`; refuse to start if layer-shell is unsupported.

- Surfaces `hyprsay-hud` (a pill, bottom center by default) and `hyprsay-hints` (full monitor,
  mapped only for badges). Overlay layer, keyboard mode NONE, exclusive zone -1, empty input
  region re-applied on every map. Display-only: nothing on it can be clicked.
- It reads the user's look in one `[[BATCH]]` getoption call (border gradient, rounding, gaps,
  bezier) and re-reads on `configreloaded`. Layer rules are installed at runtime in the detected
  dialect. Animation is drawn in-surface because rules cannot set speed.
- States: hidden, hearing (level meter, then the speculative transcript), thinking, still
  thinking, heard + action chip, swap badges, hints, countdown, refused (locked, offline, no
  key, no model), not understood.
- **Not understood is never a dead end.** When `intent` lands on `none` or under the gate, the
  pill shows the top non-none intents with p >= 0.15 as "did you mean", each with an example
  phrase from its grammar pattern. When `unsupported_kind` fires, it says so ("clicking inside
  apps is not supported yet"). "What can I say" opens an overlay of intents with examples and
  the user's opted-in binds.
- The target window is flashed with a compositor `setprop` border color.
- Frame time and multi-monitor, fractional scale, blur cost and scanout under fullscreen video
  are [A]; P4 measures them.
- The NDJSON protocol is frontend-neutral so a Quickshell frontend can follow for Omarchy 4.

## 7. Privacy

- **By default no window title leaves the machine.** R1 carries the utterance; R2 and R2b
  carry `{app, kind, workspace}`; R3 carries installed app names and kinds. Titles go only in
  R2t, only for the two or more candidates being told apart, never for typing or tier 2 targets.
  Title redaction is by pattern plus class plus kind (private and incognito suffixes, "vault",
  "password", terminal classes), because class alone does not identify sensitive content.
- **Dictated text never leaves the machine** and is journalled as length plus hash.
- The utterance itself is sensitive and is sent on every semantic command. `inspect --last`
  prints exactly what went out. `providerOptions.gateway.zeroDataRetention` is requested once P0
  confirms route B accepts it; the gateway's own catalog lists Jev as `zdr: all`, `no_training: all` [D].
- Audio never leaves the machine in v1. The key is read from `AI_GATEWAY_API_KEY` or
  `~/.config/hyprsay/ai-gateway.key` (0600) and never reaches a log, a repr or an exception.
- Voice is an unauthenticated channel. Push to talk is the main mitigation; tiers, the trusted
  anchor rule and the physical-key confirm are the rest. `docs/PRIVACY.md` is written in P0 with
  the harness, stating what is sent and where (Vercel edge, then TypeSafe in AWS us-west-2).

## 8. Fork surgery on hypruse

This is an **extraction**, not a deletion. hypruse's `server.py` holds every guarded operation
and all 22 guard call sites [S]; only its FastMCP registration is MCP-specific.

- **Lifted out of `server.py` into `core/ops.py`, guards intact:** `hypr`, `launch` and
  `_launch_and_wait`, `use_bind`, close window, the guarded `keyboard` path, `wait_for`.
  `exec.py` calls these; it does not reimplement them.
- **Kept:** `hyprctl`, `events`, `wire`, `input`, `safety` (the kill switch, under the new
  runtime dir), `trust`, `session`, `journal`, `a11y` (`trust.py` imports it at module level
  [S]), the Lua provider probe and `lua_str`.
- **Changed:** `hyprctl._run` becomes a raw socket client; `trust.session_locked` becomes the
  lock latch; READONLY and CLIPBOARD gates are enforced for library callers; `HYPRUSE_*` renamed.
- **Dropped:** FastMCP app building, the agent skill, `cli_state`, screenshot, zoom and marks
  image paths, `HYPRUSE_SEAT` and `VirtualKeyboard` (dead code that forces a virtual pointer
  [V]), the PyPI release workflow, the Waybar beacon.
- **Test salvage.** About half the suite (18 of 35 files) imports `server` [S], and
  `conftest.py` patches it in an autouse fixture. P1 deletes the tests of dropped surfaces
  (mcp_roundtrip, skill, marks, zoom, screenshot, waybar, cli_state) and **ports** the ones that
  cover safety behaviour (trust, launch, use_bind, close_window, keyboard_target, dryrun) to the
  extracted functions. `test_hyprctl.py` already patches at `_run` and survives the transport
  swap unchanged [V].

`upstream` stays a fetch-only remote so hypruse fixes can be merged.

## 9. Eval harness

Started in P0, because most of section 5 is assumption until it exists.

- `evals/utterances.jsonl`: at least 150 canonical, 100 paraphrase, 50 out-of-domain, each with
  a desktop fixture and an expected decision (an action, `hints`, or `none`).
- `evals/adversarial.jsonl`, a **P3 exit gate at zero wrong actions**: command-word stuffing in
  a title; a hostile window of the same kind as the target; titles echoing likely phrases;
  homoglyph and zero-width tricks; an attempt to poison learning; titles in state versus in criteria.
- A real microphone corpus from `hyprsay record`, in the owner's own voice. Accent against an
  English-only recognizer and an English-first model is an untested go / no-go.
- Every per-call sample is persisted. Percentiles use a proper estimator. Each case runs N
  times because the model is noisy; a case passes on decisions, never on exact probabilities.
- Metrics: action accuracy, **wrong-action rate**, hints rate, swap rate, false accepts per
  ambient hour, latency per stage. Cassettes make the pipeline replayable offline and in CI.
- `hyprsay eval --bank A --bank B` compares two question designs on the same set with state
  held constant, which the first wording experiment failed to do.

## 10. Latency budget (key release to dispatch written)

| Stage | Value | Basis |
|---|---|---|
| PTT up to engine | under 1 ms | [A] in-process protocol event |
| Capture flush | about 40 ms | [A] from the speech report's budget |
| Decode, 1 to 1.5 s command | median 120 to 170 ms, up to 372 observed | [R][V] loaded machine |
| Decode, 3 s utterance | about 360 to 510 ms | [A] from real-time factor 0.12 to 0.17 |
| Normalize, grammar, fuzzy | about 2 ms | [R] for the fuzzy scorer (1.4 ms over 86 apps, from an unverified lane), [A] for the rest |
| Jev fan-out | about 360 ms p50; p90 unknown, likely 450 to 600 ms | [M] p50 on synthetic small bodies; [A] otherwise |
| Lock latch read | under 0.1 ms | [R] for `j/locked` (0.09 ms) |
| Dispatch over the socket | under 1 ms | [A]; no lane has sent a dispatch over the raw socket |
| **Fast path, 1.2 s command** | **about 200 to 260 ms** | sum |
| **Jev path, 1.2 s command** | **about 560 to 620 ms p50** | sum |

With speculative finalize, decode and the start of Jev overlap the speaker's key-release lag;
how much that saves depends on a human measurement P0 has not made. The pill changes state on
key release within one frame. After dispatch the compositor's own animation adds roughly half a
second on the current config.

## 11. Phases

Each phase ends demoable, with green tests, merged to `main`.

**P0. Ground truth.** Every row replaces an assumption; this table is the exit criterion.

| Assumption | Task | Decision rule |
|---|---|---|
| PTT transport | both transports live, including `bindr` and a lock mid-hold | B is default if press and release arrive reliably |
| Lua provider | run a **nested Hyprland** (separate instance signature, throwaway Lua config; does not touch the live session) and execute every ops-table string | works: it becomes the local integration target for both dialects. Fails: v1 supports hyprlang only, detects Lua through `j/status`, and refuses with a clear message |
| Raw socket dispatch | first real dispatch; stale address; `locked`, malformed and connection-error replies each block | all four behave as specified |
| Recognizer | `hyprsay record`, a corpus of 150+ commands in the owner's voice, bake-off on an **idle** machine; decode time versus utterance length | Moonshine Tiny command accuracy after normalization above a bar set with the owner, else switch model or stop |
| Release lag, first-word clipping | measured on the corpus | sets the speculative trigger and the pre-roll default |
| Question design | rerun the wording experiment persisted, state constant, 100+ labelled utterances; R2 versus R2b agreement; R3 shards with rich descriptions (tokens and accuracy) | chosen bank is the one with the lowest wrong-action rate |
| Noise structure | the same R2 question in N concurrent requests, 30+ rounds | independent: dual-send and average. Correlated: keep single send |
| Tail and failures | several hundred calls with real R1, R2, R2b, R3 bodies, concurrent, failures counted; 503 versus tokens in 1k steps with one retry | sets the deadline and decides whether a hedge exists at all |
| Injection | adversarial title set against the corroboration rule | zero wrong actions, or the rule is redesigned |
| Connection | HTTP/2 idle timeout after 30 s to 15 min and after suspend | confirms that prewarm on key down is sufficient |
| Privacy | ZDR accepted on route B; model id variants; `PRIVACY.md` | written |

**P1. Core.** Packaging and lifecycle (4.1); the extraction and test salvage (section 8);
socket transport; lock latch; `world.py`; ops table with tier functions and inverses; executor
with pinned address and re-checks; config and journald logging. Exit: `hyprsay do "<op>"`
performs every v1 operation, and tests prove that a locked latch, a malformed `j/locked` reply
and a connection error each block dispatch.

**P2. Hearing, and a minimal HUD.** Activation with max hold; capture; energy gate and
speculative finalize; recognizer; normalizer with variants; `hyprsay setup`; a basic `doctor`.
The **minimal HUD** lands here (pill, hints surface, NDJSON protocol; states hearing, heard,
refused, hints, countdown) because P3's safety model is made of HUD states and it is the debug
surface for P3. Exit: first-word clipping and idle decode p90 within the bounds P0 set.

**P3. Understanding.** Lexicon with trust levels; grammar; the request fan-out; agreement;
corroboration; tier functions; act-then-swap; one-level "undo" and "again"; the late-answer
rule; offline fallback; near-miss suggestions; `inspect`; journal triples (utterance shape,
state signature, chosen action) recorded from day one so learning starts with data; the full
eval harness; thresholds from a reliability diagram. Exit: zero wrong actions on the adversarial
set, and the wrong-action and latency targets P0 set, or those targets revised here with the
data that forced it.

**P4. HUD polish.** Look matching, animation, layer rules in the supported dialects,
multi-monitor and fractional scale, "what can I say". Exit: a scripted protocol replay drives
every state and a screenshot comparison passes.

**P5. Trust and polish.** `teach --last` and promotion of confirmed Jev resolutions into the
grammar (gated on repeated confirmation plus an eval replay, never keyed on titles); final
`PRIVACY.md`; README with a demo; AUR recipe.

Later, each its own plan: compound commands; open mic and wake word with echo cancellation;
streaming dictation; clicking controls by voice; a Quickshell frontend; gateway speech to text
as a gated second opinion; TypeSafe direct as a fallback route.

## 12. Open questions for the owner

1. **Name.** `hyprsay` is free on PyPI, AUR and GitHub (checked 2026-09-21). `hyprvoice` and
   `hyprvox` are taken by existing Hyprland dictation tools.
2. **Push to talk key.** Proposed `SUPER + V`; needs one line in your Hyprland config.
3. **Accuracy bar.** What command accuracy in your own voice makes this worth using? P0 measures
   against it, and it decides whether the recognizer choice stands.
4. **Snappy animation profile.** Opt-in only. Should `doctor` offer it?
5. **Publishing.** The fork is local only. No GitHub repository will be created without a decision.
6. **Billing.** Jev is free on the gateway until 2026-09-25 [D], then $0.042 per million input
   tokens, about $0.00005 per semantic command at list price.

## 13. Engineering conventions

Branch per phase, merged to `main` with `--no-ff`. Conventional commit subjects in hypruse's
style. Every commit passes what CI runs: `uv run ruff check .` and the test suite. Live-desktop
tests are marked and skipped in CI; the nested Hyprland session, if P0 proves it, is the marked
local integration target. Secrets never enter the repository: a pre-commit check rejects key
prefixes and `AI_GATEWAY_API_KEY=` assignments. `requires-python` is pinned.

## 14. What the review changed

Four reviewers (facts, security, engineering, Jev leverage) read v1. The full findings are in
`docs/research/plan-review.json`. All four blockers were confirmed before being accepted.

| v1 said | Problem | v2 |
|---|---|---|
| Corroboration may anchor on title tokens | The attacker writes the title, so the defense anchored on attacker input | Trusted anchors only; titles break ties among already-corroborated candidates (5.6) |
| Typing is tier 1; only Enter is filtered | Focus needs no corroboration, so a hostile window could take focus and receive dictated text; shells execute on more than Enter | Context-tiered, pinned to the key-down address, refused in terminals, launchers and auth surfaces, all control characters stripped (5.6) |
| "Locked check, then dispatch"; "capture stops while locked" | Check-then-act race across a pipeline of 0.5 to 2.4 s; no mechanism to stop capture; a release bind never fires while locked [S] | Pushed latch from five sources, checked at five points, max hold (5.7) |
| Drop the MCP server surface | `server.py` holds every guard call site and every operation [S] | Extraction with a test-salvage plan (section 8) |
| Gate on p1 >= 0.8 | The plan's own data: the flagship semantic launch ranged 0.68 to 0.83 and would have acted 2 times in 9 | Gate on agreement and margin; all thresholds [A] |
| R2 is one forced choice | A window that does not exist still gets a winner | R2b absolute booleans; agreement; "no such window" offers a launch (5.5) |
| Decode once at key up; level meter only | The speaker's release lag was wasted | Speculative finalize during the hold (5.2) |
| Ambiguity always blocks on hints; undo in P5 | A second speak cycle for an action that is free to reverse | Act-then-swap for tier 0; undo in P3 |
| Timeout is a dead no-op | About 4 s to recover, on a service with real tails | Late-answer rule; offline numbered fallback (5.5) |
| "83 apps at about 1k tokens [M]" | Never recorded, bare names only; rich descriptions estimate at 3.3k | Relabelled; R3 sharded (5.5) |
| "about 410 ms p90 [M]" | Not a p90: n=12, floor index, identical tiny bodies | "p90 unknown" (section 10) |
| Decode "120 to 170 ms" | Two medians for one-second clips; decode scales with length | Budget by utterance length (section 10) |
| Several [V] labels | The checkers had explicitly not verified them | Downgraded; new [O] and [S] labels |
| HUD in P4, after the phase that depends on it | Hints and countdowns had nowhere to draw | Minimal HUD in P2 |
| Lua renderer "marked unverified" | No phase could ever execute it | Nested-session spike in P0, or cut from v1 |
| Nothing on install, config, logging, first run | Decides "extremely usable" | Sections 4.1 and 6 |
| Compound commands in P5 | Scope | Cut from v1 |
