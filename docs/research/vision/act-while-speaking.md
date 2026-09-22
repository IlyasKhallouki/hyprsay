# Lane: acting while the user is still speaking

Date: 2026-09-22. Target machine: i5-8350U (4C/8T Kaby Lake-R, AVX2, 15 W), no GPU, 23 GiB RAM,
Arch Linux, Hyprland 0.56.2, French AZERTY. Owner complaint addressed: number 5, "I have seen
systems that act WHILE the person is still speaking."

Claim labels: [PRIMARY] traced to a repository, vendor doc, paper or named author;
[SECONDARY] a report about a primary source; [VENDOR] the vendor's own claim, unreproduced;
[LOCAL] measured on this machine (by this lane, or by a previous hyprsay lane whose raw JSON is
on disk and which I re-parsed); [INFERENCE] my own reasoning, not a finding.

---

## 1. The headline, before the detail

There are exactly three mechanisms behind every system that appears to act while you talk.
Everything else is packaging.

1. **Partial hypotheses with a stability estimate.** The recognizer emits a guess every few
   frames. Most guesses are wrong and get revised. A system that acts on them needs a rule for
   which guesses are safe. The whole literature is about that rule.
2. **Prediction of the complete meaning from an incomplete prefix.** Not "what did they say so
   far" but "what will the whole sentence mean". This is a separate model or classifier, and it
   is what lets a system commit before the last word.
3. **Speculative execution with an explicit commit point.** Fire the expensive, slow work
   (network call, model call) early on the prefix; hold the irreversible part until a commit
   condition is met; throw the speculation away when the prefix changes.

hyprsay today does none of the three. It does one-shot decode on key release
(`src/hyprsay/daemon.py` `_key_up`), and the architecture header of
`src/hyprsay/audio.py` states the reason outright: "Streaming recognition measured as not
viable on this CPU, so the recorder's whole job is to hold the clip until key up".

That conclusion was about STREAMING recognizers. It is correct about streaming recognizers and
it does not follow for partials in general. **Re-running the existing one-shot offline decoder
on a growing prefix of the buffer is a fourth option that no hyprsay lane measured, and on this
machine it is affordable at command length.** I measured it. Section 6.

