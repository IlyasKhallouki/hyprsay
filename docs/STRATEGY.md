# hyprsay: the strategy for getting to the vision

Written 2026-09-22, after seven research lanes and a skeptic pass on each.
Evidence labels used throughout: **[M]** measured on this machine, **[S]** read from source with
file and line, **[D]** documented by a primary source, **[P]** a third party's self-report,
**[A]** an assumption with no measurement behind it, **[U]** untested by anyone.

A figure with no label is a bug in this document.

---

## 0. The headline, before anything else

Your vision is reachable. Four of the six complaints are code problems with no model in them at
all, and the two that are hard are hard for reasons that have already been measured rather than
guessed at.

Three things that should change how you read the rest of this.

**First, the thing you want exists on other platforms, one week after Jev launched.**
`browser-use/jev-ultrafast` (16,878 stars [M], created 2026-09-16) rebuilds its action list from
the live page on every step. `moritzkremb/jev-voice-browser` fires one Jev request per partial
transcript and acts mid-sentence. `kevinbadi/jev-voice` has already moved that loop from the DOM
to the macOS accessibility tree, which is exactly the move hyprsay would make to AT-SPI. The
shared rule, stated independently in three repositories, is **Jev selects, it never generates**,
which is already hyprsay's rule. So the architecture is not in doubt. What is in doubt is
whether hyprsay's *state* is rich enough to select from, and it is not.

**Second, the bottleneck is not Jev and it is not the LLM's decision-making.** A chooser can
only ever reach the behaviour its caller can enumerate. hyprsay currently enumerates windows,
apps and about forty grammar patterns, plus a hand-written table of key sequences in
`recipes.py` (853 lines [M]). That is the whole reachable universe. No model quality fixes
that. The fix is the function that builds candidates.

**Third, one unmeasured number decides a third of this plan.** Nobody has measured how long you
hold the push-to-talk key after your last word. `docs/PLAN.md` 5.2 already specifies speculative
finalize, `audio.py` already ships `snapshot()`, `trailing_silence()` and `voiced_after()`, and
`daemon.py` calls only `voiced_after(0.0)` [S]. The feature is designed, half-built and unwired.
Whether wiring it hides 400 ms or 40 ms depends entirely on that release lag. Phase 0 measures
it in half a day. If it comes back under 150 ms, cut Phase 3 down to decode-only and say so.

One correction you will not enjoy, and I am going to make it anyway. **Your complaint 1 is
partly wrong about your own code.** `nlu/clauses.py` does not string-match on "and". It proposes
seams from a closed list of connectives and asks Jev one Boolean per seam, in the fan-out it was
already sending, precisely so that "open bits and bytes" is not cut in half [S,
`clauses.py:1-25`]. That design is correct and it should survive. What is actually broken is
different and worse: a compound with no connective in the list is never seamed at all, and each
clause is understood independently, so clause 2 cannot see what clause 1 made true. The fix is
not a better splitter. It is to stop splitting and ask for a plan.

---

## 1. What is reachable, what needs something new, what is not worth doing

### Reachable now, with code you already have

| Goal | Evidence | Cost |
|---|---|---|
| "Open chrome" goes to the Chrome already open on workspace 8 | Reproduced live: Chrome on ws 8, focus on ws 11, "open chrome" decides launch at tier 1; "go to chrome" correctly focuses [M]. Five causes, all in code, none in the model (section 4.1) | 2 to 3 days, under 0.1 ms added latency, no model call |
| Compound commands without a connective, as one ordered plan | Question count is free: 1, 5, 15, 40 questions cost 323, 322, 312, 317 ms p50 [M, `dayone_out.json`]. Three `step_N` Choices in one request cost the same as one | 3 to 5 days, zero added latency |
| Clicking named controls in GTK and Qt windows | `a11ybus.py` already keeps one persistent D-Bus connection at 0.37 ms per call versus 9.8 ms per `busctl` spawn, about 26x [M, module docstring]. `controls.py` already caches and re-validates | Already built. Needs the stale docstring in `recipes.py` corrected and the walk warm-started during the key hold |
| Hiding the local decode behind the key hold | `PLAN.md` 5.2 is written; `audio.snapshot()` and `voiced_after()` exist [S] | About 60 lines in `daemon.py`. Commit still happens at key release, so nothing new can go wrong |
| Media context without any model | MPRIS `Properties.GetAll` on `org.mpris.MediaPlayer2.Player` returns the full `Can*` capability set even on players whose `Introspect` returns an empty node [M]. `playerctl` is already a dependency | About 50 lines |

### Reachable, but needs something hyprsay does not have yet

| Goal | What is missing | Honest cost |
|---|---|---|
| Clicking inside a web page | An MV3 extension plus a native-messaging host. Chrome publishes **zero** AT-SPI children without a flag [M: `google-chrome` child_count = 0, one node total]. Chrome 151 refuses a debugging port on the default profile and says so out loud: "DevTools remote debugging requires a non-default data directory" [M] | 1.5 to 3 weeks of engineering, plus a distribution problem that is larger than the engineering (section 5) |
| "Pick a song on Spotify" | A Spotify account with Premium, a one-time `spotify_player authenticate` that has never been run on this machine, and a candidate ranker | 3 to 5 days once the credential exists. The Web API round trip is **[A]**, never measured here |
| Acting while you are still speaking | Partial transcripts. `sherpa_onnx.OfflineRecognizer` emits no interim result [S, `stt/local.py:88-90`], so partials mean re-decoding a growing prefix on CPU | 2 to 4 days, gated on Phase 0 |
| Cross-turn memory ("no, the other one") | Extend the machinery that already exists. `daemon.py:56-57` declares `_picking` and `_picking_until` with a configured TTL, threaded into `understander.understand(..., picking=picking)` at `daemon.py:295-297` [S] | 2 days, built on top of that, not beside it |

### Not worth doing, and why

**A learned end-of-utterance model.** Voice-Light measured a learned EOT adapter at 12.53%
recall against 95.60% for plain Silero VAD over 1,636 EOT cases [P, arXiv, Bertil Braun,
2026-09-17]. You are push-to-talk. Key release *is* the turn boundary, and it is better than
anything anyone has learned. Do not build this.

**Cloud streaming speech recognition on the hot path.** Measured here: grok-stt emits exactly
one partial, median 1,520 ms after audio starts, on all 16 rows; gemini-3.5-transcribe-live
finalizes a median 3,322 ms after audio is done [M, `stt_streaming_out.json`]. A caveat the
first pass missed: roughly 490 ms of the grok figure is per-clip stream setup that a persistent
socket would remove, so a warm socket would plausibly land near 1,030 ms. That is still after a
0.85 s command ends [M: your eight command clips are 0.85 to 1.56 s].

