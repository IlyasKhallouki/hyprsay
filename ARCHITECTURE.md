# Architecture

hyprsay is two processes and one inherited library. This page says what each part owns and,
more usefully, why the boundaries are where they are. The plan of record, with the evidence
behind each decision, is [`docs/PLAN.md`](docs/PLAN.md).

```
 Hyprland ── .socket.sock (requests) ── .socket2.sock (events, including the push-to-talk key)
    ▲  │
    │  ▼
 ┌──────────────────────────── engine: src/hyprsay, a venv, one asyncio loop ─────────────┐
 │ world.py        raw-socket client, fail-closed snapshots, live DesktopState            │
 │ activation.py   key down / key up, maximum hold, forced release on lock                │
 │ audio.py        capture, level meter, energy gate, abort() zeroes the buffer           │
 │ stt/            local (sherpa-onnx), gateway, hybrid                                   │
 │ nlu/            normalize → grammar → Jev fan-out → resolve + corroborate → tiers      │
 │ jev/            the only code that talks to Jev: deadline, one retry, token cap        │
 │ lexicon.py      installed apps with TRUST, phonetic matching, corroboration            │
 │ lock.py         the latch: compositor, lock layers, logind; any doubt is locked        │
 │ ops.py          the operation table: how to do it, how to undo it                      │
 │ executor.py     the last gate before a write, on fresh state, on one worker thread     │
 │ journal.py      what was heard, decided, done. Never dictated text                     │
 │ daemon.py       the orchestration: what may start, what may leave, what must wait      │
 └───────────────────────────────┬────────────────────────────────────────────────────────┘
                                 │ NDJSON over a Unix socket (0600 in a 0700 directory)
                    ┌────────────▼────────────┐
                    │ hud/hyprsay_hud.py      │  system Python, GTK4 + gtk4-layer-shell
                    └─────────────────────────┘
 src/hypruse   the inherited control core. Every write to the desktop goes through its
               guarded functions; hyprsay swaps only its transport (process spawn → socket).
```

## Why two processes

PyGObject exists only as a distribution package and sherpa-onnx only as a pip wheel, so they
cannot share an interpreter. The split also means a GTK problem cannot take the engine down,
and the engine never blocks on, or fails because of, the overlay: `HudServer.send` drops
messages when nothing is connected. `hud/` imports nothing from `src/`; the few protocol
constants it needs are copied, and a test compares the copies.

## One vocabulary

`model.py` is the only thing every module shares. Everything in it is frozen: state is
replaced, never mutated, so a `Decision` can never drift from the `DesktopState` it was made
against. Trust travels in the types: `App.trusted`, `Candidate.corroborated`.

The flow is `Utterance → Transcript → Parse → Decision → Outcome`. A `Decision` is all the
executor and the overlay ever see: a `Verdict` (act, act and offer a swap, hints, countdown,
confirm by key, refuse, suggest, nothing), an `Action`, the candidates, a tier and a reason.

## Where the decisions are made

Not in the model. Jev answers typed questions about candidates that code built from the live
desktop; it cannot name a window that is not open or an app that is not installed. What an
answer is *allowed to do* is decided in `nlu/tiers.py` and `nlu/understand.py`:

- **Agreement.** The window question is asked twice in one round trip, as a relative choice and
  as one absolute yes/no per window. A choice always crowns a winner, even when the right
  window is not open; the booleans are what can say "none of these".
- **Corroboration.** Above tier 0 the chosen entity must be anchored in the transcript by a
  *trusted* field: a system `.desktop` file or a user alias. Titles are read in exactly one
  function, `Lexicon.title_discriminates`, to break a tie among candidates a trusted field
  already selected, and only by a token no other candidate shares.
- **Ties are ties.** Two windows the utterance corroborates are a tie however sure the model
  sounds. A focus takes the most recently used one and offers a swap; anything else asks.

Thresholds live in `config.Gates`, each with the measurement behind it. Two lessons are
recorded there because they cost real bugs: Jev's yes/no answers run low in absolute terms, so
"present" is a floor plus a standout, not "above one half"; and one knob must never serve two
different questions.

## The order of checks

```
key down   locked? refuse.  Pin the focused window.  Open the mic.  Warm the Jev connection.
key up     locked? drop the audio.  No voice? stop here.  Decode.  Understand.
           Local failed real speech? Ask the cloud about the same clip, once.
decision   the verdict says how much ceremony the action needs
act        executor: lock again, FRESH state, target still there with the same class,
           typing still on the pinned window.  Then one guarded hypruse call.
```

A lock at any point drops the utterance, cancels countdowns and badges, closes the microphone
and zeroes its buffer. Hyprland emits no lock event and skips ordinary binds while locked, so
the latch is polled while a key is held or an action is pending, and it forces the key up.

## Threads

One asyncio loop owns the conversation. Off it: recognition and microphone opening
(`asyncio.to_thread`), latch polling (it may shell out to `loginctl`), the PortAudio callback,
and exactly one executor worker, because the inherited core keeps process-global state.
Callbacks that arrive on a foreign thread hop back with `call_soon_threadsafe`.

## Testing

Unit tests never read live system state, touch the network or open the microphone; fakes
stand in for the compositor, Jev, the recorder and the overlay. `evals/` scores the
understanding step against 116 labelled cases, offline or against live Jev, and keeps the
wrong-action rate apart from merely asking. The daemon has its own suite because its first
one found that the numbered hints could never be answered.
