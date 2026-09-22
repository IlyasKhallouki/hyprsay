# Lane: intelligent agent loops, and where a non-generative model fits

Research date 2026-09-22. Target machine for every cost estimate: i5-8350U (4 cores, 8 threads,
no GPU), 23 GiB RAM, Arch Linux, Hyprland 0.56.2, French AZERTY, European network.

Label legend, used on every claim:

- **[P]** primary: the project's own repository files fetched today, vendor documentation, or a
  paper's own abstract/text.
- **[S]** secondary: a third party writing about someone else's work.
- **[V]** vendor claim: true primary source, but a marketing or self-reported number.
- **[M]** measured by me on this machine today.
- **[I]** my inference. Never presented as a finding.

Status: complete.

---

## 0. The one project that matters most

**browser-use/jev-ultrafast**, created 2026-09-16, 16,856 stars, 1,064 forks, last push
2026-09-18, Python, org `browser-use` (the same org as browser-use/browser-use) [P, GitHub API
via `gh api repos/browser-use/jev-ultrafast`, fetched 2026-09-22].

This is the viral Jev project the owner is thinking of, it is not on Hyprland, and it solves
precisely the two complaints "it cannot click inside pages" and "no pre-baked routes". Its own
README states the design in one line:

> "A browser agent with a dynamic, indexed action space." ... "There are no site-specific action
> scripts or prepared field strings in the policy." [P, README]

I read `jev_ultrafast/agent.py` (174 lines), `model.py` (198 lines) and `questions.py` (26 lines)
in full. The mechanism, in enough detail to rebuild it:

### 0.1 The loop

```
observe(page) -> element table -> one Jev request -> operation + target -> execute -> observe
```

`agent.py` `command("tick")` is literally `predict` then `act`. There is no plan, no scratchpad,
no chain of thought, no LLM in the loop at all unless text must be typed. `MAX_STEPS = 60`
actions and `MAX_STEPS * 2` model calls bound the run [P, questions.py, agent.py:75,102].

### 0.2 The candidate generator is code reading live state

`model.action_space(state["actions"])` walks the DOM snapshot and emits, for each interactive
node, one integer index and the list of operations that node supports [P, model.py:48-78]. The
element table looks like this in the README:

```
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
```

This table is rebuilt on every observation. Nothing is registered in advance. This is the answer
to "open-ended behaviour from a chooser": **the option set is a function of live state, computed
by ordinary code, and it changes every step.**

### 0.3 Speculative fan-out over heads, one round trip

The request contains one `operation` Choice whose options are exactly the operation kinds that
some element on this page supports, plus `DONE` and `BLOCKED`, and then **one target Choice per
operation** (`click_target`, `type_text_target`, `select_target`), each containing only the
elements compatible with that operation [P, model.py:91-106].

Code reads only the head the operation selected:

```python
if operation in targets:
    # Unused target heads cannot cause an action. Validate the head selected by the operation.
    target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", ...))
```
[P, model.py:125-127, comment verbatim]

So a two-level decision (what to do, and to what) costs **one** network round trip, and the
unused heads are free of consequence. This is TypeSafe's documented "speculative fan-out" pattern
applied to an action space rather than to slots.

### 0.4 The generative model appears in exactly one place

`TYPE_TEXT` is the only operation whose execution needs a string that is not on screen. When and
only when Jev picks it, `field_text()` calls an OpenAI-compatible chat model with
`response_format: {"type": "json_object"}` and a system prompt that demands **a JSON object with
exactly one key `text`** [P, model.py:160-198, questions.py:21-24]. The validator rejects anything
else:

```python
if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
    raise ValueError()
```
[P, model.py:190-191]

The contract is worth copying verbatim: the generative model never returns an action, a selector,
a coordinate or a command. It returns one field value, which is then typed into a target that Jev
already chose and that code already validated. The README says it directly: "Model output never
becomes selectors, coordinates, shell commands, or executable JavaScript."

### 0.5 The typed answer is validated like untrusted input

`validate_choice` checks that the returned `choice` is in the id set, that the probability keys
are exactly the id set, that every number is finite in [0,1], that the mass sums to 1 within
0.02 (the documented rounding), and that the argmax really is the returned choice [P,
model.py:30-45]. Any failure raises "Invalid TypeSafe response; no action executed."

### 0.6 Freshness, not confidence, is the main guard

There is no confidence threshold anywhere in `agent.py`. The safety property comes from
**fingerprints**: a decision is stamped with `page["fingerprint"]`, `act` refuses to run if the
fingerprint moved, `StalePage` forces a re-observe and a re-decide, and the decision is consumed
before any mutation so "a retry cannot double-click" [P, agent.py:88-96,108,116, comment
verbatim]. A three-step no-change loop sets status `blocked` [P, agent.py:153-158].

### 0.7 What they measured

All from `docs/performance.md` [P, self-reported by the authors on their machine, not reproduced
by me]:

- Google Flights, natural-language goal, verified independently: **7.073 s** end to end.
- **Median Jev latency 178 ms**; 17 Jev requests in the recorded run; 10 interactions plus one
  explicit WAIT; two text-helper calls.
- Text helper (`inception/mercury-2.5` through OpenRouter): **Zurich in 581 ms, London in 346 ms**,
  total **$0.00006272** for the two calls.
- 90,558 TypeSafe input tokens and 6,325 output tokens across all requests in the run. That is
  about **5,300 input tokens per request** [I, arithmetic on their figures].
- Matched A/B, 3 pairs: median 9.450 s to 7.092 s, 25.0% lower, median browser protocol calls
  1,092 to 101. The authors themselves write "Three pairs are too few for a strong statistical
  claim (two-sided sign-test p = 0.25)."
- Wikipedia article open in 2.798 s; local hotel filter task 1.896 s.

Honest limits they publish: "A valid operation can still be wrong, and DONE is never independent
evidence of success." No shadow roots, frames, canvas, uploads, new tabs.

### 0.8 The four things hyprsay should take from it

1. **Rebuild the option set from live state on every step.** A recipe table is a static action
   space; an element table is a dynamic one. Same model, completely different ceiling.
2. **One request, operation head plus one target head per operation.** Two decisions, one round
   trip, and the compatibility constraint ("only CLICK targets appear under click_target") is
   enforced by construction rather than by a cross-check after the fact.
3. **Generation is a leaf, not a loop.** One call, one JSON key, validated, never an action.
4. **Guard on state identity, not on model confidence.** Fingerprint the observation, refuse to
   act on a stale one, consume the decision before mutating.

---

## 1. What an agent loop actually is, and which of its forks are decisions

The useful framing comes from **zjunlp/JevLoop** (created 2026-09-20, 12 stars, 100+ commits,
pushed today) [P, README fetched 2026-09-22]. Its opening line is the thesis of this whole lane:

> "Every fork in your agent loop is a full LLM call. Not one of them is generation."
> "*Should I act? Which tool? Which file? Is this safe? Did it work? Am I done? Can I ship this?*"

It enumerates the loop's forks and what each conventionally costs:

| Question the loop asks | Conventional agent | JevLoop |
|---|---|---|
| Do I need to act yet? | LLM call | decision |
| Which tool? | LLM call | decision |
| Is this call safe? | LLM call, or nothing at all | decision |
| Did it work? | LLM call | decision |
| Am I done? | `max_iter` counter | decision |
| Can I ship this answer? | **nothing** | decision |

