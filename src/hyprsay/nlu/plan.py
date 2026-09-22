"""The ordered plan: what the speaker asked for, in the order they asked for it.

WHY THIS EXISTS. Until now a compound utterance was cut by proposing connective seams
(`nlu/clauses.py`) and asking Jev one Boolean per seam. That design is right about the
thing it was built for and wrong about two others, both measured against the owner's own
utterances (STRATEGY.md 0 and 4.2):

- a compound with no connective in the closed list is never seamed at all. "open chrome,
  go to youtube" only works because of the comma, and "open chrome go to youtube" is
  obeyed a third of the way, which is the bug this whole area exists to fix.
- each clause is then understood on its own, so clause 2 cannot see what clause 1 made
  true.

So stop asking about the sentence and ask about the machine. Code harvests what this
desktop can do right now as a list of `adapters.Affordance`, and one request asks three
Choices over that SAME list, `step_1`, `step_2`, `step_3`, each with a `none` option.
Code takes the prefix up to the first `none`. Nothing here ever asks how many steps
there are: the vendor documents that Jev does not count reliably (STRATEGY.md 4.2), so
the count is never an answer, only a consequence of where `none` first wins.

The count of questions is free (1, 5, 15 and 40 questions measured 323, 322, 312 and
317 ms p50), which is what makes this one round trip. The PAYLOAD of those questions is
not free, and three Choices carry the option list three times; `requests.plan_request`
holds that number and shrinks the list to fit.

WHAT THIS MODULE IS NOT ALLOWED TO DO. It reads ids and probabilities. It never reads a
label, so no text an application or a web page wrote can decide what a plan contains or
how long it is. It never decides a tier: every step is still judged by `nlu/tiers.py`
and still goes through the executor with its key-down pin, exactly as a single command
does. And it can only ever return affordances code minted, because `read` looks every
answered key up in the map code built and stops at one it does not find.

DICTATION. A step is an affordance, and dictated words can never be one: a harvester
builds options from the machine, and typing is reachable through the exact grammar alone
(`TYPE_TEXT` is absent from `JEV_INTENTS`). So "type hello and goodbye" cannot come back
cut in half here whatever the model answers, and `nlu/clauses.py` keeps the job it is
good at, which is refusing to offer any seam from the first carrier verb onward.

THE FIVE RULES, all decided here in code and all tested. A slot yields a step only when
every one of them holds; the first slot that fails ends the plan and no later slot is
read, which is what "prefix" means:

1. ANSWERED. There is a `ChoiceAnswer` for this slot. A request that failed leaves the
   utterance with no plan at all, which is v1's behaviour and the safe one, the same
   rule `understand._separating` already follows for a seam nobody could judge.
2. NAMED, and named by code. `none` ends the plan. So does a key that is not in the
   option map: the model chooses among candidates code built, and a key code never
   minted is a broken answer, not an instruction.
3. SURE. `p1` clears the floor and `margin` clears the margin. A thin pick is not a
   step, for the same reason a thin pick is not a window.
4. NEW. Jev evaluates each question independently against one shared state, so all
   three Choices see the same words and nothing tells step_2 what step_1 answered. On a
   one-step utterance the only thing making step_2 answer `none` is the wording, so a
   repeated affordance means the wording did not hold, not that the speaker asked
   twice. And once the numbering has broken, the slot after it cannot be trusted
   either, so a repeat ends the plan rather than being skipped. The cost of being wrong
   about this is one dropped step when somebody really did say "next, next"; the cost
   the other way is acting twice on something said once, which for a close is a second
   window gone.
5. BOUND, unless it is the whole plan. Every affordance was minted from ONE observation
   of the machine, taken before the words were understood. One that names its own target
   (`handle`: a bus name, a track id, a window address) still means the same thing after
   an earlier step has moved the desktop, and the adapter re-checks that the target is
   live before it acts. One with no target of its own acts on whatever is in front of
   the speaker at the moment it runs, so after any earlier step what it would hit is
   decided by a state nobody observed. That is "typing before focusing" in its general
   form, and it is the one thing about the ORDER that code can actually judge: code
   cannot know whether the model put two named targets in the order the speaker said
   them, and it must not try to work that out from labels, because labels are untrusted.
   So an unbound affordance is a legal plan all by itself and is never one step of a
   longer one.

A plan that code cut is reported as cut (`Plan.complete` is False) rather than quietly
shortened. Carrying out the front of an utterance and dropping the rest is the failure
this area exists to remove, so the caller is told, and it may refuse the way
`understand._partial` already refuses leftovers it could not judge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hyprsay.adapters.base import Affordance
from hyprsay.jev.types import ChoiceAnswer, Question
from hyprsay.nlu import bank

# Three, because three is what one push-to-talk breath holds and because a fourth slot
# costs a fourth copy of the option list (`requests.plan_request`). Past three, words
# that look like orders are far more likely to be talk, and the safe reading of talk is
# to do nothing extra.
MAX_STEPS = 3
STEP = "step_"

# A relative Choice over a mixed list, with an explicit `none`, behaves like the app
# Choice rather than like the per-window Booleans: it is confident in absolute terms, so
# a floor is meaningful. These are `Gates.app_offer` and `Gates.margin`'s values, kept
# here as their own knobs rather than read from those, because one knob must never serve
# two different questions (ARCHITECTURE.md, and the bug that lesson came from). They are
# uncalibrated for THIS question: no eval has scored a step Choice yet.
MIN_PROBABILITY = 0.5
MIN_MARGIN = 0.3


class Cut(StrEnum):
    """Why reading stopped. The first two are the model ending the plan; the rest are
    code refusing something the model named."""

    NONE = "none"  # the model said there is no step here
    END = "end"  # every slot was used
    UNANSWERED = "unanswered"  # no answer for this slot: a failed or missing request
    UNKNOWN = "unknown"  # a key code never offered
    UNSURE = "unsure"  # under the floor or inside the margin
    REPEAT = "repeat"  # an affordance an earlier step already took
    UNBOUND = "unbound"  # an option with no target of its own, in a plan with other steps


@dataclass(frozen=True)
class Step:
    """One thing to do, and how sure the answer that chose it was."""

    position: int  # 1, 2 or 3: the slot it was read from
    id: str  # the option key, which is the affordance id
    affordance: Affordance
    probability: float = 0.0
    margin: float = 0.0


@dataclass(frozen=True)
class Plan:
    """An ordered prefix of affordances, and why it stopped where it did."""

    steps: tuple[Step, ...] = ()
    cut: Cut = Cut.UNANSWERED
    cut_at: int = 1  # the slot reading stopped at; 0 when it ran out of slots
    refused: str = ""  # the option key code would not take, or "" when the model stopped

    @property
    def complete(self) -> bool:
        """Did the plan end because the model said so, rather than because code refused?

        False means the model named something that is not in this plan, so acting on
        what IS in it would do part of what was asked. The caller decides what to do
        about that; this only refuses to hide it.
        """
        return self.cut in (Cut.NONE, Cut.END)

    @property
    def compound(self) -> bool:
        return len(self.steps) > 1

    def describe(self) -> str:
        """For the journal and `inspect --last`. Ids only: every one of them was minted
        by code, while a label was written by an application or by the internet."""
        return ", ".join(f"{step.position}. {step.id}" for step in self.steps)


def step_id(position: int) -> str:
    return f"{STEP}{position}"


def bound(affordance: Affordance) -> bool:
    """Does this option name its own target, or act on whatever is in front?

    The handle IS the target. `adapters/base.py` mints one for every candidate that
    names a live thing and leaves it empty for the one kind that names none: a
    capability offered as itself because it cannot be resolved until the words exist.
    Both readings of an empty handle say the same thing about position. Such an option
    acts on whatever is in front when it runs, and its slots would have to be filled
    from an utterance nothing has cut, so neither code nor the adapter can say what a
    later step of it would do.
    """
    return bool(affordance.handle)


def offered(affordances: Iterable[Affordance]) -> dict[str, Affordance]:
    """The option map, keeping the order the caller ranked them in.

    Two things are settled here rather than discovered on the wire:

    - ids are unique. `adapters.candidates` dedupes within one capability and `offer`
      does not dedupe across adapters, while options are a dict, so a repeated id would
      swallow an option silently and leave which one survived to insertion order. First
      wins, because the caller ranked them.
    - `none` is reserved. `adapters.base._ID` allows an affordance whose id is literally
      "none", and that option would shadow the one that ends the plan, turning "there is
      no second step" into an action. It is dropped rather than renamed: a renamed id is
      one code cannot route home.
    """
    out: dict[str, Affordance] = {}
    for affordance in affordances:
        if affordance.id == bank.NONE or affordance.id in out:
            continue
        out[affordance.id] = affordance
    return out


def questions(options: Mapping[str, Affordance], count: int = MAX_STEPS) -> dict[str, Question]:
    """`count` Choices over the SAME options, one per step, for one request.

    No options means no questions: a Choice whose only option is `none` asks nothing,
    and it would still cost its own instructions on the wire.
    """
    count = min(count, MAX_STEPS)
    if not options or count < 1:
        return {}
    described: dict[str, Any] = {key: found.as_option() for key, found in options.items()}
    return {step_id(n): bank.plan_step(n, described) for n in range(1, count + 1)}


def result_questions(options: Mapping[str, Affordance]) -> dict[str, Question]:
    """ONE Choice over the same options, asking which RESULT the speaker meant.

    Keyed `step_1` like every other step question, so `read` below is the one place that
    decides what a step answer has to look like and needs no second shape to know about.
    What changes is the wording, and only because a measurement said it had to: see
    `bank.chosen_result`.
    """
    if not options:
        return {}
    described: dict[str, Any] = {key: found.as_option() for key, found in options.items()}
    return {step_id(1): bank.chosen_result(described)}


def read(
    answers: Mapping[str, Any],
    options: Mapping[str, Affordance],
    *,
    floor: float = MIN_PROBABILITY,
    margin: float = MIN_MARGIN,
    count: int = MAX_STEPS,
) -> Plan:
    """The plan the answers describe: the prefix that passes all five rules.

    `answers` is the request family's answers by question id, the shape
    `understand._Answers.r1` already carries. Anything that is not a `ChoiceAnswer` for
    the slot being read ends the plan, so a failed request, a half-answered one and a
    reply of the wrong type all come back the same way: no plan, and the caller does
    what it did before there was one.

    `count` is how many slots the REQUEST carried, and the caller passes the same number
    it passed to `questions`. Asking for more slots than were sent reads the missing one
    as a failure and reports a complete plan as cut.
    """
    count = min(count, MAX_STEPS)
    steps: list[Step] = []
    taken: set[str] = set()
    for position in range(1, count + 1):
        answer = answers.get(step_id(position))
        if not isinstance(answer, ChoiceAnswer):
            return Plan(tuple(steps), Cut.UNANSWERED, position)
        key = answer.choice
        if key == bank.NONE:
            return Plan(tuple(steps), Cut.NONE, position)
        found = options.get(key)
        if found is None:
            return Plan(tuple(steps), Cut.UNKNOWN, position, key)
        if answer.p1 < floor or answer.margin < margin:
            return Plan(tuple(steps), Cut.UNSURE, position, key)
        if key in taken:
            return Plan(tuple(steps), Cut.REPEAT, position, key)
        # rule 5: only the first step may be unbound, and then it is the whole plan
        if steps and not (bound(found) and bound(steps[0].affordance)):
            return Plan(tuple(steps), Cut.UNBOUND, position, key)
        steps.append(Step(position, key, found, answer.p1, answer.margin))
        taken.add(key)
    return Plan(tuple(steps), Cut.END, 0)
