# Research behind the plan

Collected 2026-09-20 and 2026-09-21 by research agents, one report per lane, then checked.
Read `critic.md` first: it lists the contradictions between lanes and what is still unverified.
The plan of record is `../PLAN.md`; where a report and the plan disagree, the plan records why.

## How much to trust each report

| Report | Subject | Checked by |
|---|---|---|
| `jev-wire.md` | HTTP protocol for calling Jev from Python | live calls (see `live/`), not by a checker agent |
| `jev-design.md` | how to write Jev questions | not independently checked; partly superseded by `live/` results |
| `nlu-arch.md` | intent and slot pipeline on a classifier-only model | not independently checked; its Jev latency assumption (150 to 250 ms) is wrong, measured 315 ms |
| `stt.md` | local speech to text on the target CPU | checker agent, trust high, two corrections appended |
| `gateway-stt.md` | speech models on the Vercel AI Gateway | checker agent, trust high; desk research only, no live call |
| `ui.md` | Hyprland-native overlay in GTK4 layer-shell | checker agent, trust high |
| `hypr-ipc.md` | Hyprland 0.56 sockets, dispatchers, Lua provider | checker agent, trust high; no dispatch was actually sent |
| `hypruse-map.md` | the hypruse codebase as a library | checker agent, trust high |
| `prior-art.md` | voice control UX lessons | not independently checked |
| `critic.md` | cross-lane contradictions, gaps, risks | n/a |
| `plan-review.json` | four-lens adversarial review of PLAN.md v1: 4 blockers, 21 majors, 12 minors | the 4 blockers were confirmed against source before being accepted; see PLAN.md section 14 |

Checker verdicts are appended to each checked report under `## Verification`.
`summaries.json` holds each lane's summary, recommendation and the checker's corrections.

## Caveats that apply to everything here

- Speech benchmarks ran while other agents had the machine at load average 16 to 27, on eight
  synthetic clips. They are pessimistic and thin. P0 reruns them idle with a real microphone.
- Reports written on 2026-09-20 say "no API key on this machine". A key arrived later; the
  measurements in `live/` supersede their estimates.
- Reports contain absolute paths from the research scratch directory. Those paths are gone.

## `live/`: measurements against the real Jev API

Scripts read the key from a file and never print it. Window titles in them are synthetic.

| Script | What it measured | Output |
|---|---|---|
| `probe.py` | both gateway routes work; first warm latency; first determinism check (n=6) | `probe_out.json` |
| `dayone.py` | latency vs tokens and vs question count, route A vs B, determinism (n=30), independence, option order, concurrency | `dayone_out.json` |
| `followup.py` | 503 rate small vs large, interleaved; self-ensembling | `followup_out.json` |
| `stt_bakeoff.py` | local recognizers on an idle machine vs every gateway batch speech model, same clips | `stt_bakeoff_out.json` |
| `stt_streaming.py` | gateway streaming speech models paced in real time; key release to final. It has no receive timeout and hung on gpt-realtime-whisper for nine minutes: do not rerun as is | `stt_streaming_out.json` (two of three models) |
| `stt_noise.py` | the same commands degraded with noise and a band-limited quiet mic | `stt_noise_out.json` |
| `prewarm.py` | whether an unauthenticated GET fully warms the path: three strategies, new connection per trial, interleaved | `prewarm_out.json` |
| `design.py`, `overlap.py` | question wording and the catch-all option problem | printed only; **output was not saved**, P0 reruns them inside the eval harness |

Known weaknesses of these measurements: one account, one morning, one network location; the
option-order and independence tests are underpowered against the measured run-to-run noise;
`design.py` used a different state from the earlier determinism test, so its "baseline" row
does not chain to it.
