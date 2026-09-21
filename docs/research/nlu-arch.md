# NLU architecture for a voice-controlled Hyprland when the only model is a parallel typed classifier (Jev)

Lane: nlu-arch. Date: 2026-09-20. Author: research subagent.
Scope: how to turn a spoken command into a Hyprland action when the decision engine (TypeSafe AI Jev, `typesafe-ai/jev` on Vercel AI Gateway) cannot generate text and only answers closed-set questions (choice, score, boolean) in parallel.

Labels used throughout:
- [P] primary source (vercel.com docs/changelog/kb, docs.typesafe.ai, typesafe.ai, ai-sdk.dev, source code)
- [V] vendor claim (true primary source, but a marketing or self-reported number, not independently verified)
- [M] local measurement on the target machine, taken today
- [A] assumption or design estimate, must be validated once an API key exists
- [L] literature or production-system precedent

No live Jev call was made. No Jev latency was measured. Every Jev latency figure below is either [V] or [A].

---

## 1. Executive summary

1. Jev is a good fit for the hard 20 percent of voice command understanding (paraphrase, vague references, arbitration between candidates) and a poor fit for the easy 80 percent (canonical commands), where a compiled grammar answers in well under 2 ms [M] with zero network. The right architecture is the Snips one: a deterministic high-precision parser first, a statistical parser second [L], with Jev playing the statistical parser and also the arbiter when the grammar yields more than one parse.
2. Classic joint intent classification plus slot filling maps cleanly onto Jev: intent is one Choice; every entity slot is a Choice whose options are generated from live desktop state (windows, workspaces, installed apps, keybinds, a11y controls); graded slots ("a bit bigger") are Score; gates (addressed to me, compound, destructive) are Boolean. All go in one request because Jev evaluates questions in parallel and independently [P]. TypeSafe documents this exact pattern as "speculative fan-out" and ships a function-calling cookbook that puts the function choice and the arguments of all ten functions in one request [P].
3. Open slots (dictated text, search queries, numbers) are never produced by the model. Code generates candidate spans or candidate values from the transcript (carrier-phrase rules, number normalizer, regex), and Jev only picks among them, with a NONE escape hatch. TypeSafe documents this as "pre-parsed value extraction" and notes the value "cannot invent a value or transpose a digit" because it is copied verbatim [P].
4. Question independence is both the constraint and the superpower. Constraint: answers to related questions are not guaranteed to be mutually consistent [P]. Superpower: you can ask the same thing two ways (factorized intent plus slots, and one flat joint "action candidate" Choice) in the same round trip and use agreement as a free confidence signal; you can memoize per question; you can drop or add questions without changing the others [V].
5. The marketing word "deterministic" is not confirmed by any primary source I could find. TypeSafe's own self-consistency cookbooks report a mean per-question probability standard deviation of about 0.01 over 15 repeats, in a setup that also changes a throwaway `uid` field each run, and they state the setup "cannot separate" the two causes [P]. Design consequence: memoize locally (that gives determinism on our side regardless), and never place an action threshold where a 0.01 to 0.05 wobble flips a destructive decision.
6. The network dominates the Jev path. Locally measured: a cold connection to `ai-gateway.vercel.sh` costs 150 to 770 ms (DNS plus TCP plus TLS), a request on an already-open HTTP/2 connection returns in 46 to 51 ms (edge `cdg1`, a trivial 308 response, not a Jev call) [M]. Pre-warming is therefore mandatory, and speculative evaluation on partial transcripts is what makes a sub-600 ms Jev path realistic.
7. The biggest single latency lever is not the model, it is endpointing. Talon's default trailing silence is 300 ms [L]. With an open mic the fast path needs grammar-aware adaptive endpointing (about 150 ms when the partial already parses as a complete command) [A]; with push-to-talk, key release is the endpoint and the fast path drops to well under 100 ms after release [A].
8. hypruse currently shells out to `hyprctl` for every query and dispatch (`/home/ilyask/projects/hypruse/src/hypruse/hyprctl.py:35-47`). Measured here: subprocess `hyprctl -j activewindow` p50 21.7 ms, p95 36.0 ms; the same query over the Hyprland UNIX socket directly p50 0.19 ms, p95 0.42 ms [M]. The fork must use the direct socket on the hot path.

---

## 2. Verified facts about Jev that shape the NLU design

