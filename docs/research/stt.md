# Research lane: stt (local speech to text for voice commands on Hyprland)

Date: 2026-09-21. Target: i5-8350U (4C/8T Kaby Lake-R, AVX2), no GPU, CPython 3.14.7, PipeWire 1.6.8.
Status: COMPLETE (2026-09-21). Sections 0 to 10. All latency figures are local measurements on a machine that other lanes kept at loadavg 6 to 25, so they are pessimistic.

Budget context (from main loop, measured): the Jev decision costs about 320 ms warm from this machine. So for a sub-second "speak to act" feel, STT finalization after end of speech must land in roughly 150 to 400 ms, and endpointing silence is the dominant term.

## 0. Artifacts recovered from the previous (killed) run of this lane

The previous run left these in the scratchpad (same directory as this report):
- `sttvenv/` : a CPython 3.14 venv with `moonshine_voice 0.1.5`, `sherpa_onnx 1.13.8` (+ `sherpa_onnx_core`), `sounddevice 0.5.6`, `numpy 2.5.3` installed. This alone proves cp314-compatible wheels exist for those packages on linux x86_64 (local measurement).
- `bench_moonshine.py`, `bench_tiny.json`, `bench_tiny05.json`, `bench_small.json`, `cmd_audio.npz` : a local Moonshine streaming benchmark on 8 synthetic command utterances.
- `moonshine_README.md`, `onnxasr_README.md`, `tenvad_README.md`, `ms/*.md` (Moonshine docs), `sherpa_online_recognizer.py`, `pypi_check.py`.

(Sections below are filled in as I go.)

## 1. Python 3.14 wheel availability (PyPI JSON API, queried 2026-09-21, linux x86_64)

Method: `https://pypi.org/pypi/<pkg>/json`, latest release, filter filenames for manylinux/musllinux x86_64 and look for cp314, abi3, py3-none-<platform> or pure py3-none-any. Source quality: primary (PyPI), high confidence.

| Package | Latest (upload date) | cp314 on linux x86_64? | Notes |
|---|---|---|---|
| moonshine-voice | 0.1.5 (2026-08-24) | YES, `py3-none-manylinux_2_34_x86_64` (interpreter independent, ctypes/cffi style) | MIT. Installed and imported fine on system 3.14.7 (local, previous run). Needs glibc >= 2.34 (Arch is fine). |
| useful-moonshine-onnx | 20251121 | YES, pure python (`py3-none-any`), depends on onnxruntime + tokenizers | legacy v1 non-streaming tiny/base path |
| sherpa-onnx | 1.13.8 (2026-09-10) | YES, `cp314-cp314-manylinux2014_x86_64` | Apache-2.0. Now split: `sherpa-onnx-core` 1.13.8 is `py3-none-manylinux2014_x86_64` and carries the native libs. Installed fine on 3.14.7 (local). |
| onnx-asr | 0.12.0 (2026-07-15) | YES, pure python, requires_python >= 3.10 | MIT. Needs onnxruntime (below). |
| onnxruntime | 1.30.0 (2026-09-10) | YES, `cp314` and `cp314t` manylinux_2_28 | MIT. requires_python >= 3.11 |
| vosk | 0.3.45 (2022-12-14) | YES, `py3-none-manylinux2010_x86_64` (cffi, interpreter independent) | Apache-2.0 (project), unmaintained on PyPI since 2022. Requires `cffi` which has cp314 wheels (cffi 2.1.1 is in the local 3.14 venv). |
| faster-whisper | 1.2.1 (2025-10-31) | YES pure python; hard deps: ctranslate2, tokenizers, av, onnxruntime, huggingface-hub | MIT |
| ctranslate2 | 4.8.2 (2026-08-31) | YES, cp314 + cp314t | MIT |
| tokenizers | 0.23.2 | YES via `cp310-abi3` | |
| av (PyAV) | 18.1.0 | YES via `cp311-abi3` (plus cp314t) | |
| pywhispercpp | 1.5.1 (2026-08-22) | YES, cp314 (manylinux + musllinux), even cp315 | MIT. whisper.cpp binding. |
| whisper-cpp-python | 0.2.0 (2023) | sdist only, dead | skip |
| silero-vad | 6.2.2 (2026-09-17) | pure python BUT depends on torch + torchaudio by default | MIT. Use the ONNX file directly with onnxruntime, or the wrappers below, to avoid torch. |
| pysilero-vad | 3.4.0 (2026-07-07) | YES via `cp39-abi3` | MIT, bundles the silero onnx model + tiny runtime, no torch |
| pymicro-vad | 2.1.0 (2026-06-29) | YES via `cp39-abi3` | Apache, microVAD from the Home Assistant / OHF-Voice family |
| ten-vad | 1.0.6.8 (2025-11-14) | pure python wheel that ships a prebuilt `.so`, no interpreter ABI tie | license: see section on VAD (NOT plain Apache, has non-compete clause) |
| webrtcvad | 2.0.10 (2017) | sdist only (needs compile; C extension uses removed APIs risk) | |
| webrtcvad-wheels | 2.0.14 (2024-09-05) | NO cp314 wheel (cp36 to cp313 only); sdist exists | would need a local build on 3.14 |
| openwakeword | 0.6.0 (2024-02-11) | pure python, BUT on Linux it depends on `tflite-runtime`, which stops at cp311 | effectively broken on 3.14 unless installed `--no-deps` and used in ONNX mode with onnxruntime. Stale since Feb 2024. |
| tflite-runtime | 2.14.0 (2023) | NO (cp38 to cp311) | superseded by ai-edge-litert |
| ai-edge-litert | 2.2.0 (2026-08-12) | YES cp314 | the maintained TFLite runtime |
| pymicro-wakeword | 2.5.0 (2026-09-17) | YES, `py3-none-manylinux_2_35_x86_64` | Apache-2.0, OHF-Voice, actively maintained, microWakeWord models on CPU |
| sounddevice | 0.5.6 (2026-08-17) | YES pure python (cffi + system libportaudio) | MIT |
| pyaudio | 0.2.14 (2023) | sdist only, must compile against portaudio | avoid |
| moshi (Kyutai STT, PyTorch) | 0.2.13 (2026-02-12) | pure python, requires_python <3.15,>=3.10, but needs torch | |
| rustymimi | 0.4.1 (2025-02-05) | NO cp314 (cp38 to cp313) | Kyutai rust audio codec binding |
| nemo-toolkit | 3.0.0 | pure python, drags PyTorch; not viable on this box | |
| openvino | 2026.4.0 (2026-09-16) | YES cp314 | only relevant if an OpenVINO whisper path were chosen |
| pipewire-python | 0.2.3 (2023) | pure python, but it is only a subprocess wrapper around pw-cat, stale | no value over calling pw-cat/pw-record yourself |
| kroko-onnx | not on PyPI (404) | n/a | Kroko/Banafo ships via its own fork of sherpa-onnx |

Bottom line: Python 3.14 is NOT a blocker for the serious candidates (moonshine-voice, sherpa-onnx, onnx-asr + onnxruntime, vosk, faster-whisper, pywhispercpp all install on 3.14). The 3.14 casualties are openWakeWord's tflite dependency, webrtcvad-wheels, and rustymimi.

