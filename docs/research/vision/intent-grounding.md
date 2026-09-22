# Lane: intent grounding and context (owner complaints 3 and 4)

Date 2026-09-22. Target repo /home/ilyask/projects/hyprsay.
Every claim is labelled: [PRIMARY] [SECONDARY] [VENDOR] [MEASURED-LOCAL] [INFERENCE].
No em dashes anywhere in this file.

---

# Part 1. The precise cause of the "open X launches a second X" bug

## 1.1 Reproduced on the live desktop [MEASURED-LOCAL]

Live desktop at the time of the run, from `hyprctl -j clients` (read-only):

| class | workspace | focusHistoryID |
|---|---|---|
| `chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2` | 11 | 0 (focused) |
| `kitty` | 10 | 1 |
| `kitty` | 10 | 2 |
| `google-chrome` | 8 | 3 |
| `kitty` | 6 | 4 |
| `kitty` | 6 | 5 |
| `kitty` | 6 | 6 |
| `kitty` | 10 | 7 |

Dry runs (`uv run hyprsay say ...`, no `--act`, so nothing was carried out):

```
$ uv run hyprsay say "open chrome"
heard     'open chrome'
verdict   act   (tier 1)
action    launch app Google Chrome
  [1] Google Chrome   score 0.93   trusted anchor

$ uv run hyprsay say "open kitty"
heard     'open kitty'
verdict   act   (tier 1)
action    launch app kitty
  [1] kitty   score 1.00   trusted anchor

$ uv run hyprsay say "go to chrome"
heard     'go to chrome'
verdict   act   (tier 0)
action    focus window google-chrome
  [1] Google Chrome (workspace 8)   score 0.93   trusted anchor
```

Google Chrome is open on workspace 8 and five kitty windows are open on 6 and 10.
`open chrome` still resolves to LAUNCH_APP and would spawn a second Chrome on the current
workspace (11). `go to chrome` gets it right. The decision is made purely by which verb
was spoken, never by what is on the desktop.

## 1.2 Cause, with file and line

Five places, all of which have to change. There is no single line.

**(a) The grammar hard-binds the verb to the intent.**

`src/hyprsay/nlu/grammar.py:77`
```python
_LAUNCH = r"(?:open up|open|launch|start up|start|run|fire up|boot up)"
```
`src/hyprsay/nlu/grammar.py:680-687`
```python
_rule(
    Intent.LAUNCH_APP,
    rf"{_LAUNCH} (?P<ref>{_REF})",
    _slots("app"),
    "open firefox", "launch the file manager", "start a terminal",
),
```
The focus verbs are a disjoint set at `grammar.py:688-697`
(`focus on|focus|switch to|go to|jump to|show me|show|bring up|take me to|activate|raise`).
`Grammar._try` (`grammar.py:786-799`) walks the ordered rule tuple and returns the first
full match, so "open" can never reach the FOCUS_WINDOW rule. The intent is decided by a
lexical distinction the speaker did not intend to make. In ordinary English "open Spotify"
when Spotify is running means "show me Spotify"; the grammar has no way to express that.

**(b) The launch path never receives the window list.**

`src/hyprsay/nlu/understand.py:450-451`
```python
if intent is Intent.LAUNCH_APP:
    return self._launch_locally(parse, phrase, template, heard, state)
```
`understand.py:694-706` `_launch_locally` calls
`resolve.resolve_app(phrase, heard.text, self.lexicon, self.cfg.gates)`.
`src/hyprsay/nlu/resolve.py:188-197`:
```python
def resolve_app(phrase: str, utterance: str, lexicon: LexiconLike, gates: Gates) -> Resolution:
    scored = sorted(lexicon.match_apps(phrase, limit=5), key=lambda a: -a[1])
```
Compare `resolve_window` at `resolve.py:140-147`, whose signature takes
`state: DesktopState`. `resolve_app`'s signature has no desktop in it at all. At the
moment hyprsay chooses to launch, the live window list is not in scope.

**(c) The action is forced to LAUNCH_APP after the fact.**

`understand.py:735-736`
```python
base = template if template is not None else Action(Intent.LAUNCH_APP)
action = replace(base, intent=Intent.LAUNCH_APP, app=app, window=None)
```
`_launch` is the single funnel for both the grammar path and the Jev path
(`_launch_remote`, `understand.py:1429-1458`). It unconditionally rewrites the intent to
LAUNCH_APP and blanks `window`. Even if an upstream stage had found the live window this
line would discard it. **This is also the best place to put the fix**, precisely because
it is the one funnel.

**(d) The executor does not deduplicate either.**

`src/hyprsay/ops.py:297-314` `_launch_app(action, _before: DesktopState)` builds a command
from `app.exec_argv` and calls `server.launch(...)`. `_before` is the live desktop state
and it is ignored. `ops.py:308`:
```python
workspace = workspace_selector(action.workspace) if action.workspace else ""
```
An empty selector means "wherever the user currently is", which is the second half of the
complaint: the new instance lands on the current workspace.

**(e) The asymmetry is deliberate in one direction only.**

The reverse case is handled. `understand.py:781-785`: a FOCUS_WINDOW that finds no window
calls `_launch_offer`, which SUGGESTS launching (`understand.py:813-829`). The Jev path
does the same in `_no_such_window` (`understand.py:1401-1418`). So focus-degrades-to-launch
exists and launch-degrades-to-focus does not. `grep -rn "already open|already running|
existing instance"` over the whole tree finds only prose in `bank.py` and one comment in
`recipes.py`. There is no code. [MEASURED-LOCAL]

**(f) Jev is asked the question and denied the evidence. This is the highest-leverage fact
in this lane.**

`src/hyprsay/nlu/bank.py:86-97`, LAUNCH_APP's description sent to Jev:
```python
"not_for": "going to a window that is already open",
```
`bank.py:43-55`, FOCUS_WINDOW's:
```python
"not_for": ("starting an application that is not open yet, or pulling a window onto the "
            "workspace the speaker is on (bring it here)"),
```
But the R1 state is built by `requests.utterance_state` (`requests.py:133-143`) and
carries only `{"utterance", "variants", "note"}`. Its own docstring, `requests.py:134`:
> "The one state every request carries (PLAN 5.5). Nothing about the desktop is here."

The model is asked to distinguish "already open" from "not open yet" while being given no
way to know which is true. [MEASURED-LOCAL, read of the code]

## 1.3 Second-order consequence

Because `resolve_app` has no desktop, `_launch` also cannot notice that the app it is about
to start is the app the speaker is currently looking at, nor prefer the instance on the
speaker's monitor. Every context signal that exists is already collected in
`world.py:226-243` (`focus_rank` from `focusHistoryID`, `workspace_id`, `monitor`, `pid`,
`hidden`, `at`, `size`) and thrown away before the launch decision. The data is there. The
decision does not read it.

## 1.4 A complication the live desktop shows [MEASURED-LOCAL]

The focused window's class is `chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2`. That is
a Chrome PWA window. It shares a process ancestry with `google-chrome` but is a different
thing to the user. Any "is this app already running" test must therefore treat a class
match and a pid match as different strengths of evidence. See 3.2.

---

# Part 2. What context hyprsay keeps today: almost none