| # | Fact | Source | Quality |
|---|------|--------|---------|
| 1 | Three question types: `choice`, `score`, `boolean` (TypeSafe-native name for boolean is `noul`). Choice returns `choice` plus per-option `probabilities`; score returns interpolated `score` plus per-level `probabilities`; boolean returns `probability`. | https://vercel.com/docs/ai-gateway/modalities/evaluation , https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe | [P] |
| 2 | Limits: up to 255 options per choice; 2 to 10 score levels; 64,000 tokens per request, 32,000 for state; model page lists context 32,000 and max output 0. | https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk , https://docs.typesafe.ai/api.md , https://vercel.com/ai-gateway/models/jev | [P] |
| 3 | Question fields are `type`, `instructions`, `criteria`. Choice criteria is a record of option name to description; score criteria is an ordered array; boolean criteria is `{true, false}`. Instructions and criteria accept strings, objects, arrays or null; an option can be an object such as `{what, not_for, examples: [...]}` and field names are user-defined. | https://vercel.com/docs/ai-gateway/modalities/evaluation , https://docs.typesafe.ai/primitives/advanced.md , https://docs.typesafe.ai/primitives/choice.md | [P] |
| 4 | "Jev evaluates all questions in a request in parallel, so adding questions barely changes latency." "each question is evaluated independently." | https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk | [V] |
| 5 | Confidence: a 0 to 1 statistic per choice/score answer derived from the distribution shape; for three options the formula is (3 x largest probability - 1) / 2. In the AI SDK it is at `result.providerMetadata?.typesafe?.confidence`; in the TypeSafe-native response it is a `confidence` field on the answer. | https://docs.typesafe.ai/confidence.md , https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk , https://docs.typesafe.ai/api.md | [P] |
| 6 | HTTP API without the AI SDK: `POST https://ai-gateway.vercel.sh/v1/evaluate` with `model`, `state`, `questions`, `Authorization: Bearer $AI_GATEWAY_API_KEY`. TypeSafe-compatible: `POST https://ai-gateway.vercel.sh/typesafe/v1/systemone` (uses `noul`, snake_case usage). `providerOptions.gateway` supports `zeroDataRetention` and `only`. Not available on the OpenAI-compatible endpoints. | https://vercel.com/docs/ai-gateway/modalities/evaluation , https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe | [P] |
| 7 | AI SDK: `experimental_evaluate({model, state, questions, maxRetries (default 2), abortSignal, headers, providerOptions})`; result has `answers`, `usage`, `warnings`, optional `rounding`, `providerMetadata`, `response`. Requires AI SDK 7, from 7.0.105. Test double: `Experimental_EvaluationMockModelV4` from `ai/test`. | https://ai-sdk.dev/docs/reference/ai-sdk-core/evaluate , https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway , https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk | [P] |
| 8 | Python: `typesafe-sdk` on PyPI, `TypeSafeClient` and `AsyncTypeSafeClient`, `base_url` settable (also `TYPESAFE_BASE_URL`), injectable `http_client`, per-call `timeout`, `RetryPolicy(max_retries=0)` to disable retries, built on `httpx2` with HTTP/2 support. Pointing it at `https://ai-gateway.vercel.sh/typesafe` is the documented migration path. | https://docs.typesafe.ai/sdk/python.md , https://docs.typesafe.ai/sdk/python/api/clients/async.md , https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe | [P] |
| 9 | Pricing $0.042 per 1M input tokens, no output pricing. The docs example response still reports a small `outputTokens` count (20, 21). | https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk , https://vercel.com/docs/ai-gateway/modalities/evaluation | [P] |
| 10 | Known weaknesses ("Jev 1.13 jaggedness"): literal reading of scoping words and negation; "Jev is not a calculator", unreliable counting and numeric proximity; dates read as text; double negatives and indirection degrade answers; "Accuracy falls as the state grows with content unrelated to the decision"; can be steered by injected instructions inside the data; no structural invariants between related questions (P(x) and P(not x) need not sum to 1); not trained to generate. | https://docs.typesafe.ai/model-jaggedness/jev-1.13.md | [P] |
| 11 | English is primary; other languages "are accepted but currently have lower accuracy". State is text only (no audio, no images). | https://docs.typesafe.ai/concepts/state.md | [P] |
| 12 | Vendor speed numbers: "193.6x Faster", "Completed in 0.114s" vs "8.566s" for LLMs on an unspecified workflow; parallel-questions cookbook table: one call with 13 questions over a roughly 54,000-character article "0.27s" and "$0.000497" (which implies about 11.8k input tokens at the list price), versus 13 single-question calls "2.71s" (about 0.21 s each). Machine, region and route are not stated. | https://typesafe.ai , https://docs.typesafe.ai/cookbooks/parallel_questions.md | [V] |
| 13 | Repeat-run variation: choice cookbook "TypeSafe has a mean probability std dev of `0.0098` and a max single-label std dev of `0.0515`", "TypeSafe flips on 2 of the 8 questions" on deliberately borderline cases, 15 repeats; noul cookbook mean std dev `0.0102`. Each query gets a fresh throwaway `uid`, and "This setup cannot separate sensitivity to the irrelevant field from variation that would occur on identical requests." | https://docs.typesafe.ai/cookbooks/consistency_choice_cookbook.md , https://docs.typesafe.ai/cookbooks/consistency_noul_cookbook.md | [P] |
| 14 | Threshold guidance: "Read-only actions can tolerate a wrong guess, so 0.7 might be enough. Destructive actions need a higher bar, closer to 0.9 or above"; "Separate classification from authorization"; TypeSafe: "A confidence threshold is not one number"; cookbooks use a 0.60 minimum top probability and an uncertain band of 0.30 to 0.70 for booleans. | https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk , https://docs.typesafe.ai/confidence.md , consistency cookbooks | [P] |
| 15 | TypeSafe's own smart-home demo: speculative fan-out of category, domain, device type, action; a Noul asks whether the request contains more than one distinct action, and if so "the system uses an LLM to split the request"; general conversation goes to an LLM. | https://docs.typesafe.ai/demos/smart-home.md | [P] |

Design implications drawn from the table:
- Fact 10 (irrelevant state hurts) argues against dumping the whole desktop into one state. Slice state per request (section 6.3).
- Fact 10 (injection) matters because window titles are attacker-controlled text (a web page sets its own title). Titles must be truncated and must never be able to trigger a destructive tier on their own (section 9).
- Fact 10 (not a calculator) means workspace numbers, percentages and counts are parsed by code, never inferred by Jev.
- Fact 15 shows the vendor's own demo falls back to an LLM for clause splitting. We do not have one and do not want one on the hot path; section 8 replaces it with deterministic split candidates plus booleans.

---

## 3. Prior art and what to take from each

**Snips NLU** [L]. The engine "contains two intent parsers which are called successively: a deterministic intent parser and a probabilistic one." The deterministic parser is regular expressions built from the training utterances (perfect precision on seen patterns, modest recall), and the probabilistic parser (logistic regression for intent, CRF for slots) runs only when the first finds nothing. Source: Coucke et al., "Snips Voice Platform", arXiv:1805.10190, and the Snips NLU docs. Take: exactly this cascade, with Jev replacing the LR plus CRF stage, and with learned corrections compiled back into the deterministic stage.

**Rasa** [L]. DIET does joint intent plus entity prediction (Bunk et al., arXiv:2004.09936). The `FallbackClassifier` has two knobs: `threshold` (default 0.3 in source, `DEFAULT_NLU_FALLBACK_THRESHOLD = 0.3`) and `ambiguity_threshold` (default 0.1, the minimum gap between the top two intents). Source: `rasa/core/constants.py` on branch 3.6.x, fetched today. Take: gate on both the top probability and the top-2 margin, not on the top probability alone. Rasa's RegexEntityExtractor and lookup tables are the precedent for deterministic entity candidates.

**Alexa Skills Kit** [L]. Dynamic entities let a skill replace slot values at runtime from its own data, capped at 100 entities including synonyms (developer.amazon.com, "Use Dynamic Entities for Customized Interactions"). `AMAZON.SearchQuery` is the free-text slot, and it has two rules that are instructive: "Each sample utterance must include a carrier phrase" and it "cannot be combined with another intent slot in sample utterances" (Slot Type Reference). `AMAZON.FallbackIntent` is the out-of-domain class. Take: entity slots populated from live state at request time (our Choice options), open text only after a carrier phrase, an explicit none class.

**Talon** [L]. Commands are a context-scoped grammar with typed captures and dynamic lists (for example running applications), active sets change with the focused app, and free text only follows a prefix such as "say" or "phrase". Command mode executes after `speech.timeout`, default 0.3 s of silence (talon.wiki, community settings). Take: the grammar shape, the dynamic lists, context scoping, and the 300 ms figure as the baseline our adaptive endpoint has to beat.

**Apple Voice Control** [L]. "Show numbers", "Show names", "Show grid" overlay identifiers so the user can refer to anything by number; command mode ignores non-commands ("Words and characters that aren't commands are ignored"); dictation is a separate mode (support.apple.com Mac guide, mh40719). Take: numbered overlays as the universal disambiguator, and a strict separation between command interpretation and dictated text.