## 2. Candidate by candidate

### 2.1 Moonshine Voice (moonshine-ai, v2 "Streaming" models, package `moonshine-voice` 0.1.5)

Sources: repo README and docs saved locally under `ms/` (primary, vendor), https://moonshine-voice.readthedocs.io/, PyPI, plus local measurement on the target machine.

- What it is now: no longer just ONNX files plus `useful-moonshine-onnx`. It is a C++ core (`libmoonshine.so`, bundles ONNX Runtime, `.ort` flatbuffer models, int8 post-training quantization with per-channel scales) with a Python binding shipped as an interpreter-independent `py3-none-manylinux_2_34_x86_64` wheel (19.9 MB). Includes its own VAD segmenter, a `MicTranscriber` (uses `sounddevice`), event listener API (`on_line_text_changed`, `on_line_completed`), TTS, and an intent/"AgentFlow" layer we do not need.
- Models (vendor table, Open ASR Leaderboard average WER, float reference): Tiny Streaming 34M params 12.00%, Small Streaming 123M 7.84%, Medium Streaming 245M 6.65%; legacy non-streaming Tiny 26M 12.66%, Base 58M 10.07%. Quantized LibriSpeech test-clean WER (vendor, `ms/quantization.md`): Tiny Streaming 4.83%, Small 2.61%, Medium 2.17%. All English models MIT (code MIT too).
- On-disk (local measurement of the download cache): tiny-streaming-en about 45 MB, small-streaming-en about 142 MB.
- Vendor latency claim ("time from VAD end-of-phrase to final text", `ms/vs.md`): Linux x86 column Tiny 69 ms, Small 165 ms, Medium 269 ms; whisper tiny via faster-whisper 1,141 ms on the same box. The x86 machine is not identified and the docs say the Linux column "reads pessimistically" (taken before a build fix). VENDOR CLAIM, not reproduced here.
- Streaming design: encoder output and part of decoder state are cached; every `transcription_interval` (default 0.5 s) it runs an incremental pass, with speculative decoding that verifies the previous hypothesis. Partials are real (`on_line_text_changed`).
- Endpointing: built-in VAD with `vad_threshold` 0.5, `vad_window_duration` 0.5 s (averaging window), `vad_hop_size` 512, `vad_look_behind_sample_count` 8192. There is no explicit "min trailing silence" knob; the 0.5 s averaging window is effectively the endpoint delay. It can be shortened (`vad_window_duration`) or bypassed: `vad_threshold=0` disables VAD and the app calls `stop()` itself (push to talk or external VAD).
- Contextual biasing: first class. `keyterms` (comma separated, phrases allowed, output takes the capitalization you give), `keyterm_boost` default 2.0 (usable range 1 to 4), `set_keyterms([...])` at runtime while streaming, and `set_context(text)` which mines rare words out of a passage (for example the list of window titles). Implementation is a subword prefix tree with a depth-ramped logit bonus. Vendor numbers (LibriSpeech test-clean, 100-term lists with distractors, Tiny, all 2,620 utterances): WER on listed terms 12.23% unbiased to 8.75% at boost 2.0 to 7.85% at 4.0, while WER on other words goes 6.92% to 7.11% to 8.48%. 100 terms cost about 1 ms per phrase. Only the streaming architectures support it. This maps exactly onto "app names and window titles": call `set_keyterms(app_names + window_classes)` and `set_context(" ".join(window_titles))` on every Hyprland `openwindow`/`windowtitle` event.
- Python 3.14: works (installed and ran on system 3.14.7, local).

#### Local measurement on the target i5-8350U (this lane, synthetic TTS command audio fed in real time, 80 ms chunks; script `bench_moonshine.py`)

CAVEAT: the machine was NOT idle in any run (other research lanes were running node/npm/chrome; loadavg 10 to 17 in the first runs, about 2.5 at the start of the "quiet" rerun). Treat these as pessimistic, noisy upper bounds. "ptt final" = time from the `stop()` call (issued right after the last speech sample) to `on_line_completed`. "vad final" = time from the last speech sample to `on_line_completed` when trailing silence is fed and the built-in VAD decides.

| Model | update_interval | loadavg at run | ptt final (median, range) ms | vad final (median, range) ms | first partial after speech start (median) ms | RSS MB | load ms | compute load on 44 s sample |
|---|---|---|---|---|---|---|---|---|
| Tiny Streaming | 0.5, no keyterms | 11 to 14 | 243 (61 to 361) | 780 (506 to 1009) | about 780 | 222 | 299 | 83% |
| Tiny Streaming | 0.2, keyterms | 14 to 16 | 234 (186 to 463) | 942 (635 to 1185) | about 550 | 484 | 672 | 119% |
| Tiny Streaming | 0.2, keyterms | 2.5 rising to 10 | 384 (260 to 715) | 837 (637 to 1105) | 557 (ptt rows) | 235 | 398 | 104% |
| Small Streaming | 0.3, keyterms | 14 to 18 | 1036 (606 to 1315) | 1727 (1164 to 5350) | about 1350 | 501 | 593 | 111% |

Accuracy on the 8 synthetic commands: all 8 correct for both models in every run, with inverse text normalization applied ("workspace three" comes back as "workspace 3", "fullscreen" as "full screen", trailing period). Key terms "Firefox", "kitty", "Obsidian" came back with the requested capitalization. Synthetic Kokoro TTS audio is clean speech, so this says nothing about real microphone WER.

Observations:
1. The process burned about 386% CPU during the as-fast-as-possible pass: the bundled ONNX Runtime uses all cores with spinning, and no thread-count option is documented (searched `ms/options.md`, `ms/classes.md`, and `strings libmoonshine.so`; not found). On a 4C/8T 15 W part, that competes with everything else. Tiny Streaming already costs 83% to 119% of real time here when partials are requested every 0.2 to 0.5 s, far above what the vendor table implies. Small Streaming is not comfortable on this CPU under load.
2. With push to talk (or an external VAD calling `stop()`), Tiny Streaming finalization is about 190 to 400 ms after end of speech on this machine even under load. With the built-in VAD the endpoint wait dominates: about 0.8 to 0.95 s.
3. The numbers and the vendor's "69 ms on Linux x86" differ by 3x to 5x. Plausible reasons: 15 W Kaby Lake-R vs unknown desktop CPU, contention from other lanes, and the vendor metric excludes VAD wait.

Rerun of Small Streaming at loadavg about 10 to 13 (file `bench_small_quiet.json`): ptt final median 885 ms (528 to 1041), vad final median 1375 ms (1211 to 1639), compute load 108%, RSS 467 MB. Conclusion unchanged: Small Streaming is too heavy for this CPU when anything else is running; Tiny Streaming is the Moonshine size that fits.

### 2.2 sherpa-onnx (k2-fsa, Apache-2.0, `sherpa-onnx` 1.13.8, 2026-09-10)

Sources: PyPI; the installed package source in `sttvenv/.../sherpa_onnx/online_recognizer.py` and `keyword_spotter.py` (primary); the GitHub release asset list for tag `asr-models` saved by the previous run as `asr_assets.json` (primary, fetched 2026-09-20); docs https://k2-fsa.github.io/sherpa/onnx/hotwords/index.html; local measurement.