- `Understander.pending` (`understand.py:270`) is one `Action` template.
  `understand.py:301-302` resets it on **every** utterance:
  ```python
  waits = decision.verdict in (Verdict.HINTS, Verdict.ACT_SWAP, Verdict.SUGGEST)
  self.pending = decision.action if waits and decision.candidates else None
  ```
  It survives exactly one turn and only so a spoken number can pick a badge.
- `Understander.last_exchange` (`understand.py:273`, cleared at `understand.py:285-291`) is
  a debug journal for `hyprsay inspect`. Not memory.
- `_Chain` (`understand.py:207-222`) is intra-utterance only. It binds "it", "that",
  "there" in clause n to what clause n-1 did, inside one key press. Across key presses
  nothing survives.
- `pinned_address` (`understand.py:787-811`) is the window focused at key DOWN. This is
  real deixis and it works well, but it is the only grounding signal and it resets each
  turn.

So every utterance is decided from (transcript, fresh DesktopState, pinned window). There
is no turn-to-turn state whatsoever. [MEASURED-LOCAL]

---

# Part 3. Research: how other systems ground a reference and decide open versus switch

## 3.1 jumpapp, the reference implementation of run-or-raise [PRIMARY]

Source: https://github.com/mkropat/jumpapp (the `jumpapp` shell script itself, fetched).

Algorithm, as the script implements it:

1. `list_matching_windows()` gathers candidate windows with a **hierarchy of matchers**:
   - primary: WM_CLASS, comparing the second WM_CLASS string,
     `equals_case_insensitive "$class" "$target_class"`
   - secondary: process id, via `list_pids_for_command()`, which parses `/proc/*/cmdline`
     or uses `pgrep`
   - optional filters: a title regex (`-t`), and restriction to the current workspace (`-w`)
2. `jumpapp()` then decides:
   > `elif (( ${#windowids[@]} )) && ! needs_passthrough "$@"; then` raise
   > `else` `launch_command`

   So: raise when matching windows exist and no argument forces a new instance. Launch when
   there are none, when arguments are passed in passthrough mode, or when `-N` forbids
   launching.
3. Among several matches, `get_subsequent_window()` cycles. Ordering comes from
   `list_stacking_order()`, which reads the `_NET_CLIENT_LIST_STACKING` X atom. Forward mode
   takes the most recently focused; `-r` takes the oldest.

Why this matters for hyprsay: the entire mechanism is three signals (WM class, pid, stacking
order) and one branch. hyprsay already has better versions of all three:
`App.wm_classes` (`model.py:113`), `Window.pid` (`model.py:40`) and
`Window.focus_rank` (`model.py:42`, from Hyprland's `focusHistoryID`, which is a true focus
history rather than a stacking order). The missing piece is the branch, not the data.

## 3.2 The freedesktop Desktop Entry Specification already defines this semantics [PRIMARY]

Source: https://specifications.freedesktop.org/desktop-entry/latest/recognized-keys.html

- **`SingleMainWindow`**: "If true, the application has a single main window, and does not
  support having an additional one opened."
  This is decisive. If `SingleMainWindow=true` and a window of that app exists, then
  launching **cannot** produce a second window, so "open X" can only mean "show me X". This
  is a vendor-neutral, spec-grounded rule that needs no model at all.
- **`StartupWMClass`**: "If specified, it is known that the application will map at least one
  window with the given string as its WM class or WM name hint."
  This is the sanctioned, trusted way to map a running window back to a `.desktop` file.
  hyprsay already reads it into `App.wm_classes` (`model.py:113`, comment:
  "StartupWMClass plus the file stem").
