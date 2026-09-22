"""What the machine looks like, what the last command did, and the hash of both.

Three things live here because they have one lifetime: they are built from one desktop
snapshot, they are read by one utterance, and they die together.

**The evidence.** `requests.utterance_state` sends `{utterance, variants, note}` and says
in its own docstring "Nothing about the desktop is here", while `bank.INTENTS` used to
tell the model that launching is "not_for: going to a window that is already open". The
question was asked and the evidence withheld (docs/STRATEGY.md 2.1 and 4.1). `for_request`
is that evidence: which applications are open, which workspace the speaker is on, what
they are looking at, and what the last command did. Measured with `requests.estimate`
against the eval fixture: R1's questions already go out as two parts, at 1,392 and 1,344
estimated tokens against the 1,800 cap, and the context takes them to 1,505 and 1,457. So
113 tokens on each part, nothing dropped, and latency is flat to about 2,246 tokens
(PLAN 5.5, the same table that says question count is free and payload is not). Building
the block costs 37 us and the fingerprint 4.6 us over a seven window desktop, against a
315 ms round trip.

**The memory.** `recent` is the last three commands that changed the desktop. It extends
the memory that already exists (`Understander.pending`, the daemon's `_picking`) rather
than starting a second one with its own lifetime, which docs/STRATEGY.md 4.5 calls a bug
factory: it is cleared on the same paths, expires on the same kind of timer, and is read
by the same utterance. It is evidence and never authority. Nothing here fills a slot,
names a target or lowers a tier, so a 25 second old memory cannot close a window. The
four invariants that would let it fill a slot (fill only, never above tier 1, evidence
does not refresh, one entity) need an eval family of their own and are not wired yet.

**The fingerprint.** A hash of the desktop a decision was made against, so the write can
refuse when the world has moved under it and the decision can be consumed before anything
is mutated (docs/STRATEGY.md 2.5, from `jev-ultrafast`). It sits BESIDE the confidence and
tier gates, never in place of them: a fingerprint proves the world has not moved, it says
nothing at all about whether the choice was right. Jev returns exactly 1.0 on 56.4 percent
of answers with a selective error of about 3 percent, so freshness is not a substitute for
a tier.

What may leave the machine from here (PLAN section 7): trusted `.desktop` names and kinds,
which is the same trust class `requests.describe_app` already sends, plus workspace
numbers and the normalized words of a previous command. Never a title, never an address,
never a pid, and never a word the speaker was dictating.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from hyprsay.lexicon import sanitize
from hyprsay.model import Action, App, Candidate, Decision, DesktopState, Intent, Verdict, Window
from hyprsay.nlu.resolve import LexiconLike

# the verdicts that mean this decision will change the desktop, which is what the next
# clause, and now the next utterance, is allowed to assume
ACTING = frozenset({Verdict.ACT, Verdict.ACT_SWAP, Verdict.COUNTDOWN, Verdict.CONFIRM_KEY})

# at most three, as docs/STRATEGY.md 4.5 specifies. Depth is a design choice, not a
# finding: the paper usually cited for depth one reports its own Constraint 1 failing on
# 20 to 25 percent of utterances, so the number is here to be measured, not defended.
MAX_TURNS = 3
# push-to-talk turns are not a conversation: the speaker lets go of the key and goes back
# to work. A starting assumption, to be set by the eval harness (PLAN P0).
TTL_S = 30.0
# the request carries names, not an inventory. Twelve covers every desktop measured here
# and keeps the block near the 113 tokens quoted above.
MAX_OPEN_APPS = 12
MAX_NAME_CHARS = 48
# a command is a sentence; anything longer was never a command worth remembering
MAX_SAID_CHARS = 120

# A correction opens by rejecting what just happened. A closed list, and only in the first
# two words, for the reason `understand.DICTATION_OPENERS` is one: "no" is also an
# ordinary word in the middle of a command ("the one with no terminal"), and a fuzzy rule
# would read those as corrections. Code calculates; the model is never asked to guess it.
CORRECTION_OPENERS = frozenset({"no", "nope", "nah", "not", "wrong", "oops", "undo", "cancel"})
CORRECTION_LOOKAHEAD = 2
# these mean a correction anywhere in the utterance: none is an ordinary command word
CORRECTION_WORDS = frozenset({"instead"})
# two words that only mean a correction together
CORRECTION_PAIRS = frozenset(
    {("other", "one"), ("not", "that"), ("not", "this"), ("other", "window"), ("wrong", "one")}
)

# an utterance whose action carries the speaker's own text: dictation, a recipe's spoken
# span, a control's name. Those words are theirs, not a command, so the turn remembers
# that it happened and not what was said (daemon._chip makes the same cut)
SPEAKER_TEXT = frozenset({Intent.TYPE_TEXT, Intent.RUN_RECIPE, Intent.CLICK_CONTROL})


def looks_like_correction(tokens: Sequence[str] | Iterable[str]) -> bool:
    """Do these words reject what just happened? Closed lists only, see above."""
    words = [str(word).lower() for word in tokens]
    if any(word in CORRECTION_OPENERS for word in words[:CORRECTION_LOOKAHEAD]):
        return True
    if set(words) & CORRECTION_WORDS:
        return True
    return bool(set(zip(words, words[1:], strict=False)) & CORRECTION_PAIRS)


# --------------------------------------------------------------------------- fingerprint


def fingerprint(state: DesktopState) -> str:
    """A hash of what a decision's target rests on.

    In it: which windows exist, the two class fields that identify each one, the
    workspace it is on, and whether the session is locked. Those are what a target
    choice was made from, and a change to any of them can make a chosen target wrong.

    NOT the focus, and not the active workspace. Hyprland follows the mouse by default,
    so both change while the speaker does nothing but move the pointer, and a freshness
    gate that refused a close because the pointer crossed a window would misfire on
    exactly the commands that need it most.

    NOT the title. A title is written by whoever owns the window, including a web page
    that rewrites it every second, so it may never be evidence for anything (PLAN 5.6).
    """
    parts = sorted(
        f"{w.address}\x1f{w.cls}\x1f{w.initial_class}\x1f{w.workspace_id}" for w in state.windows
    )
    parts.append(f"locked={int(state.locked)}")
    digest = hashlib.blake2s("\x1e".join(parts).encode("utf-8", "replace"), digest_size=8)
    return digest.hexdigest()


def moved(stamp: str, state: DesktopState) -> bool:
    """Has the desktop moved since it was stamped? An empty stamp never refuses: a
    decision nobody fingerprinted is judged by the tier gates alone, exactly as before."""
    return bool(stamp) and stamp != fingerprint(state)


# --------------------------------------------------------------------------- one turn


@dataclass(frozen=True)
class Turn:
    """One command that changed the desktop, as the next utterance may read it."""

    said: str = ""  # normalized words, blank when the speaker's own text was in them
    intent: str = ""  # an `Intent` value, never the enum: this is also request payload
    app: str = ""  # the trusted .desktop name of what it acted on
    kind: str = ""
    workspace: str = ""
    # never sent anywhere. Code re-resolves the window with it against fresh state,
    # because a Window snapshot is stale within milliseconds (executor._check_window).
    address: str = ""
    at: float = 0.0
    fingerprint: str = ""  # the desktop this was decided against
    ok: bool | None = None  # what the executor made of it, once it has run

    def live(self, now: float, ttl: float = TTL_S) -> bool:
        return self.at > 0.0 and 0.0 <= now - self.at <= ttl

    def seconds_ago(self, now: float) -> int:
        return max(0, int(now - self.at))


def remembered(
    decision: Decision,
    *,
    said: str,
    now: float,
    stamp: str,
    lexicon: LexiconLike | None = None,
) -> Turn | None:
    """The turn to keep for one decision, or None when there is nothing to keep.

    Only a decision that will change the desktop is remembered: `recent` is a list of
    actions, and a refusal, a question or a silence is not one. A chain is remembered by
    its head, because the daemon may stop it part way and only the head is certain to
    have been meant as it was understood.
    """
    action = decision.action
    if action is None or decision.verdict not in ACTING:
        return None
    app, kind = _named(action, lexicon)
    return Turn(
        said="" if _speaker_text(decision, action) else _short(said, MAX_SAID_CHARS),
        intent=action.intent.value,
        app=app,
        kind=kind,
        workspace=_short(action.workspace or "", MAX_NAME_CHARS),
        address=action.window.address if action.window is not None else "",
        at=now,
        fingerprint=stamp,
    )


def _speaker_text(decision: Decision, action: Action) -> bool:
    return decision.dictation or action.text is not None or action.intent in SPEAKER_TEXT


def _named(action: Action, lexicon: LexiconLike | None) -> tuple[str, str]:
    """The trusted name and kind of what an action acted on, or two empty strings.

    An untrusted `.desktop` file names nothing here, the same rule `Lexicon.kind_of`
    already applies: a user-writable entry must not be able to relabel anything.
    """
    if action.app is not None:
        return _trusted(action.app)
    if action.window is not None and lexicon is not None:
        owner = _owner(lexicon, action.window)
        if owner is not None:
            return _trusted(owner)
    return "", ""


def _trusted(app: App) -> tuple[str, str]:
    if not app.trusted:
        return "", ""
    return _short(app.name, MAX_NAME_CHARS), _short(app.kind or app.generic_name, MAX_NAME_CHARS)


def _owner(lexicon: LexiconLike | None, window: Window) -> App | None:
    if lexicon is None:
        return None
    try:
        app = lexicon.app_for_window(window)
    except Exception:  # a lexicon fault must never cost the utterance its decision
        return None
    return app if app is not None and app.trusted else None


def _short(text: str, limit: int) -> str:
    """`lexicon.sanitize` (NFKC, no control or format code points), then a cut."""
    return sanitize(text or "")[:limit].rstrip()


# --------------------------------------------------------------------------- the context


@dataclass(frozen=True)
class Context:
    """One utterance's view of the machine, and what the last few commands did.

    Frozen like everything in `model.py`: an understanding is made against one of these
    and can never drift away from it. `state` and `fingerprint` are this utterance's;
    `recent` is what survives into the next one.
    """

    state: DesktopState = field(default_factory=DesktopState)
    fingerprint: str = ""
    now: float = 0.0
    # the normalized utterance and the words the recognizer really produced
    utterance: str = ""
    tokens: tuple[str, ...] = ()
    # badges on screen: the same tuple the daemon carries as `_picking`
    picking: tuple[Candidate, ...] = ()
    pinned_address: str = ""
    recent: tuple[Turn, ...] = ()
    correction: bool = False
    lexicon: LexiconLike | None = None

    # ------------------------------------------------------------------ lifetime

    def about(
        self,
        state: DesktopState,
        *,
        now: float,
        utterance: str = "",
        tokens: Sequence[str] = (),
        picking: Sequence[Candidate] = (),
        pinned_address: str = "",
        lexicon: LexiconLike | None = None,
    ) -> Context:
        """This utterance, against this desktop, carrying only what is still in force."""
        kept = tuple(turn for turn in self.recent if turn.live(now))
        return Context(
            state=state,
            fingerprint=fingerprint(state),
            now=now,
            utterance=utterance,
            tokens=tuple(tokens),
            picking=tuple(picking),
            pinned_address=pinned_address,
            recent=kept,
            # a correction of nothing is not a correction
            correction=bool(kept) and looks_like_correction(tokens),
            lexicon=lexicon if lexicon is not None else self.lexicon,
        )

    def remember(self, turn: Turn | None) -> Context:
        return self if turn is None else replace(self, recent=(turn, *self.recent)[:MAX_TURNS])

    def did(self, ok: bool) -> Context:
        """What the executor made of the newest turn. A command that was refused at the
        write is not what the desktop looks like now, and the next utterance should be
        told so rather than left to assume it worked."""
        if not self.recent:
            return self
        return replace(self, recent=(replace(self.recent[0], ok=ok), *self.recent[1:]))

    def forgotten(self) -> Context:
        """A lock or a cancel: nothing said before this is in force any more."""
        return replace(self, recent=(), picking=(), correction=False)

    @property
    def previous(self) -> Turn | None:
        return next((turn for turn in self.recent if turn.live(self.now)), None)

    # ------------------------------------------------------------------ request payload

    def for_request(self) -> dict[str, Any]:
        """The context block for the shared request state. Trusted fields only.

        Empty keys are left out rather than sent as nulls: every one of them costs
        tokens on a request whose payload is the binding constraint (PLAN 5.5).
        """
        out: dict[str, Any] = {}
        apps = self.open_apps()
        if apps:
            out["open_apps"] = apps
        if self.state.active_workspace_id:
            out["current_workspace"] = str(self.state.active_workspace_id)
        focused = self._focused()
        if focused:
            out["focused"] = focused
        previous = self._previous()
        if previous:
            out["previous"] = previous
            if self.correction:
                out["correcting"] = True
        return out

    def open_apps(self) -> list[str]:
        """The trusted names of the applications that have a window open, most recently
        used first. A window whose class no trusted `.desktop` file claims is left out
        entirely: naming it from its own class would let a window name itself."""
        names: list[str] = []
        for window in sorted(self.state.windows, key=lambda w: w.focus_rank):
            owner = _owner(self.lexicon, window)
            if owner is None:
                continue
            name = _short(owner.name, MAX_NAME_CHARS)
            if name and name not in names:
                names.append(name)
            if len(names) >= MAX_OPEN_APPS:
                break
        return names

    def _focused(self) -> dict[str, str]:
        window = self.state.focused
        owner = _owner(self.lexicon, window) if window is not None else None
        if owner is None:
            return {}
        name, kind = _trusted(owner)
        return {"app": name, "kind": kind or "application"}

    def _previous(self) -> dict[str, Any]:
        """What the last command did. `on` is only sent while its window is still there:
        a window that has gone is not context, it is a wrong answer waiting to happen."""
        turn = self.previous
        if turn is None:
            return {}
        out: dict[str, Any] = {"did": turn.intent, "seconds_ago": turn.seconds_ago(self.now)}
        if turn.said:
            out["utterance"] = turn.said
        on: dict[str, str] = {}
        if turn.app and (not turn.address or self.state.by_address(turn.address) is not None):
            on["app"] = turn.app
            if turn.kind:
                on["kind"] = turn.kind
        if turn.workspace:
            on["workspace"] = turn.workspace
        if on:
            out["on"] = on
        if turn.ok is False:
            out["outcome"] = "failed"
        return out