Also worth stating plainly: `docs/PLAN.md` section 5.2 already specifies speculative finalize,
and `src/hyprsay/audio.py` already ships the three primitives it needs (`snapshot()`,
`trailing_silence(ms)`, `voiced_after(seconds)`, with the docstring "For the speculative decode
(PLAN 5.2)"). `daemon.py` calls only `voiced_after(0.0)`. The feature is designed, half-built,
and not wired. [LOCAL, by grep over the repository]

---

## 2. Mechanism one: partial hypotheses and how unstable they really are

### 2.1 The metrics everyone uses, and where they come from

Baumann, Atterer and Schlangen, "Assessing and Improving the Performance of Speech Recognition
for Incremental Systems", NAACL-HLT 2009, pages 380 to 388. [PRIMARY, PDF read in full]

They define the vocabulary that the rest of the field uses:

- **Edit message.** A change from one partial to the next is expressed as add (a word appended)
  or revoke (a word removed). A revision is a revoke plus an add.
- **Edit overhead (EO).** The fraction of edit messages that are spurious, meaning not one of
  the minimal adds a perfect incremental recognizer would emit. Their baseline (Sphinx-4, German
  puzzle domain, trigram LM) measured **EO = 90.5 percent**: "for every necessary add message,
  there are nine superfluous (add or revoke) messages. Thus, a consumer of the ASR output would
  have to recompute its results ten times on average."
- **WFC, word first correct response.** Time from the start of a word until it is first
  hypothesized correctly. Measured mean **0.276 s**, stddev 0.186 s, median 0.230 s, against a
  mean word duration of **0.378 s**. So a word is usually first guessed right about three
  quarters of the way through itself.
- **WFF, word first final response.** Time from the END of a word until that word stops changing.
  Measured mean **0.004 s**, stddev 0.268 s, median **-0.06 s**.
- **Correction time = WFF minus WFC.** Their Figure 2 is the single most useful curve in this
  whole lane: the percentage of words whose hypothesis is already final rises above **90 percent
  at a correction time of 320 ms** and above **95 percent at 550 ms**. Their own reading, quoted:
  "we can be certain to 90 percent (or 95 percent) that a current correct hypothesis about a word
  will not change anymore once it has not been revoked for 320 ms (or 550 ms)."

That sentence is the design rule. **Stability is bought with a fixed lag, and the price list is
published: 320 ms buys 90 percent, 550 ms buys 95 percent.**

Two further results from the same paper that matter to us:

- **Right context** (discard everything the recognizer says about the last Δ seconds): EO falls
  to 50 percent at Δ = 530 ms and to 10 percent at Δ = 1150 ms. The fraction of immediately
  correct hypotheses reaches 90 percent at Δ = 580 ms and 98 percent at Δ = 1060 ms.
- **Message smoothing** (only emit an edit once N consecutive hypotheses agree): cheaper for the
  same stability. EO falls to 50 percent with **110 ms** of smoothing and to 10 percent with
  **320 ms**, against 530 ms and 1150 ms for right context. Same stability, roughly a third of
  the delay.
- Predicting forward is nearly worthless: only **15 percent** of hypotheses are still correct
  100 ms into the future, **10 percent** at 170 ms. Do not extrapolate the transcript.

Caveat I will not hide: this is a 2009 HMM/Viterbi decoder with a trigram language model on
German read and spontaneous speech. The absolute EO of a 2026 neural transducer is better. The
SHAPE of the trade (stability costs lag, smoothing beats right context, forward prediction
fails) is architectural and survives. Treat the 320 ms and 550 ms as the right order of
magnitude, not as constants for Parakeet. [PRIMARY for the numbers, INFERENCE for the transfer]

### 2.2 How unstable, concretely, in a deployed commercial system

Selfridge, Arizmendi, Heeman and Williams, "Stability and Accuracy in Incremental Speech
Recognition", SIGDIAL 2011, pages 110 to 119. AT&T Labs and OHSU, on the AT&T WATSON recognizer
over the Carnegie Mellon "Let's Go" bus corpus. [PRIMARY, PDF read in full]

They define **stability** as "the partial 1-best is a prefix (or exact match) of the final
1-best" and **accuracy** as the same against what the user actually said. Then they measure
three ways of emitting partials:

| ISR method | what it emits | stability (all / multi-word) | accuracy (all) | partials per utterance |
|---|---|---|---|---|
| Basic (best path every 30 ms) | everything | 7 to 11 percent / 9 to 15 percent | 1 to 9 percent | 9.9 to 12.0 |
| Terminal (partial ends at a language-model terminal node) | fewer | 23 to 37 percent / 20 to 36 percent | 13 to 24 percent | 3.3 to 6.2 |
| Immortal (all lattice paths converge on one node) | very few | guaranteed stable | **91 to 93 percent** (rule LM), 55 percent (statistical LM) | **0.22 to 0.67** |

Read that Basic row again. **Seven to eleven percent of naive partials are stable.** Acting on
raw partials is acting on a coin flip weighted against you.

The Immortal row is the mechanically interesting one. An immortal node is a point where every
surviving path in the decoding lattice passes through the same node, so no future audio can
change the lattice before it. That is a structural guarantee, not a threshold, and its accuracy
is 91 to 93 percent with a constrained grammar. The catch is in the last column: it happens less
than once per utterance on average. Structural certainty is real and rare.

Their combined method, LAISR, reaches 24 to 40 percent stability and 15 to 26 percent accuracy.
They also measure when partials arrive: mid-utterance partials (50 to 60 percent of the way
through) have **accuracy near 30 percent**, against 47 percent for the final partial.

Their closing suggestion is the one hyprsay should take, quoted verbatim: "this paper has
examined the word level; however dialogue systems generally operate at the intention level. Not
all changes at the word level yield a change in the resulting intention, so it would be
interesting to apply the confidence measure and stability measures developed here to the
(partial) intention level."

**That is the whole strategy for hyprsay in one sentence.** hyprsay does not need a stable
transcript. It needs a stable DECISION. "focus fire fox", "focus firefox", "focus, Firefox."
are three unstable transcripts and one stable intention. Stability must be measured on the
`Decision`, not on the `Transcript`.

---

## 3. Mechanism two: predicting the complete meaning from a prefix

DeVault, Sagae and Traum. Two works: "Can I finish? Learning when to respond to incremental
interpretation results in interactive dialogue", SIGDIAL 2009; and "Incremental interpretation
and prediction of utterance meaning for interactive dialogue", Dialogue and Discourse 2011.
[PRIMARY for existence, titles, venue and the claimed capability, via ACL Anthology listing
aclanthology.org/2011.dnd-2.9/ and the authors' own page djdsite.org/incremental.html. I did
NOT get the full text of either through a readable fetch, so I quote no numbers from them.]

What they establish, in the terms the field then adopted: a classifier over the partial
utterance predicts (a) the semantic content of what has been said so far, (b) the predicted
semantic content of the COMPLETE utterance, and (c) a confidence in that prediction. The
"Can I finish?" policy then decides, per partial, whether the predicted complete meaning is
confident enough to respond to already. Responsive overlap behaviours (interrupting,
acknowledging, completing the user's sentence) become possible because the system has a
prediction of the whole, not a transcript of the part.

I could not verify their headline percentages from a primary text in this session. Marked
NOT FOUND rather than guessed. Secondary confirmation that this line of work exists and is
the ancestor of current practice: Selfridge et al. 2011 cite it as "DeVault et al. (2009)
showed that when using a relatively small number of semantic possibilities the correct
interpretation could be predicted by early incremental results". [PRIMARY, that citation is in
the Selfridge PDF I read.]

The structural lesson for hyprsay is independent of the numbers: **predicting the complete
intention is a different and easier problem than transcribing the complete sentence**, and it
is easier precisely when the space of possible intentions is small. hyprsay's intention space
IS small and IS enumerated: `nlu/understand.py` builds choice questions over a closed action
list (the probe data shows 15 intents) and over candidates built from the live desktop.
Jev's `choice` question type over 255 options is, structurally, exactly the DeVault classifier,
and it already returns the confidence the policy needs. [LOCAL, from
`docs/research/live/probe_out.json`, which shows an `intent` choice over 15 actions with per
option probabilities and a `confidence` field.]

---

## 4. Mechanism three: speculative execution with a commit point

### 4.1 The current state of the art, and it is one week newer than Jev

Hooper, Kang, Moon et al. (UC Berkeley, ICSI, LBNL), "Speculative Interaction Agents: Building
Real-Time Agents with Asynchronous I/O and Speculative Tool Calling", arXiv:2605.13360v2, dated
14 May 2026. [PRIMARY, fetched]

Mechanism, as described in the paper:

- The agent loop is **decoupled** from the user stream and the environment stream. The model is
  not a function of a finished turn; it runs continuously and its input can be interrupted and
  extended mid-generation (they use interruptible streaming with vLLM).
- Each generation step emits reasoning in `<think>` tags and then one of three actions:
  `<tool_call>`, `<pause>`, or `<answer>`. `<pause>` is a first-class action, which is the
  agent's way of saying "I have heard enough to think but not enough to act".
- **Tool calls carry IDs starting from 1.** A speculative call can be revised by reissuing the
  same ID, or withdrawn with a `REMOVE ID` command. Quoted: "If there were any tool calls which
  depended on the output of the cancelled tool call, these are also cancelled." That is a
  dependency graph with cascade rollback, not a single undo.
- **Tools are manually classified safe (read-only) or unsafe (write).** Unsafe tools are held
  until a commit point, which is reached when "the final query update has been received, and ii)
  the model either generates a tool call ID greater than any previously generated ... or
  generates a pause action."
- Measured: **1.3 to 1.7 times speedup** with a cloud API (OpenAI Realtime) at minor accuracy
  cost; **1.6 to 2.2 times** with edge models (Qwen2.5-3B, Llama-3.2-3B), on HotpotQA and
  TinyAgent. HotpotQA accuracy 71.0 percent speculative against 71.6 percent baseline.

The single most transferable idea: **partition the action space by reversibility, speculate
freely on the reversible half, and gate the irreversible half on an explicit commit condition.**
hyprsay already has that partition. It is `nlu/tiers.py`, and the README documents it: tier 0
focus and workspace switch, tier 1 launch and move, tier 2 close and type, tier 3 lock.
The tier table was built for a security reason (voice is an unauthenticated channel) and it
happens to be exactly the partition speculative execution needs. That is a large, free
advantage that hyprsay should not waste.

### 4.2 How badly production systems handle a prefix that the rest of the sentence changes

Lin, Chen, Chen and Lee (National Taiwan University and NVIDIA), "Full-Duplex-Bench-v3:
Benchmarking Tool Use for Full-Duplex Voice Agents Under Real-World Disfluency",
arXiv:2604.04847v1, dated 6 April 2026. [PRIMARY, fetched]

100 scenarios, five annotated disfluency classes (fillers, pauses, hesitations, false starts,
self-corrections), 21 of the 100 carrying a self-correction event where the user changes a
parameter mid-utterance. This is precisely the risk this lane has to price.

Pass@1 on self-corrections:

| system | Pass@1 on self-correction |
|---|---|
| GPT-Realtime | 0.588 |
| Gemini Live 2.5 | 0.471 |
| Gemini Live 3.1 | 0.353 |
| Ultravox v0.7 | 0.353 |
| Grok | 0.294 |
| Cascaded (Whisper to GPT-4o to TTS) | 0.176 |

**Every production voice stack tested fails the "no wait, not that one" case more often than it
passes it.** The best is 59 percent. This is the honest counterweight to the owner's "I have
seen systems that act while you speak": the systems they have seen are, measurably, bad at the
case where acting early is wrong. On pauses, Grok and Ultravox score 0.333, the weakest category
for most systems.

Their latency table (mean seconds) is also a useful reality check on the marketing:

| system | first word | tool call | task completion |
|---|---|---|---|
| Gemini Live 3.1 | 3.95 | 2.21 | 4.25 |
| Ultravox v0.7 | 3.88 | 6.01 | 8.40 |
| GPT-Realtime | 6.36 | 3.89 | 6.89 |
| Cascaded | 8.78 | 3.15 | 10.12 |

These are multi-step chained tool tasks, not single commands, so they are not comparable to
hyprsay's 0.5 s. They are comparable in one respect: the fastest full-duplex speech-to-speech
system in an academic benchmark takes 2.21 s to its first tool call. hyprsay's measured Jev
decision is 315 ms p50. hyprsay is not behind the field on latency. It is behind on
incrementality, which is a different thing and is what this lane is about.

---

## 5. What production real-time voice stacks actually do

### 5.1 Turn detection is where the modern money went

The old pipeline was: VAD says silence for N ms, therefore the turn ended. Every modern stack
has replaced that with a learned end-of-turn model, because a fixed silence threshold cannot
tell a thinking pause from a finished sentence.

**LiveKit turn detector.** [PRIMARY: docs.livekit.io/agents/build/turns/turn-detector/ and
livekit.com/blog/solving-end-of-turn-detection, both fetched]

- v1 is a **dual branch** model: "The semantic branch uses an audio encoder, a learned adapter,
  and a fine-tuned language model"; "The acoustic branch runs a separate encoder into a
  recurrent layer that captures timing and prosody"; a fusion module combines them. It consumes
  **audio directly**, and the blog states the payoff in one line: "The latency cost of waiting
  for a transcript is gone: inference proceeds directly from the audio stream."
- Published operating points: at a **300 ms latency budget, 9.9 percent false-cutoff rate**
  (against Deepgram Flux 12.9 percent and ultraVAD 27.7 percent); at **600 ms, 4.5 percent**
  (Soniox 5.5 percent, Deepgram Flux 9.9 percent). Inverted: "at a 5 percent false-cutoff target
  it reaches 543 ms mean latency, and at 10 percent it reaches 295 ms".
- Defaults with the audio detector: `min_delay` 0.3 s, `max_delay` 2.5 s, and a hard
  requirement that the VAD's `min_silence_duration` be at least 0.25 s.
- The deprecated **text** turn detector is the one whose cost is published: a 135M-parameter
  SmolLM v2 derivative over a sliding window of the last four turns, **396 MB on disk**,
  "Per Turn Latency ~50-160 ms" on CPU, defaults `min_delay` 0.5, `max_delay` 3.0, EMA
  `alpha` 0.9. A `v1-mini` variant quantizes and prunes the backbone for CPU inference.

Note the shape of those numbers: **295 ms of latency for a 10 percent false-cutoff rate.** That
is the same order as Baumann's 320 ms for 90 percent word stability, seventeen years apart, from
completely different machinery. The lag is not an implementation defect. It is the information
arriving.

**Pipecat Smart Turn v3.** [PRIMARY: daily.co blog "Announcing Smart Turn v3, with CPU inference
in just 12ms", plus huggingface.co/pipecat-ai/smart-turn-v3 and the pipecat docs, via search
result summaries; I did not fetch the model card itself]

- About **8M parameters**, a Whisper Tiny encoder with a linear classifier head. Open weights,
  open training data, open training script.
- **8 MB int8 CPU model: 12 ms inference on a modern CPU**, about 60 ms on a low-cost AWS
  instance, about 65 ms on a Pipecat Cloud standard instance. A 32 MB unquantized GPU variant is
  about 1 percent more accurate.

Eight megabytes and 12 ms is the number that matters for us. A turn detector is affordable on
an i5-8350U in a way that a streaming recognizer is not. [PRIMARY for the figures, INFERENCE for
the affordability conclusion; 12 ms was measured on an unnamed "modern CPU", so budget 30 to 60
ms on Kaby Lake-R and verify.]

### 5.2 The production pattern that matches hyprsay exactly: eager end of turn

Deepgram Flux. [PRIMARY: developers.deepgram.com/docs/flux/voice-agent-eager-eot and
developers.deepgram.com/docs/flux/configuration and
deepgram.com/learn/introducing-flux-conversational-speech-recognition, all fetched or
returned by search against the vendor domain]

Flux fuses transcription and turn detection into one model. Quoted: "the same model that
produces transcripts is also responsible for modeling conversational flow and turn detection".
It exposes three events, and they are the cleanest published statement of the speculative
protocol:

| event | meaning | what the client does |
|---|---|---|
| `EagerEndOfTurn` | moderate confidence the user has finished | start drafting the reply, do not commit |
| `TurnResumed` | they were not finished after all | cancel the draft |
| `EndOfTurn` | high confidence | commit, release the reply |

Parameters: `eot_threshold` valid 0.5 to 0.9, default **0.7**. `eager_eot_threshold` valid 0.3
to 0.9, **no default, eager mode off unless set**. Vendor guidance: with
`eager_eot_threshold` at 0.3 to 0.5 you get `EagerEndOfTurn` **150 to 250 ms earlier** than
`EndOfTurn`, at the cost of **50 to 70 percent more LLM calls**. The stated rule, verbatim:
"Avoid committing to a reply until EndOfTurn. Use EagerEndOfTurn outputs to draft, not
finalize." [VENDOR for the millisecond delta and the call-count increase; PRIMARY that the
parameters and events exist with those ranges]