[P, README table, verbatim]

The named families in the research literature map onto this cleanly:

- **ReAct** (Yao et al. 2022, arXiv:2210.03629) interleaves a free-text "thought" with an action.
  The thought is generation used as a decision procedure. Most of it is the table above.
- **Planner and executor splits** (HuggingGPT, arXiv:2303.17580, is the canonical one: a planner
  LLM decomposes a request into tasks and *selects models from a catalogue* for each) separate
  "what is the sequence" from "what is the next call". The selection half is a Choice over a
  catalogue; the decomposition half is not.
- **Code as the action space**: CodeAct (Wang et al. 2024, arXiv:2402.01030) replaces the
  JSON-action menu with executable Python. Abstract claim: "CodeAct outperforms widely used
  alternatives (up to 20% higher success rate)" across 17 LLMs [P, arXiv abstract]. This is the
  strongest known way to get *composition* out of a model, and it is exactly what a chooser
  cannot do, because writing a program is generation by definition.
- **Skill libraries / hierarchical decomposition**: Voyager (Wang et al. 2023, arXiv:2305.16291)
  keeps "an ever-growing skill library of executable code for storing and retrieving complex
  behaviors", reporting "3.3x more unique items", "2.3x longer distances", "up to 15.3x faster"
  tech-tree milestones [P, arXiv abstract]. The *retrieval* from the library is a Choice; the
  *authoring* of a new skill is generation.