**Replacing Jev with a constrained small LLM.** Measured by a third party: haiku-strict p50
691 ms, p95 1,200 ms, against Jev p50 225 ms, p95 890 ms [P, `shitianfang/jev-use`, one author,
one region, one day, unreplicated]. Jev's p50 advantage is real; at the tail it falls to about
1.35x. But 691 ms of p50 alone exceeds the entire post-release budget. Keep Jev. State its case
as answer shape and cost first, latency second.

**A per-step agent loop.** `jev-ultrafast`'s own flagship measurement artefact:
`decision_total_ms` 3,720 against `elapsed_ms` 7,073, so 52.6% of that run was spent waiting on
Jev, on the authors' network, at a 178 ms median, with fan-out already in place [M, I read
`docs/flights-measurement.json` directly]. From Europe your median is 315 ms [M]. One request
per utterance is the target. Two is the ceiling, and the only legitimate second is fetching
candidates that could not exist before the first answer.

**OCR and screenshot-first grounding.** Already decided and still right. On a GPU-less
i5-8350U it is the expensive fallback. Keep `zoom` for terminals and canvas UIs, never in the
decision loop.

**Porting `jev-voice-browser` wholesale.** Its design depends on Web Speech interim results,
which are remote, streaming and free to the laptop. Yours are local and cost CPU. Copy its
policy constants, not its pipeline.

---

## 2. The architecture that replaces grammar plus recipes

### 2.1 The one idea

> The reachable behaviour of a chooser equals the set of options its caller can build. So build
> the options from the machine, every utterance, and ask one question about them.

Today the action space is: 40 grammar patterns, plus windows, plus apps, plus a 853-line table
of key sequences keyed by application *kind*. That table is why it feels pre-baked, because it
is pre-baked, by design and for a stated reason (`recipes.py:1-30` argues it, and the argument
rests on a claim about AT-SPI that is now stale: the registry is up on this machine,
`toolkit-accessibility` is true and `at-spi2-registryd` has been running 18 hours [M]).

Replace it with an **affordance harvester** feeding **one fan-out request** into the **existing
guard stack**.

```
key down ──► harvest (parallel, all local)          ┐
             compositor windows + workspaces        │  0.1 to 0.3 ms  [M]
             .desktop apps + their Actions=         │  cached
             system controls (volume, media, layout)│  free
             MPRIS Can* flags per live player       │  ~1 ms on a warm connection [M]
             AT-SPI controls of the focused window  │  on demand, cached, warm-started
             browser snapshot (Phase 5)             │  19 to 121 ms [M]
                                                    ┘
             ──► rank against the transcript (1.5 ms [M]) ──► top ~50
                                                    │
             ──► ONE Jev request:                   ▼
                 step_1, step_2, step_3   Choice over the SAME list, each with `none`
                 is_correction            Noul
                 refers_to_previous       Boolean (only when memory is live)
                 unsupported_kind         Choice   (kept)
                 amount / direction / ws  (kept)
                                                    │
key up   ──► code takes the prefix up to the first `none`
             ──► every existing guard, unchanged: tiers, corroboration,
                 fresh-state re-check, hostile-title rule, countdown, journal
```

### 2.2 What generates candidates when the action space is open ended

Every generator returns the same record and nothing else:

```
Affordance = { id, label, kind, tier, handle, trust }
```

`id` is what Jev answers with. `handle` is opaque and only code ever dereferences it. Jev never
emits a selector, a coordinate, a command string or an address. This is not a new rule, it is
hyprsay's existing rule, extended to a larger list.

| Generator | Source | Cost | Covers |
|---|---|---|---|
| Compositor | Hyprland socket | 0.1 to 0.3 ms [M] | `focus_window:<addr>`, `move_to_ws:<n>`, close, float, fullscreen |
| Desktop entries | already parsed lexicon | free | `launch_app:<id>`. Also `Actions=`, which 9 of 234 entries declare [M] |
| System | existing ops table | free | volume, media transport, layout, lock (tier 3, never offered to the model) |
| MPRIS | `Properties.GetAll` | ~1 ms | per-player `CanPlay`, `CanSeek`, `CanGoNext`, plus live `Metadata` and `PlaybackStatus` |
| AT-SPI | `a11ybus.py` | 0.37 ms/call [M] | named controls of the focused GTK or Qt window |
| Service adapter | `spotify_player search` | [A], unmeasured | real tracks, albums, playlists, by their own ids |
| Browser | MV3 extension | 19 to 121 ms [M] | 32 to 62 deduplicated viewport candidates |

The adapter contract, which GNOME's SearchProvider2, KDE's krunner1 and Apple's App Intents all
converge on independently:

- `can_serve(context)`: a pure local predicate. Is the process running, does it own its bus
  name, is the binary installed.
- `capabilities(context)`: stable ids, each with a tier, typed parameters and one line of
  description.
- `resolve(capability, slots, context)`: **read-only**, returns real candidates from the app's
  own domain, with opaque handles.
- `perform(capability, handle, context)`: only ever accepts a handle the adapter itself minted.

The payoff for your tier model: `resolve` is always tier 0 because it only reads, so the
existing 0-to-3 tiers survive completely untouched and only `perform` carries a tier. The
model's output narrows to a capability id plus a candidate index, which is exactly the
enumerated choice Jev answers well.

Two honest caveats on the convergence claim. GNOME's `GetSubsearchResultSet` is documented as
an optimisation, not speculative execution: "the method may return less results, but not more
or different results" [D, `GNOME/gnome-shell`, `org.gnome.ShellSearchProvider2.xml`]. That
monotonic-narrowing contract is violated by streaming ASR, where a later token routinely
rewrites an earlier word. And `GetResultMetas` returns only name, id, icon and description, so
SearchProvider2 has no typed parameters at all. The convergence is on "ranked opaque ids, then
activate one". It is not on a capability-and-parameter contract. Copy the first, design the
second yourself.

### 2.3 What happens to recipes.py

It gets demoted, not deleted, and deleted last. It stops being "the way to act inside an app"
and becomes "the last resort for apps with no tree and no API". That is an honest and defensible
place for it: some applications really do expose nothing but a keyboard, and a reviewed, tiered,
kind-keyed table is the right shape for those. The browser half goes first, in Phase 5, once the
extension can reach what it hardcodes. Keep at most `ctrl+w` and `ctrl+pgdn`.

