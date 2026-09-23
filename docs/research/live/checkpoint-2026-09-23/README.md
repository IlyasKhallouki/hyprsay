# Checkpoint: one Jev request per command, 2026-09-23

The spec (`docs/superpowers/specs/2026-09-23-browser-voice-design.md`) set the bar before any
of this was built: replay the owner's real voice transcripts through the new path against live
Jev, and pass at 80 percent clearly right with no wrong action, or stop and report.

## Result

The replay fell short of 80 percent, and it made no wrong action.

| | Real voice replay, 38 cases x 3 | Eval suite, 119 cases x 3 |
|---|---|---|
| single request (new) | 84/114 correct (73.7%), 0 wrong | 309/357 correct (86.6%), 0 wrong |
| seven-request fan-out (old) | 69/114 correct (60.5%), 21 wrong | 318/357 correct (89.1%), 3 wrong |

Adversarial cases (hostile window titles): 25 of 27 correct on the new path and 2 missed, none
wrong; 27 of 27 on the old one. The old path's figure on the replay was measured before the
last round of fixes, which were to the new path's safety rules, but it shares every harvest
fix made today.

For scale, the live session on 21 and 22 September got about 14 of 44 spoken commands right.

Latency, per decision: the suite is mostly grammar, 30 ms p50 and 600 ms p90. The replay is
mostly Jev, about 500 ms p50 and 1.3 s p90; the p90 is the commands that take a second turn
(a lookup, then a choice among what came back).

## What the 30 replay misses are

None of them acted wrongly: each one asked, offered to do something, or did nothing.

- By design, 6 samples. "GitHub.com." has no verb, and a typed address is a countdown that
  needs one. "Open Zap." is ZapZap cut short by the recognizer; live, the cloud recognizer
  heard it again and got it right, and the replay does not re-hear.
- Borderline confidence, about 12. "Search for Death Punk", "Look up LT" and "play the song"
  sit at 0.45 to 0.51 against a floor of 0.5. Lowering the floor was measured against the
  saved answers and would have turned 6 to 8 of them correct and 3 to 4 other samples into
  wrong actions, so the floor stays.
- Pronouns, 3. "Focus on it" after opening Spotify: Jev picks the window in front however the
  options are labelled, and the tie rule turns that into a question.
- The rest are compound commands whose second half is unsure, and one stand-in adapter that
  is less forgiving than real Spotify search.

About half of the misses are in flows the browser extension replaces: the keystroke recipes'
near-synonyms ("find words on this page" against "search the web") and the desktop Spotify
app's search.

## What changed to get here

Each of these was measured and then fixed with a test, in this order. The first replay was 60.5
percent correct with 17 wrong samples.

1. A parse refusal of an address counted as a safety refusal and stood.
2. "GitHub. com" reached the address recipe as "github com" and was refused.
3. Nothing could bring a window here, send one to a numbered workspace, or rank what the last
   command touched first.
4. A clause after "open my browser" was harvested as if the browser were still behind.
5. Typing recipes were offered for windows the speaker was not looking at, so a search with
   Spotify in front became a web search in Chrome.
6. An over-long request dropped the options the sentence was about first.
7. Options that could not run could still win, and two ways of going to an address split one
   vote.
8. The independent suite then found 22 wrong actions: the new path had dropped the same-app
   tie rule, the offer-instead-of-launch rule for going verbs, and the launch naming gate. All
   three are back.

The replay was used to find these defects, so it is no longer an unseen test. The suite was
written before today and is the better estimate of how this generalizes. A fresh session spoken
into the microphone is still the test that counts.

## Files

`journal-single.*` and `journal-fanout.*`: the replay (`evals/journal.py`), raw samples and
report. `suite-single.*` and `suite-fanout.*`: the suite (`evals/run.py --path`).