Flux's own turn-detection claims: median end-of-turn latency 200 to 600 ms faster than pipeline
approaches, P90 about 1 s, P95 about 1.5 s, false interruptions down about 30 percent. [VENDOR]
LiveKit's independent comparison puts Flux at 12.9 percent false cutoff at a 300 ms budget and
9.9 percent at 600 ms, behind their own v1. [PRIMARY, but it is a competitor's benchmark]

Two things to take from this, and only two:

1. **The protocol shape is settled industry practice, not an experiment.** Draft on a
   moderate-confidence signal, cancel on resumption, commit on a high-confidence signal, and
   accept a 50 to 70 percent increase in speculative work as the price. hyprsay's version of
   "commit" is key release, which is a far HARDER signal than any turn detector produces, so
   hyprsay can speculate more aggressively than Flux's customers can and still be safer.
2. **Two thresholds, not one.** The eager threshold and the commit threshold are separate knobs
   with separate ranges. hyprsay's equivalents are "how many agreeing rungs before I speculate"
   (one) and "how many before I act early" (two, plus tier 0, plus a closed clause).

**AssemblyAI Universal-Streaming** is worth one line as the other commercial answer: it ships
**immutable** transcripts at about 300 ms, meaning "the text that has already been produced will
not be overwritten in future transcription responses". [VENDOR] That is Selfridge's Immortal
partial sold as a product. The engineering trade is the same one Baumann priced in 2009: they
hold text back until it cannot change, and 300 ms is what that costs.

### 5.3 What buys the speed, ranked

From the above sources taken together: [INFERENCE, but each ingredient is sourced above]

1. **Not waiting for a transcript.** LiveKit's v1 moved end-of-turn detection off the transcript
   and onto the audio, and states that as the reason the latency cost went away. Any decision
   that can be made from audio should not wait for text.
2. **A learned endpoint instead of a silence timer.** 295 ms at 10 percent false cutoff beats
   any fixed threshold that is safe enough to use.
3. **Overlapping the network with the human.** Everything after the endpoint is serial;
   everything before it is free. This is where hyprsay has the most headroom, because its
   network call is 315 ms and its speaker is holding a key.
4. **Partitioning by reversibility** so the speculative work can run without a safety argument
   for each case (Berkeley paper, section 4.1).

---

## 6. LOCAL: re-examining "streaming is not viable on this CPU"

### 6.1 What the previous lanes actually established

Re-parsed from the raw JSON on disk, not from the prose. [LOCAL]

`docs/research/live/stt_bakeoff_out.json`, machine at loadavg 1.2 to 1.5 (genuinely idle),
eight command clips of 0.85 to 1.56 s, three repetitions, one thread:

| engine | decode of a whole command | n |
|---|---|---|
| local moonshine-tiny | **median 30 ms** (23 to 55) | 24 |
| local parakeet-110m | **median 93 ms** (72 to 137) | 24 |
| gateway fish-audio/transcribe-1 (batch) | 452 ms (370 to 752) | 24 |
| gateway spacexai/grok-stt (batch) | 699 ms (489 to 1024) | 24 |
| gateway openai/gpt-4o-mini-transcribe | 697 ms (589 to 881) | 24 |
| gateway openai/whisper-1 | 1164 ms (622 to 6121) | 24 |
| gateway google/gemini-3.5-transcribe | 3180 ms (2552 to 3619) | 24 |

`docs/research/live/stt_streaming_out.json`, the real WebSocket streaming route, audio paced in
real time in 20 ms frames, 16 repetitions each:

| model | socket open | open to `stream-start` | first partial after audio start | audio-done to final | partials per clip |
|---|---|---|---|---|---|
| `google/gemini-3.5-transcribe-live` | 265 ms | 344 ms | **770 ms** (662 to 1331) | **3322 ms** (3228 to 3405) | 3 |
| `spacexai/grok-stt` | 220 ms | **713 ms** (643 to 1014) | **1520 ms** (1205 to 2047) | **442 ms** (344 to 842) | **1** |

`openai/gpt-realtime-whisper` produced no streaming rows in that file. NOT FOUND.

This table kills the cloud-streaming-for-partials idea on its own, and it is worth being
explicit about why, because the README's "442 ms for grok-stt" reads like good news:

- **grok-stt emits exactly ONE partial per command, and it arrives 1.5 s after the audio
  starts.** The commands are 0.85 to 1.56 s long. The single partial therefore lands at or after
  the end of a typical hyprsay utterance. There is nothing to act on early. Its 442 ms
  finalization is a good batch number wearing a streaming costume.
- **grok-stt needs 713 ms from socket open to `stream-start`**, during which the client must
  buffer. That is half a command spent getting ready.
- **gemini-3.5-transcribe-live does give real partials at 770 ms**, which is genuinely
  mid-utterance, but its finalization is 3.3 s, which is unusable as an endpoint, and its
  partial count is 3 for a 1 s clip.

[LOCAL, all of it, re-derived from the JSON by this lane.]

### 6.2 The option nobody measured: rescan the buffer with the OFFLINE decoder

hyprsay's decoder is an `OfflineRecognizer`. Nothing stops it from being called on the first
0.8 s of the buffer while the key is still down. The previous lane rejected STREAMING models
(cache-aware conformers at RTF 1.33, Moonshine Voice at 386 percent CPU) and never priced this.

I measured it. Script:
`/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/vision/prefix_rescan.py`
Raw output: `prefix_parakeet.json`, `prefix_moonshine.json` in the same directory.
Method: load the model exactly as `src/hyprsay/stt/local.py` `_build` does (one thread),
decode the full clip once for a gold reference, then decode every prefix in 200 ms steps and
record wall time, whether the prefix decode is a word-prefix of the final, and for each final
word the earliest prefix length after which it stayed put in every later rung.

Machine state: loadavg 11 to 16 during the runs, so these are PESSIMISTIC. Audio is the
7.43 s read-speech clip shipped with the model, because no recorded command corpus exists on
this machine (see NOT FOUND).

**Parakeet TDT 110M int8, one thread, 200 ms steps:** [LOCAL]

| prefix | decode wall time | words out | is a prefix of the final | text |
|---|---|---|---|---|
| 0.2 s | 53 ms | 0 | yes | (empty) |
| 0.4 s | 72 ms | 0 | yes | (empty) |
| 0.6 s | 78 ms | 0 | yes | (empty) |
| 0.8 s | 99 ms | 2 | yes | Well, I |
| 1.0 s | 122 ms | 3 | yes | Well, I don't |
| 1.2 s | 160 ms | 4 | yes | Well, I don't wish |
| 1.4 s | 225 ms | 6 | no | Well, I don't wish to say |
| 1.6 s | 257 ms | 7 | yes | Well, I don't wish to see it |
| 1.8 s | 264 ms | 8 | no | ... see it anymore. |
| 2.0 s | 271 ms | 9 | yes | ... see it any more. |

- Over all 37 rungs: **30 of 37 (81 percent) were exact word-prefixes of the final.** Compare
  Selfridge's Basic ISR at 7 to 11 percent. An offline rescan of the whole prefix is an order of
  magnitude more stable than a frame-synchronous partial, because it re-optimizes over all the
  audio it has rather than extending a beam.
- **The WFF analogue is tight.** Words settled at roughly their own end time: "well" and "i" at
  0.8 s, "don't" at 1.0 s, "wish" at 1.2 s, "to" at 1.4 s, "see"/"it" at 1.6 s.
- **Cost is quadratic in utterance length.** The full 7.43 s ladder cost 17.9 s of CPU, 2.41
  times real time. Unusable for dictation. But over the first 2.0 s, the ten rungs together cost
  **1600 ms of CPU for 2000 ms of audio, 0.80 of one core** of four. At command length the
  ladder is affordable; at paragraph length it is not.
- The empty strings at 0.2 to 0.6 s matter: **the transducer degrades to silence on truncated
  audio rather than inventing words.**

**Moonshine Tiny (sherpa offline export), same ladder:** [LOCAL]

- **It hallucinates on short prefixes.** 0.2 s gave "For the", 0.4 s "And then,", 0.6 s "You",
  none of which are in the audio. Encoder-decoder models of the Whisper family do this; it is
  the same failure that makes Whisper emit "Thank you for watching" over silence.
- Prefix correctness 24 of 37 (65 percent) against Parakeet's 81 percent.
- Cost over the first 2.0 s: 1591 ms, essentially the same as Parakeet, because Moonshine's
  advantage is on whole short clips, not on the ladder.

**Conclusion: if hyprsay rescans prefixes, it must use the transducer (parakeet-110m, already
the default) and must not use the Moonshine path.** This is a new, concrete constraint that no
existing hyprsay document states. [LOCAL]

### 6.3 A cheaper ladder

The ladder above is deliberately naive: every 200 ms from the start. Two refinements cut the
cost by most of it, at no stability cost: [INFERENCE from the measured rung costs]

- **Do not start before there is a word.** Rungs at 0.2, 0.4 and 0.6 s produced nothing and cost
  203 ms of CPU between them. Gate the first rescan on the existing energy gate having counted
  its `MIN_VOICED_FRAMES` plus about 400 ms, which `audio.py` already tracks.
- **Widen the step as the buffer grows.** Cost per rung grows with prefix length, so a constant
  step spends the most CPU where the marginal information is least. A step that grows (250 ms,
  then 350, then 500) keeps total cost near linear.

A realistic command-length ladder is therefore about three rescans: at roughly 0.7 s, 1.0 s and
1.35 s into a 1.5 s utterance, costing roughly 99 + 122 + 225 = **446 ms of CPU spread over
650 ms of wall time**, which is well under one core of four while the user is still talking.
[INFERENCE, arithmetic over the measured rungs]

---

## 7. The design: act while the user speaks, on this machine

Nothing here needs a new model, a new dependency, or a cloud call. It is a scheduling change
plus one new gate.

### 7.1 The pipeline

```
key down    pin focus; open mic; prewarm the Jev connection (already done today)
            [existing: audio.py energy gate frames every 20 ms]

while held  the SPECULATION LOOP, off the event loop, one worker:
              wait until the gate has seen speech plus about 400 ms
              rescan  = recognizer.transcribe(recorder.snapshot())      93 to 225 ms
              parse   = normalize + grammar                              about 1 ms
              if the DECISION is unchanged from the previous rescan:
                    stability_count += 1
              else: stability_count = 1; cancel any in-flight Jev call
              if grammar missed and this decision text is new:
                    fire the Jev fan-out speculatively, keep the future
              if tier == 0 and stability_count >= 2 and the clause is closed:
                    ACT NOW, record an undo token, show it in the overlay
              sleep until the next rung (250, then 350, then 500 ms)

key up      if no voiced frame after the last snapshot (audio.py voiced_after already
            answers this) and a speculative decision exists:
                    reuse it. No decode, no network. Latency is the executor only.
            else: one final rescan on the whole buffer, reuse the in-flight Jev future
                    if its transcript still matches, otherwise re-ask.
```

### 7.2 The commit rule, which is the whole safety argument

Three conditions, all of which must hold before an action fires before key release:

1. **Decision stability, not word stability.** The `Decision` produced by two consecutive
   rescans must be identical: same action, same target address, same tier. This is Selfridge's
   closing suggestion applied literally, and it is far more forgiving than word stability
   because `nlu/normalize.py` already collapses "fire fox", "firefox" and "Firefox." to the same
   candidate. Two consecutive agreeing rungs at a 250 to 350 ms step is a 250 to 350 ms
   smoothing window, which Baumann measured as the cheapest way to buy stability (EO to 10
   percent at 320 ms of smoothing, against 1150 ms of right context).
2. **Tier 0 only.** Focus a window, switch workspace, scroll. These are the actions whose undo
   is another action of the same kind, and the tier table in `nlu/tiers.py` already separates
   them. Every tier above 0 waits for key release exactly as today. This is the Berkeley paper's
   safe/unsafe partition, and hyprsay gets it for free.
3. **A closed clause.** The prefix must not end mid-phrase. Cheapest usable test, in order of
   cost: the grammar matched a complete pattern (about 1 ms, no model); or the trailing 150 ms
   of audio is unvoiced by the existing energy gate; or, if a turn detector is added later,
   its end-of-turn probability clears a threshold.

And one rule about what happens when the rest of the sentence contradicts the prefix:
**every speculative act records its inverse before it fires, and the inverse runs if the final
decision differs.** This is not wishful: `src/hyprsay/ops.py` line 118 declares
`inverse: Callable[[Action, DesktopState], Action | None]` as a field of the operation record,
and its module docstring says "Every inverse is computed from the desktop as it was BEFORE the
write, and is itself an ordinary `Action`, so 'undo' goes through the same checks as anything
else." Line 631 even notes "The executor asks for the inverse BEFORE performing". The
machinery a rollback needs already exists and already runs through the same guards. [LOCAL,
read in the source]

Two caveats that the plan must respect. The signature returns `Action | None`, so **some
operations have no inverse and those must never be speculated**; line 705 says so for clicks
("a click can send a message or spend money and has no inverse at all") and line 648 for Home
and new-tab. And line 612 warns that a dwindle tree "does not promise that right undoes left",
so directional focus moves are best-effort undo, not exact. Speculate only where
`inverse(...)` returns a non-None Action on the pre-write state.

### 7.3 The latency budget

Inputs, all [LOCAL] unless marked:

| term | value | source |
|---|---|---|
| capture flush | about 40 ms | PLAN.md 10, marked assumed |
| parakeet rescan of a 1.0 s prefix | 122 ms at loadavg 11 | this lane |
| parakeet decode of a whole 1.2 s command | 93 ms median at idle | stt_bakeoff_out.json |
| normalize plus grammar | about 1 ms | README |
| Jev fan-out, warm connection | 315 ms p50, 356 p90, 458 max at 3 windows; 448 p50 at 130 windows | dayone_out.json |
| Jev with 40 questions rather than 1 | 317 ms p50, indistinguishable | dayone_out.json `latency_vs_questions` |
| three concurrent Jev calls | 359 ms p50 wall | dayone_out.json `concurrent_3_wall_ms` |
| executor plus one guarded dispatch | under 1 ms | PLAN.md, assumed |
| Hyprland's own window animation | 500 ms or more | README |

Three cases, measured from the user's last word:

| case | today | with this design |
|---|---|---|
| Grammar hit, tier 0, key released promptly | release lag + 40 + 93 + 1 + dispatch = **release lag + 135 ms** | **negative**: the action has already happened, typically 150 to 400 ms BEFORE the last word ends, because the decision settled on an earlier rung |
| Jev path, tier 0 | release lag + 40 + 93 + 315 = **release lag + 450 ms** | **0 to 50 ms after the last word**, if the speculative Jev call was fired one rung earlier: the 315 ms round trip overlaps the final 315 ms of speech |
| Jev path, tier 1 or above | release lag + 450 ms | **release lag + about 30 ms**, because the decode and the Jev round trip are already done; only the fresh-state re-check and dispatch remain |

The third row is the honest headline. **Even with no early acting at all, speculation removes
about 400 ms from every non-trivial command**, because the two slow things (decode, network)
move from after the release to during the speech. It also removes the case the owner will
notice most: the model call no longer happens in silence while nothing is on screen.

The key-release lag is the free money here and nobody has measured it. `docs/PLAN.md` phase P0
lists "Release lag, first-word clipping: measured on the corpus" as an open item and it is still
open. Human push-to-talk release lag is typically a few hundred milliseconds. Every millisecond
of it is a millisecond of Jev round trip that costs nothing. [INFERENCE; NOT FOUND for the
actual number on this owner.]

### 7.4 What it costs

- **CPU:** about 450 ms of one core spread over the 650 ms before release, on a four-core
  machine, for a three-rung ladder on a 1.5 s command. Idle cost unchanged. Long utterances must
  be excluded by a hard cap (stop rescanning past about 2.5 s of buffer) or the ladder goes
  quadratic; at 7.4 s it was 2.4 times real time.
- **Money:** Jev is billed per call and the probe shows `marketCost` 0.000011802 USD for a small
  request. A ladder that fires at most two speculative Jev calls per utterance roughly doubles
  or triples Jev volume, which is still under a hundredth of a cent per command. Cost does not
  discriminate. [LOCAL, probe_out.json]
- **Reliability:** more Jev calls means more exposure to the 503 rate the followup measured, 4
  of 25 on large bodies against 0 of 25 on small. Speculative calls must carry the SMALL body
  and must fail silently into the existing key-up path. [LOCAL, followup_out.json]
- **Complexity:** one new async task, one new dataclass (the speculation: snapshot length,
  decision, stability count, Jev future, undo token), one new config block. No new dependency.
  The riskiest part is cancellation correctness, which is why the speculation must live on the
  same asyncio loop that already owns the conversation, with the decode on the existing worker.

### 7.5 What it risks, stated without softening

1. **Acting on a prefix the rest of the sentence changes.** This is real and it is measured:
   the best production system in Full-Duplex-Bench-v3 passes self-correction 58.8 percent of the
   time. The mitigations here are tier 0 plus a recorded inverse, which means the worst case is
   a visible flicker of focus, not a lost window. If the inverse is ever unavailable for a tier 0
   op, that op must not be speculated.
2. **"open firefox and move it to workspace three".** The prefix "open firefox" is a complete,
   stable, tier 1 clause. It would not fire early under this design (tier 1 waits), which is
   correct, but it shows the trap: a prefix can be a perfectly good command and still be the
   wrong thing to do. The closed-clause test must never be satisfied by "the grammar matched"
   alone when the audio continues. Requiring 150 ms of trailing silence as well is what prevents
   it.
3. **The overlay becomes a liar.** If hyprsay shows a speculative transcript that then changes,
   the user sees the system stutter. Baumann's smoothing result applies to the display too:
   show a rung only after it has survived one more rung, or show nothing but the level meter.
4. **A rescan competes with the compositor.** One core of four while an animation runs is a
   real cost on a 15 W part. The ladder must be a nice-value-lowered thread and must stop the
   moment the key is released.
5. **French AZERTY and a French speaker.** Every local number in this lane is English. The
   models are English-only. Nothing in this design changes that, and a non-English utterance
   will produce unstable rungs that never agree twice, which fails closed into today's
   behaviour. That is the right failure but it means the feature will feel absent, not broken.

### 7.6 The cheapest first step, if only one thing gets built

Wire the existing primitives, without early acting. On 150 ms of trailing silence during the
hold, call `recorder.snapshot()`, decode, understand, and fire Jev. On key up, if
`recorder.voiced_after(snapshot_seconds)` is False, use the result you already have. That is
`docs/PLAN.md` 5.2 as written, it needs no new concept, it is maybe 60 lines in `daemon.py`, and
it removes roughly 400 ms from the Jev path with **zero** risk of acting on a changed prefix,
because the commit still happens at key release. Early acting (section 7.2) is the second step
and should not be attempted before the first one is instrumented.

---

## 8. Not found

Stated plainly, because the lane rules ask for it.

- Exact figures from DeVault, Sagae and Traum. I confirmed the papers, venues, authors and the
  capability they claim, but neither full text came back readable, so no percentage from them is
  quoted here.
- Any measured key-release lag for this owner, or any recorded command corpus in this owner's
  voice on this machine. `docs/PLAN.md` P0 still lists both as open. Every "release lag" term in
  section 7.3 is therefore a symbol, not a number.
- Streaming rows for `openai/gpt-realtime-whisper` in `stt_streaming_out.json`. The file has
  gemini live and grok-stt only.
- The parameter count and CPU inference time of the LiveKit v1 AUDIO turn detector. The blog
  gives operating points but, in its own words, no parameter counts and no millisecond
  inference figures. Only the deprecated TEXT detector's 396 MB and 50 to 160 ms are published.
- Whether Pipecat Smart Turn v3's 12 ms holds on Kaby Lake-R. The figure is for an unnamed
  "modern CPU".
- WHICH tier 0 operations return a non-None inverse. `ops.py` proves the field exists and that
  some operations return None; I did not enumerate the table operation by operation. That
  enumeration is a half-hour job and it is a precondition for early acting.
- The exact millisecond delta of Deepgram's `EagerEndOfTurn` on real traffic. The 150 to 250 ms
  and the 50 to 70 percent more LLM calls are the vendor's own guidance, not an independent
  measurement, and the doc page itself declines to state the numeric ranges that the
  configuration page carries.
- Any published edit-overhead or prefix-stability number for a modern neural transducer
  (Parakeet, Zipformer, Nemotron) under prefix rescanning. My section 6.2 measurement appears to
  be the only such number in hand, and it is n=1 clip on read speech, not commands.

---

## 9. What to build, in order, with exit criteria

Every step is demoable and each one is worth shipping on its own.

**S0. Measure the thing nobody measured (half a day, no code in the daemon).**
Record 150 commands in the owner's voice with `hyprsay record`. For each clip, measure the gap
between the last voiced frame and the key-release event (the release lag), and run the prefix
ladder of section 6.2 over every clip. Exit criterion: a distribution for release lag, and the
prefix-stability number for real commands rather than for one read-speech clip. If the median
release lag is under 150 ms, the whole Jev-overlap argument shrinks and step S2 should be
re-costed before it is built.

**S1. Speculative finalize, commit at key release (about 60 lines in `daemon.py`).**
Exactly `docs/PLAN.md` 5.2, using `recorder.snapshot()`, `recorder.trailing_silence(150)` and
`recorder.voiced_after()`, which all already exist. No early acting, no rollback, no new risk.
Exit criterion: on the 150-clip corpus replayed with a synthetic release lag, the median
key-release to dispatch for Jev-path commands drops from about 450 ms to under 100 ms, and the
wrong-action rate in `evals/` is unchanged at zero for the hostile-title cases.

**S2. Decision-level stability and the speculative Jev call (a day).**
Run the ladder of section 6.3 while the key is held, keep the last two `Decision` objects,
compare them for equality on (action, target address, tier), and fire the Jev fan-out on the
first rung whose decision is new. Reuse the in-flight future at key release when the final
transcript still produces the same decision. Exit criterion: measured Jev-call count per
utterance (expect 1.5 to 2.5), measured wasted-call fraction, and no increase in the 503 rate
beyond what `followup_out.json` already shows for small bodies.

**S3. Early acting, tier 0 only, with a recorded inverse (a day, plus the ops audit).**
Precondition: the enumeration of which tier 0 operations return a non-None inverse. Fire on two
consecutive agreeing decisions plus a closed clause. Record the inverse before the write. On key
release, if the final decision differs, run the inverse and then the final action. Exit
criterion: a new eval category "prefix changed after the act", built by truncating corpus clips,
with a hard requirement that the observable end state always equals the end state of the
non-speculative path.

**S4. Optional: a real turn detector instead of the energy gate (a day, 8 MB).**
Pipecat Smart Turn v3's int8 CPU model is 8 MB and is claimed at 12 ms on a modern CPU, open
weights and open training data. It would replace the "150 ms of trailing silence" heuristic in
the closed-clause test with a learned end-of-utterance probability, which is what every
production stack moved to. Measure it on Kaby Lake-R first: if it is over about 60 ms it is not
worth the rung budget. This is the only step that adds a dependency and it should be last.

**What NOT to build.** Do not put cloud streaming transcription on the hot path. The measured
numbers in section 6.1 are conclusive: grok-stt gives one partial per command and it lands
after the command is over; gemini live finalizes in 3.3 s. Do not switch the local model to
Moonshine for the ladder: it hallucinates on truncated audio. Do not try to predict the rest of
the sentence: Baumann measured 10 to 15 percent correctness 100 to 170 ms forward.

## Verification

Skeptic pass, 2026-09-22. Method: for each of the six findings a plan would lean on hardest,
an independent check using a different source or a direct re-measurement than the one the
researcher used. Work in progress below; each entry is finalised when its check completes.

Summary of the pass: every local measurement reproduces, and every paper citation I could
open is accurate down to the table cell. What does not survive is some of the framing. Three
of the six headline findings carry a conclusion wider than the evidence under it, and two
vendor numbers are wrong in the direction that flatters the design.

| Finding | Verdict |
| --- | --- |
| F1 prefix rescan is cheap and 81 percent stable | overstated |
| F3 cloud streaming cannot supply mid-utterance partials | overstated |
| F6 Deepgram Flux EagerEndOfTurn protocol and thresholds | overstated |
| F7 Full-Duplex-Bench-v3, arXiv 2604.04847 | overstated |
| F8 Speculative Interaction Agents, arXiv 2605.13360 | confirmed |
| F9 ops.py already carries the inverse machinery | confirmed |
| bonus: Baumann 2009 stability numbers | confirmed |
| bonus: F13 speculative finalize is specified, not built | confirmed |

### F1 Prefix rescan of the offline Parakeet decoder. Verdict: overstated

Reproduced, then undermined. I re-ran the researcher's own ladder script with the project
venv on the same wav, and separately on a second clip.

What holds. The decode is deterministic: my re-run of the 7.43 s ladder produced byte
identical text at all 37 rungs, the same 30 of 37 prefix-correct count, and the same final
transcript. The ladder also keeps up with real time on one thread out to about 2.8 s of
audio, which covers the whole command window. Cumulative rung cost against prefix length, my
run: 340 ms spent by the time 800 ms of audio exists, 816 ms by 1.4 s, 1498 ms by 2.0 s,
2768 ms by 2.8 s, where it crosses over and starts falling behind.

What does not hold.

1. The sample is one utterance, and it is not a command. The wav is
   `sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8/test_wavs/0.wav`, the model
   vendor's own test clip, a 7.43 s literary read ("Well, I don't wish to see it any more,
   observed Phoebe..."). Zero hyprsay commands were rescanned. The claim's own headline
   phrase, "highly stable at command length", rests on no command-length utterance.
2. The 81 percent includes three free passes. Rungs at 0.2, 0.4 and 0.6 s decode to the empty
   string, and the script scores `[] == final[:0]` as a correct prefix. Non-empty prefix
   correctness is 27 of 34, 79 percent. Within the band that matters for a 0.85 to 1.56 s
   command (prefix at or below 2.0 s), it is 5 of 7 non-empty rungs, 71 percent.
3. The dangerous failure is inside that band and it is deterministic. At the 1.4 s rung both
   runs decode "Well, I don't wish to say" where the truth is "to see". That is a confident
   wrong content word at exactly the horizon a speculative act would fire on, and it is not
   noise: it reproduced exactly.
4. The latency figures are single samples with about 30 percent spread, and the "pessimistic
   because loadavg 11" defence does not hold. My re-run ran at loadavg 9.3, lower than the
   original 11.2, and was SLOWER at the two rungs the finding quotes: 132.7 ms at 0.8 s
   against the quoted 99.1, and 149.9 ms at 1.0 s against the quoted 121.8. Loadavg is not a
   control. Budget 100 to 135 ms at 0.8 s, not 99.
5. "0.80 of one core of four" understates the denominator arithmetic: `nproc` on this machine
   reports 8 logical cores (4 physical, SMT).

What I could add in its favour. On a second clip, `test_wavs/en-english.wav`, 0.99 s, which
is genuinely command length, a 100 ms ladder gave 9 of 9 prefix-correct rungs with the phrase
settled from 0.5 s onward, and 106 ms at the 0.8 s rung. That is one more data point, on an
easy three-word phrase, from the same vendor test directory. It is encouraging, not evidence.

Consequence for the plan: the technique is real and cheap, and the recommendation to rescan
rather than stream survives. The number 81 percent does not transfer, and no rung threshold
should be chosen until the ladder has been run over the eight recorded command clips the
bakeoff already uses.

### F3 Cloud streaming cannot supply mid-utterance partials. Verdict: overstated

I re-parsed `docs/research/live/stt_streaming_out.json` independently and read
`stt_streaming.py`. Every number the finding quotes reproduces exactly: `spacexai/grok-stt`
emits exactly 1 partial on all 16 rows, first partial median 1520 ms after audio start, range
1205 to 2047, stream-start median 713 ms from socket open, final after audio-done median
442 ms. `google/gemini-3.5-transcribe-live` first partial median 770 ms, final after
audio-done median 3322 ms.

Two corrections that matter.

1. The harness opens a NEW websocket for every clip. `stream_start_ms` is measured from
   socket open and its median is 713 ms with a median `open_ms` of 220, so roughly 490 ms of
   per-clip server-side stream setup sits inside the "1520 ms after audio start" figure,
   because `t_audio0` is set the moment feeding begins, before `stream-start` arrives. A
   daemon holding one socket open would plausibly see a first partial near 1030 ms, which
   lands inside a 1.56 s command rather than after it. The finding's conclusion is right for
   short commands and not established for long ones.
2. gemini-3.5-transcribe-live emits 1 to 4 partials, median 3, not "3 partials". And n=16 is
   8 clips repeated twice, not 16 independent utterances.

Small attribution slip: the 442 ms figure the finding attributes to the README is in
`docs/PLAN.md` line 44, not the README.

### F6 Deepgram Flux eager end of turn. Verdict: overstated

The protocol and the operating guidance are real and I confirmed the two key sentences
verbatim on the vendor page: "Avoid committing to a reply until `EndOfTurn`" and "Use
`EagerEndOfTurn` outputs to draft, not finalize". The three events EagerEndOfTurn,
TurnResumed and EndOfTurn are documented, and `eager_eot_threshold` is 0.3 to 0.9 with no
default, so eager mode is off unless set. The "50-70% more LLM calls" is quoted correctly.

Two numbers are wrong.

1. `eot_threshold` range is 0.5 to 1.0 with default 0.7 in the configuration docs, not "0.5
   to 0.9".
2. The vendor does not say eager fires "150 to 250 ms earlier". The sentence carrying that
   trade-off reads "Good for trimming that last 100-200ms of end-to-end latency at the cost
   of 50-70% more LLM calls". The finding inflated the saving by roughly 50 ms at each end.
   The docs elsewhere say "hundreds of milliseconds" without a figure.

The shape of the argument survives intact. The prize is smaller than quoted, and the price
tag is the vendor's own, unmeasured by anyone independent.

### F7 Full-Duplex-Bench-v3. Verdict: overstated

The paper exists and the hard numbers are right. arXiv:2604.04847v1, 6 April 2026, Guan-Ting
Lin (NTU), Chen Chen (NVIDIA), Zhehuai Chen (NVIDIA), Hung-yi Lee (NTU). Self-correction
Pass@1: GPT-Realtime 0.588, Gemini Live 2.5 0.471, Gemini Live 3.1 0.353, Ultravox 0.353,
Grok 0.294, Cascaded 0.176. "21 of our 100 scenarios" carry self-corrections. All six values
and both counts match the finding exactly.

The latency claim is wrong. The finding says "mean latency to first tool call was 2.21 s for
the fastest system (Gemini Live 3.1)". Table 6 gives tool-call latency of Grok 0.63 s,
Gemini Live 3.1 2.21 s, Cascaded 3.15 s, GPT-Realtime 3.89 s, Gemini Live 2.5 4.61 s,
Ultravox 6.01 s. Grok is the fastest to a tool call by a factor of three and a half. Gemini
Live 3.1 is fastest on TASK COMPLETION at 4.25 s, which is the column the finding's 4.25
came from. So the rhetorical line "the fastest of them takes 2.21 s against hyprsay's 315 ms"
should read 0.63 s against 315 ms, a gap of about 2x rather than 7x. Note also that Grok is
both the fastest to act and the second worst at self-correction, at 0.294, which is a sharper
version of the same warning and worth making instead.

Minor: the table I read names the system "Ultravox", not "Ultravox v0.7". The version is
unverified.

### F8 Speculative Interaction Agents. Verdict: confirmed

arXiv:2605.13360, v1 13 May 2026, v2 14 May 2026, ten authors headed by Coleman Hooper,
Minwoo Kang, Suhong Moon, through to Amir Gholami and Kurt Keutzer. The mechanism is quoted
accurately. Tool calls carry integer IDs from 1; "To modify a tool call, the model needs to
generate a tool call with the same ID as the one that it wants to overwrite"; "To remove a
tool call, the model instead generates a special tool call 'REMOVE ID.'"; "If there were any
tool calls which depended on the output of the cancelled tool call, these are also
cancelled". Tools are "manually classified as either safe or unsafe to execute speculatively
based on whether the tool has any side effects", read-only being safe, and unsafe calls are
held until a confirmation signal. Speedups 1.3 to 1.7x on the OpenAI Realtime API and 1.6 to
2.2x on Qwen2.5-3B-Instruct and Llama-3.2-3B-Instruct. HotpotQA 71.6 percent baseline against
71.0 percent speculative is the cloud row and is quoted correctly.

One thing the finding left out, and it cuts against hyprsay. The cloud row is where the
accuracy loss is smallest. On the edge models, the ones that resemble a laptop, the drop is
larger: Qwen 68.6 to 67.5 and Llama 70.4 to 68.7. The honest summary is "about half a point
on a large hosted model, one to two points on a small local one".

Affiliations: the abstract page does not print them, so UC Berkeley, ICSI and LBNL remain the
researcher's inference from the author list. Plausible, not verified here.

### F9 The rollback machinery already exists in ops.py. Verdict: confirmed

Every line citation is exact. `src/hyprsay/ops.py:118` is
`inverse: Callable[[Action, DesktopState], Action | None]`; line 612 is the dwindle-tree
warning; line 631 is "executor asks for the inverse BEFORE performing, so this reads the old
level"; line 705 is "a click can send a message or spend money and has no inverse at all".
The ordering claim holds in code, not only in a comment: `src/hyprsay/executor.py:168` calls
`_quietly(operation.inverse, checked, fresh)` and line 169 then calls `operation.perform`.
Line 170 drops the inverse when the message starts with DRY RUN or "Nothing changed".

Two refinements the plan needs.

1. SCROLL is not in TIER0. `nlu/tiers.py` TIER0 is FOCUS_WINDOW, SWITCH_WORKSPACE,
   FOCUS_DIRECTION, HELP, CANCEL, PICK, NONE. Scroll is in IN_APP and gets its tier from
   `inapp.scroll(...).tier` at runtime, whose docstring does claim tier 0. So the finding's
   parenthetical "(focus, workspace switch, scroll)" is right in effect and wrong in
   mechanism, and a speculation gate written against TIER0 membership would silently exclude
   scroll.
2. The intents with `_none` as their inverse are CLOSE_WINDOW, LAUNCH_APP, TYPE_TEXT,
   LOCK_SCREEN and CLICK_CONTROL. LAUNCH_APP is the one that hurts. The owner's complaint
   number 3 is precisely about launching, and launching is the single most-wanted early act
   with no inverse at all. A speculation rule of "only where inverse() returns non-None"
   therefore excludes app launch by construction, and the plan must either say so plainly or
   propose an inverse for it (close the window this launch created, which is tier 2 and by
   the codebase's own rule an undo may never cost more than the thing it undoes).

### Bonus check: Baumann, Atterer, Schlangen 2009. Verdict: confirmed

I pulled the PDF and extracted the text. Verbatim: "it rises above 90 % for a correction time
of 320 ms and above 95 % for 550 ms... we can be certain to 90 % (or 95 %) that a current
correct hypothesis about a word will not change anymore once it has not been revoked for
320 ms (or 550 ms respectively)". Smoothing: "The edit overhead falls rapidly, reaching 50 %
... with only 110 ms ... and 10 % with 320 ms. The same thresholds are reached through the
use of right context at 530 ms and 1150 ms respectively". Forward prediction: "15 % of our
hypotheses will still be correct 100 ms in the future and 10 % will still be correct for
170 ms". Edit overhead 90.5 percent and "for every neccessary add message, there are nine
superfluous" both appear as quoted.

The omitted caveat. The same paragraph says smoothing's r-correctness "is poor, even under
the 'fair' measure... due to correct hypotheses being held back too long". So the finding's
advice, prefer smoothing over right context, is the paper's own result on edit overhead but
not a free lunch: smoothing delays correct hypotheses as well as wrong ones.

### Bonus check: F13, speculative finalize specified but not built. Verdict: confirmed

`docs/PLAN.md` section 5.2 describes the snapshot at 150 ms of trailing silence, decode,
grammar, Jev fan-out and reuse on key up. `src/hyprsay/audio.py` has `snapshot()` at line 226
with the docstring "For the speculative decode (PLAN 5.2)", `trailing_silence(ms)` at 245 and
`voiced_after(seconds)` at 257. `daemon.py` line 226 is the only caller and it is
`voiced_after(0.0)` in the key-up path. Nothing calls `trailing_silence` or `snapshot`.

One correction that changes a different finding: PLAN 5.2 says "Default Moonshine Tiny ...
Parakeet TDT 110M selectable", but `src/hyprsay/config.py:40` is
`local_model: str = "parakeet-110m"` and `stt/local.py:37` defaults to the same. Parakeet is
the default today and the PLAN text is stale. Finding 2's premise is safe.