- **Retrieval over a capability index**: rather than putting every tool in the prompt, index them
  and fetch on demand. Anthropic's engineering post on code execution with MCP reports the
  concrete win: "This reduces the token usage from 150,000 tokens to 2,000 tokens, a time and
  cost saving of 98.7%" [P, anthropic.com/engineering/code-execution-with-mcp]. I am running
  inside a live instance of this pattern right now: this session was given roughly 150 deferred
  tool names with no schemas, and a `ToolSearch` call that loads schemas by query [M, observed in
  this session's own system prompt].

**The split that matters for hyprsay.** Of the six forks above, five are picks, scores or
yes/no questions over sets that code can build. One, "write the thing that is not on screen", is
not. That is the whole of the answer to the owner's "no pre-baked routes" demand: you do not need
a generative model to get open-ended behaviour, you need an **open-ended option set**.

---

## 2. Candidate generation is the whole game

### 2.1 The principle, stated plainly

A chooser's reachable behaviour is exactly the set of options its caller can construct. Therefore:

- A **static table** of options (hyprsay's `recipes.py`, a 40-pattern grammar) gives a fixed
  action space, and the model's only freedom is picking a row. That is what the owner is
  complaining about, and he is right: no amount of model quality fixes it.
- A **generator that reads live state** gives an action space that is different on every step and
  was never enumerated by the author. jev-ultrafast's element table is 30 rows on one page and 80
  on the next, and no line of its code knows what Google Flights looks like.

The difference is not model capability. It is one function: `candidates(state) -> list[option]`.

### 2.2 Six candidate generators available to hyprsay today, measured on this machine

All measurements [M] taken 2026-09-22 on the target laptop, read-only.

| # | Generator | What it enumerates | Live check on this machine |
|---|---|---|---|
| 1 | Hyprland IPC | windows, workspaces, monitors, layers | already in hypruse; socket query p50 0.19 ms per earlier lane [P, docs/research/nlu-arch.md §1.8] |
| 2 | `.desktop` scan | installed apps, **their declared Actions**, **their URI schemes** | 236 files in `/usr/share/applications`; **14** declare `x-scheme-handler`, **9** declare `Actions=` (e.g. `new-window;new-private-window`, `NewChat;NewCode`) [M] |
| 3 | AT-SPI tree | named controls inside a running app | registry **does** activate now: `org.a11y.Bus GetAddress` returns `unix:path=/run/user/1000/at-spi/bus_1`; `org.a11y.Status IsEnabled = true`, `ScreenReaderEnabled = false`; 15 apps on the bus; **waybar exposes 88 nodes**; **Google Chrome exposes 3 frames and no content tree** [M] |
| 4 | MPRIS / D-Bus | media players, their capabilities, current track | live right now: `org.mpris.MediaPlayer2.chromium.instance1325064`, `plasma-browser-integration`, `playerctld`; `playerctl metadata` returns real fields [M] |
| 5 | A web API result | e.g. the 20 tracks a search returns | `spotify.desktop` declares `Exec=spotify --uri=%u` and `MimeType=x-scheme-handler/spotify` [M] |
| 6 | Browser DOM/CDP or an extension | every control on the current page | Chrome runs here with `--ozone-platform=wayland` and no debugging port [M] |

### 2.3 The owner's own example, solved with no generation and no pre-baked route

"Pick a song on Spotify" decomposes into:

1. Code slices the query span out of the transcript after the carrier verb ("play **the weeknd
   blinding lights**"). This is hyprsay's existing dictation-span machinery, not a model.
2. Code calls a **search API** and receives N real candidates, each with a title, artist, album
   and a `spotify:track:...` URI. The candidate set did not exist five seconds ago and is not in
   any table.
3. **Jev picks one** with a Choice over those N options, descriptions being `{title, artist,
   album, year}`. This is precisely TypeSafe's documented "IDs pointing into state" pattern.
4. Code plays the winner by handing the chosen URI to the vendor's own entry point:
   `spotify --uri=spotify:track:<id>`, which the distribution's own `.desktop` file declares [M].

Nothing in that chain is a key sequence, a hardcoded `ctrl+L`, or a model-authored string. It is
the jev-ultrafast shape with a REST result in place of a DOM snapshot. The same skeleton gives
"open the pull request about the socket timeout" (GitHub search), "play the episode about X"
(podcast client), "call the second one" (contacts).

**Cost on this machine** [I, arithmetic over measured rates]: one HTTPS search call from Europe,
call it 200 to 400 ms; one Jev Choice over 20 options with short descriptions, about 700 to 1,200
input tokens, about 225 ms p50 by jev-use's measurement (§4.2); one `spotify --uri` exec, a few
ms. Total roughly 500 to 800 ms after end of speech, all of it network. Money: about
$0.00005 per command at Jev list price ($0.042/Mtok in).

### 2.4 The two generator classes that need work before they pay

**AT-SPI (generator 3).** The premise written into `recipes.py`'s docstring, that the AT-SPI
registry "will not even activate" on this machine, no longer holds: it activates and answers [M].
But the content tree of the one application the owner most wants to click inside, Chrome, is
still empty: three frames, zero named actionable descendants [M]. The two known levers are
`--force-renderer-accessibility` on the Chrome command line, or a screen-reader signal on the
session bus (`ScreenReaderEnabled` currently reads `false`). Neither was exercised here, because
both change the desktop. Treat "flipping one of these makes Chrome's tree appear" as **untested**
on this machine.

**Browser DOM (generator 6).** jev-ultrafast gets its element table from a Chrome DevTools
Protocol connection, one `snapshot.js` call per observation, and reports the payoff of making it
atomic: median browser protocol calls fell 1,092 to 101 [P]. On this laptop Chrome is already
running with no debugging port, so CDP means either restarting the browser or a second profile. A
Chrome *extension* with native messaging avoids the restart and is how the browser tooling in
this very session reaches pages. Either way the output is the same artefact: a numbered table of
roles, names and values.

### 2.5 The design rules a candidate generator must follow, from measured failures

- **Prune hard, and rank before you cut.** jev-ultrafast sends only visible controls and the
  element's own role/value/checked/expanded; it does not send the DOM [P]. TypeSafe's own docs
  say accuracy falls as state grows with irrelevant content.
- **Watch the option count, because confidence gets structurally thin.** jev-use measured that
  "over 25 links a correct pick can carry margin 0.02 to 0.08, so most such steps flag `unsure`
  (69 of pong's 86 decisions escalated)" [P, bench/RESULTS.md]. For hyprsay this is the single
  most important number in this report: **an a11y element table of 25+ rows will produce correct
  picks with margins too small to gate on.** The fix is not a higher threshold; it is fewer,
  better options (fuzzy prefilter against the utterance first) plus the numbered-badge fallback
  that hyprsay already has.
- **Check that every option can actually win.** jev-use found Jev "answered `stay` zero times in
  80 calls, perfect on move states, 0 for 13 on hold states", and draws the right lesson: "A
  model that never selects one of your options fails silently; check the answer distribution, not
  only the accuracy" [P]. hyprsay should log the per-option win rate of `none`, `none_mentioned`
  and every rarely-chosen intent.
- **Make compatibility structural.** In jev-ultrafast each operation gets its own target head
  containing only elements that support that operation [P, model.py:94-106]. Compare hyprsay's
  current shape, where an intent Choice and a window Choice are independent and code must
  cross-check them afterwards. Per-intent target heads make the illegal combination
  unrepresentable, at the cost of a few hundred extra input tokens.

---

## 3. Where a generative model is genuinely required

Three jobs, and only three, survive the test "code cannot enumerate the answer and it is not on
screen":

1. **Text that must be authored.** A search query the user did not utter verbatim, a commit
   message, a reply. jev-ultrafast reduces this to one call returning `{"text": ...}` and
   validates the shape before typing [P]. Measured on their run: 581 ms and 346 ms per call with
   `inception/mercury-2.5`, $0.00006272 for two calls [P].
2. **A novel plan, when the step sequence is not a fixed shape.** "Open the PDF I downloaded
   yesterday, extract the table and paste it into the spreadsheet" is not a clause chain over a
   closed intent set. Note what the evidence says about this: jev-ultrafast has **no planner at
   all**, only a per-step choice with a goal in the instructions, and it completes a 10-interaction
   Google Flights task in 7.073 s [P]. Greedy per-step choice with the goal carried in the
   instruction is a much larger fraction of "planning" than it looks.
3. **Naming something not on screen.** "Move it next to the thing I was working on before lunch"
   requires resolving a reference no candidate list contains.

Cost of adding a generative model on top of Jev, on this machine, through the gateway the owner
already has a key for [I, arithmetic from measured third-party latencies; not measured by me]:

| Shape | Added latency | Added money |
|---|---|---|
| One short JSON-only completion from a fast small model | 350 to 1,100 ms (jev-ultrafast measured 346 and 581 ms; jev-use measured "~0.8 to 1.1 s each" for its LLM writes) | about $0.00003 per call (jev-ultrafast measured $0.00006272 for two) |
| A constrained-enum decision from a frontier-small model instead of Jev | p50 691 ms (haiku-strict) or 1,027 ms (gemini-strict) versus Jev 225 ms | $0.30 or $0.09 per 1,000 judgments versus Jev $0.018 |
| An unconstrained agent-style LLM call per loop step | p50 2,874 ms (haiku) to 6,406 ms (gemini) | $1.67 to $2.60 per 1,000 judgments |

[P, all latency and dollar figures from shitianfang/jev-use `bench/RESULTS.md`, measured
2026-09-19 from one Linux dev container through one Vercel AI Gateway key, 40 fresh states per
arm, run twice]

Read that table twice. The honest conclusion the jev-use author draws himself is:

> "Jev's latency lead over a *properly configured* baseline is **3.0 to 3.1x**, not 14x. ... What
> survives the fair fight is **cost** (16x cheaper than constrained Haiku) and **answer shape**:
> the verdict is inside the option set by construction instead of parsed out of prose." And:
> "**Decision quality is a wash** (all five arms 24 to 30 of 40 against a geometric reference)."

For a voice UI on a 400 ms to 1 s budget, 3x on the critical path is still the difference between
usable and not. But the argument for Jev is not "the LLM cannot do it". It is latency, price and
the fact that an out-of-set answer is impossible.

---

## 4. Hybrid shapes, and what was actually measured for each

### 4.1 Jev as a pre-execution gate on a generative agent (measured, strongest evidence)

`jev-use hook gate` judges every shell command a coding agent wants to run, before it runs, from
a `PreToolUse` event [P]. Their matched comparison, 24-command session, two runs per lane:

| Lane | Wall s | s/decision | LLM tokens | $ | dangerous stopped | benign allowed |
|---|---|---|---|---|---|---|
| supervisor LLM | 47.6 / 46.1 | 1.98 / 1.92 | ~931,000 | 0.2339 / 0.2393 | 8/8 | 16/16 |
| Jev hook | 17.1 / 17.2 | 0.71 / 0.72 | **0** | 0.000464 | 8/8 | 15/16 |

"Per blocking decision, all-in: **$0.009857 (supervisor LLM) vs $0.0000194 (Jev), 510x cheaper**,
at ~340 ms of gateway latency" [P]. They verified zero LLM usage by pointing `ANTHROPIC_BASE_URL`
at a closed port. Their own counterweight: the supervisor was 24/24, the hook missed one benign
command (`sed -i` on tracked source) both runs, and the miss was an over-refusal, never an
over-permit. **Direct read-across to hyprsay: this is exactly hyprsay's tier gate, and the
measurement says a model gate at this price can be run on every single action.**

### 4.2 Jev as verifier on a generative model's output, with repair (measured)

jev-use's `collab.mjs` drives OpenStreetMap directions: "Jev clicks, the LLM types". Measured
live 2026-09-19: "10 Jev decisions (p50 274 ms) · 4 LLM writes (~0.8 to 1.1 s each) · 20.7 s to
the real 3.7 km route; the wrong-geocode route was rejected by Jev's goal check (0.33, `unsure`)
and repaired by the LLM, unstaged, occurred in **8/8 live runs**" [P].

Mechanically: after the generative model supplies a field value and the value is entered, a Jev
`check` asks a goal-level question about the resulting page. A low answer sends the field back to
the model with the observed evidence attached. Second attempt verified at 0.94 [P]. This is the
one place in the whole survey where a generative output is *checked by the chooser* rather than
trusted, and it reproduced in every run.

### 4.3 The escalation contract (measured, and the cleanest API shape found)

`jev-use` returns, per answer, `{answer, confidence, confidenceFrom, escalate}` where
`escalate: true` carries a typed reason and means "the LLM must take this one" [P, README]. Over
454 judgments on 422 real states across 5 families [P, bench/RESULTS.md]:

- agreement with reference 82.2% overall,
- **14.1% escalated**,
- **agreement among non-escalated verdicts 89.5%**,
- majority-class baseline 68.7%,
- whole corpus $0.0051, 77 s, p50 232 ms per call.

And the sentence that justifies the whole pattern: "**Escalation is doing real work:** the
verdicts Jev escalated would have been right 51% of the time; the ones it acted on, 87.5%."

Two further details worth stealing. First, the threshold follows the *provenance* of the
confidence number: 0.5 when Jev reported it, 0.4 when it was estimated from the distribution [P].
Second, their probe over 318 `choice` answers found the reported confidence equals
`(p_top - 1/n) / (1 - 1/n)` "to a maximum residual of 0.015, exactly the wire's 2-decimal
rounding" [P]. That **independently confirms the formula hyprsay's own `jev-design.md` §7.2
derived as an inference**, and it also confirms the corollary: for `score` questions beyond two
levels no function of the returned distribution reproduces the head (median residual 0.11, max
0.285), so score confidence is not recomputable client side.

### 4.4 Jev as a model router in front of generative models (real, but thin evidence)

LangChain's own post is explicit about the division of labour: "use an LLM for open-ended
reasoning and generation, and Jev for fast, structured decisions", with Jev in three roles,
model router, safety gatekeeper ("AutoModeMiddleware ... uses Jev to check tool calls for risky
decisions") and parallel decision evaluator [P, langchain.com/blog/building-a-harness-with-jev].
It publishes no latency or accuracy measurement of its own; the only numbers it carries are
TypeSafe's vendor claims [V]. Several router projects exist (`jev-router`, `pi-jev-router`) but I
did not verify any of them, and routing is not on hyprsay's path anyway.

### 4.5 Speculative execution of a Jev answer while a generative model confirms

**Not found.** I searched for a project that acts on the fast chooser's answer and rolls back
when a slower generative model disagrees, and found none in the Jev ecosystem. What exists is
adjacent and worth naming precisely:

- *Within one request*: jev-ultrafast's speculative target heads (§0.3). Real, measured, and the
  cheapest speculation available, because it costs tokens rather than a round trip.
- *Within one utterance*: jev-voice-browser fires a request on every partial transcript, 200 ms
  debounce, at most 2 in flight, older ones aborted, and "a response for a partial transcript may
  still act if the words already commit to a closed-set action ('go back'), but is never treated
  as final for free text" [P, its README, per hyprsay's own prior-art lane]. **This is the
  mechanism behind "it is already doing stuff as they are talking."** It is not clairvoyance; it
  is a request per stable partial plus a rule that only lets reversible, closed-set actions
  commit early.
- *In the decoding literature*: speculative decoding (Leviathan et al., arXiv:2211.17192) is the
  formal version, a cheap draft model proposing and an expensive model verifying, with the
  guarantee that the output distribution is unchanged. That guarantee does not carry over here,
  because Jev and an LLM are not two samplers of one distribution. Any rollback design for
  hyprsay must be justified by the action's reversibility, not by a distributional argument.

---

## 5. Latency arithmetic, which decides the architecture

The most important architectural number in this lane is not accuracy. It is this, from JevLoop's
own "honest numbers", which compares the same loop over three decision backends [P]:

| Decision backend | Per decision | decisions : model calls | decision share of wall clock |
|---|---:|---:|---:|
| offline rule table | 4 ms | 12 : 1 | ~8% |
| Laya `typed-decisions`, local A100 | 30 to 85 ms | 8 : 1 | ~38% |
| Jev `jev-latest`, hosted API | ~390 ms | 13 : 1 | **79%** |

Their conclusion, unsoftened: "Over the hosted API it does not [hold]. ~390 ms per decision is
network round-trips, and with 13 decisions for 1 generation the decisions dominate the clock."

hyprsay measures 315 ms median to Jev from Europe [P, its own README]. So:

- **A multi-step agent loop with one Jev call per step is the wrong architecture for a voice
  command on this machine.** Three steps is a second of network before anything moves.
- **The fix is not a faster model, it is fewer round trips.** jev-ultrafast collapses a two-level
  decision into one request by fanning out heads. hyprsay should collapse a *whole chain* the same
  way: clause-splitting Booleans, per-clause intent heads and per-clause target heads all in one
  body. Extra questions are close to free in latency and cost about 30 to 60 input tokens each
  [P, TypeSafe cookbook arithmetic recorded in hyprsay's `jev-design.md` §14].
- **Anything that can be decided from a second observation should be decided by code**, because a
  second observation means a second round trip. jev-ultrafast's freshness fingerprint is code,
  not a model call, and it is what makes a 17-request run possible instead of a 40-request one.
- Budget sketch for the deepest realistic hyprsay flow, "play blinding lights on spotify" [I,
  arithmetic over measured components]: local ASR 93 ms + search API 200 to 400 ms + one Jev
  Choice 225 to 400 ms + exec a few ms, so **520 to 900 ms after key release**, with the two
  network legs sequential because the candidates must exist before the question can be asked.
  Overlap is impossible here; the only lever is a warm connection on both legs.

---

## 6. Honest assessment: what is reachable, and with what

### 6.1 Reachable with Jev alone (plus real code)

| Owner complaint | Reachable? | The mechanism |
|---|---|---|
| Compound commands split on literal "and" | **Yes, already partly built.** | hyprsay already asks a Boolean per code-proposed seam (`nlu/clauses.py`). The remaining gap is not the split, it is that each clause then runs through a shallow intent table. Fix the action space, not the splitter. |
| Cannot click inside pages | **Yes, if a generator exists.** | The chooser is not the blocker; the element table is. Chrome publishes nothing today [M]; a CDP connection or an extension produces exactly jev-ultrafast's table, after which one Choice per operation does the rest. |
| Ignores context, opens a new window instead of focusing the existing one | **Yes, and this one needs no model at all.** | The live window list is already in state. This is a code defect in how launch-versus-focus is decided, not a model failure. §6.3. |
| Deep in-app ability, "pick a song on Spotify" | **Yes, with no generation.** | §2.3: API search generates candidates, Jev picks, a URI handler executes. |
| Acting while the person still speaks | **Yes.** | Partial transcripts, debounce, at most 2 in flight, and a rule that only reversible closed-set actions may commit before the final transcript. |
| "Nowhere near intelligent" | **Partly.** | Most of the perceived intelligence in jev-ultrafast comes from the option set being alive, plus the goal being carried in the instruction of every question. Both are code. |

### 6.2 Genuinely needs a generative model

- Typing text the user did not say (a composed search query, a message body). One JSON-only call,
  validated, never an action. 350 to 1,100 ms, about $0.00003.
- Recovering from a stuck loop: jev-use's escalation contract, where a typed `escalate` reason
  hands the step to the LLM with the observed evidence attached. 14.1% of steps in their corpus.
- Naming a target that is on no list.

### 6.3 Needs neither, and where hyprsay should spend the first week

The owner's complaint #3, "if an app is already open on another workspace, saying its name opens
a NEW one", is the most damaging of the six and it contains no model question at all. The window
list is already in state; the rule "if a window of this app exists, focus it; otherwise launch"
is four lines of code plus a policy on what "bring it here" means. Any architecture work that
ships before that fix will be judged against a system that still does the wrong thing on the
simplest sentence in the language.

Two more in this class: the undo journal (compositor actions have inverses; no model can supply
one), and the freshness fingerprint (hyprsay already re-reads state between steps; making it a
stamped precondition is code).

---

## 7. Recommendation for hyprsay, concretely

1. **Replace `recipes.py`'s role, do not delete the file.** Keep it as the fallback for
   applications with no tree and no API. Demote it from "the way to act inside an app" to "the
   last resort", and make the first resort a generator: AT-SPI for GTK/Qt (works today, waybar
   shows 88 nodes [M]), CDP or an extension for Chrome, D-Bus/MPRIS and URI handlers for media,
   `.desktop` Actions for launch-time variants (9 apps declare them here [M]).
2. **Adopt the jev-ultrafast request shape.** One `operation` Choice built from the operations
   the current state actually supports, plus one target head per operation containing only
   compatible targets, in a single request. Read only the head the operation selected. This kills
   the intent/slot incoherence problem structurally and costs one round trip for a two-level
   decision.
3. **Fingerprint the observation and stamp every decision with it.** Refuse to execute against a
   moved desktop; consume the decision before mutating so a retry cannot double-act. This is
   cheaper and stricter than any confidence threshold, and it is what makes speculative execution
   on partial transcripts safe.
4. **Add exactly one generative seam, with jev-ultrafast's contract.** A single call that returns
   one JSON key, validated, whose output can only ever become typed text into a target Jev
   already chose. Never a selector, never a dispatch string, never a shell command.
5. **Add the escalation contract as a first-class verdict.** Every Jev answer resolves to act,
   badge, or escalate; escalate is the only path that may reach a generative model; log the rate.
   jev-use's numbers say to expect roughly 14% and to gain about 7 points of accuracy on the
   steps that do act.
6. **Budget round trips, not milliseconds.** One request per utterance is the target; two is the
   ceiling (the legitimate second is "fetch candidates that did not exist before the first answer",
   for example the a11y tree of the window Jev just chose, or a search result).
7. **Measure the per-option win rate.** If `none_mentioned` never wins, or if the margin over 25
   controls is routinely under 0.08, the badge fallback is the real UI and the threshold is
   cosmetic. jev-use measured exactly that failure and it is the most likely way this design
   disappoints in practice.

---

## 8. Not found (searched, absent, and worth saying)

- **No project that speculatively executes a Jev answer while a generative model confirms.** The
  speculation that exists is either inside one request (unused heads) or across partial
  transcripts.
- **No accuracy-versus-option-count curve** for Choice from any source, vendor or third party.
  The closest is jev-use's observation that margins collapse past ~25 options, which is a
  confidence statement, not an accuracy one.
- **No measurement of Jev latency from a European consumer connection other than hyprsay's own
  315 ms median** and lindfors.no's 0.32 s median recorded in an earlier hyprsay lane. Every other
  figure in this report was measured from a dev container or a US machine.
- **No Jev project on Wayland or Hyprland that drives applications through an accessibility tree.**
  `jev-desktop` (60 stars, 7 commits, one day of pushes) claims Jev action selection inside Codex
  Computer Use; I did not verify its code and its commit history is thin enough that I will not
  present it as evidence.
- **Whether Chrome's content tree appears on this machine** when `--force-renderer-accessibility`
  is passed or `ScreenReaderEnabled` is set. Both change the desktop, so neither was tried.
- **Whether `spotify --uri=spotify:track:<id>` plays a specific track on this machine.** The
  `.desktop` file declares the interface [M]; running it would have changed the desktop.

### A note on ecosystem noise, since the lane asked for it

Jev is one week old and the ecosystem is already noisy. `yibie/awesome-jev` claims 273 entries
(1,108 stars, created 2026-09-17) and a rival `hellogumbo/awesome-jev` (132 stars) was created 67
minutes later [M, GitHub API]. Several "JevLoop" and "jevloop" names point at different projects;
two of the URLs search returned redirected to differently-named repositories [M]. I therefore
used GitHub's API for creation date, push date, star count, commit count and size on every repo I
cite, and I read the actual source of the two I lean on most. The projects I would defend as real
are: **browser-use/jev-ultrafast** (16,856 stars, a known org, source read in full),
**shitianfang/jev-use** (15 stars but 286 lines of published methodology with negative results in
it), and **zjunlp/JevLoop** (a real research lab, 100+ commits, publishes a result that undercuts
its own thesis). Everything else in this report is cited as a name, not as evidence.

---

## 9. Sources

Primary, read directly today:
- https://github.com/browser-use/jev-ultrafast : README, `jev_ultrafast/agent.py`,
  `jev_ultrafast/model.py`, `jev_ultrafast/questions.py`, `docs/performance.md`
- https://github.com/shitianfang/jev-use : README, `bench/RESULTS.md`, `docs/reference.md`
- https://github.com/zjunlp/JevLoop : README
- https://github.com/yibie/awesome-jev : `categories/agent-decisions.md`
- GitHub REST API metadata for every repository named above
- https://www.anthropic.com/engineering/code-execution-with-mcp
- arXiv abstracts: 2402.01030 (CodeAct), 2305.16291 (Voyager); cited by id only:
  2210.03629 (ReAct), 2303.17580 (HuggingGPT), 2211.17192 (speculative decoding)
- https://www.langchain.com/blog/building-a-harness-with-jev

Local, measured on the target machine 2026-09-22, all read-only:
- `/usr/share/applications` counts and `spotify.desktop` contents
- AT-SPI: `org.a11y.Bus GetAddress`, `org.a11y.Status`, desktop child walk, Chrome and waybar trees
- D-Bus MPRIS names, `playerctl --list-all`, `playerctl metadata`
- installed binaries, running Chrome process flags

Earlier hyprsay lanes reused for figures rather than re-derived:
`docs/research/jev-design.md`, `docs/research/nlu-arch.md`, `docs/research/prior-art.md`.
## Verification

---

Skeptic pass, 2026-09-22. Method: for every claim below I went to a *different* artefact than the
researcher cited where one existed (raw measurement JSON instead of the prose that summarises it,
vendor documentation instead of a third party's probe, a fresh local measurement instead of the
lane's own), and I re-ran every local measurement myself. Labels: **[P]** primary artefact I
fetched, **[M]** measured by me on the target machine today, **[V]** vendor claim, **[I]** my
inference.

**Headline: nothing in this lane is fabricated.** All three repositories exist, all three sets of
numbers appear verbatim in the artefacts cited, and the two source files the plan leans on hardest
read exactly as quoted. What the pass did find is four places where the *interpretation* is
stronger than the evidence, one local measurement that does not reproduce, and two numbers that
were in the sources and should have been surfaced because they change the architecture.

---

### V1. jev-ultrafast exists and has a dynamic action space: CONFIRMED; "no pre-baked routes" OVERSTATED

Re-fetched the repository metadata and all five source files myself [P, `gh api`, 2026-09-22].
`browser-use/jev-ultrafast`, created 2026-09-16T21:30:12Z, now **16,878 stars / 1,067 forks** (the
lane said 16,856 / 1,064 six hours earlier; consistent with growth, not with a fabricated figure),
4.4 MB, 111 open issues, 49 watchers, and real third-party issue traffic from five distinct
accounts. The repository is real and actively used.

`model.py:48-78` `action_space()` reads as described: it walks `state["actions"]`, assigns
`str(len(elements) + 1)` as the index, and carries only `role, value, checked, selected, expanded,
label`. Nothing is registered ahead of time. The README line is verbatim at line 51.

**But the claim "no site-specific action scripts" is not the same claim as "no pre-baked routes",
and the lane treats them as one.** `questions.py` `NEXT_ACTION` is a fourteen-line hand-written
policy carrying exactly the procedural web knowledge that a recipe table would carry, moved into
natural language [P, verbatim]:

> "A typed query still needs its matching autocomplete suggestion selected. For date pickers,
> CLICK the field, date, then confirmation. Set every requested filter/control; a matching result
> alone does not prove a requested filter was set. ... Submit populated search fields before
> opening a result; a populated field alone is not an applied search."

That is a route for search boxes, a route for date pickers, a route for filter panels, and a route
for autocomplete. It is *portable* across sites, which is the genuine advance, and it never becomes
a selector or a key sequence, which is the genuine safety property. It is not the absence of
prepared procedure. For hyprsay this matters concretely: the owner's demand is "no pre-baked or
pre-programmed routes", and the honest read-across is **"the routes move from a Python dict keyed
by application name into one page of instruction text that applies to every application"**, not
"the routes disappear". A plan that promises the second will not deliver it.

Second correction, same finding: `action_space` splits `state["actions"]` into elements
(`click`/`fill`/`select`) and a `controls` dict for every other kind [P, model.py:53-56], and those
controls are merged into the operation head at line 89. So the operation set is **dynamic elements
plus a fixed global set** (WAIT and navigation-style controls emitted by the snapshot, plus the
hardcoded `DONE` and `BLOCKED` at line 90). The action space is hybrid, not purely generated.

Verdict: **confirmed** on existence and mechanism, **overstated** on "no pre-baked routes".

---

### V2. The per-operation target head, one round trip: CONFIRMED, with one correction that matters for gating

`model.py:91-106` builds `questions["operation"]` plus one `<op>_target` head per operation present,
each `criteria` restricted to that operation's candidates. Line 125-127 reads only the selected
head and the comment is verbatim: *"Unused target heads cannot cause an action. Validate the head
selected by the operation."* [P]. `validate_choice` (lines 30-45) enforces every property the lane
lists. All confirmed at source.

**The correction.** Line 138 returns `"confidence": operation_answer["confidence"]`, the
**operation** head's confidence. The target head's confidence is returned separately as
`target_confidence` (line 142), and `agent.py:128` logs the operation confidence into history as
the step's `confidence`. So the single number this design surfaces as "how sure am I" answers
*what to do*, not *to what*. Since the operation head has roughly 5 options and the target head has
tens, and confidence rescales with option count (V7), the operation confidence is systematically
the flattering one. **If hyprsay copies this shape and gates on one confidence number, it must gate
on the target head, not the operation head.** The lane's finding does not say which.

Verdict: **confirmed**, with a material omission.

---

### V3. The generative leaf returns one validated JSON key: CONFIRMED; the default model is not the measured one

`model.py:160-198` confirmed line for line, including the validator at 190-191 with the 2000-char
bound, and the README safety sentence at line 105 verbatim [P].

Two corrections. First, the lane says the generative model is "an OpenAI-compatible chat call" and
attributes the measured 581 ms / 346 ms to it; the code's **default** is
`https://api.deepseek.com/v1` with `deepseek-chat` (line 164-165), while the measured run used
`inception/mercury-2.5` through OpenRouter [P, `docs/flights-measurement.json`
`models.text = ["inception/mercury-2.5"]`]. The 346-581 ms figure is a diffusion-model number and
does not transfer to the default, or to any model hyprsay would reach through the Vercel gateway.

Second, `questions.py` `TEXT_VALUE` instructs the model to return `{"text": null}` when a value is
missing, but the validator at line 190 requires `isinstance(value, str)`, so the documented
missing-value path raises `"Text helper returned no valid field value; nothing typed."` The
contract is safe, but the graceful-degradation branch the prompt promises does not exist.

Verdict: **confirmed** on the contract, **overstated** on the latency being a general property.

---

### V4. Freshness rather than confidence as the guard: CONFIRMED as fact, REFUTED as reasoning

Every mechanical detail checks out in `agent.py` [P]: no threshold anywhere in the file; the
fingerprint precondition at line 88; `state["decision"] = None` at line 91 with the verbatim
comment *"Consume once, before any mutation or model call. A retry cannot double-click."*; the
re-check before text generation at 107-108; the three-step no-change rule at 153-158 (with the
extra condition, unstated in the lane, that the three steps must not be `wait`).

**But the lane's `matters_because` is a category error.** It says state identity is "a cheaper and
stricter guard than a probability threshold". The two guard different failures. A fingerprint
proves the world has not moved since the observation; it says nothing about whether the choice was
right. The authors say so themselves in the same repository: *"A valid operation can still be
wrong, and DONE is never independent evidence of success."* [P, performance.md:55]. So the correct
statement is that **jev-ultrafast ships with no correctness guard at all** and pays for it with a
60-action budget and a blocked-after-three-no-ops heuristic. That is an acceptable trade for a
browser agent that can be watched and restarted. It is not acceptable for a voice agent that fires
compositor actions, and it is emphatically not the "precondition that makes acting on partial
transcripts safe". Freshness does not make a wrong early commit recoverable. Reversibility does,
and reversibility is a separate mechanism the lane names only in its finding 14.

Verdict: **overstated**. Adopt the fingerprint; do not let it replace a confidence gate.

---

### V5. The 7.073 s / 178 ms / A-B numbers: CONFIRMED against the raw artefact, and the framing is wrong twice

I did not rely on `docs/performance.md`; I pulled `docs/flights-measurement.json` and read the
fields directly [P]:

```
elapsed_ms = 7073 · decision_requests = 17 · decision_median_ms = 178
decision_total_ms = 3720 · tokens.input = 90558 · tokens.output = 6325
browser_actions = 11 · wait_actions = 1 · models.decision = ["jev-1.13.0"]
text_calls[0].latency_ms = 581, usage.cost = 2.93e-05
```

Every number the lane quotes is in the artefact. Recorded per-request latencies for the eleven
executed actions were 112, 120, 121, 126, 141, 143, 178, 203, 217, 247, 299 ms.

**Framing error one.** The lane calls 7.073 s "end to end". The artefact's own `timing` field says:
*"First prediction after initial homepage observation through final DONE; setup and independent
final verification excluded."* Browser launch, page load and verification are outside the clock.

**Framing error two, and this is the one a plan could be built wrongly on.** The lane reports
"Matched A/B over three pairs: median 9.450 s to 7.092 s" without saying what the control arm is.
It is **the same project's own earlier commit** (`68c077bf79caca4e817b8e8a5854b2efa0c81ff6`), with
both arms using Jev and both using Mercury, explicitly so that *"the runtime comparison does not
conflate a helper-model change with code changes"* [P, performance.md:9,20]. It is a
self-optimisation measurement about DOM-reading strategy. **It is not evidence that Jev beats an
LLM agent at anything.** With p = 0.25 on three pairs it is barely evidence of itself.

**The number the lane should have surfaced.** `decision_total_ms / elapsed_ms = 3720 / 7073 =
52.6%` [P, arithmetic on the artefact's own fields]. Even with the fan-out design, even at a
178 ms median, even on the authors' own network, **more than half the task's wall clock was
waiting for Jev.** This is the same finding as JevLoop's 79% (V6), reproduced inside the lane's own
flagship exhibit, and the lane's §5 does not mention it. From Europe at hyprsay's measured 315 ms
the same run would spend roughly 5.4 s of its 8.7 s on the network [I].

Also: the total task cost is **unknown**, not $0.00006272. The source states it plainly: *"That is
the text-helper charge, not total task cost: the TypeSafe responses contain token counts without a
billed dollar amount"* [P, performance.md:32]. 90,558 input tokens at the list price the lane uses
elsewhere is about $0.0038 for the run [I].

Verdict: **confirmed** on the numbers, **overstated** on "end to end" and on what the A/B compares.

---

### V6. JevLoop's hosted-API conclusion: CONFIRMED, and it is stronger than the lane reports

`zjunlp/JevLoop` exists: created 2026-09-20T15:21:09Z, TypeScript, 12 stars, 3 forks, pushed today
[P, `gh api`]. One caution on provenance: the lane calls it "a real research lab". The org is real
(ZJUNLP), but the repository is **122 commits by a single account, `Xubqpanda`, plus one commit by
`Ylr9933`** [P, `gh api repos/zjunlp/JevLoop/contributors`], two days old. Treat it as one
engineer's careful project published under a lab's org, not as lab output.

The quoted table is verbatim at README lines 321-323, and the conclusion at line 328 is verbatim
[P]. The lane's reading is correct.

**Two things in the same README that the lane omitted and that change the recommendation.**

1. A latency decomposition the lane does not have (README lines 347-357): by sending a request the
   server rejects during validation, over the same URL, edge and auth, JevLoop separates transport
   from compute. *"one decision, Jev: 254 ms [total] · 78 ms [compute] · 23%"* against *"one
   generation, one line: 2114 ms · 2024 ms · 96%"*, and the conclusion *"A decision costs 78 ms of
   compute against 2000+ ms for a generation, 26 to 54 times less, and then spends 254 ms
   waiting."* This is the strongest available justification for the lane's own "collapse decisions
   into one fan-out request" recommendation, because it says explicitly that the cost being
   collapsed is transport, not thinking. It should be the headline of the lane's §5, not absent
   from it.
2. The local-model escape hatch is marked **"not enough zero-shot"** in the same table (line 322).
   The lane quotes the 30-85 ms figure and the 38% share without that column. Since the target
   machine has no GPU at all, an A100-served decision model was never an option; but the plan must
   not carry an implication that a local decision model is a latency fix in waiting, because the
   one project that tried it says the quality was not there.

Verdict: **confirmed**, with two omissions that both strengthen the fan-out recommendation.

---

### V7. The Jev confidence formula: CONFIRMED by a second, independent source; scope OVERSTATED

The lane's only source is shitianfang's probe. I checked the **vendor documentation** instead
[V, docs.typesafe.ai/confidence, fetched 2026-09-22]. The confidence page gives, for a three-option
question, the formula **"(3 × largest probability − 1) / 2"**. Substituting n = 3 into the probe's
formula: `(p_top − 1/3) / (1 − 1/3) = (3·p_top − 1) / 2`. **Identical.** Two independent sources,
one of them the vendor, now agree. This is the single best-supported claim in the lane and it is
more solid than the lane itself claims.

Two caveats. The vendor presents its version as a demo approximation, not as the production
formula. And the probe's 318 answers covered *"`choice` at 2-8 options, `score` at 2-7 levels"*
[P, RESULTS.md:75]. **Nothing establishes that the identity survives at n = 25 or n = 255**, which
is precisely the regime an element table would use. Compute choice confidence client side by all
means; validate it against the returned head at your own option counts before trusting it.

Separately, the API surface the plan assumes: the vendor documents **three** primitives, *"Choice,
Score, Noul"* [V, docs.typesafe.ai], not "boolean"; questions are *"evaluated in parallel and in
isolation against the same state in one go. Adding questions barely changes the response time"*
[V]. That last sentence is the load-bearing assumption under the whole fan-out plan and **it is a
vendor claim with no independent measurement anywhere in this lane**. The nearest corroboration is
weak and indirect: jev-ultrafast asks four heads in a median 178 ms while jev-use asks one
three-option question in a median 225 ms, from different machines in different regions [I]. Before
committing to a single fan-out request carrying clause Booleans plus per-clause intent and target
heads, hyprsay should measure its own latency against question count and option count. That
measurement does not exist in public.

Verdict: **confirmed and upgraded** on the formula, **overstated** on its range of validity.

---

### V8. jev-use's fair baseline (225 ms, $0.018/1k): CONFIRMED verbatim; the number that decides a voice UI was left out

Every figure is in `bench/RESULTS.md` lines 168-189, verbatim, including the author's own
`3.0-3.1x` and "Decision quality is a wash" [P, fetched directly].

Provenance is thinner than "primary" suggests and should be stated in any plan that cites it:
**15 stars, 1 fork, 0 watchers, 15 commits, all by one author `shitianfang`, repository three days
old, empty contributors list** [P, `gh api`]. The author's methodology is unusually honest, since the
document publishes results against its own thesis, but **no one has replicated any of it**. It is
one person's self-reported benchmark, epistemically closer to a vendor claim than to independent
measurement, and the source itself closes with *"One provider, one region, one day."*

**The omission.** The lane quotes `p50 225 ms` and never quotes the **p95 of 890 ms** in the same
row (RESULTS.md:170). For a browser agent a 4x tail is a shrug. For a push-to-talk voice command on
a 400 ms to 1 s budget, the p95 is the number that decides whether the product feels broken, and
890 ms of network alone exceeds the whole budget. The same table gives `haiku-strict` p95 1,200 ms,
so **at the tail Jev's advantage over a properly configured Haiku falls from 3.1x to 1.35x** [I,
arithmetic on their table]. Any hyprsay latency budget must be written against p95, and the honest
statement of the Jev case becomes cost and answer shape first, latency second.

Verdict: **confirmed**, with an omission that inverts the conclusion for this specific use case.

---

### V9. The escalation contract, 82.2% / 14.1% / 89.5%: CONFIRMED verbatim; the "7 points gained" is inside the author's own noise band

Lines 203-211 of `bench/RESULTS.md` match the lane word for word, including the
*"Escalation is doing real work"* sentence [P].

**The refutation is in the paragraph immediately below the table, which the lane does not quote**
(RESULTS.md:223-228):

> "the reference is `claude-opus-5`, not ground truth, and LLMs agree with LLMs [em dash in source] on a 45-item
> subsample claude-haiku-4.5 agreed with the reference 35/45 where Jev agreed 33/45, and a hand
> audit of 34 reference labels disagreed with 3 (8.8%), so **differences under ~9 points are within
> reference noise**."

The lane's `matters_because` promises "about 7 points of accuracy gained on the steps that do act".
89.5% − 82.2% = 7.3 points. **The author's own stated noise floor is ~9 points.** The headline
benefit of the escalation contract is, by the measurement's own terms, not distinguishable from
noise. The pattern may still be right; the number must not appear in a plan as a forecast.

**Second refutation: the corpus contains nothing resembling hyprsay's task.** The five families are
110 gated shell commands, 73 completion checks against real exit codes, 120 Hacker News rows, 87
transcript keep-or-drop messages, and 32 commit triages [P, RESULTS.md:195-197]. Not one is a
selection of a UI target from live state, which is the only decision hyprsay actually needs to make
well. Per-family agreement ranges from `completion` 100% to **`compact` 56.3%, below the 68.7%
majority baseline** [P, RESULTS.md:213-221]. An 89.5% average over that spread predicts nothing
about a sixth family.

**Third, and it undercuts the gating idea generally:** *"A separate calibration experiment (8
hand-labeled 'clear' vs 8 'borderline' ship/hold states) did not separate: Jev answers decisively
on states a human labels borderline ... Treat `unsure` as a coarse signal"* [P, RESULTS.md:83-87].
Confidence did not track genuine difficulty in the one experiment designed to test it.

Verdict: **overstated**. Adopt the three-verdict shape because it is structurally right; do not
budget for 14% escalation or for 7 points.

---

### V10. "Confidence is structurally thin over 25 options": CONFIRMED as a quote, and the supporting evidence does not support it

RESULTS.md:257-262 is verbatim [P]. But read what is inside the parentheses. The "69 of pong's 86
decisions escalated" clause is offered in the same bullet as evidence for the many-option claim,
and **Pong is a three-option question**: the same document reports Jev *"answered `stay` zero times
in 80 calls [em dash in source] perfect on move states, 0 for 13 on hold states"* [P, RESULTS.md:186-187], so the
options are up/down/stay. An 80% escalation rate on a *three*-option question is evidence that
those particular states were near-tied, not evidence about option count. The source conflates them;
the lane inherits the conflation.

The "over 25 links, margin 0.02-0.08" observation carries no n, no task and no table anywhere in
the document, and the 318-answer probe that *is* systematic covered only 2-8 options [P,
RESULTS.md:75]. **There is no measurement, from any source, of Jev confidence or accuracy as a
function of option count.** The lane says as much in its own §8 ("No accuracy-versus-option-count
curve") and then leans on the 25-option claim in §2.5 as "the single most important number in this
report". Both cannot be true.

One more thing the lane omits which points the other way: *"The reported head rescales for option
count and reads higher"* [P, RESULTS.md:260-262]. Since hyprsay would gate on the reported head, not
on a raw margin, the practical thinness is smaller than 0.02-0.08 suggests. The fuzzy-prefilter
recommendation stays right on token-cost and accuracy grounds; its confidence justification is
unsupported.

Verdict: **unverifiable**, and internally inconsistent with the lane's own §8.

---

### V11. Local AT-SPI state: PARTIALLY REFUTED by direct re-measurement

I re-ran every local check rather than trusting the lane's [M, 2026-09-22, all read-only].

| Lane's claim | My measurement | Verdict |
|---|---|---|
| `org.a11y.Bus GetAddress` returns `unix:path=/run/user/1000/at-spi/bus_1` | identical | confirmed |
| `IsEnabled = true`, `ScreenReaderEnabled = false` | `b true`, `b false` | confirmed |
| "Fifteen apps are on the bus" | **13** | not reproduced |
| "waybar exposes 88 nodes" | **88** including the app node | confirmed exactly |
| "Google Chrome exposes three frames with zero named actionable descendants" | **`child_count = 0`; 1 node total** | **not reproduced** |

The Chrome conclusion survives in the stronger direction, so the lane's architectural point stands:
Chrome publishes nothing usable and in-page clicking needs CDP, an extension, or a renderer flag.

**The claim that does not survive is the `matters_because`: "GTK and Qt apps are clickable by name
today."** The full bus contents are: `xdg-desktop-portal-gtk` ×2, `udiskie` ×2, `nm-applet` ×2,
`blueman-applet` ×2, `blueman-tray` ×2, `waybar` ×2, `google-chrome`. Every entry is a panel applet,
a tray helper, a portal, or the bar, each listed twice (which is also the likely origin of "15"
versus my 13: the count is of duplicated registrations, not of distinct applications). **Zero
user-facing applications are on the bus.** `hyprctl clients` shows what is actually running: seven
`kitty` terminals and two Chrome windows [M]. kitty is neither GTK nor Qt and exposes no tree;
Chrome exposes nothing.

So the evidence base for "AT-SPI is a live candidate generator on this machine" is exactly one
GTK layer-shell bar. It is real evidence that the GTK ATK bridge works here (`at-spi2-core
2.60.6-1` installed, `gtk3 1:3.24.52-1`, `qt6-base 6.11.1-1`, no `QT_ACCESSIBILITY` or `GTK_MODULES`
override in the environment [M]). It is **no evidence at all about Qt**, and no evidence that the
apps the owner will actually name expose useful trees, because none of them was running. That check
requires launching a GTK and a Qt application and walking their trees, which changes the desktop
and was correctly out of scope for both of us. Until it is done, treat the AT-SPI generator as
**promising and unproven**, and do not let a plan's first phase depend on it.

Verdict: **overstated**. The stale-docstring correction is right; the capability claim is not
demonstrated.

---

### V12. The Spotify path: CONFIRMED on the desktop facts, INCOMPLETE on the step that does the work

Re-measured [M]: `/usr/share/applications/spotify.desktop` declares `Exec=spotify --uri=%u` and
`MimeType=x-scheme-handler/spotify;`, owned by package `spotify 1:1.2.95.453-1`; `/usr/bin/spotify`
is a 486-byte wrapper that execs `/opt/spotify/spotify` with `"$@"`; `playerctl` is installed.
Desktop-entry counts: **234** `.desktop` files (the lane's 236 is the count of all files in the
directory, including two non-`.desktop` entries), **14** declaring `x-scheme-handler`, **9**
declaring `Actions=` ,  the two counts the lane leans on are exact.

One correction: the lane says "three MPRIS names are live on the session bus right now". `busctl
--user list` shows three, but `org.mpris.MediaPlayer2.playerctld` is marked **`(activatable)`**,
not running, and `playerctl --list-all` returns **two** players. Two are live.

**The gap.** The finding's `matters_because` is "a search API returns real candidates". That step is
the only one in the chain that produces the candidate set, and nothing about it was checked. The
Spotify Web API requires a registered application's client id and secret even for the
client-credentials search flow; `~/.config/hyprsay/` contains exactly one file, `ai-gateway.key`
[M]. So the chain as written has an unpriced prerequisite: credential registration, secret storage,
token acquisition and refresh, plus a second network leg whose latency the lane estimated
(200-400 ms) but did not measure. And whether `spotify --uri=spotify:track:<id>` starts playback in
an already-running client rather than opening a second instance remains untested, as the lane says.

Verdict: **confirmed** on the desktop facts, **unverifiable** on the mechanism that makes it work.

---

### What I could not refute, and would build on

- The `action_space` / per-operation-target-head request shape. Read at source, coherent,
  and its safety properties are structural rather than asserted.
- The generative leaf contract: one JSON key, validated, into a target already chosen.
- The confidence formula for `choice` at small n, now corroborated by vendor documentation.
- The transport-versus-compute decomposition: 78 ms of thinking behind 254 ms of waiting. Every
  latency decision follows from this one number.
- Chrome publishes no accessibility tree on this machine. Measured twice, by two people, and the
  second measurement was worse than the first.

### The three numbers a plan should not use

1. "About 14% escalation and about 7 points of accuracy gained." Inside the source's own ~9-point
   reference-noise band, from a corpus with no family resembling hyprsay's task.
2. "225 ms." Use 890 ms, the p95 from the same row, whenever sizing a voice budget.
3. "Margins collapse past 25 options." No measurement supports it; the lane's own §8 says the curve
   does not exist.

### The one number that should be added

`3720 / 7073 = 52.6%` of jev-ultrafast's flagship run was spent waiting for Jev, on the authors'
own network at a 178 ms median, with the fan-out design already in place. From Europe at 315 ms it
would be worse. Every architectural conclusion in this lane about collapsing round trips is correct,
and this is the number that proves it from inside the lane's own best exhibit.