One thing that must not be promised: routes do not disappear. `jev-ultrafast`'s `questions.py`
is a fourteen-line hand-written policy carrying the procedural knowledge a recipe table carries,
verbatim: "A typed query still needs its matching autocomplete suggestion selected. For date
pickers, CLICK the field, date, then confirmation." [M, I read the file]. Its README's "no
site-specific action scripts" is true and is a *different* claim from "no pre-baked routes".
The routes move from a dict keyed by application name into one page of instruction text that
applies to every application. That is a large improvement, because one page generalizes and 853
lines do not. It is not the same as vanishing.

### 2.4 Request shape and the payload cap

Question count is free. Payload is not. Both measured on this machine, n=30 per series
[M, `dayone_out.json`]:

| Input tokens | Total p50 | Total p90 | HTTP 503 |
|---|---|---|---|
| 725 | 315 ms | 356 ms | 0 of 30 |
| 2,246 | 314 ms | 362 ms | 0 of 30 |
| 6,037 | 448 ms | 513 ms | 1 of 30 |
| 11,542 | 544 ms | 682 ms | 13 of 30, then 4 of 25 |

So the binding constraint is the **size of the affordance list**, not Jev's 255-option limit,
which is never reached. Cap each request near 1,800 tokens, which is the existing rule and it
still holds.

How many candidates fit in 1,800 tokens is where the first pass got it wrong and it matters for
you specifically. `tokens.py`'s own docstring says the fit is eight points for four parameters
on synthetic English, that "paths, Unicode and long identifiers in real window titles tokenize
worse", and that running low is the dangerous direction [S]. Measured with that estimator: 120
candidates at real YouTube result length (71 chars average) comes to 3,446 tokens, 1.9x the
cap; 120 French accented labels at 52 chars average comes to 2,674 tokens, 1.5x the cap [M].
You are French, on AZERTY, and your stated example is picking a song, so your labels are exactly
the long accented punctuated strings the fit never saw.

**Plan for 50 to 60 candidates, not 120. Truncate labels at 60 characters. Wire `tokens.Drift`
in from the first request so the error is measured rather than assumed.**

Rank before asking, fuzzy-prefiltered against the transcript. Note carefully that the
justification is token cost and measured payload latency. It is **not** that Jev's confidence
thins past 25 options: that claim traces back to a three-option Pong question and no
measurement of Jev accuracy as a function of option count exists from any source.

### 2.5 What guards it, since confidence cannot

Keep every tier 2 and tier 3 guard in code, permanently. The evidence is blunt: Jev returns
exactly 1.0 on 56.4% of answers and 9 of those were wrong, over 860 answers
[P, `nikkoxgonzales/jev-certify`, arithmetic verified against `results/results.json`]. Its
selective error is 2.65% with a Wilson 95% interval of [1.40%, 4.97%], so the honest statement
is "about 3%, possibly 5%".

Add one thing `jev-ultrafast` has and hyprsay does not: **fingerprint the observation**. Stamp
every decision with a hash of the state it was made against, refuse to execute against a moved
desktop, and consume the decision before mutating so a retry cannot double-act. This is
orthogonal to confidence, not stricter than it: a fingerprint proves the world has not moved
since the observation, it says nothing about whether the choice was right. `jev-ultrafast` ships
with freshness and *no* correctness guard, and pays for it with a 60-action budget and a
blocked-after-three-no-ops heuristic. You keep both.

Add one generative seam, and exactly one, with `jev-ultrafast`'s contract: one call, one JSON
key `text`, validated as a non-empty string under 2,000 characters before use, whose output can
only become typed text into a target Jev already chose [M, `model.py:160-198`]. It must inherit
tier 2, which means the countdown, the pinned window and the terminal/launcher/password refusal
all still apply.

---

## 3. Acting while you speak, with a budget built from the measured numbers

### 3.1 Today, after you release the key

All [M], from `docs/research/live/`:

| Step | Cost |
|---|---|
| key release lag (last voiced frame to key up) | **unmeasured. This is the whole game.** |
| capture flush | ~40 ms |
| Parakeet 110M decode of a 0.85 to 1.56 s clip | 93 ms p50 idle, ~165 ms under load |
| grammar decision | 1 ms |
| Jev fan-out (725 to 2,246 tokens) | 315 ms p50, 356 to 362 ms p90 |
| three concurrent small requests, one connection | 359 ms p50 wall (n=12) |
| executor + guarded write over the socket | 0.1 to 0.3 ms |
| **total, key-up to action, Jev path** | **about 450 ms** |
| Hyprland window animation, after that | 500 to 600 ms |

Note the last row. Even at zero latency, your compositor's animation is the largest single
delay in the system. Nothing in this plan touches it, and an opt-in snappy profile would buy
more perceived speed than everything in section 3 combined. Say that out loud rather than
pretending the animation is not there.

### 3.2 The design, in three steps, each shippable alone

**S1, speculative finalize. About 60 lines. No new risk.** Wire `PLAN.md` 5.2 exactly as
written. While the key is held, the energy gate watches the ring buffer; on 150 ms of trailing
silence, `recorder.snapshot()`, decode, normalize, grammar, and on no match fire the Jev
fan-out. On key up, if `recorder.voiced_after(snapshot_seconds)` is False, the hypothesis stands
and its answers are reused. **Commit still happens at key release**, so nothing can act on a
prefix the sentence later changes.

Budget: the ~40 ms flush, the ~93 to 165 ms decode and the ~315 ms round trip all move into the
time you are still holding the key. What remains after release is a fresh-state re-check and a
dispatch, roughly 30 ms.

Exit criterion: median key-release-to-dispatch on the Jev path falls from about 450 ms to under
100 ms, with the hostile-title wrong-action rate still at zero.

**S2, the growing-prefix ladder. GATED on Phase 0.** Re-decode a growing prefix during the hold
at steps of 250, 350 and 500 ms, gated on the energy gate having heard speech plus about 400 ms
so the empty early rungs are skipped. Compare the last two `Decision` objects on
`(action, target address, tier)`, never on transcript text. Fire the fan-out on the first rung
whose decision is new; cancel and re-fire when it changes.

The measured cost, on this machine, Parakeet TDT 110M int8, one thread [M, `prefix_parakeet.json`,
re-run and reproduced byte-identically by the skeptic]:

| Prefix | Decode |
|---|---|
| 0.8 s | 99 ms, and 133 ms on the skeptic's re-run at *lower* load |
| 1.0 s | 122 ms, and 150 ms on the re-run |
| 1.2 s | 160 ms |
| 1.4 s | 225 ms |
| 1.6 s | 257 ms |

So budget 100 to 135 ms at the 0.8 s rung, not 99. Past about 1.2 to 1.4 s of speech one decode
exceeds the step interval. Hard-stop the ladder past 2.5 s of buffer: at 7.43 s of audio the
naive 200 ms ladder cost 17.9 s of CPU, 2.41 times real time [M].

The danger is measured and specific. Across that 7.43 s clip, 30 of 37 rungs were exact word
prefixes of the final decode, but three of those were free passes on empty rungs, so non-empty
correctness is 27 of 34, and inside the 0.85 to 2.0 s band that actually matters it is 5 of 7.
At the 1.4 s rung, deterministically, both runs decoded "Well, I don't wish to say" where truth
was "to see": a confident wrong content word at exactly the horizon a speculative act fires on
[M]. And that clip is a literary read, not a command. A second clip at genuine command length
(0.99 s) gave 9 of 9 prefix-correct rungs [M]. **Nobody has run this ladder over a single
hyprsay command.** Phase 0 does.

**S3, early acting. Tier 0 only.** Act before release only when all of: two consecutive agreeing
decisions, a *closed clause* (the grammar matched a complete pattern **and** 150 ms of trailing
silence), tier 0, and an inverse recorded before the write. If the final decision at key release
differs, run the inverse then the final action. Precondition: audit which tier 0 operations in
`ops.py` return a non-None inverse and exclude the rest.

The trap, named precisely: "open firefox and move it to workspace three", where the prefix is
itself a complete valid command. The trailing-silence requirement on top of the grammar match is
what prevents it. Nothing else does.

Copy Voice-Light's invalidation rule verbatim, because it is free: revisions limited to case,
punctuation, whitespace or apostrophes preserve the speculated work; any lexical change
discards it [D]. And copy `jev-voice-browser`'s policy constants: 200 ms debounce, at most 2
requests in flight with `AbortSignal` cancellation, 900 ms silence, 600 ms extra for free-text
payloads [P, its `src/constants.js`].

### 3.3 The budget, assembled

For a 1.5 s spoken command on the Jev path, with S1 and S2 in place:

```
t=0.00  key down, mic opens, connection warmed (prewarm GET costs 206 to 301 ms
        and cuts the first request from ~500 ms to ~320 ms [M, prewarm_out.json])
t=0.40  first non-empty rung fires: decode ~100 ms, done at t=0.50
t=0.50  Jev fan-out fires, lands t=0.82  [M: 315 ms p50, 362 ms p90]
t=0.90  second rung agrees, decision unchanged, nothing re-fired
t=1.50  last word ends
t=1.50  +release lag [UNMEASURED]
t=???   key up: fresh state re-check + guarded dispatch, ~30 ms
t=???   +500 to 600 ms animation [M]
```

If the release lag is 300 ms, the action dispatches about 30 ms after key up and you perceive it
as instant. If the release lag is 80 ms, S1 still hides the decode but the Jev round trip does
not fit, post-release cost lands near 250 ms rather than 30 ms, and S2's extra CPU buys little.
**That is the decision Phase 0 makes, and it should be made before a line of S2 is written.**

For calibration on what "fast" means elsewhere: across the whole production field, the fastest
system to a first tool call is Grok at 0.63 s, and it is simultaneously the second worst at
self-correction with Pass@1 0.294 [D, Full-Duplex-Bench-v3, arXiv:2604.04847, Table 6]. Your
315 ms Jev decision is already twice as fast as the fastest, and the trade Grok made is exactly
the one S3 is being asked to make. That is the reason S3 is tier 0 only, with an inverse.

---

## 4. The bugs you named, each with its fix

### 4.1 "It opens a new one instead of going to the one that is open"

Five causes, not one, all located [S]:

1. `grammar.py:680-687` binds every launch verb to `Intent.LAUNCH_APP` unconditionally, and
   `Grammar._try` returns the first full match, so "open" can never reach the `FOCUS_WINDOW`
   rule at `grammar.py:688`.
2. `understand.py:450` routes to `_launch_locally`, which calls `resolve.resolve_app`
   (`resolve.py:188`), whose signature has no `DesktopState` in it at all, unlike
   `resolve_window` at `resolve.py:140`.
3. `understand.py:735-736` forces `intent=Intent.LAUNCH_APP` and blanks `window`, and it is the
   single funnel for both the grammar path and the Jev path.
4. `ops.py:297-314` ignores the `_before` state it is handed, and `ops.py:308` sends the launch
   to the current workspace when none was named.
5. `bank.py:86-97` tells Jev that `LAUNCH_APP` is "not_for: going to a window that is already
   open", while `requests.py:133-143` sends only `{utterance, variants, note}` and says in its
   own docstring "Nothing about the desktop is here". **The model is asked the question and
   denied the evidence.**

One correction to the first pass that makes this cheaper than it looked: `_launch` is declared
`def _launch(self, app, candidates, evidence, heard, state: DesktopState, template=None)` and
passes `state` through to `_finish` [S]. The desktop **is** in scope at the decision point. Only
`resolve_app` lacks it. The missing piece is the branch, not the data.

**The fix.** A new module `nlu/ground.py`, about 150 lines, one entry point
`reach(app, prefer, state, lexicon, gates) -> LAUNCH | FOCUS(window) | AMBIGUOUS`, called from
the single funnel at `understand.py:708`.

Matching, in strict trust order:
- `App.wm_classes` against `Window.cls` and `initial_class` (exact, trusted).
- `lexicon.app_for_window` (exact, trusted).
- **Never by title.** The hostile-title rule is not negotiable.
- **Never by pid.** `jumpapp` uses pid as corroborating evidence and here it would be actively
  wrong: `hyprctl -j clients` gives the Chrome PWA and both `google-chrome` windows the same
  pid 1325064, and `/proc/1325064/cmdline` is the bare `/opt/google/chrome/chrome` [M]. For
  Chromium-family apps pid is not weak evidence, it is false evidence. Drop that signal.

Decision table:

| Condition | Result |
|---|---|
| `prefer == NEW` | LAUNCH |
| zero exact-matched windows | LAUNCH |
| `SingleMainWindow=true` and any window | FOCUS |
| exactly one exact window | FOCUS |
| several, one of them already focused | LAUNCH (you want a second terminal) |
| several, otherwise | AMBIGUOUS, so hints |