**Joint intent and slot filling** [L]. Liu and Lane 2016 (arXiv:1609.01454), Goo et al. 2018 (slot-gated), Chen et al. 2019 "BERT for Joint Intent Classification and Slot Filling" (arXiv:1902.10909). The lesson from that literature is that intent and slots are correlated and joint modeling beats independent modeling. Jev gives us independent heads only, so the correlation must be restored in code (section 6.4) or by a flat joint-candidate question (section 6.2).

**NLU as question answering** [L]. Namazifar et al. 2020, "Language Model is All You Need: Natural Language Understanding as Question Answering" (arXiv:2011.03023) recasts intent and slot detection as questions over the utterance. Yin et al. 2019 (arXiv:1909.00161) does zero-shot classification by describing labels in natural language. Jev is the productized version of that idea: label descriptions in `criteria` are the zero-shot classifier's label text.

**Out-of-scope detection** [L]. Larson et al. 2019 (CLINC150, arXiv:1909.02027) show that in-scope accuracy is easy and out-of-scope recall is hard, and that an explicit out-of-scope class with examples outperforms thresholding a softmax. Hendrycks and Gimpel 2017 (arXiv:1610.02136) give the max-probability baseline; Guo et al. 2017 (arXiv:1706.04599) explain why thresholds need calibration measurement. Take: use both an explicit none class and a separate boolean, and calibrate thresholds on a labelled replay set.

**Option-position bias** [L]. Zheng et al. 2023, "Large Language Models Are Not Robust Multiple Choice Selectors" (arXiv:2309.03882). Whether Jev has position bias over 255 options is not documented. Take: a day-one permutation test, and a stable option order so the cache stays valid.

**Incremental understanding** [L]. DeVault, Sagae, Traum 2009, "Can I Finish? Learning When to Respond to Incremental Interpretation Results in Interactive Dialogue" (SIGDIAL 2009, aclanthology W09-3902); Schlangen and Skantze 2009 incremental-unit model. Take: interpret partial ASR hypotheses, commit when the interpretation stops changing.

**Tail latency** [L]. Dean and Barroso, "The Tail at Scale", CACM 2013: hedged requests (send a duplicate after the p95 wait, take the first answer). Take: hedging is nearly free at $0.042 per 1M tokens.