- **`DBusActivatable`**: "A boolean value specifying if D-Bus activation is supported for
  this application. If this key is missing, the default value is `false`."
  The `org.freedesktop.Application` interface's `Activate` method
  (https://specifications.freedesktop.org/desktop-entry/latest/dbus.html) is "called when
  the application is started without files to open". For a single-instance GApplication the
  second invocation reaches the first and it raises or opens a window.

  [NOT FOUND] The spec text I could retrieve does not state explicitly what `Activate` must
  do when the application is already running; that behaviour is a GApplication convention
  rather than a spec requirement. Do not rely on it for correctness.

**Recommendation on Activate**: read `DBusActivatable` as *evidence* that the app is
single-instance-ish, but do not dispatch through it. hyprsay's whole design is "act against
fresh state, to an exact window address" (`executor._check_window`), and `Activate` makes the
outcome unobservable: the app decides, and hyprsay cannot verify. Also, a D-Bus call must not
run on the event loop, for the reason `understand.py:665-668` already documents about the
accessibility walk ("a D-Bus walk, so never on the loop: it may take a second and a half").

## 3.3 GNOME's behaviour, as a sanity check on user expectation [SECONDARY]

GNOME Shell activates an existing window rather than opening a new instance, and ships a
preinstalled extension, "Launch new instance", specifically for users who want the opposite.
Window-to-launcher association is done through `WM_CLASS` and `StartupWMClass`.
(Sources: GNOME wiki GApplication introduction; extensions.gnome.org/extension/600.)
[SECONDARY: the behaviour is widely documented but I did not read GNOME Shell source.]

The inference an engineer should draw: **raise-not-relaunch is the platform default users are
trained on**, and the opt-out is the explicit gesture. hyprsay currently ships the opt-out as
the only behaviour. [INFERENCE]

## 3.4 Centering theory, as the model of salience and reference [PRIMARY]

Source: Poesio, Stevenson, Di Eugenio and Hitzeman, "Centering: A Parametric Theory and Its
Instantiations", Computational Linguistics 30(3), https://aclanthology.org/J04-3003.pdf,
read directly. It quotes Grosz, Joshi and Weinstein (GJW) 1983 and 1995, and Brennan,
Friedman and Pollard (BFP) 1987. The definitions below are quoted from that article.

Definitions:
- The **local focus** is updated after every utterance. It holds a set of **forward-looking
  centers** (CFs), the discourse entities the utterance realizes, plus "information about the
  relative prominence or **rank** of these CFs".
- The **preferred center** (CP) is "the most highly ranked CF realized by an utterance".
- **Constraint 3 (the CB definition)**: "CB(U_i), the backward-looking center of utterance
  U_i, is the highest-ranked element of CF(U_{i-1}) that is realized in U_i."
- **Constraint 1 (Strong)**: "All utterances of a segment except for the first have exactly
  one CB." (A weak form says "at most one".)
- **Rule 1 (GJW95)**: "If any CF is pronominalized, the CB is."
- **Rule 2 (GJW95)**: "(Sequences of) continuations are preferred over (sequences of)
  retains, which are preferred over (sequences of) shifts."
- **Rule 2 (BFP, single transitions)**: "Transition states are ordered. The CON transition is
  preferred to the RET transition, which is preferred to the Smooth Shift transition, which
  is preferred to the Rough Shift transition."
- Transition table, quoted:

  | | CB(U_n) = CB(U_{n-1}) or CB(U_{n-1}) = NIL | CB(U_n) != CB(U_{n-1}) |
  |---|---|---|
  | CB(U_n) = CP(U_n) | Continue | Smooth Shift |
  | CB(U_n) != CP(U_n) | Retain | Rough Shift |

- And the psycholinguistic anchor: "the CB is most likely to be realized as a pronoun", with
  the **repeated-name penalty** (Gordon, Grosz and Gillion 1993): a proper name in place of a
  pronoun for the most salient entity measurably slows reading.

What an engineer takes from this, concretely (see section 5 for the mapping into hyprsay):
1. Salience is a **ranking**, not a probability. Ranking is a parameter, set per language and
   per domain. For a desktop the domain ranking is not grammatical role, it is focus history
   and place.
2. **Constraint 1 licenses a memory of depth one entity.** At most one thing is carried
   forward. That is a strong, cheap, testable invariant and it is exactly what stops a memory
   from causing wrong actions.
3. **Rule 1 says which entity a bare pronoun resolves to**: the CB, which is the top-ranked
   carried candidate, not "whatever scores best right now".
4. **Rule 2 is a usable tiebreak**: when two readings are equally plausible, prefer the one
   that keeps the same entity in play (Continue over Shift).

## 3.5 Short-term memory in a real commercial voice assistant, with numbers [PRIMARY]

Source: Naik, Gupta, Ge, Mathias, Sarikaya (Amazon Alexa Machine Learning), "Contextual Slot
Carryover for Disparate Schemas", Interspeech 2018, https://arxiv.org/abs/1806.01773, read
directly from the PDF.

Mechanics, quoted or closely paraphrased from the paper:
- A dialog turn at time t is `{a_t, S_t, w_t}`: dialog act, a set of slots (key-value pairs),
  and the utterance words.
- Given D previous user turns `{u_{t-D+1}, ..., u_{t-1}}` and their system turns, the
  candidate set is the **union of all slots from those turns**:
  `C(S) = union over i in {u,v}, j = t-D+1..t-1 of S_j^i`.
- The task is "to predict a carryover decision over each of the candidate slots". A slot is
  carried when `P(+1 | s, u_t, u_{t-D+1..t-1}, v_{t-D+1..t-1}) > tau`, "where tau is a
  decision threshold to be optimized". In their runs they "only select those slots as the
  final hypothesis, whose tau > 0.5, which was optimized over the dev set".
- **Recency is an explicit feature**: "The slot distance d_s, defined as the integer offset of
  the candidate slot from the current turn, is encoded as one-hot {0,1}^|D|", then passed
  through an affine transform.
- Each candidate gets an **independent** binary decision: "independent carryover decisions are
  made for each candidate slot".

Numbers, from their Table 2 and Table 3, on a commercial voice assistant corpus:
- Average turns per session: 2.2.
- Average **positive** carryover candidates per turn: 0.37. Average **negative**: 4.07.
- Naive baseline, which "carries over all the slots from the most recent turn":
  **precision 17.01, recall 92.50, F1 28.74**.
- Hand-coded rule baseline: **precision 91.79, recall 67.11, F1 77.53**.
- Their encoder-decoder with word attention: precision 75.76, recall 94.65, F1 84.16.

This is the single most useful empirical result for hyprsay in this lane. It says: **carrying
everything from the previous turn is wrong about five times out of six.** And it says the
hand-built rule sits at precision 91.8 with recall 67.1, which for a system whose stated
acceptable wrong-action rate on adversarial cases is zero (`evals/run.py` docstring) is the
right operating point. hyprsay should build the rule baseline, not the neural carryover, and
should ask rather than guess when the rule does not fire.

## 3.6 Dialogue state tracking and coreference in task-oriented dialogue [SECONDARY]

- DST is the component "responsible for extracting the goals of the user and the (slot, value)
  pairs corresponding to them", benchmarked on DSTC2, WOZ 2.0, MultiWOZ 2.0/2.1/2.2 and SGD.
- The **schema-guided** framing (SGD) supplies "a list of slots and intents for the service
  accompanied by their natural language description", so the model recognizes semantics from
  descriptions rather than memorizing values, enabling zero-shot generalization to new
  schemas. hyprsay's question bank is already exactly this shape: `bank.INTENTS` is a schema
  with `what`, `not_for` and `examples` per intent, and `requests.describe_window` is a slot
  description. That is a good sign: the architecture is already schema-guided, it is just
  missing the state half of "dialogue state tracking".
- **TripPy**'s triple copy strategy (Heck et al., https://arxiv.org/abs/2005.02877) fills a
  slot from one of three sources: the user utterance, the system's informed values, or
  **another slot** (this last is the coreference mechanism). The three-source framing maps
  cleanly onto hyprsay: a target comes from the utterance, from the pinned window, or from the
  previous turn.
  [SECONDARY: I read the abstract and summaries, not the full paper.]
- Slot-carryover status classes in the SGD line of work are enumerated as
  `none`, `in_sys_uttr`, `in_service_hist`, `in_cross_service_hist`. Same idea: provenance of
  a filled slot is itself a typed field. [SECONDARY]

## 3.7 Talon Voice, the closest neighbour in the "voice command, not conversation" genre [SECONDARY]

Talon keeps a **command history** (a toggleable display of recent voice commands) and exposes
`core.repeat_command(n)` to repeat the last command n times; community modules add a `Rep`
class with timeouts. Context activation is by **application focus and window title**: contexts
are named per app (`sublime`, `git`, `google_chrome`) and gate which commands exist.
(Sources: github.com/talonhub/community, talon.wiki.)
[SECONDARY: derived from documentation and community repos, not from Talon's closed source.]

Two things to take: (1) even the most mature voice-control system for desktops keeps its
cross-utterance memory to "the last command, repeatable", not a dialogue state. (2) Its
context model is *which app has focus*, which is precisely the signal hyprsay collects and
does not use in the launch decision.

## 3.8 Hyprland's own signals [PRIMARY, from the repo and from hyprctl on this machine]

- `hyprctl -j clients` gives per window: `class`, `initialClass`, `title`, `workspace{id,name}`,
  `monitor`, `pid`, `floating`, `fullscreen`, `pinned`, `hidden`, `at`, `size`, and
  **`focusHistoryID`** (0 is the focused window, 1 the one before it). All of it is already
  parsed into `Window` at `world.py:226-243`.
- The event socket (socket2) at `$XDG_RUNTIME_DIR/hypr/<instance>/.socket2.sock` emits, per
  `src/hypruse/events.py:31-46`, at least: `openwindow` (address, workspace, class, title),
  `closewindow` (address), `movewindow` (address, workspace), `workspace` (name),
  `activewindowv2` (address), `windowtitlev2` (address, title), `openlayer`/`closelayer`
  (namespace), `urgent` (address), `screencast` (state, owner). Addresses on socket2 come
  without the `0x` prefix.
- Dispatchers accept a window selector of `class:<regex>`, `title:<regex>`, `pid:<pid>` or
  `address:<addr>`, and Hyprland parses regexes with RE2. [SECONDARY, Hyprland wiki]
  hyprsay should keep using `address:` as it does today, since a regex built from speech is
  exactly the injection surface the trust guards exist to close.

**What is missing and what it buys**: `focusHistoryID` is an *ordinal*. It says "the third
most recently focused" and nothing about *when*. A wall clock is what distinguishes "I was in
that window five seconds ago" from "I have not touched it since this morning", and it is the
signal a salience model actually needs. It is not available from `hyprctl clients`; it must be
accumulated by listening to `activewindowv2` on socket2 and stamping a monotonic clock.
[MEASURED-LOCAL: `hyprctl -j clients` output contains no timestamp field.]

---

# Part 4. Design: a context model for hyprsay

Three layers. Each is independently shippable and each is useful on its own.

## 4.1 Layer A: the Surface, an entity-salience model over the desktop

A single long-lived object fed by socket2 rather than polled. `hypruse.events` already has the
listener. Per live window it holds, beyond what `Window` carries today:

```python
@dataclass
class Trace:
    address: str
    opened_at: float          # monotonic, from `openwindow`
    last_focused_at: float    # monotonic, from `activewindowv2`
    last_acted_at: float      # when hyprsay itself last acted on this window
    last_acted_intent: Intent | None
    last_mentioned_at: float  # when a user utterance resolved to this window
    last_mention_phrase: str  # normalized, never raw, never a title
```

`last_acted_at` and `last_mentioned_at` are the novel part. They are hyprsay's own history,
and no compositor can supply them. They are also the two signals that make the system feel
like it is following you: "make it bigger" after "go to chrome" should mean chrome.

**The salience score** is a small deterministic linear function computed in code, never by a
model, so it is explainable, testable and free:

```
S(w) = 3.0 * recency_focus(w)        # exp(-(now - last_focused_at) / 45s), or 1.0 if focused
     + 2.0 * recent_mention(w)       # exp(-(now - last_mentioned_at) / 45s)
     + 2.0 * recent_action(w)        # exp(-(now - last_acted_at) / 20s)
     + 1.0 * (w.workspace_id == state.active_workspace_id)
     + 0.5 * (w.monitor == focused_monitor)
     - 1.0 * w.hidden
```

Weights are initial assumptions, to be set by the eval harness exactly as `config.Gates`
documents for its own thresholds ("All are assumptions until the eval harness sets them",
`config.py:88`). Put them in `Gates` so they are tunable and journalled.

**The hard rule that keeps the existing safety model intact**: salience may only ever break a
tie inside a candidate set the speaker's words already licensed through a trusted field. It
may never select across apps, and it may never promote a window the words did not license.
This is the same rule `docs/PLAN.md` 5.6 already states for titles, and the same role
`focus_rank` already plays at `lexicon.py:822`:
```python
scored.sort(key=lambda pair: (-pair[1], pair[0].focus_rank, pair[0].address))
```
So this is a generalization of an existing, tested mechanism, not a new class of evidence.

**Cost**: a dictionary of at most a few dozen entries, one exp() per window per decision.
Well under 0.1 ms on the i5-8350U against the measured 1 ms grammar decision. No GPU, no
network, no allocation pressure. The socket2 listener replaces polling, so it probably
*reduces* work.

## 4.2 Layer B: reach, which replaces "launch versus focus"

### 4.2.1 The grammar change

Do not delete the launch/focus split. Demote it from deciding the **action** to declaring a
**preference**:

| spoken verb | preference |
|---|---|
| `focus`, `switch to`, `go to`, `show me`, `bring up`, `raise`, ... | `EXISTING` |
| `open`, `launch`, `start`, `run`, `fire up`, ... | `EITHER` |
| a launch verb plus a novelty word | `NEW` |

The novelty words are a **closed list** in the grammar, for exactly the reason
`DICTATION_OPENERS` is a closed list (`understand.py:114-124`, where a fuzzy match scored
"rid" at 0.83 against "write"):

```python
NOVELTY = frozenset({"new", "another", "second", "third", "extra", "fresh", "more", "again"})
```
Phrases: "open another firefox", "open a new terminal", "one more kitty", and the existing
`in a new workspace` rule at `grammar.py:674-679`, which already sets `workspace="empty"` and
must keep meaning NEW.

`EXISTING` keeps today's behaviour exactly, including the launch offer at
`understand.py:781-785`. Nothing regresses.

### 4.2.2 The grounding function

One new module, `src/hyprsay/nlu/ground.py`, around 150 lines. One entry point, called from
the one funnel at `understand.py:708`:

```python
def reach(app: App, prefer: Prefer, state: DesktopState, lexicon, gates) -> Reach
```

Returns exactly one of `Reach.LAUNCH`, `Reach.FOCUS(window)`, `Reach.AMBIGUOUS(windows)`.

**Step 1, find the app's live windows, with evidence strength.** Three matchers, strongest
first, mirroring jumpapp's hierarchy but with a trust grade attached:

| matcher | source | strength |
|---|---|---|
| `w.cls` or `w.initial_class` equals any of `app.wm_classes`, case-insensitively | `StartupWMClass` plus the desktop file stem, both from a root-owned `.desktop` file | **exact** |
| `lexicon.app_for_window(w) is app` | the lexicon's existing reverse map | **exact** |
| basename of `/proc/<w.pid>/cmdline` argv[0] equals basename of `app.exec_argv[0]` | the kernel | **corroborating** |
| window title | the window's owner, possibly hostile | **never** |

Title is excluded by construction, not by policy, which is the only way it stays excluded.

**Step 2, decide.**

```
prefer == NEW                                   -> LAUNCH
no exact-strength window                        -> LAUNCH   (corroborating-only matches do
                                                             not suppress a launch)
app.single_main_window and any window exists    -> FOCUS(most salient)
exactly one exact window                        -> FOCUS(that one)
several exact windows, prefer == EXISTING       -> today's path (AMBIGUOUS -> hints, or the
                                                  R2/R2b arbitration that already exists)
several exact windows, prefer == EITHER         -> AMBIGUOUS, unless one of them is already
                                                  focused, in which case LAUNCH
```

That last clause is worth defending. If the speaker is looking at a kitty window and says
"open kitty", they want a second terminal. `focus_rank == 0` is unambiguous evidence of that,
it is free, and it is the case jumpapp handles with its cycling rule. Everything else with
several matches goes to hints, which is hyprsay's existing "nobody can know" answer.

**Step 3, tiering.** `tiers.py:44` puts FOCUS_WINDOW at tier 0 and `tiers.py:56` puts
LAUNCH_APP at tier 1. Converting a launch into a focus **lowers** the tier, which is safe by
construction: focusing is strictly less consequential than spawning a process. Converting a
focus into a launch **raises** the tier, which is why the existing `_offer` path only
SUGGESTS. Keep that asymmetry and state it in the code comment: it is the principled reason
the new path may act while the old one must ask.

### 4.2.3 The workspace half of the complaint

Even when a launch is right, `ops.py:308` sends it to the current workspace. Add to
`ground.py` a `where(app, state)` that, when the speaker named no workspace and an existing
window of the same app sits on another workspace, launches onto **that** workspace. Rationale:
the user has already expressed where this app lives. This is one line of `workspace_selector`
and it is reversible (a workspace switch), so it stays tier 1.

[INFERENCE: this specific rule is my own proposal, not drawn from any source. It should be
behind a config key and validated by the eval set before being made the default.]

### 4.2.4 One new `.desktop` key to parse

`lexicon.py` already parses desktop files. Read two more keys into `App`:
`single_main_window: bool = False` from `SingleMainWindow`, and
`dbus_activatable: bool = False` from `DBusActivatable`. Zero extra IO: the file is already
open. `SingleMainWindow=true` makes the decision certain without any model.

## 4.3 Layer C: short-term conversational memory

### 4.3.1 What to keep

One bounded ring buffer on `Understander`, at most three turns, each entry:

```python
@dataclass(frozen=True)
class Turn:
    at: float                       # self._clock(), already injected at understand.py:259
    utterance: str                  # NORMALIZED text only, never raw, never dictated text
    intent: Intent
    verdict: Verdict
    address: str                    # the window acted on, by address, not the Window object
    app_id: str
    workspace: str | None
    candidates: tuple[Candidate, ...]   # what was offered and not chosen: this is CF(U_i)
```

The address rather than the `Window` object, because a `Window` snapshot goes stale in
milliseconds and hyprsay's invariant is to resolve against fresh state at the moment of the
write (`executor._check_window`). `state.by_address(...)` re-resolves it, and a `None` result
means the memory is void.

Dictated text must never enter this buffer, for the same reason it never enters
`last_exchange` (`understand.py:271-273`). Assert it: a turn whose intent is `TYPE_TEXT` is
recorded with an empty utterance.

### 4.3.2 The centering mapping, made concrete

| centering | hyprsay |
|---|---|
| CF(U_i), the ranked forward-looking centers | `Decision.candidates`, already ordered best first |
| CP(U_i), the preferred center | `candidates[0]`, that is, what it acted on |
| CB(U_i), the backward-looking center | the highest-ranked member of the previous turn's `candidates` that this utterance realizes |
| ranking parameter | the salience score S(w) of Layer A, not grammatical role |
| Constraint 1 (strong): exactly one CB | **carry at most one entity, ever** |
| Rule 1: if any CF is pronominalized, the CB is | a bare "it"/"that"/"this one" resolves to the carried entity, not to a fresh best match |
| Rule 2: Continue > Retain > Smooth Shift > Rough Shift | when two readings tie, prefer the one that keeps the same window in play |

Constraint 1 is the load-bearing one. It is what makes this memory small enough to be safe.

### 4.3.3 When memory may be read: three independent gates, all cheap

A carry happens only when **all three** pass.

**Gate 1, time.** TTL of 30 s from the end of the previous utterance. Push-to-talk turns are
not a conversation; the user lets go of the key and goes back to work. Use the injected
`self._clock` so it is testable. [INFERENCE: 30 s is a starting assumption, to be set by the
eval harness. The Alexa corpus averaged 2.2 turns per session, which supports "short".]

**Gate 2, segment break.** Centering's rules hold only inside a discourse segment. Clear the
whole buffer on any of these socket2 events, all already parsed in `events.py`:
- `workspace` (the user switched workspace by hand)
- `closewindow` whose address is the carried address (the entity is gone)
- `activewindowv2` to a window hyprsay did not itself focus (the user's attention moved on
  their own, so the discourse segment ended)

This is the cheapest and most important gate. It is pure event handling, no inference.

**Gate 3, referential incompleteness.** The current utterance may read memory only when it
does not name its own target:
- grammar path: the slot is empty, that is `not phrase` at `understand.py:449`, or a back
  reference word is present
- Jev path: R1's `names_window` is below `gates.spoken` (the test already exists at
  `understand.py:1240-1243`)

If the utterance names its target, memory is not consulted at all. This is the same rule
`_bound` already enforces inside one utterance (`understand.py:461-491`, gated on
`chain.refers_back`). Generalizing it across turns is a small, well-precedented change.

### 4.3.4 How memory is stopped from causing wrong actions

Four invariants, each one testable:

1. **Fill only, never override.** A carried value is applied only when the corresponding slot
   is `None`. Mechanically: `if getattr(slots, name) is None: slots = replace(slots, **{name: carried})`.
2. **Never above tier 1.** A carried target is refused for any tier 2 or 3 action (close, type,
   lock) and downgraded to hints. Closing the wrong window because of a 25-second-old memory
   is the failure the owner would never forgive, and `tiers.py` already has the machinery.
3. **Evidence does not refresh.** The carried candidate's `corroborated` flag is the one it had
   when it was recorded. A memory cannot manufacture corroboration.
4. **One entity.** Constraint 1. If the previous turn's top two candidates were within
   `gates.margin` of each other, carry nothing: there was no unique CB.

The Alexa numbers are the justification for this severity. Their naive "carry everything from
the last turn" baseline scored **precision 17.01** (recall 92.50). hyprsay's published metric
is the wrong-action rate, with zero required on adversarial cases. An 83 percent false-carry
rate would be catastrophic against that metric. Their **rule** baseline, at precision 91.79
and recall 67.11, is the shape to build: carry rarely, carry correctly, ask otherwise.

## 4.4 How this feeds Jev

### 4.4.1 A `context` object in the R1 state

Change `requests.utterance_state` (`requests.py:133-143`) to take an optional context and
emit:

```python
{
  "utterance": "open chrome",
  "variants": [...],
  "note": bank.NOTE,
  "context": {
      "open_apps": ["Firefox", "kitty", "Google Chrome"],   # trusted .desktop names only
      "current_workspace": "11",
      "focused": {"app": "Google Chrome", "kind": "web browser"},
      "previous": {                    # omitted entirely when memory is empty or expired
          "utterance": "go to chrome",
          "did": "focus_window",
          "on": {"app": "Google Chrome", "workspace": "8"},
          "seconds_ago": 6
      }
  }
}
```

Privacy: no titles, no dictated text, no window addresses, no pids. `open_apps` are
`App.name` values read from root-owned `.desktop` files, which is the same trust class
`requests.describe_app` already sends in R3 (`requests.py:167-171`). So PLAN section 7 holds
unchanged, and `docs/PRIVACY.md` gains one row.

`seconds_ago` is included deliberately: the Alexa paper encodes recency as an explicit
feature ("the integer offset of the candidate slot from the current turn, encoded as
one-hot"), and a model cannot infer elapsed time from text.

**Cost**: roughly 40 to 120 tokens for a typical desktop, on R1 only.
`requests.py:5-11` records the measured curve: "1, 5, 15 and 40 questions cost the same
320 ms" and "latency is flat to about 2.2k input tokens and then climbs, and the HTTP 503
rate climbs with it (13 of 30 at 11.5k)". R1 today is far below 2.2k, so this addition is
free in latency and a small percentage in money.
[INFERENCE: I did not measure the token count of the proposed object; the repo's own
`requests.estimate` would give it exactly.]

### 4.4.2 Two new questions, both Boolean, both with a code-verified premise

1. **`reach_or_new`**, asked **only** when `ground.reach` has established that a live window of
   the named app exists:
   > "The speaker named an application that is already open. Do they want an additional new
   > window, or the one that is already open?"
   > true: "a new, additional window or instance: they said new, another, a second, one more,
   > or asked for a fresh workspace"
   > false: "the window that is already open"

2. **`refers_to_previous`**, asked only when memory is live and gate 3 passed:
   > "Does this command act on the same thing as the previous command?"

   This is centering's CB question. A Boolean is the right shape, and it is what Constraint 1
   licenses: at most one entity is in play, so there is nothing to choose among.

**Do not** add a Choice over previous entities. A Choice always crowns a winner, which is the
failure mode this codebase has already measured and documented twice (`bank.py:251`, "it
always crowns a window, which is why R2b exists"; `understand.py:1299-1302`, and the R2t note
at `bank.py:270-278` where a Choice answered 0.97 on an utterance that matched nothing).

### 4.4.3 The offline answer must be code's answer

Jev is off for some users, times out about 1 request in 200 by the repo's own figure, and
tails to seconds. So `ground.reach` must be **complete without Jev**:

- one live window of the named app, no novelty word said: FOCUS. No network.
- zero live windows: LAUNCH. No network.
- several live windows: hints. No network.

Jev is consulted only for the genuinely ambiguous middle, and its answer can only choose
between two options code already built. This also makes the common case *faster* than today:
"open chrome" with one Chrome open currently costs a grammar parse and then a launch; it will
still cost a grammar parse and then a focus, and focus is tier 0 rather than tier 1.

I confirmed the timeout path is real on this machine: a dry run of "open the browser" (which
misses the grammar) returned `hints` with reason "Jev took too long; pick a number".
[MEASURED-LOCAL]

---

# Part 5. How it is tested

The harness already has the right shape and the right metric. `evals/run.py`'s docstring
separates `correct` / `asked` / `missed` / `noisy` / **`WRONG ACTION`**, and says of the
adversarial cases that "the only acceptable value is zero". `evals/cases.py` supplies a
fixture desktop with two Firefox and two kitty windows on purpose, and each case is labelled
with an expected outcome class rather than a probability.

## 5.1 New eval family: `reach`

Against the existing fixture (`ff1` ws1, `ff2` ws2, `kt1` ws1 focused, `kt2` ws3, `spot` ws4,
`obs` ws2, `dol` ws1; `APPS` includes discord and code, which have no window):

| utterance | expect | target |
|---|---|---|
| "open spotify" | act | FOCUS_WINDOW `spot` |
| "open obsidian" | act | FOCUS_WINDOW `obs` (the owner's exact complaint, on a different workspace) |
| "open discord" | act | LAUNCH_APP discord |
| "open firefox" | hints | two firefox windows, nobody can know |
| "open another firefox" | act | LAUNCH_APP firefox |
| "open a new terminal" | act | LAUNCH_APP kitty |
| "open kitty" (focused window IS kitty) | act | LAUNCH_APP kitty |
| "open firefox in a new workspace" | act | LAUNCH_APP firefox, workspace `empty` (guards the existing `grammar.py:674` fix) |
| "go to discord" | suggest | the existing launch offer, unchanged |

## 5.2 New adversarial cases, where the acceptable wrong-action count is zero

- a window with `cls="firefox"` and `title="Spotify - Premium"`: "open spotify" must LAUNCH
  Spotify, never focus the firefox window. This is a unit test on `ground.reach`, asserting it
  never reads `Window.title`.
- a window whose class matches nothing but whose pid's cmdline basename matches
  `app.exec_argv[0]`: "open X" must still LAUNCH, because a pid match is corroborating and
  does not suppress a launch. The live Chrome PWA on this machine is the real-world instance
  of this case.
- a user-writable `.desktop` file with `SingleMainWindow=true`: `app.trusted` is false, so the
  key must be ignored entirely, exactly as `_launch` already refuses an untrusted app at
  `understand.py:717-723`.

## 5.3 Memory tests need a clock, and one already exists

`Understander.__init__` takes `clock: Callable[[], float] = time.monotonic`
(`understand.py:259`). Reuse it, do not add a second one. A memory test is then a sequence of
`understand()` calls against a fake clock:

- carry at t+29 s, no carry at t+31 s
- a `workspace` event between the two turns clears the buffer, so no carry at t+5 s either
- the carried window being closed between turns yields "that window is gone", not a wrong
  target (the code already has this string at `understand.py:404`)
- a tier 2 verb ("close it") with only a carried target yields hints, never a countdown
- when the previous turn's top two candidates were within `gates.margin`, nothing is carried

## 5.4 A two-turn eval mode

`evals/run.py` scores one utterance per case today. Add an optional `turns: tuple[str, ...]`
to a case, run them in order against a fixture that mutates between them, and score only the
last turn. Keep the same outcome classes and the same wrong-action metric, so the headline
number stays comparable across versions.

## 5.5 The two numbers to publish

1. **Reach accuracy**: the fraction of "open X" utterances producing the action a human
   labeller says was wanted, reported separately for "X was already open" and "X was not".
2. **Second-instance rate**: how often a launch happened when an exact-strength window of that
   app existed and no novelty word was said. **The target is zero**, and it is currently 100
   percent. This single number is the owner's complaint 3, made countable.

---

# Part 6. Cost on the target machine (i5-8350U, no GPU, 23 GiB, Arch, Hyprland 0.56.2)

| item | latency | money | complexity |
|---|---|---|---|
| Salience surface, S(w) over N windows | under 0.1 ms (N under 30, one exp each) against the measured 1 ms grammar decision | none | one dataclass, one function, tunable weights in `Gates` |
| socket2 listener for `last_focused_at` etc. | one long-lived socket read; replaces polling, so likely net negative | none | `hypruse/events.py` already does the parsing |
| `/proc/<pid>/cmdline` pid matching | 8 small reads on this desktop, well under 1 ms; cacheable per pid for the window's life | none | about 20 lines |
| Two extra `.desktop` keys | zero, the file is already parsed | none | two lines |
| `ground.reach` | pure Python over the window list, microseconds | none | one new module, about 150 lines |
| Memory buffer | negligible; three small frozen dataclasses | none | one class, three gates |
| `context` in R1 state | about 40 to 120 extra tokens on R1; the repo's measured latency curve is flat below about 2.2k input tokens, so effectively free [INFERENCE, not measured] | small percentage on requests that already go out; **fewer requests overall**, since "open chrome" with one Chrome open stops needing Jev | one function signature, one privacy doc row |
| Two new Boolean questions | question count is measured free (1, 5, 15 and 40 questions all cost about 320 ms) | negligible | two entries in `bank.py`, plus a `VERSION` bump |

**The riskiest change** is the grammar preference split, because every existing LAUNCH case
runs through it. Ship it behind the eval suite: the 348-case live run and the 116-case offline
run both exist and both already report the wrong-action rate.

**Ordering, if only one thing ships**: `ground.reach` plus the `SingleMainWindow` and
`wm_classes` matchers, called from `understand.py:708`. It fixes the owner's stated bug, needs
no model, needs no network, adds no latency, and cannot raise a tier.

---

# Part 7. Not found, and honest limits

- **[NOT FOUND]** No recent or viral project using Jev on a non-Hyprland platform for intent
  grounding could be traced to a primary source. Jev launched 2026-09-15, seven days ago. This
  lane found nothing citable about Jev-based projects and reports nothing.
- **[NOT FOUND]** No primary source for how any commercial assistant (Siri, Alexa, Google
  Assistant, Gemini) decides between "open an app" and "switch to an app" on a desktop. The
  Alexa paper is about slot carryover, not application launching. GNOME's raise-not-relaunch
  default is documented in wikis and extension pages, which is secondary.
- **[NOT FOUND]** The Desktop Entry Specification text I could retrieve does not state what
  `org.freedesktop.Application.Activate` must do when the application is already running. That
  behaviour is a GApplication convention. Do not rely on it.
- **[SECONDARY]** Rule 1, Rule 2, Constraint 1 and Constraint 3 of centering are quoted here
  from Poesio et al. 2004, which quotes Grosz, Joshi and Weinstein 1983 and 1995 and Brennan,
  Friedman and Pollard 1987. I read Poesio et al. directly; I did not read GJW 1995 itself.
- **[INFERENCE, not measured]** The token and latency cost of the proposed `context` object.
  `requests.estimate` would give the exact number in one line of code.
- **[INFERENCE]** All proposed salience weights, the 30 s TTL, the 45 s and 20 s decay
  constants, and the "launch onto the workspace the app already lives on" rule. These are
  starting assumptions in the same class as `config.Gates`' own documented assumptions, and
  they must be set by the eval harness before being called measured.
- **One accidental live Jev call** was made while probing ("open the browser", which misses the
  grammar). It timed out and degraded to hints. No further off-grammar phrases were run.

---

# Part 8. The one-paragraph answer to complaint 4

"Nowhere near intelligent" has a single architectural cause, and it is the same cause as
complaints 2 and 3: **Jev is asked to decide about a world it is never shown.** The R1 state
is `{utterance, variants, note}` and its docstring says outright "Nothing about the desktop is
here", while the intent schema asks the model to tell "already open" from "not open yet". The
fix is not a bigger model or a longer prompt. It is to build, in code, from trusted data, a
small typed description of the world and of the last few seconds of the user's attention, and
to hand it over as state alongside the questions. Everything in that description comes from
root-owned `.desktop` files, the compositor's own IPC, and hyprsay's own action history.
Nothing comes from a window title. That keeps the trust model exactly as it is while removing
the reason the system feels blind.

## Verification

_Skeptic pass, lane grounding, 2026-09-22. In progress; verdicts appended as each check completes._


Method: every check below was done with a different instrument than the one the
researcher used. Code claims were re-read from the tree and re-run against the live
desktop with Jev disabled (`HYPRSAY_JEV_ENABLED=false`, so no paid call was made and
no desktop state changed: `hyprsay say` is a dry run unless `--act` is passed,
cli.py:41). Paper claims were checked by pulling the PDFs and extracting them locally
with `pdftotext -layout`, not by reading a summary. Spec and repository claims were
fetched from the primary URL.

### V1. resolve_app takes no DesktopState - CONFIRMED, with one correction

`resolve.resolve_app(phrase, utterance, lexicon, gates)` (resolve.py:188) really has no
state parameter, while `resolve_window` (resolve.py:140) takes `state: DesktopState`.
`_launch` (understand.py:708) really ends with
`action = replace(base, intent=Intent.LAUNCH_APP, app=app, window=None)`, and
`_launch_remote` (understand.py:1429) funnels into the same `_launch`, so the
"one function fixes both paths" claim holds.

Correction: "at the moment hyprsay chooses to launch, the live window list is not in
scope" is false as written. `_launch` is declared
`def _launch(self, app, candidates, evidence, heard, state: DesktopState, template=None)`
and passes `state` straight on to `_finish`. The desktop IS in scope at the decision
point; what is missing is the branch, not the data. This makes the fix cheaper than the
finding implies, but it also removes the "we cannot see the windows" excuse.

### V2. Live reproduction - CONFIRMED, different numbers

Re-run today against the live session, grammar only:

    $ HYPRSAY_JEV_ENABLED=false uv run hyprsay say "open chrome"
    verdict   act   (tier 1)
    action    launch app Google Chrome

    $ HYPRSAY_JEV_ENABLED=false uv run hyprsay say "go to chrome"
    verdict   act_swap   (tier 0)
    action    focus window google-chrome
      [1] Google Chrome (workspace 11) ... [2] Google Chrome (workspace 8)

    $ HYPRSAY_JEV_ENABLED=false uv run hyprsay say "open kitty"
    verdict   act   (tier 1)
    action    launch app kitty      (six kitty windows are open)

The behaviour reproduces and does so with Jev switched off entirely, which is stronger
evidence than the researcher gave: this is a pure grammar decision, so no model change
can fix it.

Corrections: "go to chrome" returns `act_swap`, not `act`, and it picks workspace 11,
not workspace 8, because there are now two google-chrome windows. `act_swap` matters for
the plan: it is the verdict that arms the numbered-pick path, so the focus branch already
has a "more than one fits, take the best and let them correct it" affordance the launch
branch does not. Also, the researcher's parenthetical about launching a sixth kitty is
the dry-run decision text, not an action: nothing was launched.

### V3. The R1 state carries no desktop - CONFIRMED, with a nuance that changes the fix

Verbatim from the file: LAUNCH_APP `not_for` at bank.py:88 is
"going to a window that is already open", and FOCUS_WINDOW `not_for` at bank.py:45-48 is "starting an application that is not open yet, or pulling a window onto the
workspace the speaker is on (bring it here)". The researcher quoted the FOCUS_WINDOW
string truncated at the comma without an ellipsis. `requests.utterance_state`
(requests.py:133) does return exactly `{"utterance", "variants", "note"}` and its
docstring does say "Nothing about the desktop is here." bank.py's own module docstring
line 18 states the rule: "Nothing here carries desktop data."

Nuance the finding misses: `_fan_out` (understand.py:1019) builds R1, R2, R2b, R2t and
R3 from the SAME `base` state, in parallel, and `requests.window_requests`
(requests.py:244) does hand Jev the live window list as options, ordered by
`focus_rank`. So the system does send the desktop to Jev; it is the intent question
specifically, sitting in a sibling request with its own state, that cannot see it. The
consequence for the plan is that the change is not "add desktop context to Jev" (it is
already there in R2) but "make the intent question and the window evidence share one
state, or decide open-versus-focus locally before the fan goes out". Those are different
amounts of work and only the second one is free of a round trip.

### V4. SingleMainWindow and StartupWMClass - SPEC CONFIRMED, LEVERAGE OVERSTATED

Both strings are verbatim correct in the Desktop Entry Specification 1.5 recognized-keys
page: SingleMainWindow, "If true, the application has a single main window, and does not
support having an additional one opened", boolean, REQ? NO, Type 1. StartupWMClass, "If
specified, it is known that the application will map at least one window with the given
string as its WM class or WM name hint", string, REQ? NO, Type 1.

What the finding does not say, and what kills it as "costs nothing":

- On this machine 10 of 245 installed .desktop files declare `SingleMainWindow=true`
  (`grep -rl` over /usr/share/applications and ~/.local/share/applications). All ten are
  KDE or Qt utilities: knewstuff-dialog6, partitionmanager, plasma-systemmonitor,
  plasma.emojier, kmenuedit, kdesystemsettings, systemsettings, kdeconnect.sms,
  qBittorrent, com.anthropic.Claude.
- None of google-chrome, kitty, spotify, firefox or code declares it. Every application
  in the owner's own complaint is outside the covered set.
- `lexicon._KEYS` (lexicon.py:392-404) is a closed frozenset of Type, Name, GenericName,
  Exec, Categories, Keywords, StartupWMClass, NoDisplay, Hidden. `SingleMainWindow` is
  not parsed, and `model.App` (model.py:100-117) has no field for it. Adding it is small
  but it is not zero, and it inherits the `trusted` flag question.

So the signal is sound and nearly useless: it decides about 4% of apps and 0% of the
ones that prompted the complaint. A plan that leads with it will look correct and change
nothing the owner notices.

### V5. jumpapp - MECHANISM CONFIRMED, FRAMING OVERSTATED

Fetched the script and the README directly. The raise-or-launch branch is
`elif (( ${#windowids[@]} )) && ! needs_passthrough "$@"; then ... else launch_command "$@"`,
and the matcher is the pipeline
`list_windows | where_title_matches | where_class_or_pid_matches | where_workspace_matches | where_normal_window`,
with WM_CLASS taken from the second string via
`xprop -id "$windowid" ' $0+\n' WM_CLASS | sed -E -e 's/^.*", "(.*)"$/\1/'`, pids via
`list_pids_for_command` (pgrep, else procfs), and cycling over
`_NET_CLIENT_LIST_STACKING`. That is exactly as described.

Two things the finding leaves out, both of which matter to a plan:

- jumpapp is X11 only. The README describes it as "A run-or-raise application switcher
  for any X11 desktop" and states "All the heavy lifting is done by Tomas Styblo's
  powerful wmctrl. You must have it installed to use jumpapp." There is no Wayland
  statement. It cannot be run on this machine as a reference, only read.
- jumpapp never decides between "open" and "go to". It ALWAYS raises when a window
  matches; the user chose run-or-raise by binding `jumpapp firefox` instead of `firefox`.
  Its flags (`-f` force launch when a process but no window is found, `-N` never launch,
  `-p` always launch when args are passed, `-w` current workspace only, `-R` bring to
  current workspace) are all about how to raise, not whether the sentence asked to.
  So jumpapp validates the MECHANISM and provides no evidence at all for the POLICY that
  "open X" should mean raise. That policy decision is hyprsay's alone, and it is the part
  that can annoy someone who really wanted a second window.

A concrete counter-case measured here: the focused window is the Chrome PWA
`chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2` and the workspace 8 browser window,
and it do not merely share "process ancestry": `hyprctl -j clients` gives both pid
1325064, the same process, and `/proc/1325064/cmdline` is just `/opt/google/chrome/chrome`
with no arguments. jumpapp's pid matcher would call them the same application. So the
finding's own safety rule (pid is corroborating only) is not conservative enough: for
Chromium-family apps a pid match is affirmatively wrong, not merely weak.

### V6. Alexa slot carryover numbers - CONFIRMED EXACTLY, INFERENCE OVERSTATED

Extracted arXiv:1806.01773v1 locally with pdftotext. Every number is right:

    Method               Precision Recall     F1
    Naive Baseline         17.01     92.50  28.74
    Rule Baseline          91.79     67.11  77.53
    Encoder-Decoder        73.31     96.17  83.20

(Table 3, multi-domain commercial dataset.) Table 2 gives Avg. turns per session 2.2,
Avg. positive carryover candidates per turn 0.37, Avg. negative 4.07 - those three are
the TRAIN column; test is 2.18, 0.35, 4.00. The naive baseline is defined as
"carries over all the slots from the most recent turn in the dialogue session". Recency
is `hd = Wd * OneHot(ds) + bd` (equation 9) with a "Recency One-Hot 00010" in Figure 1,
and the figure caption says "for each slot in the transformed candidate list the model
makes an independent decision for carryover", with tau defined at section 2.1 and set by
"whose tau > 0.5, which was optimized over the dev set". All confirmed.

The refutation is of the conclusion, not the data. Table 4 of the SAME paper reports the
naive baseline on DSTC2 at Precision 80.58, Recall 75.44, F1 77.93. DSTC2 is a single
narrow domain. So "carrying everything from the previous turn is wrong about five times
in six" is a property of a seven-domain commercial assistant with 4.07 negative
candidates per turn, and the same baseline is right four times in five on a one-domain
corpus. hyprsay is one domain (windows, workspaces, apps) with a handful of candidates.
The paper is therefore evidence that carryover precision depends on candidate density,
which is an argument for MEASURING hyprsay's own rate, not for importing 17%.

### V7. Centering theory - QUOTES CONFIRMED, USE INVERTED

Extracted aclanthology.org/J04-3003.pdf locally. The paper is Poesio, Stevenson, Di
Eugenio and Hitzeman, "Centering: A Parametric Theory and Its Instantiations",
Computational Linguistics 30(3). All four quotes are verbatim: Constraint 3 at page 313,
"CB(Ui), the backward-looking center of utterance Ui, is the highest-ranked element of
CF(Ui-1) that is realized in Ui"; "Constraint 1 (Strong): All utterances of a segment
except for the first have exactly one CB"; "Rule 1 (GJW95): If any CF is pronominalized,
the CB is"; and the BFP form of Rule 2 (page 315 region), "...preferred to the RET
transition, which is preferred to the Smooth Shift transition, which is preferred to the
Rough Shift transition". Ranking really is treated as a parameter.

But the finding cites the paper for the opposite of what the paper concludes. From its
own abstract: Constraint 1 "is much more instantiation-dependent: It is not verified if
the parameters are instantiated according to very mainstream views ('vanilla
instantiation'), it holds only if indirect realization is allowed, and is violated by
between 20% and 25% of utterances in our corpus even with the most favorable
instantiations." The abstract also reports "a trade-off between Rule 1, on the one hand,
and Constraint 1 and Rule 2, on the other: Setting the parameters to minimize the
violations of local coherence leads to increased violations of salience, and vice versa."

So "Constraint 1 licenses carrying exactly one entity forward" is not supported: this
specific paper is the standard citation for Constraint 1 FAILING on a fifth to a quarter
of utterances, and it says you cannot satisfy Rule 1 and Rule 2 at the same settings.
A depth-one memory may still be the right engineering choice; it just cannot be
justified by this paper, and anything in the plan that reads "centering theory says we
may carry exactly one" should be cut or rewritten as a design choice with a measured
error rate attached.

### V8. "hyprsay has no turn-to-turn state" - REFUTED

There is turn-to-turn state, and it is already wired end to end:

- `daemon.py:56-57` declares `self._picking: tuple[Candidate, ...] = ()` and
  `self._picking_until = 0.0`.
- `daemon.py:406-408` `_arm_picking(candidates, seconds)` sets both, called at
  `daemon.py:363` for ACT_SWAP with `cfg.hud.swap_timeout_s` and at `daemon.py:367` for
  HINTS with `cfg.hud.hint_timeout_s`.
- `daemon.py:295-297` `_understand` reads
  `picking = self._picking if time.monotonic() < self._picking_until else ()` and passes
  it into `understander.understand(..., picking=picking)`.
- In `understand.py:301-302` `self.pending` is not reset, it is SET:
  `waits = decision.verdict in (Verdict.HINTS, Verdict.ACT_SWAP, Verdict.SUGGEST)` then
  `self.pending = decision.action if waits and decision.candidates else None`, i.e. it
  survives deliberately into the next utterance.

So the correct statement is: hyprsay has no cross-turn SEMANTIC memory of entities, but
it does have a TTL-bounded cross-turn candidate list with a configured expiry and a
parameter already threaded from the daemon into the understander. The plan should
extend `_picking`/`pending` (including its TTL, its clearing at `daemon.py:159` and
`daemon.py:303`, and its interaction with `_forget_everything`) rather than build a new
memory layer beside it. Building a second, parallel memory with its own lifetime is how
two clearing paths disagree.

### Not separately checked

Findings 9 (focus_rank has no timestamp, the socket2 event list) and 10 (the tier
asymmetry, labelled an inference by the researcher) were not attacked directly. The tier
line numbers were spot-checked and are right: `tiers.py:44` is `Intent.FOCUS_WINDOW`
inside TIER0 and `tiers.py:56` is `Intent.LAUNCH_APP` inside TIER1.

### Overall

The researcher's primary sourcing is unusually good: every verbatim quote from a spec,
a repository or a paper survived being re-pulled and re-extracted, and the code line
numbers are right. The failures are all one level up, in the "matters_because" column,
where three sources are made to support conclusions they do not support (V4 leverage,
V6 rate transfer, V7 inverted), and one plain factual claim about the codebase is wrong
(V8). Treat the evidence as solid and the recommendations as unreviewed.