- One runtime that covers: streaming transducers (zipformer, NeMo cache-aware FastConformer, Nemotron streaming), streaming paraformer (Chinese/English bilingual only, not useful here), offline models (Parakeet TDT, Whisper, Moonshine v1/v2 non-streaming exports, Canary, SenseVoice), VAD (Silero and TEN VAD), keyword spotting, and speaker ID. `num_threads` is an explicit parameter (unlike Moonshine Voice), which matters a lot on a 4C/8T laptop.
- English streaming models available as prebuilt downloads (from the release asset list, sizes are the tar.bz2):
  - `sherpa-onnx-streaming-zipformer-en-kroko-2025-08-06` 57 MB (Banafo Kroko ASR community model; license pointer in its README goes to https://huggingface.co/Banafo/Kroko-ASR, NOT verified by me, check before shipping)
  - `sherpa-onnx-streaming-zipformer-en-20M-2023-02-17` 128 MB (fp32 + int8; LibriSpeech-trained, Apache-2.0)
  - `sherpa-onnx-streaming-zipformer-en-2023-06-26` 310 MB, `-2023-06-21` 507 MB, `-2023-02-21` 398 MB
  - `sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-{80,480,1040}ms-int8` about 103 to 106 MB each (NVIDIA cache-aware FastConformer 114M)
  - NEW in 2026: `sherpa-onnx-nemotron-speech-streaming-en-0.6b-{80,160,560,1120}ms-int8-2026-04-25` 464 MB, `sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-{80,160,320,560,1120}ms-int8-2026-06-11` 475 MB, `sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-{240,560,1120}ms` 501 MB (2026-05-12)
  - Offline: `sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8` 482 MB, `-v3-int8` 487 MB, `sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8` 108 MB, `sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27` 30 MB, `sherpa-onnx-moonshine-base-en-quantized-2026-02-27` 111 MB, whisper tiny.en 118 MB / base.en 209 MB.
- Endpointing (installed source, defaults): `enable_endpoint_detection=False` by default; `rule1_min_trailing_silence=2.4` (nothing decoded yet), `rule2_min_trailing_silence=1.2` (after something was decoded), `rule3_min_utterance_length=20.0`. Trailing silence is counted from decoded blank frames, so its resolution is the model's chunk size. The stock 1.2 s is far too slow for commands; it must be lowered to about 0.3 to 0.5 s, or replaced by an external VAD.
- Hotwords (docs + installed source): transducer models only (streaming and offline), requires `decoding_method="modified_beam_search"`, `hotwords_file` one phrase per line with optional per-phrase score `PHRASE :3.5`, global `hotwords_score` default 1.5, needs `modeling_unit` (`bpe`, `cjkchar`, `cjkchar+bpe`) and `bpe_vocab` for BPE models. Per-stream runtime hotwords are supported: `OnlineRecognizer.create_stream(hotwords: Optional[str])` exists in the installed 1.13.8 source (the docs page summary did not mention it, the code does). No accuracy table in the docs (not found). Practical catch: the Kroko model directory ships only `tokens.txt` (no `bpe.model`/`bpe.vocab`), so hotwords on it need a vocab file derived from tokens; NeMo-exported transducers in sherpa-onnx historically did not support hotwords (not re-verified for 1.13.8, treat as open).
- Keyword spotting: `KeywordSpotter` with `keywords_file`, `keywords_score=1.0`, `keywords_threshold=0.25`, and `create_stream(keywords=...)` for per-stream extra keywords (installed source). Open vocabulary: keywords are token sequences, so any wake phrase works without training. English KWS model: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (from sherpa docs, 3.3M params).

#### Local measurement, sherpa-onnx streaming zipformers on the i5-8350U (script `bench_sherpa.py`, 2 threads, greedy search, same 8 synthetic commands, real-time 80 ms feeding, loadavg 6 to 8 from other lanes)

| Model | RTF (44 s sample, 2 threads) | RSS MB | load ms | 8-command accuracy | ptt final ms (flush with tail padding + `input_finished`) | endpoint rule2=0.4 s: final ms after speech end | first partial after speech start |
|---|---|---|---|---|---|---|---|
| zipformer-en-kroko-2025-08-06 | 0.076 to 0.111 | 196 | 5600 to 8000 (fp32 encoder, 70 MB) | 8/8 words correct, cased, some punctuation ("Focus, Kitty") | 95 to 196 (median about 180) with 1.6 s of zero padding | 997 to 1726 | about 1.2 to 1.3 s |
| zipformer-en-20M-2023-02-17 (int8) | 0.093 | 141 | 1700 | 0/8 usable: drops utterance onsets ("'S THIS WINDOW", "US KITTY", "ON") | 24 to 72 with 0.6 s padding | 818 to 1384 | 0.83 to 1.8 s |

Findings:
1. Compute is a non-issue for small zipformers here: RTF about 0.1 on 2 threads, under 200 MB RSS. They are 8x to 10x cheaper than Moonshine Tiny Streaming as deployed by `moonshine-voice`.
2. The Kroko model has a large encoder chunk (first partial only about 1.25 s after speech onset, and with only 0.4 s or 1.0 s of tail padding the last chunk was dropped: "Switch to worksp", "Open Fire"). With 1.6 s of zero padding pushed instantly after the VAD/PTT end, finalization is fast (95 to 196 ms) and complete. So it works well as "VAD or PTT closes the utterance, then flush", but it does NOT give useful live partials for 1 s commands, and sherpa's built-in blank-frame endpointing is quantized by the chunk size (about 1.0 to 1.7 s after speech end even with rule2 = 0.4 s).
3. The 2023 LibriSpeech-trained 20M zipformer is unusable for short commands (it eats the first word or two). This matches the general experience that read-speech models do badly on 1 to 3 word utterances. Do not pick a model on LibriSpeech WER.

### 2.3 NVIDIA Parakeet TDT 0.6b (v2 English, v3 multilingual), int8, via sherpa-onnx or onnx-asr

Sources: onnx-asr README (saved, `onnxasr_README.md`) and https://istupakov.github.io/onnx-asr/benchmarks/ (primary for that project); sherpa-onnx asset list; local measurement.

- Non-streaming (offline) transducer, 600M params. Weights license CC-BY-4.0, Open ASR average WER 6.05% for v2 (NVIDIA model card https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2, fetched). Int8 package 482 MB (v2) / 487 MB (v3). Outputs punctuation and capitalization.
- onnx-asr benchmark (author's numbers, AMD Ryzen 7 9800X3D): Parakeet TDT 0.6B v2 RTFx 36.8 fp32 / 30.5 int8; v3 35.4 / 31.4; Whisper base 31.4 fp32 / 61.8 int8. On a Cortex-A53, Parakeet is RTFx about 1.0 to 1.1. No laptop-class x86 numbers there (not found). onnx-asr itself is pure Python + onnxruntime, declares Python 3.10 to 3.14 support, MIT. It has NO streaming API and no hotword support (README lists greedy search only).
- LOCAL MEASUREMENT (sherpa-onnx 1.13.8 `OfflineRecognizer.from_transducer(model_type="nemo_transducer")`, i5-8350U, loadavg 10 to 13 from other lanes, second pass after warmup):

| Threads | decode time for a 0.85 to 1.56 s command (+0.4 s padding) | RTF on a 15 s clip | RSS | model load |
|---|---|---|---|---|
| 4 | median 689 ms (484 to 907) | 0.338 | 1151 MB | 7.0 s |
| 2 | median 661 ms (566 to 1165) | 0.408 | 1180 MB | 8.7 s |

  All 8 commands correct with casing and ITN ("Switch to Workspace 3.", "Open Firefox.", "Focus Kitty.").
- Verdict: accuracy class is far above the tiny models, but there is a fixed cost of about 0.5 to 0.9 s per utterance on this CPU regardless of how short the command is, plus 1.15 GB resident. Adding Jev (about 320 ms) puts the command path above 1 s after endpoint. Good as an on-demand dictation engine, wrong as the command fast path. The 110M `parakeet_tdt_transducer_110m` int8 (108 MB) is the interesting middle option (not measured here).

### 2.4 Nemotron Speech Streaming 0.6b / Nemotron 3.5 ASR streaming (NVIDIA, 2026) via sherpa-onnx

Sources: https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b (primary, NVIDIA model card), sherpa-onnx asset list, arXiv 2604.14493 (Microsoft CoreAI, April 2026).

- Cache-aware FastConformer (24 layers) + RNN-T, 600M params, punctuation and capitalization, NVIDIA Open Model License. Chunk sizes 80 / 160 / 560 / 1120 ms, average WER (model card) 8.43% / 7.67% / 7.07% / 6.93%.
- This is the "anything newer in 2026" candidate with the best streaming accuracy, and sherpa-onnx already ships int8 exports (464 to 475 MB).
- CPU cost: Microsoft's paper reports RTFx 2.46 (fp32) and 7.20 (int4) for Nemotron-0.6B with 0.56 s chunks on an AMD EPYC 7V12 pinned to 32 cores. A 4-core 15 W Kaby Lake-R has nowhere near that throughput. See local measurement below (if the download finished) for the verdict on this machine.

### 2.5 Vosk (Alpha Cephei, Kaldi based)

Sources: https://alphacephei.com/vosk/models (primary), PyPI.

- `vosk` 0.3.45 on PyPI is from 2022-12-14 (stale) but the wheel is `py3-none-manylinux` with cffi, so it installs on 3.14.
- English models: `vosk-model-small-en-us-0.15` 40 MB, WER 9.85 LibriSpeech test-clean / 10.38 TED-LIUM, Apache-2.0, supports runtime vocabulary/grammar reconfiguration; `vosk-model-en-us-0.22-lgraph` 128 MB, 7.82 / 8.20, dynamic graph (also supports grammar); `vosk-model-en-us-0.22` 1.8 GB static graph (no grammar); `vosk-model-en-us-0.42-gigaspeech` 2.3 GB.
- Unique feature: a hard grammar. `KaldiRecognizer(model, 16000, '["open firefox", "switch to workspace", "one", "two", "[unk]"]')` restricts decoding to the listed words and phrases, which makes out-of-grammar hallucination impossible and makes tiny models very accurate on a closed command set. Caveats: words must already be in the model lexicon (an app name like "obsidian" or "hyprland" that is not in the dictionary cannot be added to the small model without rebuilding the graph); no casing or punctuation; true streaming partials (`PartialResult()`), very low CPU (Kaldi TDNN-F, runs on a Raspberry Pi per the vendor page).
- Verdict: excellent as a closed-grammar command recognizer or verifier, poor for open titles and dictation. onnx-asr lists "Alpha Cephei Vosk 0.52+" zipformer models as supported, so the newer Vosk models are Zipformer transducers consumed through onnx-asr or sherpa-onnx, not the old Kaldi API. No English 0.52+ model is listed on the Vosk models page (not found).

### 2.6 whisper.cpp tiny/base and faster-whisper int8 (with VAD chunking)

- Both install on 3.14 (`pywhispercpp` 1.5.1 cp314 wheels; `faster-whisper` 1.2.1 + `ctranslate2` 4.8.2 cp314).
- Whisper is not streaming: the encoder always processes a 30 s padded window, so a 1 s command costs the same as 30 s of audio. Moonshine's vendor benchmark (faster-whisper on their unnamed Linux x86 box) gives 1,141 ms response latency for Whisper Tiny and 3,425 ms for Whisper Small, vs 69 / 165 ms for Moonshine Tiny / Small Streaming. onnx-asr's author reports Whisper base int8 at RTFx 61.8 on a Ryzen 9800X3D, which would be roughly 5x to 8x lower on this laptop. (whisper.cpp has an `audio_ctx` hack to shrink the encoder window for short clips, which makes tiny usable around 200 to 400 ms on AVX2 laptops, but it is off-spec and degrades accuracy; no primary benchmark found, flagged as my own prior knowledge.)
- No partials, no contextual biasing beyond `initial_prompt` (weak, and with tiny/base it often causes prompt leakage or hallucination on silence).
- Verdict: dominated by Moonshine and the transducer models for this use case. Only reason to keep it: multilingual dictation.

### 2.7 Kyutai STT (delayed streams modeling)

Source: https://github.com/kyutai-labs/delayed-streams-modeling (primary).

- `kyutai/stt-1b-en_fr` (about 1B params, 0.5 s delay, semantic VAD) and `kyutai/stt-2.6b-en` (2.5 s delay). Runtimes: PyTorch, Rust (candle) server, MLX. All published throughput numbers are GPU (L40S: 64 streams at 3x real time; H100: 400 streams). Code MIT/Apache-2.0, weights CC-BY 4.0.
- No CPU-only numbers exist in the primary source (not found). A 1B-parameter decoder-only model producing 12.5 Hz frames with a Mimi codec front end is out of reach for a 15 W 4-core CPU in real time. The `moshi` PyPI package needs torch; `rustymimi` has no cp314 wheel. Rejected for this machine.

### 2.8 KEY LOCAL FINDING: non-streaming Moonshine Tiny through sherpa-onnx, one thread

Model: `sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27` (30 MB tar.bz2; `encoder_model.ort` 13 MB + `decoder_model_merged.ort` 30 MB; bundled LICENSE says English models are MIT), loaded with `sherpa_onnx.OfflineRecognizer.from_moonshine_v2(encoder=..., decoder=..., tokens=..., num_threads=N)`. Whole-utterance decode after end of speech, same 8 commands, second pass after warmup, machine at loadavg 16 to 18 because of other lanes (so this is a worst-case environment).

| num_threads | decode time per command: median (min to max) ms | RSS MB | load ms | accuracy |
|---|---|---|---|---|
| 1 | 120 (87 to 214) | 182 | 1800 | 8/8 ("Switch to Workspace 3.", "Open firefox.", "Focus kitty.", "Toggle full screen.") |
| 2 | 216 (90 to 321) | 183 | 2200 | 8/8 |
| 4 | 281 (215 to 410) | 184 | 2600 | 8/8 |

Reading: on a contended machine MORE threads is SLOWER (thread wake/spin contention). With one thread, a 1 s command is transcribed in about 0.1 to 0.2 s, which is faster than what the `moonshine-voice` streaming pipeline achieved for finalization on the same machine (about 0.24 to 0.38 s) and it costs zero CPU while the user is speaking. For 1 to 6 word commands, "buffer the utterance, endpoint with a VAD, decode once on 1 thread" beats "stream partials all the time" on this hardware. The trade: no live partial text (irrelevant for a command app that acts rather than talks; a level meter or "listening" indicator is enough), and no `keyterms` biasing (the sherpa offline Moonshine path has greedy decoding only; hotwords in sherpa are transducer-only).

### 2.9 LOCAL: Parakeet TDT 110M (`sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8`, 108 MB) offline, loadavg 16 to 18

| num_threads | decode per command: median (min to max) ms | RTF on 15 s clip | RSS MB | load ms | accuracy |
|---|---|---|---|---|---|
| 1 | 162 (151 to 229) | 0.173 | 394 | 3700 | 8/8, cased + punctuated, numbers spelled ("Switch to Workspace three.", "Focus, Kitty.") |
| 2 | 500 (219 to 719) under heavy contention | 0.205 | 394 | 5200 | 8/8 |

- This is the accuracy/latency sweet spot found in this lane: a 110M FastConformer TDT (NVIDIA `parakeet-tdt_ctc-110m` family, CC-BY-4.0 per NVIDIA's card, not re-fetched) decodes a command in about 160 ms on ONE thread, with much tighter variance than the others (151 to 229 ms at loadavg 16).
- NVIDIA model card (https://huggingface.co/nvidia/parakeet-tdt_ctc-110m, primary): about 114M params, hybrid FastConformer TDT-CTC, CC-BY-4.0, punctuation and capitalization, trained on 36K hours, mean WER 7.49% (AMI 15.88, Earnings-22 12.42, GigaSpeech 10.52, LS clean 2.4, LS other 5.2, SPGI 2.54, TED-LIUM 4.16, VoxPopuli 6.91). For comparison the 0.6B v2 card says 6.05% average, CC-BY-4.0, 600M.
- It is a transducer, so sherpa-onnx `modified_beam_search` works with it: I constructed `OfflineRecognizer.from_transducer(..., model_type="nemo_transducer", decoding_method="modified_beam_search")` on 1.13.8 and it decoded correctly ("Focus kitty"). Per-stream hotwords are accepted without error (`create_stream(hotwords="▁K id d ie")`, tokens hand-split from `tokens.txt`), and at `hotwords_score=5.0` the hypothesis changed ("Focus kitty" became "Focus, kitty.") but not toward the hotword, so whether biasing is effective on NeMo transducers is INCONCLUSIVE (open question; a proper test needs the model's sentencepiece vocab and a word the model gets wrong).

## 3. Audio capture from Python on PipeWire 1.6.8 (LOCAL MEASUREMENT, script `bench_capture.py`)

Local facts: `pipewire`, `pipewire-pulse`, `pipewire-alsa`, `pipewire-jack` 1.6.8, `wireplumber` 0.5.15, system `portaudio` 19.7.0. Graph clock 48 kHz, `clock.quantum=1024` (21.3 ms), `min-quantum=32`, `max-quantum=2048`. One capture source ("Built-in Audio Analog Stereo"). `libpipewire-module-echo-cancel.so` and `/usr/lib/spa-0.2/aec/libspa-aec-webrtc.so` are present.

| Method | Settings | First data after open | Delivery cadence | Notes |
|---|---|---|---|---|
| `pw-record --raw --rate 16000 --channels 1 --format s16 --latency X -` piped to Python | X = 100 ms, 20 ms, 10 ms | 245 to 308 ms | 2048 samples every 128 ms regardless of `--latency` | the 128 ms is stdio block buffering of the pipe (4096 bytes = 2048 s16 samples), NOT PipeWire. A naive `pw-record` subprocess adds up to 128 ms of latency and jitter. |
| same, wrapped in `stdbuf -o0` | `--latency 20ms` | 87 ms | about 171 samples every 10.7 ms (max gap 22 ms) | fixes it completely. PipeWire does the 48 k to 16 k resampling for free. |
| `sounddevice.InputStream` (PortAudio, ALSA host API, routed through `pipewire-alsa`), 16 kHz mono int16 | `blocksize=320, latency="low"` | 106 ms | callback every 21.3 ms (= one PipeWire quantum), max gap 28.7 ms | PortAudio reported latency 20 ms; ADC-to-callback time 42 ms median |
| same | `blocksize=160, latency="low"` | 93 ms | every 10.7 ms, max 21.3 ms | reported 10 ms; ADC-to-callback 22 ms median |
| same | `blocksize=0, latency="high"` | 555 ms | bursty (many tiny callbacks) | avoid; always set an explicit blocksize |

Conclusions:
- `sounddevice` with `blocksize=160..320, latency="low"` is the lowest-friction choice: pure Python wheel on 3.14, system PortAudio, about 20 to 40 ms capture latency, no subprocess to supervise. `moonshine-voice` already depends on it.
- `pw-record` is a fine zero-dependency alternative ONLY with `stdbuf -o0` (or by reading 48 kHz so 4096 bytes is 43 ms). It has one real advantage: `--target <node>` lets you bind to the echo-cancelled virtual source by name, and `-P '{ node.latency = "256/48000" }'` style properties are explicit.
- A native PipeWire client from Python does not exist in maintained form (`pipewire-python` 0.2.3 from 2023 is only a `pw-cat` subprocess wrapper). GStreamer `pipewiresrc` through the already-installed `python-gobject` is the native-ish option if the UI lane is already in a GLib main loop; not measured here.
- Keep the stream open all the time (wake word, or pre-roll buffer for push to talk). Opening a stream costs 90 to 300 ms and would clip the first word. Keep a 300 to 500 ms ring buffer and prepend it when PTT/VAD fires.

### 2.10 (continues section 2) LOCAL: larger streaming models are not real time on this CPU

All at loadavg 19 to 25 (other lanes), so pessimistic, but the margins are large:

| Model | threads | Result |
|---|---|---|
| `sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-80ms-int8` (NVIDIA cache-aware FastConformer, about 114M, 80 ms chunks) | 2 | RTF 1.33 (slower than real time), RSS 279 MB, ptt final 444 to 1812 ms, errors on commands ("taggleful screen", "workspace too", truncated "focus kit") |
| `sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11` (600M) | 4 | RTF 5.45 on a 10 s clip, RSS 982 MB, load 10 s, 5 to 8 s to transcribe a 1 s command. Text was correct ("Open Firefox", "Focus Kitty"). Rejected for this machine. |

Small chunk cache-aware conformers pay the full encoder cost every 80 to 160 ms, which a 15 W 4-core part cannot sustain. The 480 ms / 1040 ms chunk variants would be cheaper but then offer no latency advantage over "VAD then offline decode".

(Housekeeping: I deleted the 0.6B models, the FastConformer 80 ms model and the 20M zipformer from the scratchpad after measuring because `/tmp` is tmpfs and the machine is under memory pressure. Kept under `scratchpad/models/`: kroko streaming zipformer, Moonshine tiny (sherpa export), Parakeet TDT 110M int8, `silero_vad.onnx`, `silero_vad_v5.onnx`, `ten-vad.onnx`.)

## 4. VAD and endpointing

Sources: Silero VAD repo (https://github.com/snakers4/silero-vad, `utils_vad.py` fetched raw), TEN VAD README (saved) and LICENSE (fetched), sherpa-onnx installed defaults, local measurement.

| VAD | Size | Frame | License | Python 3.14 route | Notes |
|---|---|---|---|---|---|
| Silero VAD v5 / v6.2 | about 2.2 MB ONNX (sherpa's `silero_vad.onnx` is the older 644 KB v4; `silero_vad_v5.onnx` is 2.3 MB) | 512 samples (32 ms) at 16 kHz | MIT | `pysilero-vad` 3.4.0 (abi3, no torch), or sherpa-onnx `VoiceActivityDetector`, or raw onnxruntime. The official `silero-vad` 6.2.2 pip package hard-depends on torch + torchaudio: avoid. | Vendor: "One audio chunk (30+ ms) takes less than 1ms to be processed on a single CPU thread". v6.0 claims 16% fewer errors on noisy real-life data vs v5. Defaults in `get_speech_timestamps`: threshold 0.5, `min_silence_duration_ms=100`, `speech_pad_ms=30`, `min_speech_duration_ms=250`, neg_threshold = threshold minus 0.15. |
| TEN VAD | 306 KB lib / 332 KB ONNX | 160 or 256 samples (10 / 16 ms) | Apache-2.0 WITH extra Agora conditions: "You may not Deploy the ten-vad in a way that competes with Agora's offerings..." and deploy "solely for your benefit and the benefit of your direct End Users". Not OSI-clean; a problem for an open source fork that others redistribute. | built into sherpa-onnx (`c.ten_vad.model`), or `ten-vad` pip (prebuilt .so, README says Python 3.8/3.10 verified, needs libc++1) | Vendor: better precision-recall than Silero and WebRTC on their test set; detects speech-to-silence transitions faster, "Silero VAD suffers from a delay of several hundred milliseconds". Vendor RTF 0.0086 to 0.016 on desktop/server x86 vs Silero 0.0127. |
| WebRTC VAD | tiny, GMM | 10/20/30 ms | BSD | `webrtcvad-wheels` has no cp314 wheel (builds from sdist) | cheapest, but poor precision with keyboard noise and fans, which is exactly the desktop environment. Not recommended except as a pre-gate. |
| microVAD (`pymicro-vad` 2.1.0) | tiny | 10 ms | Apache | abi3 wheel works on 3.14 | Home Assistant family; not evaluated further. |

sherpa-onnx `VadModelConfig` defaults (installed 1.13.8): threshold 0.5, `min_silence_duration=0.5`, `min_speech_duration=0.25`, Silero window 512, TEN window 256, `max_speech_duration=20`.

LOCAL MEASUREMENT (sherpa-onnx VAD, 1 thread, loadavg about 24, 8 synthetic commands followed by digital silence; delay is in audio time from true end of speech until the detector emits the segment):

| VAD | min_silence 0.5 | min_silence 0.3 | min_silence 0.2 | CPU cost (RTF, 1 thread, contended) |
|---|---|---|---|---|
| Silero v4 (`silero_vad.onnx`) | 504 to 594 ms | 312 to 402 ms | 216 to 306 ms | 0.036 to 0.050 |
| Silero v5 | 568 to 594 ms | 376 to 402 ms | 280 to 306 ms | 0.029 to 0.055 |
| TEN VAD | 523 to 552 ms | 315 to 344 ms | 219 to 248 ms | 0.043 to 0.128 |

So endpoint delay is approximately `min_silence` + 20 to 100 ms, and TEN VAD has the tightest overhang. With real room noise the speech-to-silence transition is less crisp than with digital silence, so expect the upper end.

What silence timeout is usable for commands:
- 100 to 200 ms: splits inside phrases (stop consonants and natural gaps in "move window ... to workspace two" reach 150 to 250 ms). Only safe as a SPECULATIVE trigger.
- 300 to 400 ms: the practical floor for committed endpointing of short commands. sherpa's own stock 1.2 s rule2 and Moonshine's 0.5 s averaging window are tuned for dictation and feel sluggish for commands.
- 600 to 800 ms: right for dictation mode so that thinking pauses do not split sentences.
- Recommended design, because decode is cheap (about 120 to 160 ms on one thread) and Jev is about 320 ms: SPECULATIVE FINALIZE. At 150 to 200 ms of silence, run the decode and fire the Jev request on the hypothesis. Commit the action only when silence reaches 350 to 400 ms; if speech resumes, discard and redo. The endpoint confirmation wait then overlaps with Jev's round trip instead of adding to it. Expected end of speech to action: about max(380 ms, 180 + 140 + 320 = 640 ms) = about 0.65 s with VAD, and about 0.5 s with push to talk release (40 ms capture + 140 ms decode + 320 ms Jev). Jev calls cost fractions of a cent per thousand, so wasted speculative calls are irrelevant; the 503 and 6.9 s outlier the main loop saw argue for a hard 1 s timeout plus a local fallback for the top few commands.

## 5. Wake word versus push to talk

- Push to talk is the right default on Hyprland. `bind`/`bindr` give press and release events (release flag `r` verified in the Hyprland 0.56.2 source, `src/config/legacy/ConfigManager.cpp:1519`), e.g. `bind = SUPER, space, exec, voicectl ptt-down` and `bindr = SUPER, space, exec, voicectl ptt-up`, or a toggle. PTT removes the endpointing delay entirely (release is the endpoint), removes false activations, removes the always-on privacy question, and costs no idle CPU. The local numbers show PTT finalization at 120 to 240 ms vs 650 to 950 ms for VAD paths in `moonshine-voice`.
- Wake word options if hands-free is wanted:
  - sherpa-onnx keyword spotting (`sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`, English, 3.3M params, Apache-2.0 runtime; newer bilingual `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20`). Open vocabulary: the wake phrase is just a token sequence in `keywords_file` with per-keyword boost and threshold, and `create_stream(keywords=...)` allows per-stream additions. No training, same runtime already in the stack, cp314 wheel exists. It can also act as a zero-Jev fast path for a handful of fixed commands ("stop", "cancel", "scroll down"). Accuracy numbers for English false accepts per hour: not found in the docs.
  - microWakeWord via `pymicro-wakeword` 2.5.0 (2026-09-17, Apache-2.0, OHF-Voice, bundled TFLite C runtime, `py3-none-manylinux_2_35_x86_64` so it installs on 3.14): 16 kHz 16-bit mono, 10 ms chunks, built-in `okay_nabu` (other models such as hey_jarvis exist in the microWakeWord project). Designed for ESP32-class MCUs, so CPU cost on a laptop is negligible. Custom phrases need the microWakeWord training notebook (synthetic TTS data).
  - openWakeWord 0.6.0: last release 2024-02-11; on Linux the package requires `tflite-runtime`, which has no wheel past cp311, so a normal install FAILS on Python 3.14 (workaround: `--no-deps` and the ONNX inference path). Code Apache-2.0 but the pretrained models are CC BY-NC-SA 4.0. Vendor targets: under 5% false reject, under 0.5 false accepts per hour; 80 ms frames; "a single Raspberry Pi 3 core can run 15-20 models". Given staleness, NC model license and the 3.14 problem, it ranks last.
- Recommendation: ship PTT (hold) plus a toggle-to-listen mode with VAD endpointing; offer wake word as opt-in using sherpa-onnx KWS since it adds no dependency.

## 6. Contextual biasing so app names and window titles are recognized

| Mechanism | Works with | Runtime update | Strength | Evidence |
|---|---|---|---|---|
| Moonshine `keyterms` / `set_keyterms()` / `set_context(text)` | Moonshine Streaming models in `moonshine-voice` only | yes, while streaming | soft logit bias, boost 1 to 4, phrases allowed, controls output casing | vendor eval: Tiny term WER 12.23% to 8.75% at default boost with 100-term lists, about 1 ms cost. Local: "kitty", "Firefox", "Obsidian" came back in the requested casing. |
| sherpa-onnx hotwords | transducers only, `modified_beam_search` | yes, per stream via `create_stream(hotwords=...)` | soft, per-phrase score | docs + installed source; needs `bpe_vocab`; no published accuracy table (not found) |
| Vosk grammar (`KaldiRecognizer(model, rate, json_list)`, `SetGrammar`) | small and lgraph Kaldi models | yes | HARD constraint, closed vocabulary with `[unk]` | vosk-api `python/example/test_words.py`; words outside the model lexicon cannot be added |
| Whisper `initial_prompt` | whisper family | per call | weak, leaks | general knowledge |
| Downstream repair by Jev | any STT | n/a | strong for this app | see below |

The important architectural point: this app does not need a perfect transcript, it needs the right ACTION. Jev gets the state (open windows, classes, titles, workspaces) and declared choice questions with up to 255 options. A transcript of "focus kiddy" or "open fire fox" is still resolved correctly by a choice question over the actual window list, and Jev's per-option probabilities give a confidence gate. So biasing in the recognizer is a nice-to-have that mainly reduces weird outputs for rare names (Obsidian, Hyprland, kitty, Zed, Ghostty). A cheap extra layer: fuzzy/phonetic match (rapidfuzz or double metaphone) of transcript n-grams against the live app/title list before building the Jev state, and pass the N-best (sherpa `modified_beam_search` can expose alternatives) into the state.

## 7. Echo cancellation

- PipeWire ships it: `libpipewire-module-echo-cancel` with `library.name = aec/libspa-aec-webrtc` (both present on this machine under `/usr/lib/pipewire-0.3/` and `/usr/lib/spa-0.2/aec/`). It creates a virtual "Echo Cancellation Source" (capture from this) and an "Echo Cancellation Sink" (playback must go through it to be used as the reference), or with `monitor.mode = true` it taps the default sink's monitor so apps do not need rerouting. The installed webrtc plugin exposes `webrtc.gain_control`, `webrtc.high_pass_filter`, `webrtc.noise_suppression`, `webrtc.transient_suppression`, `webrtc.voice_detection`, `webrtc.mobile_mode` (from `strings` on the local .so). Docs: https://docs.pipewire.org/page_module_echo_cancel.html.
- Drop-in: a file `~/.config/pipewire/pipewire.conf.d/60-echo-cancel.conf` with the `context.modules` block from the docs, `monitor.mode = true`, `aec.args = { webrtc.noise_suppression = true webrtc.gain_control = false }`. Then capture with `pw-record --target "Echo Cancellation Source"` or select that device in sounddevice.
- Is it needed? This app "acts, not talks": there is no TTS barge-in problem. Echo only matters if music or a video is playing through laptop speakers while commanding. With PTT the simplest mitigation is ducking: lower the default sink volume while the key is held (`wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.2`, restore on release). Recommendation: no AEC by default, PTT ducking on, AEC as an optional documented config. AGC should stay off (it pumps noise up between words and hurts VAD).
- `transient_suppression` is worth enabling on a laptop because key clicks are the dominant false VAD trigger.

## 8. Cloud STT (one paragraph; the gateway-stt lane owns this)

A cloud path only makes sense for long dictation or as an accuracy fallback, because any cloud STT adds a network round trip (this machine sees about 320 ms to the Vercel gateway for Jev) on top of upload, which already exceeds the 120 to 160 ms that local decode costs for a command. Candidates the other lane should price and measure: Groq `whisper-large-v3-turbo` (batch, file upload, fast per-request but no streaming partials), Deepgram Nova-3 streaming and Deepgram Flux (conversational model with built-in end-of-turn detection), and whatever transcription models the Vercel AI Gateway exposes so one key covers both Jev and STT. I did not fetch or verify latency numbers for any of them in this run (not found / out of scope after the scope change), so no figures are quoted here.

## 9. Summary comparison (this machine)

| Option | Streaming partials | Finalize after end of speech (local, contended) | CPU while speaking | RAM | Biasing | License | cp314 | Verdict |
|---|---|---|---|---|---|---|---|---|
| Moonshine Tiny (non-streaming export) via sherpa-onnx offline, 1 thread | no | 87 to 214 ms (median 120) | none | 182 MB | none | MIT | yes | fastest and smallest; low-memory pick |
| Parakeet TDT 110M int8 via sherpa-onnx offline, 1 thread | no | 151 to 229 ms (median 162) | none | 394 MB | beam search works, hotwords inconclusive | CC-BY-4.0 | yes | best accuracy per ms; default pick |
| Moonshine Tiny Streaming via `moonshine-voice` | yes | PTT 190 to 715 ms (median 234 to 384); built-in VAD 640 to 1185 ms | very high (about 100% real time, 386% CPU bursts) | 222 to 484 MB | yes, best in class (`set_keyterms`, `set_context`) | MIT | yes | good for dictation with live text; too hungry as an always-on command path |
| Moonshine Small Streaming | yes | PTT 528 to 1315 ms | over real time under load | 470 to 500 MB | yes | MIT | yes | too heavy here |
| Kroko streaming zipformer (sherpa-onnx) | yes but first partial after about 1.25 s | flush 95 to 196 ms (needs 1.6 s zero pad) | low (RTF 0.08 to 0.11) | 196 MB | hotwords possible in principle | CC-BY-SA (community models) | yes | viable fallback; chunk too large for live partials |
| zipformer-en-20M-2023 | yes | 24 to 72 ms | low | 141 MB | hotwords | Apache-2.0 | yes | REJECT: drops command onsets |
| NeMo streaming FastConformer 80 ms | yes | 444 to 1812 ms | RTF 1.33 | 279 MB | ? | CC-BY-4.0 | yes | REJECT |
| Nemotron streaming 0.6B int8 | yes | seconds | RTF 5.4 | 982 MB | ? | NVIDIA Open Model License | yes | REJECT on this CPU |
| Parakeet TDT 0.6B v2 int8 offline | no | 484 to 1165 ms (median about 675) | none | 1.15 GB | no (onnx-asr), unverified (sherpa) | CC-BY-4.0 | yes | dictation-quality fallback, on demand only |
| Vosk small-en 0.15 with grammar | yes | not measured | very low | about 40 MB model | HARD grammar | Apache-2.0 | yes (cffi) | optional closed-set verifier |
| whisper.cpp / faster-whisper tiny, base | no | about 1.1 s for tiny (Moonshine vendor bench on their x86) | none | | weak | MIT | yes | dominated |
| Kyutai STT 1B | yes | GPU only | | | | CC-BY-4.0 weights | torch needed | REJECT |

## 10. Ranked recommendation

1. PRIMARY (commands): sherpa-onnx 1.13.8 as the single speech runtime, on system Python 3.14. Capture with `sounddevice` (blocksize 160 to 320, latency low, stream always open, 400 ms pre-roll ring buffer). Endpoint with push to talk release, or Silero v5 VAD (MIT) in sherpa with `min_silence_duration` 0.35 plus a speculative decode at 0.18 s. Decode the buffered utterance once with `OfflineRecognizer` on `num_threads=1`.
   - Model 1a: Parakeet TDT 110M int8 (`sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8`): about 160 ms per command, 394 MB RSS, CC-BY-4.0, NVIDIA card mean WER 7.49% across the 8 Open ASR sets (AMI 15.88, GigaSpeech 10.52, LibriSpeech clean 2.4), trained on 36K hours, punctuation and capitalization. Tightest latency variance measured here.
   - Model 1b (low memory): Moonshine Tiny non-streaming export (`sherpa-onnx-moonshine-tiny-en-quantized-2026-02-27`): about 120 ms, 182 MB, MIT, vendor leaderboard average 12.66%, does digit ITN ("workspace 3").
   - The 40 ms that 1a costs over 1b buys roughly 40% fewer word errors on public benchmarks, so 1a is the default and 1b the memory-pressure setting. Both got 8/8 on the clean synthetic set, so A/B them on a real-microphone command set before freezing.
   - Feed transcript + N-best + fuzzy-matched app/title candidates into the Jev state and let the choice question do entity resolution. Expected speech-end to action: about 0.5 s with PTT, about 0.65 s with VAD, given Jev at 320 ms.
2. DICTATION MODE: `moonshine-voice` Tiny Streaming (live partials, `set_context` with the focused window's text, MIT) started only while dictation is active, because it costs roughly a full core-equivalent or more on this CPU. If quality matters more than liveness, run Parakeet TDT 0.6B v2 int8 per VAD segment instead (about 0.35 RTF, 1.15 GB while loaded; load lazily and unload after idle).
3. FALLBACK if the offline-decode design disappoints on real audio: Kroko streaming zipformer in sherpa-onnx with external VAD and a 1.6 s zero-pad flush (about 100 to 200 ms finalize, 196 MB, RTF 0.1), license CC-BY-SA to be checked; or Vosk small with a generated grammar for a closed command set.
4. Do not use on this machine: any 0.6B streaming model, NeMo 80 ms cache-aware FastConformer, Kyutai, Whisper family for commands, openWakeWord (3.14 install + NC models), TEN VAD if license cleanliness matters.
5. If a managed Python is ever needed (it is not, today), `uv python pin 3.13` would only buy `webrtcvad-wheels`, `rustymimi` and `tflite-runtime`-era packages, none of which are in the recommended stack.

All latency numbers above were taken while other research lanes kept the machine at loadavg 6 to 25. They should be re-run on an idle machine with a real microphone recording set before being quoted anywhere; expect them to improve, and expect multi-thread settings to look better than they did here.

## Verification

Independent checks by the verifier lane, 2026-09-21. Machine loadavg was about 27 during reruns (heavier than the researcher's 16 to 18).

1. Python 3.14 wheel availability: CONFIRMED. Re-queried PyPI JSON for 12 packages. sherpa-onnx 1.13.8 cp314 manylinux2014 wheel exists; moonshine-voice 0.1.5 is py3-none-manylinux_2_34_x86_64 (note glibc 2.34 floor); vosk 0.3.45 is py3-none; onnxruntime 1.30.0, ctranslate2 4.8.2, pywhispercpp 1.5.1 have cp314; webrtcvad-wheels 2.0.14, rustymimi 0.4.1, tflite-runtime 2.14.0 have no cp314 linux x86_64 wheel; openwakeword 0.6.0 requires tflite-runtime on Linux.
2. Moonshine Tiny non-streaming latency: PARTIALLY CORRECT. Stored JSON matches the claim (median 119.5 ms, 87 to 214, RSS 182 MB). Rerun at loadavg 27.8, 1 thread: median 166 ms (123 to 372), RSS 183 MB, RTF 0.169. Numbers are load-dependent; treat 120 ms as a contended-but-lighter figure, not a constant. "8 of 8 correct" holds only after normalization: output is "Switch to Workspace 3.", "Toggle full screen.", digits and casing differ from the prompts, and the downstream state string must normalize.
3. Parakeet TDT 110M int8 latency: PARTIALLY CORRECT. Stored JSON matches (median 161.5 ms, 151 to 229, RSS 394 MB, RTF 0.173). Rerun at loadavg 27.4, 1 thread: median 511.5 ms (373 to 845), RTF 0.302, RSS 394 MB. It degrades far more under contention than Moonshine Tiny (3.2x vs 1.4x). Output included "Focus, Kitty." (spurious comma). NVIDIA model card numbers were not re-checked.
4. sherpa-onnx endpoint defaults and hotwords API: CONFIRMED. online_recognizer.py lines 112 to 115 show enable_endpoint_detection=False, rule1 2.4, rule2 1.2, rule3 20.0; line 345 raises unless decoding_method is modified_beam_search when hotwords_file is set; create_stream(hotwords=...) at line 1034; keyword_spotter.py create_stream(keywords=...) at line 197. Minor: the cited range 113-120 is off by one for the first default.
5. Hyprland 0.56.2 release binds: CONFIRMED with a caveat. legacy/ConfigManager.cpp:1519 is exactly `case 'r': release = true; break;`. The running compositor is v0.56.2. 0.56.2 also ships a Lua config path (src/config/lua/bindings/LuaBindingsToplevel.cpp:190, `release` bool option), so the syntax depends on which config manager the user runs; this machine uses legacy .conf files. No release binds are currently configured (hyprctl binds -j: 131 binds, 0 release), so bindr behaviour was not exercised live. "Zero-delay endpoint" is an inference, not measured.
6. TEN VAD license and Silero packaging: CONFIRMED. LICENSE line 10 to 11 reads "You may not Deploy the ten-vad in a way that competes with Agora's offerings". silero-vad 6.2.2 requires_dist has torch>=1.12.0 and torchaudio<2.10 unconditionally. pysilero-vad 3.4.0 ships cp39-abi3 manylinux_2_28 wheels.
7. PipeWire echo-cancel (bonus): CONFIRMED. /usr/lib/pipewire-0.3/libpipewire-module-echo-cancel.so and /usr/lib/spa-0.2/aec/libspa-aec-webrtc.so exist; strings show all six webrtc.* properties. monitor.mode was not found in the AEC plugin strings (it belongs to the module; not re-checked).