**TypeSafe cookbooks directly reusable** [P]: function calling (one request holds the function choice and every function's closed-set arguments; confidence is the "least certain judgement"); pre-parsed value extraction (regex candidates plus Choice plus NONE); date extraction (decompose into closed components, code does the calendar math); semantic find (one Choice over 218 line ids plus a Noul "does an answer exist"); skill suggestion (182 options, wide cheap pass then a top-3 rerank with full descriptions, gate at 0.30); hierarchical classification (beam search over Choice probabilities, width three, geometric-mean path score). All at https://docs.typesafe.ai/cookbooks/.

---

## 4. The reformulation: a voice command as N parallel closed-set questions

A command is a typed record:

```
Action = { intent, target_window?, target_app?, workspace?, direction?, amount?, control?, bind?, text?, number? }
```

Classical NLU predicts `intent` with a classifier and the rest with a sequence tagger (BIO tags over tokens). With Jev:

| Classical component | Jev reformulation | Who generates candidates |
|---|---|---|
| Intent classifier | Choice `intent` over 40 to 60 intents plus `none` | static table, rich `{what, not_for, examples}` criteria |
| Entity slot with a gazetteer (app, window, workspace) | Choice over live candidates plus `focused` plus `none_mentioned` | context compiler from Hyprland state and `.desktop` files |
| Graded slot ("a bit", "a lot", "much louder") | Score with 3 to 5 levels | static |
| Binary modifiers (silently, toggle on, in background) | Boolean | static |
| Sequence tagger for open text | Choice over deterministic candidate spans, or Choice over token-boundary ids | carrier-phrase rules and tokenizer |
| Numbers, percentages, ordinals | deterministic normalizer; Jev only assigns a found number to a slot when there are several | regex and number-word parser |
| Out-of-domain detector | Boolean `addressed` plus the `none` option on every Choice | static, with examples from the user's ambient log |
| Multi-intent detector | Boolean per deterministic split point | tokenizer (conjunction positions) |
| Joint decoding | code-side compatibility table, or a flat Choice over enumerated full action candidates | planner |

Sizing on this machine today [M]: 245 `.desktop` files, 86 without `NoDisplay=true` (fits one Choice); 7 open windows; 131 keybinds (fits one Choice). A focused window's a11y tree can exceed 255 controls, which needs prefiltering or a two-stage pass (section 6.5).

---

## 5. Proposed pipeline

```
 mic -> AEC/VAD -> streaming ASR --partials--> [S2 normalize] -> [S3 fast path grammar+fuzzy]
                                   |                                   |  EXACT -> [S7 gate] -> [S8 execute]
                                   |                                   |  AMBIGUOUS(k parses) --+
                                   |                                   |  NO MATCH -------------+
                                   +--stable partial / early silence--> [S4 speculative Jev requests R1,R2,(R3)]
                                                                        |
 final transcript -> same as speculated? -> reuse answers : re-issue -> [S5 resolve+cross-check] -> [S6 disambiguate?]
                                                                        -> [S7 tier gate] -> [S8 execute via hypruse core] -> [S9 learn]
 [S0 context compiler] runs continuously off the hot path and feeds S3 and S4.
```

### S0. Context compiler (event-driven, never on the hot path)

- Subscribes to Hyprland `socket2` events (hypruse already has `events.py` with an `EventStream`) and keeps an in-memory `DesktopState`: windows (address, class, title, workspace, floating, fullscreen, focus-history rank), workspaces, monitors, layers.
- Builds and keeps current, as pre-serialized JSON fragments with stable ids: the window option table, the app option table (Name, GenericName, Keywords, Exec basename, user aliases), the workspace option table, the bind option table (from `hyprctl binds`, which hypruse already parses), and the a11y control table for the focused window (fetched on focus change with a debounce, cached per window address, invalidated on title change, because an AT-SPI walk is too slow for the hot path [A]).
- Compiles the same tables into the fast-path lexicon (Talon-style dynamic lists).
- Computes a `state_signature` per table (hash), used as cache keys.
- Reads via the direct Hyprland socket. Measured: `[[BATCH]]j/clients;j/workspaces;j/monitors` p50 0.54 ms, p95 1.53 ms [M].

Result: at end of speech, building a request body is concatenation of cached bytes plus the transcript. Measured cost of a full `json.dumps` of a 3.7 KB body with 86 app options on this CPU: p50 0.16 ms [M], so even the naive approach is fine.

### S1. Audio and ASR (other lane; only the contract matters here)

The NLU needs from ASR: streaming partial hypotheses with a stability signal, a final hypothesis, word timestamps if available (pauses help clause splitting), and an n-best list if available (n-best lets the grammar rescue a misrecognition: if hypothesis 2 parses exactly and hypothesis 1 does not, take 2). Vocabulary biasing toward the live lexicon (app names, window classes) is the ASR-side analogue of Snips entity injection [L].

### S2. Normalizer (deterministic, under 0.1 ms [A])

Lowercase; strip politeness and fillers ("please", "can you", "uh"); number words to digits with slot-context homophone repair ("workspace to" -> "workspace 2", "for" -> 4 only after a number-taking head); ASR split-word repair from the lexicon ("fire fox" -> "firefox"); user alias substitution. Keeps a token list with character offsets into the raw transcript, because spans for dictation must be cut from the raw text, not the normalized text.

### S3. Fast path: compiled grammar plus fuzzy entity matcher (zero network)

- Grammar: 60 to 120 canonical patterns with typed captures `<ws>`, `<window>`, `<app>`, `<dir>`, `<amount>`, `<key>`, `<n>`, and a greedy tail `<text>` that is only legal after a carrier verb (type, say, dictate, search for, google, rename to). Compiled to a trie or a single alternation. Measured worst case for a linear scan of 105 compiled regexes that all fail: p50 0.047 ms [M].
- Entity captures resolve against the live lexicon with a fuzzy scorer. Measured with the slow stdlib `difflib` over 86 app names: p50 1.4 ms [M]; rapidfuzz (not installed in system Python today) would be an order of magnitude faster [A]. Add a phonetic key (Double Metaphone or similar) because ASR errors are phonetic, not typographic.
- Context scoping like Talon: a small set of app-specific rules activates by focused window class (browser: "new tab", "back"; terminal: "clear").
- Hint mode grammar: when numbered hints are on screen, bare numbers and "cancel" are the only live rules, which makes the follow-up instant and unambiguous.

Outcomes:
- EXACT: one parse, every capture resolved with fuzzy score >= 0.92 and gap to runner-up >= 0.15 [A]. Go straight to S7. Cancel any speculative Jev request.
- AMBIGUOUS: the grammar matched but produced k > 1 parses or k close entity candidates. If the transcript contains extra discriminating words ("the chrome with youtube"), send Jev a tiny arbitration request whose Choice options are exactly those k parses described in plain English (plus `none`). If the transcript contains no discriminating information ("focus kitty" with four kitty windows), skip Jev and show numbered hints immediately; the model cannot know either.
- NO MATCH: semantic path. Before paying for it, apply the cheap ambient filter (section 10).

### S4. Jev semantic path: state-sliced parallel requests

Two requests fired concurrently on one warm HTTP/2 connection, a third only when relevant. Splitting by state, not by question type, follows from fact 10 (irrelevant state lowers accuracy) and makes caching far more effective.

**R1, utterance-only state.** `state = { utterance, normalized, focused_app_class }`. Questions: `addressed` (boolean), `intent` (choice), `destructive` (boolean), split booleans for each conjunction (section 8), open-slot span Choice (section 7), `amount` (score), `direction` (choice), `workspace` (choice, as a cross-check on the deterministic number), modifier booleans. R1 does not depend on the desktop, so its cache key is just the normalized utterance plus the question-bank version.

**R2, desktop-sliced state.** `state = { utterance, focused_window, windows[], workspaces[] }` with titles truncated to 60 characters. Questions: `target_window` (choice over windows plus `focused_window` plus `none_mentioned`), `target_app` (choice over installed apps plus `none_mentioned`), `bind` (choice over the user's binds plus `none`), and the flat joint `action` Choice (section 6.2).

**R3, a11y controls (conditional).** Fired speculatively only when the utterance contains a UI-verb cue (click, press, toggle, check, select, open the ... menu, tab) and the control table for the focused window is warm. `state = { utterance, window: {class, title} }`, one Choice over up to 254 controls plus `none`.

Requests go through `POST https://ai-gateway.vercel.sh/v1/evaluate` with plain `httpx` (HTTP/2, one persistent client), `maxRetries` equivalent 0 on the hot path (the AI SDK default of 2 retries [P] would silently destroy the budget). The TypeSafe Python SDK pointed at the `/typesafe` base URL is the alternative; it supports an injected `http_client` and per-call timeouts [P]. A Python app does not need the TypeScript AI SDK at all.

### S5. Resolve and cross-check (code, under 1 ms [A])

- Read only the slot answers relevant to the chosen intent's signature (function-calling cookbook practice [P]).
- Command confidence = minimum over the required questions ("least certain judgement" [P]), not a product.
- Cross-checks that convert independence into signal: factorized answer (`intent` + `target_*`) versus the flat joint `action` answer; deterministic workspace number versus Jev `workspace`; lexical anchor (does the transcript contain a verb from the intent's verb lexicon). Agreement raises the tier the command may reach; disagreement caps it at "hints or no-op".
- Defaults: a missing window target on a non-destructive intent defaults to the focused window; on a destructive intent it requires the transcript to say "this", "it", or name the window.

### S6. Disambiguation (section 9), S7. tier gate (section 9), S8. execution

Execution goes through the hypruse core (dispatch, launch, `click_ui`, `keyboard`, `use_bind`, `wait_for`, `sequence`), using the direct socket for dispatch. A small peephole pass fuses plans where Hyprland has a native form, for example "open firefox and put it on workspace two" becomes one `exec [workspace 2 silent] firefox` dispatch instead of launch, wait, move [A: exec rules syntax to be confirmed against Hyprland 0.56 Lua provider in the fork].

### S9. Learn (section 11)

---

## 6. Question bank details

### 6.1 Writing questions for a literal reader

From the jaggedness page [P]: write positive, literal, single-hop instructions; no double negatives; no counting; keep state relevant. From the KB [P]: atomic questions, rich option descriptions ("Descriptions can be strings, objects, or arrays of examples"), thresholds per action. Example R1 body (abbreviated, this is the shape to build, not a tested prompt):

```json
{
  "model": "typesafe-ai/jev",
  "state": { "utterance": "could you put the browser over on the second desktop",
             "focused_app": "kitty" },
  "questions": {
    "addressed": { "type": "boolean",
      "instructions": "Is the utterance a command spoken to a desktop voice controller?",
      "criteria": { "true": "An instruction to operate windows, workspaces, apps, keys or on-screen controls.",
                    "false": "Conversation with another person, thinking aloud, media audio, a question needing a spoken answer, or a fragment." } },
    "intent": { "type": "choice", "instructions": "Which desktop action does the utterance request?",
      "criteria": {
        "focus_window":   { "what": "Bring an existing window to the front", "examples": ["go to the terminal", "show me slack"] },
        "move_window_to_workspace": { "what": "Send a window to another workspace", "not_for": "switching the view to a workspace", "examples": ["put this on three", "throw the browser over to desktop two"] },
        "switch_workspace": { "what": "Change which workspace is shown", "examples": ["desktop four", "go to the next workspace"] },
        "launch_app":     { "what": "Start an application that may not be running", "examples": ["open files", "start a new terminal"] },
        "close_window":   { "what": "Close or quit a window", "examples": ["close this", "get rid of that window"] },
        "type_text":      { "what": "Enter the spoken words as text into the focused app", "examples": ["type hello world"] },
        "web_search":     { "what": "Search the web for the spoken query", "examples": ["look up hyprland animations"] },
        "none":           { "what": "No supported desktop action is requested" }
      } },
    "workspace": { "type": "choice", "instructions": "Which workspace does the utterance name as the destination?",
      "criteria": { "ws1": "workspace 1, first", "ws2": "workspace 2, second", "ws3": "workspace 3, third",
                    "next": "the next workspace", "previous": "the previous workspace",
                    "none_mentioned": "no workspace is named" } },
    "amount": { "type": "score", "instructions": "How large a change does the speaker ask for?",
      "criteria": ["tiny: a touch, slightly", "small: a bit, a little", "medium: no size word", "large: a lot, much", "maximum: all the way, fully"] },
    "destructive": { "type": "boolean", "instructions": "Would carrying out the utterance close a window, end a program, discard work, log out, or power off?" }
  }
}
```

### 6.2 Flat joint action candidates (the consistency fix)

Independence means `intent = close_window` can come back with `target_window = none_mentioned` and `target_app = firefox`. Two remedies, used together:

1. Compatibility table in code (each intent declares which slots it reads and how to default them).
2. One extra Choice in R2 whose options are full enumerated actions generated by the planner: every window-intent crossed with every window, every launchable app, every workspace switch and move, the global actions, plus `none`. On today's desktop that is roughly 7 windows x 8 window intents + 86 launches + 20 workspace actions + 20 globals, about 180 options, inside the 255 limit [M for the counts, A for the intent counts]. One softmax over whole actions gives coherent probabilities, so the top-2 margin is directly meaningful for the hint UI. When the product exceeds 254, prune by the factorized answers from the previous utterance class or by lexical prefilter, or fall back to factorized only.

Whether a 180-option Choice is as accurate as several small ones is unknown. TypeSafe's skill-suggestion cookbook handles 182 options with a wide pass then a top-3 rerank and notes that errors cluster within a category when descriptions are thin [P]. That is why the joint question is an additional vote, not the sole decision maker, and why a second-round top-3 rerank is reserved for the low-margin case only (it costs a second round trip).

### 6.3 Why slice state across requests

One request shares one state across all its questions [P]. Putting 131 binds and 86 apps into `state` would make them distractors for the `intent` question. Note that option tables live in `criteria`, not in `state`, so a question only "sees" its own options plus the shared state. Rule: `state` holds only what every question in that request needs; per-question material goes in that question's `criteria`.

### 6.4 Confidence arithmetic

- Per Choice: use `p1` (top probability) and `margin = p1 - p2`. TypeSafe's `confidence` generalizes to (K x p1 - 1)/(K - 1), which tends toward `p1` as K grows, so for large option sets it adds little over `p1`; the margin is the more useful second number (Rasa's `ambiguity_threshold` precedent [L]).
- Probabilities may be rounded (the SDK exposes an optional `rounding` field [P]; doc examples show two decimals). Do not design thresholds finer than 0.01.
- Command confidence = min over required answers [P pattern]. Agreement bonus: if factorized and joint answers agree, allow the tier; if they disagree, cap.

### 6.5 a11y controls beyond 254

Prefilter deterministically: keep controls that are visible and actionable, drop unnamed ones, rank by fuzzy score against the utterance and by role cue ("button", "tab", "checkbox" in the utterance), keep the top 200, always include `none`. If recall matters more (huge Electron trees), use the documented two-stage narrowing: first a Choice over containers (toolbar, sidebar, dialog, menu bar), then a Choice within the winner [P: pre-parsed extraction cookbook mentions two-stage narrowing past 255; hierarchical cookbook gives beam search]. That costs a second round trip, so only do it after the single-stage answer comes back as `none` or low margin.

---

## 7. Open slots without generation

Principle (verbatim from the TypeSafe cookbook): "Because TypeSafe only ever chooses among the spans the regex found, the value you get back is one of those spans, copied unchanged. It cannot invent a value or transpose a digit." [P]

1. **Carrier-phrase capture in the grammar** (Alexa `AMAZON.SearchQuery` and Talon precedent [L]). "type X", "say X", "dictate X", "search for X", "google X", "rename workspace to X". The tail is opaque: nothing inside it is ever interpreted as a command. This covers most dictation with zero model involvement.
2. **Candidate spans plus Choice** for paraphrased forms ("could you look up how tall the eiffel tower is in firefox"). Code enumerates a handful of plausible spans: the tail after each verb-like token, the same tail minus a trailing target phrase that matches a live entity ("in firefox", "on workspace two"), the quoted region if ASR emits pauses. Typically under 10 candidates. One Choice: "Which candidate is exactly the text the speaker wants searched or typed?" with a `NONE` option. Preferred over independent start and end questions because two independent boundary answers can cross.
3. **Token-boundary Choice** as the fallback when no candidate is accepted: options `t0..tN` (utterances are far below 255 tokens), each described by the token and its next two neighbours, one Choice for start and one for end, validated in code (`start < end`, otherwise reject). This is the "semantic find" pattern (one Choice over 218 line ids) applied at token granularity [P pattern, A for accuracy].
4. **Numbers** by a deterministic normalizer (cardinals, ordinals, percentages, "half", "double"). If several numbers are present and the grammar did not bind them, a Choice per numeric slot over the found values plus `NONE`. Never ask Jev to compute or count [P: "Jev is not a calculator"].
5. **Relative quantities** by Score ("a bit", "a lot") mapped in code to pixels or percent.
6. **Spelling mode** (Apple precedent [L]) for text ASR cannot get right: phonetic alphabet handled entirely in the grammar.

---

## 8. Compound commands under question independence

Example: "open firefox and put it on workspace two".

1. **Deterministic split candidates.** Positions of "and", "then", "and then", "after that", "also", and long ASR pauses. Usually 0 to 2 per utterance.
2. **One boolean per candidate in R1**: "Does the word 'and' at position 3 separate two different commands?" with criteria true = "two commands, each with its own verb", false = "joins two parts of one name, one phrase, or one dictated text". A per-position boolean avoids counting (weak spot) and is exactly the shape of TypeSafe's own demo Noul, minus their LLM splitter [P].
3. **Speculative per-clause requests at the same instant.** If the transcript has any split candidate, fire per-clause R1 and R2 requests for the segmentation implied by all candidates, concurrently with the whole-utterance requests, on the same HTTP/2 connection. When the answers land, the split booleans decide which set to read. Latency stays at one round trip; cost is a few more thousand tokens, about $0.0002 [A on token counts].
4. **Coreference by rule.** In clause n > 1, every target Choice gets an extra option `previous_result` ("the window or app the previous command acted on; words like it, that, this one"). The executor binds it at run time: after a launch it blocks on hypruse `wait_for(window_open)` and applies the next step to the new window.
5. **Plan fusion.** Known pairs collapse to native Hyprland forms (launch plus move to workspace, focus plus fullscreen).
6. **Tiering.** A compound plan takes the highest tier of its steps; the HUD shows the chips for every step before executing when any step is tier 1 or above.
7. **Cap.** At most three clauses. Anything longer is rejected with a visible "too long" cue; long utterances are also far more likely to be ambient speech.

---

## 9. Margin-based disambiguation and confidence tiers

All thresholds below are initial assumptions [A], chosen to be consistent with the vendor guidance (0.7 for read-only, 0.9 or above for destructive, 0.60 minimum top probability in the cookbooks [P]) and Rasa's 0.1 ambiguity gap [L]. They must be tuned on a labelled replay set with a reliability diagram (Guo et al. [L]).

| Tier | Examples | Fast-path rule | Jev-path rule |
|---|---|---|---|
| 0 reversible, harmless | focus window, switch workspace, toggle floating, scroll, show hints | execute on EXACT | `addressed` >= 0.60, p1 >= 0.55, margin >= 0.15 |
| 1 reversible, disruptive | launch app, move window, fullscreen, resize, type dictated text, press an app shortcut | execute on EXACT | `addressed` >= 0.70, p1 >= 0.75, margin >= 0.25, factorized and joint answers agree |
| 2 destructive or hard to undo | close or kill window, exit session, power, Enter in a form, delete, clipboard overwrite | execute on EXACT only if the target is explicit ("this", a name, a hint number) | never on Jev alone: needs p1 >= 0.92, `addressed` >= 0.90, a destructive verb from a closed lexicon literally present in the transcript, and then a 1.5 s cancellable countdown chip, or a spoken "confirm" when the mic is open-mode |

Decision rule after S5:
- `p1 >= act(tier)` and `margin >= gap(tier)`: act.
- `p1 + p2 + p3 >= 0.85` but margin below gap: **numbered hints**. Draw labels 1..k on the actual candidate window rectangles (geometry from Hyprland), on workspace pills for workspace ambiguity, or on a11y control rectangles for control ambiguity (hypruse `marks` already produces numbered legends). Enter hint mode: bare numbers resolve on the fast path, timeout 4 s, "cancel" dismisses. This is the Apple "Show numbers" interaction used as an automatic repair step [L].
- Mass diffuse or `none` on top: do nothing, flash a subtle "not understood" cue with the transcript so the user can see whether ASR or NLU failed. Act, do not talk.

Because repeat calls may wobble by about 0.01 (up to 0.05 on a borderline label) [P], put a dead band of 0.05 under each act threshold that routes to hints rather than to no-op, so a borderline command degrades gracefully instead of flickering between acting and ignoring.

Prompt-injection hardening, since window titles and a11y labels are untrusted text and the vendor says Jev can be steered by injected instructions [P]: truncate titles, never let tier 2 pass without the lexical anchor in the user's own transcript, run acoustic echo cancellation so speaker audio does not become transcript (audio lane), and keep "classification separate from authorization" (KB wording [P]): Jev proposes, the tier table disposes.

---

## 10. Out-of-domain rejection: ambient speech does nothing

Layered, cheapest first:

1. **Activation mode** (other lane, but it changes NLU thresholds): push-to-talk, wake word, or open mic. In open-mic mode add 0.10 to every act threshold and make tier 2 always-confirm.
2. **Deterministic ambient filter before any Jev call**: grammar miss and no token from the command-verb lexicon and length above 14 tokens, or ASR average confidence low, means drop silently. Saves money and removes most false-accept opportunities.
3. **Two independent Jev rejections in the same request**: the `addressed` boolean and the `none` option on `intent` (and on the joint `action`). Accept only when both pass. Two differently phrased detectors are valid here precisely because questions are independent. CLINC150 shows the explicit out-of-scope class with examples is the stronger of the two [L].
4. **Personal negatives**: utterances the user cancelled or that were rejected get appended (top few by similarity) to the `none` option's `examples` and to the `addressed.false` criteria.
5. **Behavioural guard**: two undo or cancel events within a minute raise thresholds for the next few minutes (the room is noisy or a video is playing).

Metric to track: false accepts per hour of ambient audio, measured with a recorded ambient corpus through the replay harness.

---

## 11. Personalization without fine-tuning

- **Alias table** (user-declared and learned): "browser" -> firefox, "comms" -> workspace 4, "the music" -> spotify. Applied twice: in the normalizer and lexicon (so aliased commands become fast-path), and as `aliases: [...]` inside the matching option's criteria object for Jev (structured criteria with user-defined fields are supported [P]).
- **Correction capture**: a hint selection, an "undo" followed by a different command within 5 s, or an explicit "no, the other one" yields a triple (normalized utterance with entities abstracted, state signature, chosen action).
- **Promotion**: after two identical corrections, or after a Jev answer with p1 >= 0.9 that was not undone, compile the entity-abstracted utterance into the deterministic parser as a learned pattern ("put `<app>` over on `<ws>`" -> move_window_to_workspace). This is the Snips design in reverse: the statistical stage continuously trains the deterministic stage, so the Jev hit rate falls over time and median latency with it [A for the rate, L for the structure].
- **Few-shot through criteria**: inject at most 3 to 5 of the user's own past phrasings, selected by lexical similarity to the current utterance, as `examples` on the corresponding option. Keep it small because unrelated state and text reduce accuracy [P].
- **Priors in code, not in the prompt**: focus-history rank and time of day only break ties inside the hint band. They never override a confident answer.
- Everything is local JSON; a reset is deleting a file.

---

## 12. Caching and determinism

- **Do not rely on server determinism.** No primary source states that identical requests return identical probabilities; the only published data shows small run-to-run variation in a setup that cannot isolate the cause [P]. Put a determinism check in the day-one test plan.
- **Local memoization gives determinism on our side.** Per-question cache, which independence makes valid: `key = hash(response.model, bank_version, question_id, canonical(question), canonical(state slice that request used))`. If every question of a request hits, no request is sent. If some miss, send only the misses. Caveat: that an answer is unaffected by which other questions ride along is a vendor claim [V]; verify with an A/B (same question alone versus among 20).
- **R1 is highly cacheable** because its state is only the utterance: "close this" is the same request all day.
- **Entity-abstracted keys** raise hit rates further: replace resolved entity mentions with typed placeholders before hashing the intent question.
- **Stable option ordering** (sort by id, not by recency) keeps keys stable and keeps any position bias constant [L for the risk].
- **Invalidation**: on question-bank version bump, and when `response.model` changes. TypeSafe's direct API uses the alias `jev-latest` and the jaggedness page is versioned "Jev 1.13" [P], so silent model upgrades are plausible; re-verify a sample of cached entries weekly.
- **Negative cache**: utterances classified as not addressed are cached too, so a looping video cannot keep costing requests.
- **Record and replay**: store raw responses as cassettes so the full pipeline can be regression-tested offline. The AI SDK ships `Experimental_EvaluationMockModelV4` [P]; the Python equivalent is a fake evaluator with the same response shape.

---

## 13. Networking: pre-warm, speculate, cancel, hedge

Local measurements to `https://ai-gateway.vercel.sh/` (root path, returns a 308, no API call, edge header `cdg1`) [M]:

| Condition | Time |
|---|---|
| New connection, 5 runs, total | 184, 200, 331, 745, 769 ms (DNS 9 to 298, TCP 41 to 260, TLS done at 133 to 696) |
| Reused HTTP/2 connection, requests 2 to 5 | 46.2, 46.2, 48.7, 50.9 ms |
| ICMP RTT | min 31.7, avg 61.1, max 140.9 ms, 20 percent loss in a 5-packet sample |

Consequences:
- **Pre-warm always.** One persistent `httpx` HTTP/2 client. Open it when the daemon starts, keep it alive with a cheap request every 20 to 30 s while the mic is armed [A for the interval, the gateway idle timeout is not documented], and re-open on speech onset if it died: speech lasts at least half a second, which covers a handshake.
- **One connection, many streams.** R1, R2, R3, per-clause requests and hedges multiplex on the same HTTP/2 connection (the gateway negotiated HTTP/2 in the test [M]).
- **Speculative evaluation on partials** [L: DeVault et al.]. Fire the request set when (a) a partial has been stable for about 150 ms, or (b) VAD reports 80 to 100 ms of silence. On the final transcript: if the normalized final equals the speculated text, use the answers (already back or in flight); otherwise abort and re-issue. At most 4 speculative sets per utterance and 6 streams in flight [A], since rate limits are not documented (the API does return 429 and 529 [P]).
- **Cancel stale requests.** `abortSignal` in the AI SDK [P]; in `httpx`, cancelling the task resets the HTTP/2 stream and keeps the connection. Whether aborted evaluations are billed is not documented; assume they are. At roughly 2k to 4k tokens a set, that is about $0.0001 to $0.0002 per wasted set [A on token counts].
- **Hedge** [L: Dean and Barroso]. If no response by the observed p95, send a duplicate on a second warm connection and take the first. If the user also has a direct TypeSafe key, hedge to `https://api.typesafe.ai/v1/systemone` for path diversity [P for the endpoint, A for benefit].
- **Hard deadline.** If nothing usable is back 450 ms after the final transcript, give up visibly. A late action is worse than no action in a voice UI because the user has already started repeating.
- **Retries off** on the hot path (AI SDK default is 2 [P]; the TypeSafe SDK retries 429 and 529 with backoff by default [P]).
- **Where inference runs is unknown.** The edge answered from Paris, but the gateway-to-TypeSafe hop and TypeSafe's serving region are not documented. If inference is in a US region, add roughly a transatlantic round trip [A]. This is the largest unknown in the Jev-path budget.

---

## 14. Latency budgets

t0 is the true end of speech (last voiced frame). "Action" means the Hyprland dispatch has been written to the socket.

### 14.1 Fast path, open mic, target under 300 ms

| Stage | Budget | Basis |
|---|---|---|
| Adaptive endpoint: trailing silence when the partial already parses as a complete command | 150 ms | [A]; baseline is Talon's 300 ms default [L] |
| ASR finalization after endpoint (streaming model, partials already decoded) | 40 ms | [A], owned by the ASR lane |
| Normalize | < 0.1 ms | [A] |
| Grammar match | < 0.1 ms | [M] 0.047 ms worst case for 105 regexes |
| Fuzzy entity resolution | < 2 ms | [M] 1.4 ms with difflib over 86 apps; rapidfuzz faster [A] |
| Tier gate and plan | < 0.5 ms | [A] |
| Dispatch over the Hyprland socket | < 1 ms | [M] 0.19 to 0.54 ms for socket queries; subprocess hyprctl is 21.7 ms p50 and must be avoided |
| HUD confirmation on next frame | 16 ms | [A], parallel to dispatch |
| **Total to action** | **about 195 ms** | sum; leaves about 100 ms of slack for ASR variance |

With push-to-talk, key release replaces the 150 ms endpoint, so action lands roughly 45 to 60 ms after release, bounded by ASR finalization [A].

If the partial is a prefix of a longer rule ("move this to"), the endpoint extends to 600 ms or more so the user is not cut off. Grammar-aware endpointing therefore both speeds up complete commands and protects incomplete ones.

### 14.2 Jev path, target under 600 ms

Assumed Jev round trip on a warm connection for a 1.5k to 4k token request: 150 to 250 ms p50 [A]. Basis for the assumption: [V] 0.114 s for an unspecified workflow; [V] 0.21 to 0.27 s for roughly 11k-token requests in TypeSafe's cookbook under unknown conditions; [M] 46 to 51 ms floor to the gateway edge from this machine. Not measured.

Speculation hit (final transcript equals the speculated partial):

| Event | Time after t0 | Basis |
|---|---|---|
| Early-silence trigger fires speculative R1, R2 (body build 0.2 ms [M]) | 90 ms | [A] |
| Standard endpoint declared (partial is not a complete grammar command) | 300 ms | [A], Talon default [L] |
| ASR final available, equals speculated text | 340 ms | [A] |
| Jev answers arrive (sent at 90, RTT 150 to 250) | 240 to 340 ms | [A] |
| Resolve, cross-check, gate | + 1 ms | [A] |
| Dispatch | + 1 ms | [M] |
| **Action** | **about 345 ms** | the endpoint, not Jev, is the critical path |

Speculation miss (the final differs, request issued at 340 ms):

| Event | Time after t0 |
|---|---|
| Request sent | 341 ms |
| Answers arrive | 490 to 590 ms [A] |
| **Action** | **about 495 to 595 ms**, p95 likely above 600 ms [A] |

So the 600 ms target is comfortably met on a hit, met at p50 on a miss, and at risk at p95 on a miss. Levers, in order of value: raise the speculation hit rate (stable-partial detection), shorten the non-grammar endpoint to 250 ms, hedge at p95, keep requests lean (state slicing), and promote learned phrases to the fast path so fewer utterances take this path at all.

Hint follow-ups ("two") are always fast path: about 195 ms [A].

### 14.3 Cost

At $0.042 per 1M input tokens [P]: a full speculative set of about 6k tokens costs about $0.00025; three sets per utterance and 500 utterances a day is about $0.38 per day worst case, and far less once the cache and the fast path absorb the common commands [A on token counts, derived from a measured 3.7 KB body for an 86-option app Choice].

---

## 15. Day-one validation plan once a key exists

1. Latency curve: RTT versus input tokens (500, 2k, 6k, 12k) and versus question count (1, 5, 15, 40), 50 samples each, on the warm connection, from this machine. Replace every [A] in section 14.
2. Determinism: 30 byte-identical requests; report max absolute probability difference. Then the same with one irrelevant field changed.
3. Independence: one question alone versus the same question among 20 others; compare probabilities.
4. Option-order sensitivity: permute a 100-option and a 200-option Choice 10 times.
5. Large-Choice accuracy: flat joint action (about 180 options) versus factorized, on a labelled set of 300 utterances (150 canonical, 100 paraphrase, 50 ambient or out-of-domain).
6. Span selection accuracy: candidate-span Choice versus token-boundary Choice on 60 dictation and search utterances.
7. Abort billing: fire and cancel 100 requests; compare reported usage.
8. Calibration: reliability diagram for `intent` p1 and for `addressed`; set tier thresholds from it.
9. Rate limits: ramp concurrent streams until 429.
10. Raw `/v1/evaluate` JSON for a Choice: confirm whether a `confidence` value is present outside the AI SDK's `providerMetadata`.

---

## 16. Risks

- Inference region and true p50 and p95 are unknown; the Jev-path budget rests on [V] and [A] numbers.
- Determinism is marketing language, not a documented guarantee; treated as unconfirmed here.
- 255 options is a hard cap; large a11y trees and a growing joint action space need pruning.
- English-first model [P]; non-English commands degrade.
- Injection through window titles and control labels [P]; mitigated by tiering and lexical anchors, not eliminated.
- `experimental_` API surface can change; isolate it behind one `Evaluator` interface with a fake, an HTTP, and a TypeSafe-SDK implementation.
- No local fallback model exists for Jev; when offline the app must degrade to grammar-only and say so in the HUD.

---

## 17. Sources

Primary, Jev and gateway:
- https://vercel.com/docs/ai-gateway/modalities/evaluation (last updated 2026-09-16)
- https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe
- https://vercel.com/kb/guide/typesafe-jev-and-ai-sdk
- https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway (published 2026-09-16)
- https://vercel.com/ai-gateway/models/jev
- https://vercel.com/i/what-is-jev
- https://ai-sdk.dev/docs/reference/ai-sdk-core/evaluate
- https://typesafe.ai
- https://docs.typesafe.ai/llms.txt (index), and pages: introduction, api, confidence, concepts/state, concepts/system-one, primitives/choice, primitives/advanced, patterns/fan-out, patterns/intent-routing, demos/smart-home, model-jaggedness/jev-1.13, sdk/python, sdk/python/api/clients/async, cookbooks/function_calling, cookbooks/pre_parsed_value_extraction_cookbook, cookbooks/date_extraction_cookbook, cookbooks/semantic_find, cookbooks/skill_suggestion, cookbooks/hierarchical_classification, cookbooks/parallel_questions, cookbooks/consistency_choice_cookbook, cookbooks/consistency_noul_cookbook

Literature and production systems:
- Coucke et al. 2018, Snips Voice Platform, https://arxiv.org/abs/1805.10190 ; Snips NLU docs https://snips-nlu.readthedocs.io
- Rasa source, https://raw.githubusercontent.com/RasaHQ/rasa/3.6.x/rasa/core/constants.py ; Bunk et al. 2020 DIET, arXiv:2004.09936
- Alexa: https://developer.amazon.com/en-US/docs/alexa/custom-skills/use-dynamic-entities-for-customized-interactions.html ; https://developer.amazon.com/en-US/docs/alexa/custom-skills/slot-type-reference.html
- Talon: https://talon.wiki/Resource%20Hub/Speech%20Recognition/improving_recognition_accuracy/ ; https://github.com/talonhub/community/blob/main/settings.talon
- Apple Voice Control: https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac
- Liu and Lane 2016 arXiv:1609.01454; Chen et al. 2019 arXiv:1902.10909; Namazifar et al. 2020 arXiv:2011.03023; Yin et al. 2019 arXiv:1909.00161
- Larson et al. 2019 arXiv:1909.02027; Hendrycks and Gimpel 2017 arXiv:1610.02136; Guo et al. 2017 arXiv:1706.04599
- Zheng et al. 2023 arXiv:2309.03882
- DeVault, Sagae, Traum 2009, https://aclanthology.org/W09-3902/
- Dean and Barroso 2013, The Tail at Scale, CACM 56(2)

Local measurements (2026-09-20, target machine): curl timing to ai-gateway.vercel.sh root; Hyprland socket versus `hyprctl` subprocess; difflib fuzzy match; regex scan; JSON body build; counts of `.desktop` entries, windows and binds. Code reference: `/home/ilyask/projects/hypruse/src/hypruse/hyprctl.py:35-47`.

Citations recalled from memory and not re-fetched today (treat as medium confidence): Goo et al. 2018 slot-gated model; Schlangen and Skantze 2009; Dean and Barroso page numbers; the arXiv ids for Liu and Lane, Chen et al., Yin et al., Hendrycks and Gimpel, Guo et al., and DIET.

---

## 18. Looked for and not found in any primary source

- Any statement that Jev is deterministic or that identical requests return identical probabilities.
- Any measured latency percentile (p50, p95) for Jev on Vercel AI Gateway; the model page shows no latency or throughput stats.
- The serving region(s) for Jev inference, and the gateway-to-provider hop latency.
- A maximum number of questions per request (only the 64k token request limit is stated).
- Rate limits (requests per second, concurrency) for `/v1/evaluate`.
- Whether aborted or cancelled evaluation requests are billed.
- Whether the raw `/v1/evaluate` HTTP response includes a `confidence` value for choice and score (it is documented for the AI SDK via `providerMetadata.typesafe.confidence` and for the TypeSafe-native response).
- Prompt or prefix caching for evaluation requests on the gateway (one TypeSafe cookbook mentions keeping prefix caching efficient, but no API-level documentation of it).
- Any documentation of option-order (position) bias or accuracy versus option count for Choice.
- Gateway keep-alive or idle timeout for HTTP/2 connections.
- An official Python binding for `experimental_evaluate` (the documented Python routes are raw HTTP and the TypeSafe Python SDK).