Demote the grammar's verb split from deciding the action to declaring a preference: focus verbs
mean EXISTING (today's behaviour, unchanged), launch verbs mean EITHER, launch verbs plus a
closed novelty word (new, another, second, third, extra, fresh, more, again) mean NEW. Closed
list, for the same reason `DICTATION_OPENERS` is one.

And when launching with no workspace named, launch onto the workspace where an existing window
of that app already lives, behind a config key.

`SingleMainWindow` is a nice-to-have, not the lead. Only 10 of 245 installed `.desktop` files
declare it, all KDE and Qt utilities, and **none** of chrome, kitty, spotify, firefox or code
[M]. It covers about 4% of installed apps and 0% of the apps in your own complaint. It also is
not free: `lexicon._KEYS` is a closed frozenset that does not include it and `model.App` has no
field for it [S].

Then feed Jev the evidence it is currently denied: `open_apps` as trusted `.desktop` names,
`current_workspace`, `focused {app, kind}`. No titles, no dictated text, no addresses, so
`PRIVACY.md` gains one row and PLAN section 7 holds. Roughly 40 to 120 extra tokens on a
request that today sits at 725, so free in latency by the table in 2.4.

**Net effect on network traffic: negative.** "Open chrome" with one Chrome open stops needing
Jev at all and resolves in the grammar at tier 0.

**Regression tests, with a published number.** Add a `reach` family against the existing
fixture: "open spotify" focuses it; "open obsidian" focuses it on another workspace (your exact
complaint, as a permanent regression test); "open discord" launches; "open firefox" with two
firefox windows gives hints; "open another firefox" launches; "open firefox in a new workspace"
still launches with the workspace preserved. Adversarial cases where zero is the only acceptable
wrong-action count: a firefox window titled "Spotify - Premium" must not answer to "open
spotify"; a `SingleMainWindow` in an untrusted `.desktop` must be ignored. Publish the
**second-instance rate**, whose target is zero and whose current value is 100%.

### 4.2 "It splits on the word and"

As stated in section 0, it does not. `clauses.py` proposes seams from a closed connective list
and asks Jev one Boolean per seam [S]. The real defects are no-connective compounds and
clause-independent understanding.

**The fix.** Replace the per-seam Booleans with `step_1`, `step_2`, `step_3`: three Choices over
the **same** affordance list, each with a `none` option, in the same request. Code takes the
prefix up to the first `none`. Never ask Jev to count the steps; the vendor says it does not
count reliably. Instruct it to build the complete ordered plan in one response, and that after a
`stop` or any failing choice every later slot must also be `stop`.

Keep `clauses.py` for what it is actually good at and must not lose: dictation opacity. "type
hello and goodbye" must remain one piece, and everything from a carrier verb onward must stay
invisible to Jev, exactly as `DICTATION_OPENERS` enforces today.

A note on provenance, because it matters for how much to trust the pattern. `devfros/omause`
implements this exact mechanism against Hyprland, in real code (`src/jev.ts:98-130`), and it
calls `hyprctl` directly and in batched form (`src/state.ts:48`) [M]. But it has 0 stars, 0
forks, 0 issues, all 17 commits inside one 28-hour window, eleven of them authored by an agent
identity, no CI, no demo, and it was last pushed eight seconds after it was created and never
touched again [M]. Read it as a design sketch that compiles. Copy the mechanism, do not treat
its existence as evidence the mechanism works on a real desktop.

### 4.3 "It cannot click in pages"

Section 5, in full. The short version: this is not hyprsay's fault. Chrome publishes zero AT-SPI
children [M] and refuses a debugging port on your logged-in profile [M].

### 4.4 "Pick a song on Spotify, without pre-baked routes"

Measured, and the news is mixed.

**Spotify on Linux gives you transport control and nothing else.** The shipped binary contains
exactly three MPRIS strings, and neither `TrackList` nor `Playlists` appears anywhere in it. The
live MPRIS interface has nine methods, none taking a search string, none returning a list [M].
So no search, no playlist enumeration, no queue, over D-Bus. Ever.

**What answers the question is already installed.** `spotify-player` 0.24.1, as
`/usr/bin/spotify_player`, from Arch's `extra/` repository, packaged by an Arch staff packager
(not AUR, so downgrade the supply-chain worry) [M]. It does `search` returning ranked JSON and
`playback start track --id <id>`.

**Do not use `--name`.** Upstream `spotify_player/src/cli/client.rs` at tag v0.24.1 calls
`search_specific_type(&name, SearchType::Track)` and then takes `page.items[0].id`, bailing with
"Cannot find track with name=" otherwise [M, source read; the literals are in the local binary
too]. That is a blind `[0]` wearing a nicer name, and it is exactly the heuristic this whole
design rejects. More practically, it can never serve "the second one" or "the live version".

**The shape.** `search "<query>"` returns ranked JSON, code builds candidates with opaque ids,
one Jev Choice picks, `playback start track --id <id>` performs. Jev generates nothing, code
writes no command string, and there is no route in any table.

**The unpriced prerequisites, stated plainly.** Spotify Premium is required by the Web API
playback endpoints. A one-time `spotify_player authenticate` with two browser OAuth approvals
has never been run on this machine. And the search round trip is **[A], never measured by
anyone**: process start is 16 to 22 ms and after the first call the CLI talks to a
already-running client over a localhost socket [M], so budget the network leg like your own
measured Jev round trip, about 315 ms, and note that the first call additionally pays a full
client cold start. A systemd user unit keeping the client warm removes that.

End-to-end estimate: about 0.7 to 1.0 s after key release with two sequential network legs, or
close to zero perceived if the search fires on a partial transcript while the key is still held.
This is the single strongest argument for speculation in the entire plan, and it is a real one.

**Superseded, and by the owner's own code.** `waytify` (`~/projects/waytify`) is a Rust daemon
for media control built on MPRIS with a Spotify layer, and it already exposes `Search`,
`PlayTrack` and `PlayContext` over newline-delimited JSON on a Unix socket, with its refresh
token in the system keyring. It answers this section's question better than the CLI on four
counts: search results arrive as structured `SearchResult` rows rather than a JSON blob to pick
apart, there is no process spawn per query, the protocol is the same shape hyprsay already
speaks to its own overlay, and a defect in it is one the owner can fix. So
`adapters/waytify.py` is the route, `adapters/spotify.py` is demoted to the fallback its
`can_serve` stands down from whenever the waytify socket exists, and the systemd unit that was
going to keep the CLI's client warm is not needed: the daemon already is the warm client. The
Premium prerequisite is unchanged, because it belongs to Spotify's own playback endpoints.

### 4.5 "Nowhere near intelligent"

This one has no separate fix, and I am not going to pretend it does. It is the sum of 4.1
through 4.4 plus one thing none of them covers: **hyprsay has no idea what it just did.** Add
`context.recent_actions` to the state: three entries of
`{said, action, target, outcome, seconds_ago}`, plus an `is_correction` Noul. Cheap in tokens,
and it is what makes "no, not that one" and "the other one" work at all.

Build it on the machinery that exists, not beside it. `understand.py:300-302` does not reset
`pending`, it *sets* it, deliberately surviving into the next utterance, and `daemon.py:56-57`
already carries `_picking` and `_picking_until` with configured expiries, read at
`daemon.py:295-297` and threaded into `understand(..., picking=picking)` [S]. Extend those and
their clearing paths (`daemon.py:159`, `:303`, `_forget_everything`). Do not build a second
memory with its own lifetime.

Gates, all three required: a 30 s TTL on the injected clock; a segment break clearing the buffer
on a manual workspace switch, on the carried window closing, or on an `activewindowv2` hyprsay
did not cause; and referential incompleteness, meaning the utterance did not name its own target.
Invariants: fill only, never override; never above tier 1; evidence does not refresh; carry
nothing when the previous turn's top two were within `gates.margin`.

And do not justify depth-one memory with centering theory. The paper usually cited for it
measures Constraint 1 failing on 20% to 25% of utterances and reports a trade-off where Rule 1
and Rule 2 cannot both hold at the same parameter settings [D, Poesio et al. 2004, its own
abstract]. Depth-one is a design choice. Attach a measured error rate to it and move on.

Finally, add a **correction grammar** ("no, the other one", "undo that", "cancel") before any
speculation work. Recovering gracefully from a wrong guess is what earns the right to guess
harder.

---

## 5. The browser: the decision and the reasoning

**Recommendation: build a Manifest V3 extension plus a native-messaging host. Ship a doctor
check for `--force-renderer-accessibility` in week one as a stopgap. Do not pursue CDP on a
debugging port.**

### The evidence, in the order it decides things

**Chrome publishes nothing over AT-SPI.** Re-measured twice, read-only:
`google-chrome` child_count = 0, one node total. Not three frames, not an empty about:blank
frame. Zero [M]. The whole AT-SPI bus on this machine is `xdg-desktop-portal-gtk`, `udiskie`,
`nm-applet`, `blueman-applet`, `blueman-tray`, `waybar` and `google-chrome`: thirteen
connections, not one user-facing application window [M].

**Chrome 151 refuses a debugging port on the default profile.** Verified live, and with one
correction to the first pass that matters for planning: the failure is **loud**, not silent.
Chrome prints "DevTools remote debugging requires a non-default data directory. Specify this
using --user-data-dir." [M]. Headless is not exempt either; it rewrites its own args to a scoped
temp profile under `google-chrome-headless/` [M]. So a separate profile is the only way to get a
port, and a separate profile has none of your logins. **"Pick a song on Spotify" dies at the
login screen on any separate-profile approach, Playwright included.** That is the decisive fact.

One loose end that must be closed before Phase 5 is scheduled: `jev-ultrafast`'s README says
Chrome connects via a "Browser Harness", tells users to "Allow remote debugging in Chrome when
prompted", and states "owned tabs share the existing Chrome profile", which on its face
contradicts the above. Nobody has read `browser_harness` to find out how it gets a port. Half an
hour of reading in Phase 0.

**Reading the page is cheap if done in the page.** Measured over CDP on this machine
[M, `cdp_out2.json`]:

| Method | Wikipedia | YouTube search results |
|---|---|---|
| one `Runtime.evaluate` that filters, names and formats in-page | **19.3 ms**, 12.7 KB wire, 62 deduplicated viewport candidates | **121.3 ms**, 5.2 KB wire, 32 candidates |
| `Accessibility.getFullAXTree` | 172.1 ms, 699 KB, 1,826 AX nodes | 185.7 ms, 582 KB, 1,554 nodes |
| `DOM.getDocument` | 77.0 ms, 653 KB, 2,851 nodes | 243.0 ms, 1.09 MB, 4,509 nodes |
| `Page.captureScreenshot` (jpeg) | 151.8 ms, 288 KB | 316.1 ms |

Real pages are 1,554 to 1,826 AX nodes. At the AT-SPI cost measured on this machine, 0.32 to
0.33 ms per node with name and role fetched, a walk of 1,826 nodes would be roughly **600 ms**
[M]. So even with `--force-renderer-accessibility`, reading a page over AT-SPI does not fit the
budget. AT-SPI stays correct and cheap for GTK and Qt *application chrome* (88 elements in 29 ms
[M]) and is the wrong transport for web content. `jev-ultrafast` reports the same lesson from
the other direction: its speedup came from cutting median protocol calls per task from 1,092 to
101 [P, n=3 pairs on live Google from unstated hardware, so an architectural signal and not a
latency budget].

**Native messaging already works here.** Three native-messaging hosts are installed and
functioning on this machine, one of them Claude Code's own [M]. So the pattern is proven under
Hyprland with this Chrome. That is the single strongest de-risking fact in the browser plan.

### The build, split so the extension is the eyes and hypruse is the hands

1. **Native host first.** A `hyprsay browser-host` subcommand speaking the 32-bit
   length-prefixed stdio protocol, plus an installer writing
   `~/.config/google-chrome/NativeMessagingHosts/dev.hyprsay.browser.json`. Known quantity.
2. **Content script exposing one function, `snapshot()`.** Returns title, url, headings and
   candidates with index, role, accessible name, rect and section. Viewport first, named only,
   deduplicated by role and name, hit-tested with `elementFromPoint`, plus the `cursor:pointer`
   second pass. Names truncated at 60 characters, capped at 120 before ranking, sliced to 50 to
   60 after (section 2.4). Budget 15 to 20 ms warm, since extraction is 8 to 14 ms of in-page JS
   [M]. Send the title, the URL host and about 400 characters of heading context. **Never a
   6,000-character page-text block**: that estimates at 3,339 tokens, 1.9x the cap [M].
3. **Rank in the daemon**, 1.5 ms [M], and size the Choice with the existing estimator against
   the state actually being sent.

### Acting, in three tiers, quietest first

- **Tier 1.** The content script focuses and clicks, dispatching proper input and change events.
  No permission prompt, no banner, works on most of the web. This is the default.
- **Tier 2, the one nobody else can do.** The extension returns the element rect, the daemon
  converts it to Hyprland global coordinates using the window rect it already has from `hyprctl`
  plus `outerHeight - innerHeight`, `outerWidth - innerWidth` and `devicePixelRatio`, then clicks
  through `zwlr_virtual_pointer_v1`. The click is `isTrusted`, needs no debugger permission,
  shows no infobar, and every existing trust guard, the journal, the beacon and the panic key
  apply unchanged.
  **This is [U]: untested by anyone, and it is the linchpin of the whole no-banner story.** Under
  Wayland an unfocused client, a pointer-constrained page, or a fractional-scale output can each
  break the coordinate mapping, and the chrome-offset derivation is untested against Chrome under
  `--ozone-platform=wayland`. **Spike it in Phase 0, in half a day, before Phase 5 is scheduled.**
- **Tier 3.** Opt-in `chrome.debugger` for the few sites that defeat both. It shows a banner.
  Say so in the config comment.

### Why not the flag route as the answer

`--force-renderer-accessibility` turns 1 element into 413 including 44 links [P, `trycua/cua`
issue 2915]. It is worth shipping as a doctor check in week one, because it converts complaint 2
from a mystery into a one-line fix in hours of work. It is not the answer, for three reasons: it
changes how your browser starts; it makes Chrome maintain an accessibility tree per frame for
every open tab, at a CPU cost nobody publishes and nobody has measured here; and the 600 ms walk
above means it cannot carry a spoken command anyway.

### The real cost, which is not engineering

About 400 lines of extension JS and 200 lines of native host Python on top of what exists. The
genuine costs are: **distribution** (unpacked developer-mode loading is a poor experience, and
the Web Store means review, a developer account and a release cadence hyprsay does not have);
**surface area** (all-urls access plus a local process that can click anything makes the
extension a security boundary the project has to defend, and the existing prompt-injection
warning gets real teeth when page content feeds a Choice); and **breadth of failure** (shadow
roots, iframes, canvas, file uploads, drag-and-drop and hover-only menus are documented gaps in
every project surveyed, so some sites will simply not work and the overlay has to say so rather
than guess).

---

## 6. Sequencing, and what each phase proves

Something visibly better exists after three days.

### Phase 0. Measurement and spikes. Half a day to one day. No daemon code.

- Record 150 commands in your own voice, half of them French, with `hyprsay record`.
- Measure the **key-release lag** (last voiced frame to key up). Still open in `PLAN.md` P0.
- Run the prefix ladder over the eight existing command clips and the new 150.
- Run `spotify_player authenticate`, then measure `search` latency, warm and cold.
- Measure `tokens.estimate` drift against real French and music-metadata labels.
- **Spike the tier-2 compositor click into a Chrome page rect.**
- Read `jev-ultrafast`'s `browser_harness` and settle the CDP question.

**Proves:** whether Phases 3 and 5 are worth building as designed. Four assumptions die or live
here, and all four are cheap to test and expensive to be wrong about.

### Phase 1. Grounding. Two to three days.

`nlu/ground.py`, the verb-as-preference split, the workspace-follows-existing-window rule, the
Jev context object, the `bank.VERSION` bump, the `reach` eval family, the correction grammar,
the `recipes.py` docstring correction, and the `--force-renderer-accessibility` doctor check.

**Proves:** complaint 3 is gone, with zero added latency, no model call and less network traffic
than today. The second-instance rate goes from 100% to 0%. **If only one thing ever ships, ship
this one.**

### Phase 2. The affordance harvester and the single fan-out. Three to five days.

Compositor, desktop entries with `Actions=`, system controls and the MPRIS capability probe, all
into one ranked list. `step_1..step_3` replacing the seam Booleans. Observation fingerprinting.
`context.recent_actions` and `is_correction`. Re-run the 116-case offline and 348-case live
evals.

**Proves:** compound commands work without a connective, and the action space is generated
rather than enumerated. Gate: the wrong-action rate must not move, and the 27 hostile-window
attacks must still be zero.

### Phase 3. Speculation. Two to four days. GATED on Phase 0.

S1 first, alone, shipped (about 60 lines, commit still at key release). S2 only if Phase 0's
release-lag and prefix-agreement numbers justify it. S3 only after the `ops.py` inverse audit
and a new eval category built by truncating corpus clips, where the observable end state must
always equal the non-speculative end state.

**Proves:** it acts as you finish the word, and the compositor animation becomes the dominant
remaining delay.

### Phase 4. App adapters. Three to five days.

`can_serve` / `capabilities` / `resolve` / `perform`. Spotify over waytify's `Search` plus
`PlayTrack` and `PlayContext`, with `spotify_player` kept as the fallback for a machine that
daemon is not on (see 4.4). MPRIS context reader. `FileManager1`. `spectacle`'s six declared
actions.

**Proves:** "play the second live version of X" works with no route in any table. Complaint 6,
answered literally.

### Phase 5. The browser. One and a half to three weeks.

Native host, then `snapshot()`, then tier 1, then tier 2 if the Phase 0 spike passed. Then delete
the browser half of `recipes.py`; `open_url` becomes `chrome.tabs.create` or `chrome.tabs.update`.

**Proves:** complaint 2 is gone. "Click the second result", in the profile that holds your logins.

Deleting `recipes.py` is not work. It is the reward for finishing the rest.

**Cross-cutting, all cheap, do them whenever:** pin `jev-1.13.0` rather than a floating latest;
hold the warm HTTP/2 connection (the prewarm GET costs 206 to 301 ms and cuts the first request
from about 500 ms to about 320 ms [M]); force IPv4 or fix `/etc/resolv.conf`, since about 580 ms
of an apparent cold-DNS cost is the glibc stub resolver walking `search Home home` and asking for
both A and AAAA, while the real lookup is about 3 ms [M]; restate confidence thresholds on the
peak probability, because the published formula makes them loosen as candidate lists grow; and
stop treating a Choice and a Boolean as two formulations of one question, since the vendor's own
jaggedness page shows those primitives disagreeing.

---

## 7. What it costs to run, per day

### Money

Jev is $0.042 per million input tokens, output free [D], confirmed on this machine by a
`marketCost` of 0.000011802 on a 281-token request [M]. That is $0.000000042 per token.

| Scenario | Requests/day | Tokens/request | Cost/day | Cost/month |
|---|---|---|---|---|
| Today (200 commands, ~40% reach Jev, ~1.5 shards each) | ~120 | ~800 | **$0.004** | $0.13 |
| After Phase 2 (one richer request per Jev-touching command) | ~80 | 1,800 | **$0.006** | $0.18 |
| After Phase 3 (speculation, 2.5x requests per utterance) | ~200 | 1,800 | **$0.015** | $0.46 |
| Heavy day (400 commands, 70% Jev, 3x speculation) | ~840 | 1,800 | **$0.064** | $1.92 |
| Same, with the browser candidate table | ~840 | 2,200 | **$0.078** | $2.37 |

An independent measurement puts it at $0.1455 per 1,000 queries [P, `jev-certify`], which lands
in the same place. **Money is not a design constraint anywhere in this plan.** Latency and rate
limits are.

Not counted, and it should be: Spotify Premium, which the Web API playback endpoints require.
And the cloud STT fallback on failed local decodes, which is priced per audio minute and was not
measured here, so no number goes in this table.

### CPU and battery

The prefix ladder is the only new CPU cost. Three rungs on a 1.5 s command is about 450 ms of
one core (99 + 122 + 225 ms measured at loadavg 11 [M]) spread over roughly 650 ms of wall time.
At 200 commands a day that is about 90 core-seconds, well under 0.001 kWh on a 15 W part. The
energy is irrelevant. What is not irrelevant is *when* it runs: while the compositor may be
animating. So it must be a lowered-priority thread that stops the instant the key is released
and hard-stops past 2.5 s of buffer.

Idle cost is unchanged, because nothing runs unless the key is held. Memory is unchanged:
`parakeet-110m` is already resident at about 394 MB and each rung reuses the same recognizer.
Reading AT-SPI is local and effectively free. The content script's `MutationObserver` at a
250 ms debounce is under 6% of one core [M].

---

## 8. Risks, and what would falsify this plan

| # | Risk | Evidence it is real | What falsifies it, and when |
|---|---|---|---|
| R1 | The key-release lag is short, so speculation hides almost nothing | **Unmeasured by anyone.** The entire S2/S3 argument rests on it | Phase 0. If the median is under 150 ms, cut Phase 3 to S1 only and say so publicly |
| R2 | Prefix decodes are confidently wrong at exactly the acting horizon | At the 1.4 s rung, deterministically across two runs, Parakeet decoded "wish to say" where truth was "wish to see". Non-empty prefix correctness in the 0.85 to 2.0 s band is 5 of 7 on that clip [M]. Zero hyprsay commands have ever been laddered | Phase 0. If agreement across two consecutive rungs is below 90% on `(action, target, tier)` over the recorded commands, never act early; use speculation only to pre-warm |
| R3 | Tier-2 compositor clicking into Chrome does not work under Wayland | **[U], untested by anyone.** Unfocused clients, pointer constraints and fractional scale can each break the mapping | Phase 0 spike, half a day. If it fails, the browser becomes tier 1 plus opt-in tier 3 with a banner, and the headline advantage over existing extensions disappears |
| R4 | Payload growth crosses the 503 knee | 1 of 30 at 6k tokens, 13 of 30 then 4 of 25 at 11.5k [M]. The estimator's own docstring warns it runs low on paths, Unicode and long identifiers, and low is the dangerous direction [S]. 120 French labels measured at 2,674 tokens, 1.5x the cap [M] | Instrument `tokens.estimate` and `tokens.Drift` on every real request from Phase 2 onward, alert above 1,800. If real requests routinely exceed it, cut the candidate list before cutting the guards |
| R5 | French | Every local number here is English, Parakeet 110M is English-only, and nobody anywhere has measured Jev on French | Phase 0's 150 clips, half French. If local WER is unusable, the hybrid cloud path becomes the default rather than the fallback, which changes both the privacy story and the budget (fastest cloud model measured 452 ms [M]) |
| R6 | Jev's tail | p50 0.357 s, p95 0.558 s, **max 20.041 s** across 2,412 journalled decisions [P] | Already true. Not falsifiable, only mitigable: a hard deadline plus a local fallback, which hyprsay already has. Keep it |
| R7 | Jev says 1.0 and is wrong | 56.4% of answers at exactly 1.0, 9 of those wrong over 860 [P]. Selective error 2.65%, Wilson 95% [1.40%, 4.97%] | Not falsifiable. Consequence: tier 2 and tier 3 guards stay in code permanently, and confidence never authorizes anything |
| R8 | A larger, more heterogeneous option list makes Jev *worse*, not better | No measurement of Jev accuracy versus option count exists from any source. The "confidence thins past 25 options" claim traces to a three-option Pong question and is unsupported | Run both eval suites against the new request shape in Phase 2, **before** deleting anything. If the wrong-action rate rises above today's, or any of the 27 hostile-window attacks flips, the shape is wrong and the list gets smaller |
| R9 | The whole thing ends up slower, and you feel it | Every phase adds state to the request and work to the hold | Publish a p50 and p95 key-up-to-action column in the eval harness from Phase 1. If p50 rises above today's roughly 450 ms at any phase gate, stop and cut |
| R10 | Extension distribution | Unpacked developer-mode is a poor experience; the Web Store means review, a developer account and a release cadence this project does not have | Decide it **before** writing Phase 5 code, not after |
| R11 | Your platform is getting crowded | `gh search repos "omarchy voice"` returns ten voice controllers for a Hyprland distribution, several created in the last week [M]. None mentions Jev, but they exist. Meanwhile Talon 1.0 shipped 2026-09-20 and its changelog says it is the last public release for Linux/X11 [D] | Not falsifiable, and it cuts both ways: the strongest competitor is leaving the platform, and newcomers appear weekly. The differentiators are the guarded execution model and real in-app reach, not being alone |

### The three sentences that would make me abandon parts of this

1. If Phase 0 measures a key-release lag under 150 ms, **S2 and S3 are not worth building** and
   the honest answer on "acting while you speak" is that push-to-talk already gives you most of
   it and the compositor animation is what you are actually feeling.
2. If the tier-2 compositor click fails, **the browser plan loses its distinguishing feature**
   and becomes a competent implementation of what `chy4pro/jev-for-chrome` already does.
3. If the affordance-table request shape raises the wrong-action rate on the existing 348-case
   live eval, **the whole section 2 architecture is wrong for this problem** and the right move
   is a larger grammar with better grounding, not a larger option list.

None of those three is likely. All three are cheap to test. Test them first.
