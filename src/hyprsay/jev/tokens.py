"""How big is this request going to be, before it is sent.

Size matters more than anything else about a Jev request. Latency is flat up
to roughly 2.2k input tokens (p50 about 315 ms), then climbs: 448 ms at 6k,
544 ms at 11.5k. Failures climb with it: interleaved in the same minute, small
requests succeeded 25 of 25 while 11.5k-token requests returned HTTP 503 four
times in 25. So the client refuses oversized requests up front and the caller
slices state across several small parallel requests instead, which costs about
45 ms for three.

The estimator is a linear fit over eight real requests spanning 517 to 11,542
reported tokens, worst error 2.9 percent:

    tokens = 250 + 0.424 * state_chars + 0.339 * question_chars - 11.7 * n_questions

The negative per-question term is real, not noise. Every question carries about
35 characters of JSON scaffolding (the "type" and "instructions" keys, braces,
quotes) that the service parses away instead of feeding to the model, and 11.7
tokens at 0.339 tokens per character is that scaffolding. An earlier version of
this module dropped the term as a supposed fitting artifact. That over-estimated
a 40-question request by 33 percent (1915 against a reported 1438) and would
have refused it at the cap, which defeats the one property the design leans on
hardest: extra questions are free. Eight points for four parameters is still a
thin fit, which is why the cap keeps headroom below the 2.2k knee and why
`Drift` exists.

The sample was synthetic English. Paths, Unicode and long identifiers in real
window titles tokenize worse, so `Drift` compares every estimate against the
count the API reports and the client logs when the fit stops holding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

FIXED_OVERHEAD = 250
TOKENS_PER_STATE_CHAR = 0.4241
TOKENS_PER_QUESTION_CHAR = 0.3386
TOKENS_PER_QUESTION = -11.7

# latency and the 503 rate are both flat below about 2.2k; leave headroom for
# the estimator being wrong
DEFAULT_CAP = 1800


def _chars(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def estimate(state: Any, questions: dict[str, Any]) -> int:
    """Estimated input tokens for a request body, as the wire will carry it."""
    return round(
        FIXED_OVERHEAD
        + TOKENS_PER_STATE_CHAR * _chars(state)
        + TOKENS_PER_QUESTION_CHAR * _chars(questions)
        + TOKENS_PER_QUESTION * len(questions)
    )


@dataclass
class Drift:
    """Running comparison of estimated against reported tokens.

    `ratio` above 1 means the estimator is running low, which is the dangerous
    direction: requests believed to be under the cap are not.
    """

    samples: int = 0
    estimated: int = 0
    reported: int = 0
    worst_under: float = 0.0

    def record(self, estimated: int, reported: int | None) -> None:
        if not reported or estimated <= 0:
            return
        self.samples += 1
        self.estimated += estimated
        self.reported += reported
        self.worst_under = max(self.worst_under, reported / estimated - 1.0)

    @property
    def ratio(self) -> float:
        return self.reported / self.estimated if self.estimated else 1.0

    @property
    def unreliable(self) -> bool:
        """The fit is off by more than 15 percent in the unsafe direction."""
        return self.samples >= 5 and self.ratio > 1.15
