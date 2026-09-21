# Prior art in voice control of desktops: UX lessons for a Jev-driven Hyprland voice app

Lane: prior-art. Retrieved 2026-09-20/21. Author: research subagent.
Scope: survey of voice control systems, what users actually say, how systems disambiguate, recover from errors, confirm risky actions, give non-spoken feedback, plus HCI latency thresholds and activation trade-offs. Ends with prioritized UX principles.

Source labels used below:
- [primary] = vendor or project's own docs, repo files, or peer-reviewed paper.
- [secondary] = reputable third-party write-up.
- [self-reported] = a number a third-party developer printed in their own README. Not verified by us.
- [inference] = my reasoning from the evidence.

No live Jev calls were made. No Jev latency was measured by us. Every Jev latency number in this file is a third-party self-reported figure and is labeled as such.

---

## 0. Executive findings

1. The field has converged on a very small, stable window-management vocabulary: "open X", "switch to X" / "focus X", "close window", "minimize / maximize window", "snap (window) to left", "show numbers", "show grid", "go to sleep" / "wake up", "undo that", "scratch that". These same verbs appear in Apple Voice Control, Windows Voice Access, Dragon, Talon community, Serenade, and in the 2026 Hyprland projects. Copy them. Do not invent vocabulary.
2. Every mature system solves ambiguity the same way: draw numbers next to the candidates, then the user says a number. Apple, Microsoft, Google, Dragon, and Serenade all do this. No mature system asks a spoken clarifying question as the primary path. This maps exactly onto a Jev `choice` with per-option probabilities plus hypruse's existing `marks` tool.
3. Serenade's pattern is the best fit for a probabilistic decision engine: act on the top guess immediately, show the ranked alternatives, and let the user say a number to swap, which automatically undoes the first action.
4. Six days after Jev's launch there are already at least 10 public repos pairing Jev with voice or UI control, including one targeting Omarchy (Hyprland): devfros/omause. They have independently converged on a common architecture: closed-set choice questions, "select instead of generate" for free text (code extracts candidate spans, Jev picks one), gate booleans `is_command`, `complete`, `destructive`, thresholds in code, numbered overlay when target confidence is low, spoken "confirm" for destructive actions.
5. Endpointing silence, not model latency, dominates perceived latency in every pipeline I looked at (Talon 300 ms default, one Jev Mac app 550 ms, another 900 ms fallback). Microsoft shipped a user-facing "Wait time before acting" setting for this reason. The response-time literature (Miller 1968, Card et al. 1991 via Nielsen; Doherty and Thadani 1982; Stivers et al. 2009) says: acknowledge within 100 ms, finish within about 400 ms to 1 s of end of speech.
6. Talon, the power-user gold standard, has no Wayland support and (per secondary reporting from May 2026) is dropping public Linux support entirely. numen is X11-only per its own site. There is a real gap for a hands-free command system on Hyprland; the existing Hyprland voice tools are almost all dictation-only.
7. Always-listening is dangerous without a sleep mode and strong gating. Measured smart speaker misactivation is about 0.95 per hour of background TV (Dubois et al. 2020). Talon users report "all hell breaks loose" when they forget sleep mode. Default to push-to-talk, offer hands-free as an opt-in with a sleep/wake grammar copied from Talon.

---

## 1. System-by-system survey

### 1.1 Talon Voice (+ community command set, Cursorless, Rango)

What it is: a closed-source hands-free input engine (voice, noises, eye tracking) driven by user-editable `.talon` grammar files and Python. The community command set `talonhub/community` (875 stars, pushed 2026-09-20) is the de facto vocabulary. [primary: https://github.com/talonhub/community]

Grammar design:
- Rules are small context-free patterns: `[optional]`, `a | b`, `{list}`, `<capture>`, anchors `^` `$`. [primary: https://talon.wiki/Customization/talon-files/]
- Continuous command recognition: unanchored rules can be chained in one utterance. The wiki says: "In general you shouldn't anchor rules since it prevents the user from chaining them together." Anchors are reserved for dangerous or mode-changing commands so that they only fire as a standalone utterance. [primary: same URL]
- Context headers scope commands by `app`, `title`, `os`, `mode`, `tag`. Same-type requirements are OR-ed, different types AND-ed. This keeps the active vocabulary small per context, which raises accuracy. [primary: same URL]

Window management vocabulary (verbatim from `core/windows_and_tabs/window_management.talon`): [primary: https://github.com/talonhub/community/blob/main/core/windows_and_tabs/window_management.talon]
- `window (new | open)`, `window next`, `window last`, `window close`, `window hide`
- `focus <running_application>`, `focus$` (opens a switcher menu), `focus last`
- `running list` / `running close` (shows list of running apps as a help overlay)
- `launch <application>`
- `snap <position>` where positions are left, right, top, bottom, thirds, quarters, sixths, plus simpler spoken aliases ("left small", "right large")
- `snap next [screen]`, `snap last [screen]`, `snap screen <number>`
- `snap <running_application> <position>` and `snap <app> [screen] <number>`: app plus target in one phrase, no prior focus step.

Sleep / wake (verbatim from `core/modes/*.talon`): [primary: https://github.com/talonhub/community/tree/main/core/modes]
- `^go to sleep [<phrase>]$` disables speech. The optional trailing `<phrase>` is deliberate: "you often need to put Talon asleep in order to immediately talk to humans ... With this, you can say 'sleep all hey bob' and Talon will immediately go to sleep and ignore 'hey bob'."
- `^(wake up)+$` re-enables. The repeater `+` exists because frustrated users say "wake up wake up" without pausing; fully anchored so that background chatter does not wake it.
- `^sleep all [<phrase>]$` also hides every overlay (running list, history, homophones, help) and sleeps the mouse.
- Deep sleep tag: "requiring a longer wakeup command ... helping prevent unintended wakeups from conversations, meetings, listening to videos". Wake phrase becomes `^wake up and listen$`. It can be auto-enabled per app (for example meeting apps).
- In 2025 the community deprecated `talon sleep` / `talon wake` in favor of the better-known Dragon phrases `go to sleep` / `wake up`.
- Optional `user.listening_timeout_minutes`: auto-sleep after N minutes with no commands. [primary: settings.talon in the same repo]

Error handling and feedback:
- On-screen subtitles for every recognized phrase since 0.2.0 (Jul 2021). [primary: https://talonvoice.com/dl/latest/changelog.html]
- `command history` toggles a persistent on-screen list of recent commands (default size 50); `command history more/less/clear`. [primary: plugin/command_history/command_history.talon]
- `help active`, `help alphabet`, `help context`: on-screen cheat sheets of what can be said right now. [primary: https://talon.wiki/Basic%20Usage/basic_usage/]
- `undo that` / `redo that` map to app undo; `scratch that` / `nope that` deletes the last dictated phrase. [primary: core/edit/edit.talon, core/modes/dictation_mode.talon]
- Repeater grammar: `repeat that`, `twice`, `<ordinal>`, `<n> times`, `again`. Cheap to say, big speedup for window cycling. [primary: plugin/repeater/repeater.talon]
- Advice for misrecognized commands is to change the word, not retrain the user: "If only a few specific commands give you trouble, change those command words!" with examples like replacing "close" with "wipe". [primary: https://talon.wiki/Resource%20Hub/Speech%20Recognition/improving_recognition_accuracy/]

Latency:
- `speech.timeout` is "the amount of time after you stop speaking until Talon starts processing the spoken audio. Default is 0.3s." [primary: settings.talon] The default was raised from 150 ms to 300 ms in 0.1.5 (Mar 2021). [primary: changelog]
- A first-month user reports the 0.3 s timeout is too short for dictation ("the slightest of pauses exits dictation mode"). [secondary: https://www.fileside.app/blog/2025-04-14_voice-computing/] Lesson: endpoint timeout must differ by mode (short for commands, longer for dictation) and must be user-tunable.

Cursorless: every command is "an action performed on a target"; targets are addressed by a colored "hat" drawn over one letter of each token, for example "chuck bat". [primary: https://www.cursorless.org/docs/] Lesson: persistent, always-visible addressable labels remove the need for a separate "show numbers" step. The same user found the color hats visually fatiguing and moved to desaturated colors, then shapes. [secondary: fileside] Rango does the same for browser links (letter hints always on). [primary: https://github.com/david-tejada/rango]

Platform status: Talon's last stable release in the public changelog is 0.4.0 (Jul 2023); the changelog itself does not mention dropping Linux. [primary: changelog] OSnews (2026-05-31) reports that Talon's developer announced removal of all Linux support from public releases and no Wayland plans, quoting: "Talon requires deep integration with the window manager and compositor ... and Wayland offers... Absolutely no way to perform any of those actions." KDE developers dispute the claim. [secondary: https://www.osnews.com/story/145162/] I could not reach the original post (HTTP 403), so treat as medium confidence.

Copy: chaining by default, anchoring only for risky commands; sleep grammar with trailing-phrase swallow and deep sleep; subtitles plus command history; context-scoped vocabulary; repeaters; `snap <app> <position>` compound target form.
Avoid: a vocabulary that must be memorized before anything works (HN commenter on numen: "we have to memorize new constants that a programmer defines"); always-on color overlays.

### 1.2 numen

"Voice control for handsfree computing, letting you type efficiently by saying syllables and literal words. It works system-wide on Linux and the speech recognition runs locally." Built on Vosk. The site states "Linux (X11 only)". AGPL. [primary: https://numenvoice.org/ ; secondary for Vosk: https://news.ycombinator.com/item?id=34816000]
Author's positioning versus Talon: "I was inspired by talon but all I really wanted was some phrases that worked everywhere and to use normal tools like vim and a tiling window manager ... talon's more about app specific phrases and trying to take the role of your text editor and window manager." [primary quote via HN thread]
Lesson: for tiling WM users, a thin layer that speaks keybinds is already valuable. hypruse already has `binds` and `use_bind`, so "say the description of a keybind" is a cheap, high-coverage feature. I could not read numen's README or phrases files directly (sourcehut served an anti-bot challenge), so details of its chaining grammar are not confirmed here.

### 1.3 Linux dictation tools (hyprwhspr, voxtype, Handy, nerd-dictation, Speech Note, hyprvoice)

All of these are dictation first, not command and control. What matters for us is activation and feedback conventions that Hyprland users already know.

| Tool | Activation | Feedback | Notes | Source |
|---|---|---|---|---|
| hyprwhspr (goodroot, 1.2k stars, pushed 2026-09-17) | toggle, push-to-talk, auto/VAD, long-form | "audio feedback start/stop sounds", themed visualizer, Waybar module | backends: pywhispercpp, Parakeet TDT V3, onnx-asr ("wild CPU speeds"), REST; word overrides; wtype paste | [primary: https://github.com/goodroot/hyprwhspr] |
| voxtype (1.5k stars) | hold a key, release to transcribe. On Hyprland: `bind = SUPER, V, exec, voxtype record start` plus `bindr = ... record stop` | optional start/stop sounds, Waybar status, notifications | has a `post_process.command` hook that pipes the transcript through an external program. omause uses exactly this hook to add commands to a dictation key | [primary: https://github.com/peteonrails/voxtype] |
| Handy (32k stars) | hold-to-record, tap-to-toggle, or either | on-screen overlay plus optional sounds; on Linux the overlay can be disabled "to prevent window focus issues" | Whisper or Parakeet V3 (CPU-optimized), Silero VAD | [primary: https://github.com/cjpais/Handy] |
| nerd-dictation (1.9k stars) | no daemon: `begin` / `end` / `cancel` / `suspend` / `resume` CLI verbs bound to keys | none built in | Vosk; user Python hook can "implement your own actions using keywords of your choice" | [primary: https://github.com/ideasman42/nerd-dictation] |
| Speech Note (1.7k stars) | CLI actions `start-listening`, `start-listening-active-window`, `cancel` | app window | insertion on Wayland needs ydotool | [primary: https://github.com/mkiol/dsnote] |
| hyprvoice (273 stars, archived 2026) | toggle | notifications | archived | [primary: GitHub API metadata] |

Lessons:
- The Hyprland-native idiom for push-to-talk is a compositor `bind` + `bindr` pair calling a CLI verb on a running daemon. Do that; do not grab evdev.
- Every popular tool ships start/stop earcons and a Waybar state module. Users expect both.
- Overlays on Wayland must never take focus (Handy had to add a switch to disable its overlay on Linux for this reason; oc-voice uses a `nofocus` window rule). With gtk4-layer-shell this means keyboard interactivity NONE on the layer surface.
- A `cancel` verb (abort the current recording without acting) is standard and cheap.

### 1.4 Serenade

Voice coding assistant, open-sourced (serenadeai/serenade, 415 stars, last push 2024-06-11, not archived; effectively dormant). [primary: GitHub API metadata]
Patterns worth copying: [primary: https://serenade.ai/docs/]
- Alternatives list: "Sometimes, Serenade isn't sure what you said, so you'll see a few different options. The first one will be used automatically, but to use a different option (and undo the first one), just say the number you want to use instead."
- Chaining without pauses: "you can say `save focus terminal` to save your current file and then focus your terminal".
- System verbs: `focus chrome`, `launch atom`, `close terminal`. `pause` to stop listening. `undo`.
- Numbered overlays for clicking links in Chrome.

### 1.5 Apple Voice Control (macOS, iOS)

[primary: https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac ; https://support.apple.com/en-euro/guide/accessibility-mac/item-number-grid-overlays-voice-control-mchl26854b08/11.0/mac/11.0 ; settings: https://support.apple.com/en-is/guide/mac-help/change-voice-control-settings-spc002/26/mac/26]
- App and window verbs: "Open Mail", say an app name or "Switch to", "Quit <app>", "Hide <app>", "Close window".
- Overlays: "Show numbers" (then "Click 36"), "Show names", "Show grid", "Show window grid" (active window only), each with a "continuously" variant, and "Hide numbers / names / grid".
- Grid drill-down: "Say a grid number to drill down in that area ... When an area can't be drilled into further, saying a number performs the Click command." Commands can take a grid number as an argument: "Click 15", "Zoom in 11".
- Automatic disambiguation: when multiple items match, "numbers are shown next to each item on the screen. You can then use the item's name or number to interact with it." Numbers are also always shown for menu items.
- Three modes: Dictation (default; non-command words are typed), Spelling, and Command mode where "Words and characters that aren't commands are ignored".
- "Stop listening" / "Start listening" (iOS also "Go to sleep" / "Wake up").
- Feedback settings: "Show hints: Display suggested commands and hints on the screen"; "Play sound when command is recognized"; "Fade overlay after inactivity: ... dim the overlay after the specified period of inactivity"; "Show commands" lists everything sayable; custom Vocabulary.

Copy: window-scoped overlay variant; auto-numbers only on ambiguity; overlay dimming after inactivity; one opt-in recognition earcon; near-miss hints; command-only mode as our default since we are not a dictation app.

### 1.6 Windows Voice Access (Windows 11)

[primary: https://support.microsoft.com/en-us/accessibility/windows/voice-access/voice-access-command-list ; https://support.microsoft.com/en-us/accessibility/windows/voice-access/use-voice-to-interact-with-items-on-the-screen ; https://support.microsoft.com/en-us/accessibility/windows/voice-access/history-of-voice-access-updates]
- Listening control: "Voice access wake up" / "Unmute"; "Voice access sleep" / "Mute"; "Turn off microphone".
- Apps and windows: "Open <app>" / "Start <app>"; "Close <app>" / "Exit" / "Quit"; "Switch to <app>" / "Go to <app>"; "Minimize window" / "Minimize <app>"; "Maximize window"; "Restore window"; "Show task switcher" / "List all windows" / "Show all windows"; "Snap window to <direction>".
- Overlays: "Show numbers", "Show numbers here", "Show numbers on <app>", "Show numbers everywhere" (multi-monitor); "Show grid".
- Disambiguation: when a command matches multiple items Voice Access "attaches a number to each of them and asks you to pick which one you want to interact with." Overlays "get automatically dismissed when you select a numbered item".
- "Undo that", "Correct that".
- 2025 updates: natural phrasing on Copilot+ PCs: "Voice access now understands multiple variations of an existing command. You can say: 'Can you open Edge application', 'Switch to Microsoft Edge', 'Please open the Edge browser'". And: "We are introducing a new 'Wait time before acting' setting in voice access, allowing users to configure the delay before a voice command is executed."

Lessons: Microsoft moved from rigid grammar to paraphrase tolerance using on-device models. This is exactly the role Jev plays for us: paraphrase-tolerant mapping onto a closed action set. Also, synonyms are everywhere in their table (open/start, close/exit/quit, switch to/go to), so our option descriptions should list synonyms. The endpoint delay is a user setting because speech patterns vary (accessibility).

### 1.7 Android Voice Access

[primary: https://support.google.com/accessibility/android/answer/6151848 ; https://support.google.com/accessibility/android/answer/6151854]
- Activation: "Hey Google, start Voice Access", floating button, notification, lock screen options. Optional "Time out after no speech" of thirty seconds.
- "Show numbers" ("Numbers stay visible until you say 'Hide numbers'"), "Show labels" ("Labels hide automatically when any content changes"), "Show grid" with "More squares" / "Fewer squares".
- Disambiguation: "If you say a command that could affect more than one item on the screen, Voice Access asks 'Which one?' You can then say the number of the item."
- Discoverability by query: "What is [number]?" and "Show commands for [label or number]".
- "Stop listening", "Cancel". Navigation: "Open <app>", "Go back", "Go home", "Show recent apps".

Copy: overlays auto-dismiss when screen content changes (stale numbers are dangerous); "cancel" as a universal escape word.

### 1.8 Dragon (NaturallySpeaking / Professional v16)

[primary: https://dragon.nuance.com/shared/data-sheets/ct-dragon-professional-v16-and-legal-v16-command-cheat-sheet-en-us.pdf]
- Principle printed on the cheat sheet: "Pause before and after commands but not within them." This is the classic way to separate commands from dictation; we do not need it if we have a command-only mode plus an `is_command` gate.
- Mic: "Go to sleep | Stop listening", "Wake up".
- Windows: "Switch to <window name>", "Minimize window", "Show Desktop", "Restore windows", "List all windows", "List windows for <program>".
- Correction: "Scratch that <n> times", "Undo that", "Correct that", "Correct <xyz>".
- Discoverability: "What can I say", "Give me help".
- Clicking by name: "click <link name>"; "If more than one match: choose <n> or hide numbers or cancel".
- "MouseGrid", "MouseGrid window", "MouseGrid <1 to 9> <1 to 9>" (grid coordinates can be chained in one breath).

Dragon is the origin of most of this vocabulary ("go to sleep", "wake up", "scratch that", "switch to", "list all windows", "what can I say"). The Talon community explicitly adopted the Dragon phrases because users already know them.

### 1.9 Hyprland and Wayland voice control projects, 2025 to 2026

| Project | What | Activation | Decision engine | Notable UX | Source |
|---|---|---|---|---|---|
| wombatoperator/omarchy-voice (184 stars, created 2026-08-31) | Voice control plus durable tasks for Omarchy with Lua Hyprland | Super+Shift+V or bar widget toggles listening; "OMA starts muted" | OpenAI Realtime/Live (cloud audio) + local tools behind a "policy gate" | Bar indicator shows listening, working, and confirmation states; "Clicking it while a confirmation is pending confirms the held action"; CLI `listen confirm` / `listen cancel`; `--dry-run`; `barge_in = false` by default to avoid echo-triggered commands; ships a PipeWire echo-cancel config | [primary: https://github.com/wombatoperator/omarchy-voice] |
| nklaveren/oc-voice (1 star, 104 commits) | Dictation plus window control for Hyprland, fully local, Rust | SUPER+R or overlay buttons; no wake word | Keyword matcher, Jaro-Winkler threshold 0.82 with word-count gating | Floating overlay with live partial transcript every ~800 ms; `nofocus` window rule; send words ("envia", "pronto"), discard words ("cancela"); window verbs: "workspace 3", "next window", "fullscreen", "floating", "close"; destructive actions need spoken "confirma"; "Ambiguous window targets are reported with candidate counts rather than guessing"; CPU Whisper large-v3-turbo took ~51 s for 3 s audio on an i7-12700H [self-reported], a warning for our i5-8350U | [primary: https://github.com/nklaveren/oc-voice] |
| Fiercejojo88862/omarchy-siri (1 star) | Offline PTT assistant for Hyprland 0.56 | INS toggles talk/send | whisper-cli base.en + Ollama qwen2.5:3b to JSON + `hyprctl dispatch` | Popup plus spoken confirmation; "shutdown/reboot/logout ask for a spoken yes first"; router "refuses shell/file-destructive output" | [primary: https://github.com/Fiercejojo88862/omarchy-siri] |
| devfros/omause (0 stars, created 2026-09-20) | "Silent Omarchy voice control powered by TypeSafe Jev: voice command in, visible Omarchy action out." | Rides voxtype's F9 hold-to-talk through `post_process.command`; trigger phrase "hey omarchy" separates commands from dictation | Jev bounded planner + deterministic local fast path | See section 1.10 | [primary: https://github.com/devfros/omause] |
| Many `omarchy-voice*` repos with 0 to 1 stars | mostly dictation or chatty assistants | PTT | LLMs | not reviewed in depth | [primary: gh search, 2026-09-21] |

Takeaways: Hyprland users already have at least two voice control options, but they are either cloud-audio LLM agents (omarchy-voice) or tiny hobby repos. None has numbered overlays. None uses the accessibility tree. None is hands-free (all are key-activated). hypruse's `marks`, `click_ui`, `binds`, `use_bind`, `sequence`, and `wait_for` are differentiators nobody else has.

### 1.10 Projects already pairing Jev with voice or UI control

This is the most load-bearing prior art. All retrieved 2026-09-21. All latency numbers are [self-reported] by those authors on their hardware and networks. We have not reproduced any of them.

**moritzkremb/jev-voice-browser** (180 stars, MIT, created 2026-09-17). [primary: https://github.com/moritzkremb/jev-voice-browser]
- Streams partial transcripts; "on every partial transcript the server asks Jev ... one request with a dozen typed questions".
- Questions: `intent` (choice, each option documented with `{what, not_for, examples}`), `target` (choice over on-page element ids + `none`), `site` (choice), `complete` (boolean: "has the user finished the command?"), `is_command` (boolean: "is the user addressing the browser at all?"), `destructive` (boolean), `scroll_amount` (3-level score), `text_span` / `url_span` (choice over regex-extracted candidate spans), `tab_direction`.
- "Jev never generates text. Search queries, typed text and URLs are extracted as candidate spans by code and Jev only picks one, which is copied verbatim."
- Policy in code: `is_command >= 0.5` else ignore; `intent.confidence >= 0.55` else wait; `complete >= 0.6`, or 900 ms silence, or recognizer final, else wait; free-text intents additionally wait for final or 600 ms silence "so a query is never truncated"; click targets need `target.confidence >= 0.45` and top probability >= 0.35, "else the top 2 to 3 candidates get numbered overlays in the page and a spoken number picks one (no model call)"; `destructive >= 0.5` on a click requires spoken "confirm" / "cancel".
- In-flight management: 200 ms debounce, max 2 requests in flight, older ones aborted. "A response for a partial transcript may still act if the words already commit to a closed-set action ('go back'), but is never treated as final for free text."
- "One action per utterance; extra words after an executed command are treated as a new command only if there are at least two of them." Two commands in one breath are supported.
- Self-reported: avg about 330 ms per Jev call, first request about 700 ms (TLS), last word to decision about 300 ms including debounce, about $0.0002 per call at 3k to 6k input tokens. Thresholds "are calibrated on jev-1.13.0; re-check if you move the model alias."
- Honest caveat in the README: destructive confirm is "a convenience, not a guarantee".

**kevinbadi/jev-voice** (43 stars, MIT, macOS). [primary: https://github.com/kevinbadi/jev-voice]
- Pipeline: mic, energy VAD, whisper.cpp base.en, one Jev request with about 15 "speculative questions evaluated in parallel", macOS actions. "Code reads only the answers the chosen action needs. Plan confidence is the minimum over the judgements used. Below ACTION_MIN_CONFIDENCE (0.35) it says 'not sure' instead of acting."
- Activation matrix in one app: wake word (default "Alfred" / "Jarvis", detected by local whisper on every utterance), hold Caps Lock, tap Caps Lock to latch, `--always-on`. After a command there is an 8 second follow-up window during which no wake word is needed: "Alfred, open chrome ... go to youtube ... scroll down". Saying only the wake word chimes and arms the next utterance.
- Feedback: "floating transcription pill" at top center that "never takes keyboard focus", colors: "gray idle, red listening, yellow heard, blue thinking, green done, orange error". Default `FEEDBACK=ding`: chime on success, low buzz on failure. Hold key earcons: "Tink = recording, Pop = sent".
- Self-reported stage latencies on a Mac mini M4: end-of-speech detection 550 ms of silence, whisper base.en 80 to 130 ms, Jev 170 to 420 ms, execute 50 to 100 ms. Note the silence timer is the largest single stage.
- Compound: a `compound` boolean flags multi-step utterances, "code splits it, each step runs in order".

**chris-wozniczek/jev-voice-control** (Swift menu-bar app). [primary: https://github.com/chris-wozniczek/jev-voice-control]
- ClauseSplitter splits multi-verb utterances; one Jev call per clause with state `{clause, full_transcript, frontmost_app, installed_apps}` and questions `action`, `target_app` (up to 254 app names + none), `system_action`, `mentions_url`, `refers_to_frontmost` ("quit it" resolves to the frontmost app), `composes`, `destructive`.
- "Most commands execute locally without a network call." Deterministic parse first, Jev second.
- "low-confidence or ambiguous commands ask for confirmation before anything executes"; Post/Send/Publish controls always confirm; optional global "Ask before running commands".
- End words to close an utterance: "do it", "execute", "send it", "over", "that's it". "`go` is kept as navigation language rather than ending a command."
- Learned shortcuts: after a successful UI task it remembers which control worked. Extra vocabulary list is fed to the STT.
- UI tasks: observe accessibility tree, Jev picks next control, act, verify (reads the field back after typing).

**devfros/omause** (Omarchy, TypeScript, created 2026-09-20). [primary: https://github.com/devfros/omause]
- "Omause is not a conversational assistant. It is an input layer: voice command in, visible Omarchy action out."
- Affordance catalog built live: Hyprland windows, workspaces, `omarchy commands --json`, menu entries, `.desktop` apps, PATH executables, keybinding combos as aliases, user aliases. Candidates are ranked and budgeted per kind before being offered to Jev; "risky affordances are hidden from Jev" and "the executor fails closed if one is ever selected".
- "Resolve obvious single actions locally, without an API call. For ambiguous or compound requests, ask Jev for one bounded ordered plan in a single request." `max_steps = 8`.
- Verification after each step: diffs Hyprland clients before/after, probes toggle state, polls up to 2 s for launches, "stop immediately on execution or verification failure".
- Handoff concept: interactive pickers are opened and then the loop stops so the human finishes.
- Misheard names: Whisper `initial_prompt` biased with app names discovered on the machine; fuzzy ranker ("yazy", "neo-vim" still surface the right candidates); `omause teach "niolvim" path-exec:nvim` and `omause teach --last ...` persist corrections.
- Risk tiers: risky (hidden), caution (close window, disable connectivity: "require an explicit `confirm` prefix"), safe. "System lock is safe".
- Dry-run by default. Bar widget states: idle, recording, planning, executing, handoff, error; panel shows last transcript, chosen affordance, warnings, five recent history entries.
- Deferred by the author: "confirmation UI for risky affordances".
- Weakness: it is a Node CLI spawned per utterance through voxtype's post-process hook, so it cannot stream partials and it needs a spoken trigger phrase on every command.

**jonatasperaza/jev-voice-windows**: faster-whisper + Jev + PowerShell; PTT Ctrl+Space with RMS endpointing; always-on-top status overlay (loading, listening, thinking, executing). Its README flags the key Jev limitation: "`type_text` and `keystroke` exist ... but Jev only returns categorical answers ... There's currently no reliable way to get 'what text to type.'" The other projects solve this with candidate span selection. Also notes timeout must grow with installed-app count (state size). [primary: https://github.com/jonatasperaza/jev-voice-windows]

**gaborishka/jev-canvas**: voice plus finger pointing on a tldraw canvas. Same gate trio (`is_command >= 0.5`, `action >= 0.55`, `complete >= 0.6`, 900 ms pause fallback), 200 ms debounce, at most 2 in flight, 429 retried twice. It compensates for STT lag by reading the pointer position 300 ms in the past "because a word reaches the transcript after it was said". Undo vocabulary: "undo", "go back", "redo", "bring it back". Self-reported 300 to 550 ms per decision. [primary: https://github.com/gaborishka/jev-canvas]

**Yappy** (macOS agent): reads the front window's accessibility tree "as an indexed table of controls (role, label, value, position). Text, not pixels", asks Jev one operation choice and one target choice per step, executes only validated answers, and escalates to a full LLM when "confidence under 0.25, three actions that changed nothing, or a field with no known value". Self-reported 275 to 690 ms per decision on one run. [vendor page: https://yappy.biz/jev/]

Convergent design pattern across these independent projects [inference from the above]:
1. Closed-set `choice` for intent, with option descriptions containing positive and negative examples.
2. Targets are a `choice` over live, enumerated entities (elements, apps, windows) plus `none`.
3. Free text is never generated: code extracts spans, Jev selects.
4. Gate booleans: `is_command`, `complete`, `destructive` (and `compound`).
5. All thresholds live in code and are tuned per model version.
6. Low target confidence leads to numbered overlay, resolved locally with no model call.
7. Deterministic fast path before any network call.
8. A non-focus-stealing status pill or bar widget with 5 to 6 color states.

---

## 2. Cross-cutting analysis

### 2.1 What users actually say for window management

Collected verbatim from the primary sources in section 1.

| Intent | Apple VC | Windows Voice Access | Dragon | Talon community | Serenade | 2026 Hyprland / Jev projects |
|---|---|---|---|---|---|---|
| Launch app | "Open Mail" | "Open <app>", "Start <app>" | "Open <app>" | "launch <app>" | "launch atom" | "open yazi", "open a terminal", "open cursor" |
| Focus app/window | "<app name>", "Switch to" | "Switch to <app>", "Go to <app>" | "Switch to <window name>" | "focus <app>", "focus last" | "focus chrome" | "switch to chrome", "next window" |
| Close | "Close window", "Quit <app>" | "Close <app>", "Exit", "Quit" | "Close window" | "window close" | "close terminal" | "close this tab", "quit spotify", "fecha" |
| List windows | n/a | "Show all windows", "List all windows", "Show task switcher" | "List all windows", "List windows for <program>" | "running list", "focus" (menu) | n/a | n/a |
| Min/max | n/a in this doc | "Minimize window", "Maximize <app>", "Restore window" | "Minimize window", "Restore windows", "Show Desktop" | "window hide" | n/a | "maximize chrome", "full screen safari", "tela cheia", "flutuante" |
| Tile/snap | n/a | "Snap window to <direction>" | n/a | "snap left", "snap right third", "snap <app> <position>", "snap screen 2" | n/a | "Open a terminal beside the browser" |
| Workspace | n/a | n/a | n/a | n/a | n/a | "workspace 3", "Move this window to workspace three", "move my browser to workspace 3", "open two terminals in workspace three" |
| Overlays | "Show numbers / names / grid / window grid", "Hide ..." | "Show numbers [here / on <app> / everywhere]", "Show grid" | "MouseGrid [window]", "hide numbers" | "mouse grid", Cursorless hats, Rango hints | numbered links | jev-voice-browser auto numbers |
| Sleep/wake | "Stop listening" / "Start listening"; iOS "Go to sleep" / "Wake up" | "Voice access sleep" / "wake up", "Mute" / "Unmute" | "Go to sleep" / "Stop listening", "Wake up" | "go to sleep", "sleep all", "wake up", "wake up and listen" | "pause" | follow-up window, wake word "Alfred" |
| Undo | "Undo that" | "Undo that", "Correct that" | "Undo that", "Scratch that <n> times" | "undo that", "scratch that", "nope that" | "undo" (or say another number) | "undo", "go back", "bring it back" |
| Help | "Show commands" | "What can I say" | "What can I say", "Give me help" | "help active", "help context" | n/a | n/a |
| Repeat | "Repeat that" | n/a | n/a | "repeat that", "twice", "<n> times", "again" | n/a | n/a |
| Escape | n/a | "Cancel" | "Cancel" | n/a | n/a | "cancel", "cancela" |

Observations:
- Verb-first imperative with the object last is universal: VERB [the] OBJECT [to DESTINATION].
- Deictics are common and must be supported: "this window", "it", "here", "the browser". chris-wozniczek's `refers_to_frontmost` boolean and omause's alias "the browser" both exist for this reason.
- Workspace vocabulary is missing from every mainstream system because mainstream desktops do not expose workspaces by voice. The only evidence is from the 2026 Hyprland projects. Users say "workspace three", "move this to workspace three", "beside the browser". Hyprland specifics like special workspaces, groups, pin, pseudo-tile have no established spoken forms. We will have to choose them and should reuse the dispatcher names users already type in config ("toggle floating", "fullscreen", "pin", "scratchpad").
- Number words arrive from STT as either digits or words ("3" or "three"); omause and jev-voice-browser both normalize in code.

### 2.2 Disambiguation ("which Firefox?")

Universal pattern, with no exceptions among mature systems: label candidates with numbers on screen, the user says a number.
- Apple: "numbers are shown next to each item". Microsoft: "attaches a number to each of them and asks you to pick". Google: asks "Which one?" and accepts a number. Dragon: "choose <n> or hide numbers or cancel". Serenade: numbered alternatives list. jev-voice-browser: numbered overlays for top 2 to 3 when confidence is low, resolved with no model call.
- Counter-example to avoid: oc-voice reports "candidate counts rather than guessing", which leaves the user with nothing to say next.

Design for us [inference]:
- Window-level ambiguity (two Firefox windows): draw a large number badge centered on each candidate window via a layer-shell overlay, on every workspace that is visible, and list off-screen candidates in the HUD with workspace and title ("1 Firefox, ws 2, GitHub"; "2 Firefox, ws 5, YouTube"). Say "one" or "two". Parse the number locally, never call Jev for it.
- Prefer smart defaults over asking. Heuristics observed in prior art: most recently focused match (Talon `focus last`), the match on the current workspace, title words in the utterance ("the GitHub Firefox"). Jev's per-option probabilities give a principled trigger: ask only when top1 is below a floor or top1 minus top2 is below a margin. jev-voice-browser uses confidence >= 0.45 and top probability >= 0.35.
- Serenade's variant is better for reversible actions: focus the top candidate immediately, show badges for the alternatives for about 3 seconds, and "two" switches. Focus changes are free to undo, so never block on them.
- Control-level ambiguity inside an app: hypruse `marks` already produces a numbered screenshot plus legend, and `click_ui` already "returns the candidates instead of guessing". That is "Show numbers". A window-scoped variant ("show numbers here") matches Apple's "Show window grid" and Microsoft's "Show numbers here".
- Overlays must auto-dismiss on selection (Microsoft), on content change (Android), on "cancel" / "hide numbers", and on timeout; dim after inactivity (Apple).
- Fallback when the accessibility tree is empty: numbered grid with drill-down (Apple, Dragon MouseGrid, Talon mouse grid). Dragon lets users chain grid digits in one breath ("MouseGrid 5 3").

### 2.3 Misrecognition and undo

Evidence:
- Misrecognition is the top complaint of a new Talon user: the engine "repeatedly misidentif[ying] a command you are trying to give" and causing unintended deletions. [secondary: fileside]
- Every system shows the user what it heard (Talon subtitles, Apple hints, oc-voice live transcript, kevinbadi pill, omause panel). This is the single cheapest error-recovery tool: the user can tell an STT error from a decision error.
- Vocabulary biasing: Apple/Microsoft custom vocabulary; omause injects discovered app names into Whisper's `initial_prompt`; chris-wozniczek feeds an extra vocabulary list to both STT engines.
- Fuzzy matching before the decision step: oc-voice Jaro-Winkler 0.82; omause fuzzy ranker.
- Teachable corrections: `omause teach --last <target>` binds the last misheard phrase to the right target. Talon's guidance is to change the command word itself when a word keeps failing.
- Refuse rather than guess: kevinbadi says "not sure" below 0.35; jev-voice-browser "wait"s; Yappy escalates below 0.25.
- Undo vocabulary is uniform: "undo that" / "undo" / "scratch that". In every surveyed system undo is delegated to the app (Ctrl+Z) or applies only to dictated text. No surveyed system has a real undo for window management actions. That is an opening for us.

Design for us [inference]:
- Keep an action journal with an inverse for every reversible compositor action: focus (previous window), move to workspace (previous workspace), workspace switch (previous workspace), toggle floating / fullscreen / pin (toggle back), resize/move (previous geometry), close window (not reversible, so it is in the confirm tier). "undo" or "undo that" pops the journal. Hyprland IPC gives us all the before-state for free via `desktop`.
- "repeat that" / "again" / "twice" from Talon: cheap and high value for "next window", "move left".
- Show heard text and chosen action together in the HUD, plus top alternatives when confidence is middling (Serenade).
- Keep a visible command history (Talon) reachable from Waybar.
- STT-side: bias with the live list of window classes, titles, app names, and workspace names. Number normalization in code.

### 2.4 Confirmation for destructive actions

- Platform guidance: Google conversation design says to confirm explicitly "prior to performing an action that would be difficult to undo" and otherwise to "use confirmations sparingly, only when the cost of misunderstanding the user is high". Amazon's Alexa guidance says the same ("Use confirmations sparingly"). [primary: https://developers.google.com/assistant/conversation-design/confirmations ; https://developer.amazon.com/en-US/alexa/alexa-haus/dialogs-for-ac]
- Observed tiers in 2026 projects: omause has risky (never offered to the model, executor fails closed), caution (needs "confirm" prefix: close window, disable connectivity), safe. jev-voice-browser asks a separate `destructive` boolean and demands spoken "confirm" or "cancel". omarchy-siri requires a spoken "yes" for shutdown/reboot/logout. omarchy-voice holds the action, shows a confirmation state on the bar widget, and accepts a click, a CLI verb, or voice to confirm. oc-voice needs "confirma".
- jev-voice-browser's README is candid that a model-judged `destructive` flag is "a convenience, not a guarantee".

Design for us [inference]:
- Risk tier is a static property of the action type decided in code, never solely a model judgment. A Jev `destructive` boolean may only raise the tier, never lower it.
- Tier 0, reversible (focus, move, workspace, float, fullscreen, resize, launch): act immediately, journal the inverse, no confirmation.
- Tier 1, lossy but local (close window, kill, close all on workspace, anything that types Enter into a terminal): hold, show a HUD card "Close Firefox, GitHub? say confirm or cancel", timeout to cancel after about 5 s. Raise to a hard requirement when the STT or decision confidence is low.
- Tier 2, session or system (exit Hyprland, poweroff, reboot, logout, package operations, arbitrary shell): not offered to the decision engine by default; opt-in with confirmation that cannot be satisfied in the same utterance.
- The confirm word must be parsed locally and anchored as a standalone utterance (Talon anchoring) so that "confirm" inside a sentence or background speech does not fire it.
- Offer a non-voice confirm path (click the HUD or a keybind), like omarchy-voice, for noisy rooms.

### 2.5 Feedback when the system does not speak

Since our app should act, not talk, feedback must be visual plus optional earcons.

Evidence:
- Earcons are an established HCI construct: Blattner, Sumikawa, and Greenberg (1989), "Earcons and Icons: Their Structure and Common Design Principles", Human-Computer Interaction 4(1):11 to 44, define earcons as short structured audio messages giving "information and feedback to the user about computer entities". [primary: https://www.tandfonline.com/doi/abs/10.1207/s15327051hci0401_1]
- Apple ships "Play sound when command is recognized" as an optional setting. hyprwhspr, voxtype, and Handy all ship start/stop sounds. kevinbadi/jev-voice uses "Tink = recording, Pop = sent", chime on success, "low buzz on failure", and made the ding the default instead of a spoken persona.
- State displays converge on 5 to 6 states: idle, listening, heard / transcribing, thinking / planning, executing / done, error (+ confirm-pending in omarchy-voice, handoff in omause).
- Waybar modules are the ambient status surface for Hyprland users (hyprwhspr, voxtype, omause, omarchy-voice all do this).
- Subtitles of the heard phrase (Talon since 2021).

Design for us [inference]:
- The action itself is the primary feedback. When a window visibly moves, do not add more noise. Extra feedback is for the cases where nothing visible happens: ignored, not understood, low confidence, waiting for confirm, error.
- Layer-shell HUD pill, top center, overlay layer, keyboard interactivity none, input region empty so clicks pass through, except while a confirm card is up. Shows: state color, heard text, chosen action in plain words ("Move kitty to workspace 3"), and alternatives when relevant.
- Waybar module with the same states for users who disable the HUD. The hypruse repo already has a `waybar/` directory.
- Earcon set of at most four: listening start, accepted, rejected / not understood, confirm needed. Short, quiet, distinct in contour not only pitch, all optional. Blattner et al. recommend building families from shared motives; success and failure should be recognizably related but opposite in contour.
- Reuse hypruse's existing cursor "beacon" concept for pointer actions so that the user sees where a click landed.
- Latency of feedback matters more than latency of the action: the listening state change and partial transcript must appear within 100 ms (section 2.6).

### 2.6 Latency thresholds from the HCI literature

| Threshold | Meaning | Citation |
|---|---|---|
| 0.1 s | "the limit for having the user feel that the system is reacting instantaneously" | Nielsen, "Response Times: The 3 Important Limits", summarizing Miller (1968), "Response time in man-computer conversational transactions", AFIPS FJCC 33, and Card, Robertson, Mackinlay (1991), "The information visualizer", CHI '91. [secondary summarizing primary: https://www.nngroup.com/articles/response-times-3-important-limits/] |
| about 0.2 s | Typical gap between turns in human conversation. Across 10 languages: mode 0 ms, median about +100 ms, mean about +208 ms; language means range from +7 ms (Japanese) to +469 ms (Danish). | Stivers et al. (2009), "Universals and cultural variation in turn-taking in conversation", PNAS 106(26). [primary: https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/] |
| 0.3 s | Talon's default end-of-speech timeout (raised from 0.15 s in 2021). Practitioner evidence of a workable command endpointing delay. | [primary: talonhub/community settings.talon; Talon changelog] |
| 0.4 s | The "Doherty threshold": system response under 400 ms keeps user and computer from waiting on each other; productivity rises sharply. Previous norm was 2 s. | Doherty and Thadani (1982), "The Economic Value of Rapid Response Time", IBM. [primary: https://www.vm.ibm.com/devpages/jelliott/evrrt8.html ; summary: https://lawsofux.com/doherty-threshold/] |
| 1.0 s | "the limit for the user's flow of thought to stay uninterrupted" | Nielsen / Miller / Card et al., as above |
| about 4 s | For spoken conversational agents in cars, quality of experience degrades above about 4 s; fillers help under high delay. Upper bound only, not a target. | Funk et al. (2020), AutomotiveUI '20, doi:10.1145/3409120.3410651. [secondary: search summary only, ACM page returned 403; medium confidence] |
| 10 s | "the limit for keeping the user's attention focused on the dialogue" | Nielsen, as above |

What this means for a voice command pipeline [inference]:
- The user's clock starts at the end of their last word, not when we receive a final transcript. Budget, in order: endpointing silence, STT finalization, decision call, compositor action, first frame.
- Target: state acknowledgement within 100 ms of key press or speech onset (HUD goes to "listening", earcon). Visible effect within 1 s of end of speech, with 400 ms as the stretch goal for closed-set commands.
- Endpointing is usually the largest item. Observed settings: Talon 300 ms; kevinbadi/jev-voice 550 ms; jev-voice-browser 900 ms silence fallback plus 600 ms for free text. With push-to-talk, key release is the endpoint and this cost is zero. That is a strong latency argument for PTT as default.
- Two ways prior art beats the silence timer in hands-free mode: (a) the `complete` boolean on partial transcripts, acting early when the words "already commit to a closed-set action" (jev-voice-browser, jev-canvas); (b) a local deterministic fast path for exact commands with no network call (omause, chris-wozniczek).
- Third-party self-reported Jev decision latencies range 170 to 690 ms per call (kevinbadi 170 to 420; moritzkremb avg about 330, first call about 700 due to TLS; jev-canvas 300 to 550; Yappy 275 to 690). Unverified. If those hold, a network decision alone consumes most of the 400 ms budget, which makes the local fast path and a kept-alive HTTP connection design requirements, not optimizations.
- Microsoft exposes "Wait time before acting" as a user setting. Users with slower or disfluent speech need a longer endpoint; power users want it shorter. Make it a setting, and consider separate values for commands and dictation (Talon user complaint about 0.3 s in dictation).
- If anything will exceed 1 s, show progress state immediately. Nielsen: between 2 and 10 s a simple busy indicator is enough.

### 2.7 Push-to-talk vs wake word vs always listening

| Mode | Pros | Cons | Who uses it |
|---|---|---|---|
| Push-to-talk (hold) | Zero false activations; key release is a perfect endpoint, so lowest latency; privacy is obvious; no `is_command` gate needed; trivial on Hyprland with `bind` + `bindr` | Needs a hand (or foot pedal), so it fails users who need fully hands-free control; awkward while typing | voxtype, hyprwhspr, Handy, omause, jev-voice-windows, omarchy-siri (toggle) |
| Toggle (tap on, tap off) | One tap, hands then free; supports long utterances | Easy to forget it is on; needs very visible state | hyprwhspr, Handy, omarchy-voice ("starts muted") |
| Wake word | Hands-free; socially legible | Adds a word to every command; misfires. Dubois et al. (2020) measured "0.95 misactivations per hour, or 1.43 times for every 10,000 words spoken" of TV audio across commercial smart speakers, some lasting 10 s or more [primary: https://petsymposium.org/popets/2020/popets-2020-0072.php]; needs an always-on local detector (CPU cost on a 4C i5) | kevinbadi/jev-voice ("Alfred"), omause trigger phrase, Android "Hey Google" |
| Always listening with command/sleep modes | Fastest for heavy users; chaining and repeaters shine; the only option for many motor-impaired users | Background speech and media trigger commands ("start watching a Talon tutorial video with Talon awake. No surer way to make all hell break loose!" [secondary: fileside]); speaker echo can trigger commands (omarchy-voice ships `barge_in = false` and an echo-cancel config); continuous STT load | Talon, Dragon, Apple VC, Windows Voice Access, numen |

Mitigations observed for open-mic modes:
- Sleep / wake grammar, fully anchored wake phrase, deep sleep with a longer phrase, auto-sleep after N idle minutes, per-app auto deep sleep for meeting apps (Talon).
- Follow-up window: after a wake-word command, accept further commands without the wake word for 8 s (kevinbadi). This recovers most of the chaining benefit.
- `is_command` boolean gate (jev-voice-browser scores small talk about 0.02; jev-canvas notes that two short real commands scored only about 0.12 and were dropped, so the gate has false negatives on very short utterances). [self-reported]
- Echo cancellation (the target machine has webrtc-audio-processing and PipeWire's echo-cancel module) and pausing listening while media plays.
- Command-only mode where non-commands are ignored rather than typed (Apple).

Recommendation [inference]: default to hold-to-talk via Hyprland `bind`/`bindr` because it gives the best latency, zero false accepts, and lowest CPU on an i5-8350U. Ship tap-to-latch and an opt-in hands-free mode with Talon's sleep grammar and a follow-up window, because a voice control app that cannot be used hands-free fails the users who need it most. Privacy note: in any open-mic mode, transcripts of non-commands must be dropped locally and never sent to the decision API; only utterances that pass a local gate (wake word, follow-up window, or grammar hit) should leave the machine.

### 2.8 Command chaining ("open terminal and move it to workspace three")

How prior art does it:
- Talon: continuous recognition; any unanchored commands can be concatenated in one breath with no conjunction. Chaining is also recommended for voice health: "repetitive single command utterances are often the most straining. Try to string together commands." [secondary: https://dictation-toolbox.github.io/dictation-toolbox.org/voice%20strain.html]
- Serenade: "save focus terminal" executes two commands.
- kevinbadi: a `compound` boolean, then code splits on conjunctions and runs steps in order.
- chris-wozniczek: ClauseSplitter, one Jev call per clause, and it "carries the opened app forward as the UI task target".
- omause: "ask Jev for one bounded ordered plan in a single request", max 8 steps, verify after each step, "stop immediately on execution or verification failure". Example: "open two terminals in workspace three" focuses workspace 3 first, then launches twice. "move my browser to workspace 3" focuses, then moves.
- jev-voice-browser: "One action per utterance", but supports two commands in one breath.

Hard parts identified:
- Anaphora across clauses: "open terminal and move **it** to workspace three". "it" is a window that does not exist yet. Requires waiting for the window to appear, then binding "it" to the new window. hypruse has `wait_for(window_open)` and `sequence`, which "stops if the desktop changes structurally between steps". No surveyed project handles this robustly; omause reorders instead (go to the workspace first, then launch). Reordering is the simpler and faster trick and Hyprland also supports launching directly onto a workspace with `[workspace 3 silent]` exec rules, which makes the two-step command a single atomic dispatch.
- Jev evaluates questions independently and in parallel, so it cannot produce a dependent ordered plan natively in one question. Observed workarounds: clause splitting in code then one question set per clause (can be packed into one request as `step1_intent`, `step2_intent`, ... since all questions share one state), or a slot-filling schema (`action`, `target`, `destination`, `count`) that lets code expand the plan.
- Partial failure: stop on the first failed step, report which step failed, journal completed steps so "undo" reverts the whole chain as a unit.

Recommendation [inference]: support two-step chains from day one since they are what users naturally say, cap at a small number, split in code, execute through hypruse `sequence` with `wait_for` between dependent steps, bind "it" / "that" to the most recently created or acted-on window, and prefer atomic Hyprland forms when available.

### 2.9 Accessibility

- The core population for full voice control is people with RSI or motor impairments. HN: "Because they might be physically impaired in some way. Or have severe Repetitive Strain Injury (RSI)"; "using talon and cursorless i am faster than I was using just a keyboard and mouse". [secondary: HN thread] Anything that requires a key press to start, confirm, or recover excludes them. Every path (activate, confirm, cancel, undo, sleep, wake, help, dismiss overlay) needs a voice-only route.
- Voice strain is a real occupational hazard of voice control. Community guidance: avoid repeated single-word commands, chain commands, speak at normal volume, take breaks, supplement with other inputs. [secondary: dictation-toolbox voice strain page; https://talon.wiki/Resource%20Hub/Speech%20Recognition/improving_recognition_accuracy/] Design consequences: short commands, repeaters ("again", "twice"), chaining, no mandatory wake word on every utterance, no forced re-speaking of long commands after an error (offer numbered alternatives instead).
- Non-standard speech: accents and dysarthria raise STT error rates. Mitigations seen: user-replaceable command words (Talon), custom vocabulary (Apple, Microsoft), teachable aliases (omause), adjustable "Wait time before acting" (Microsoft). A paraphrase-tolerant decision layer helps here since it does not need exact words, but it cannot fix STT garbage, so show the transcript.
- Non-speech input: Talon supports pop and hiss noises and eye tracking as complementary inputs; pop is widely used for click. Out of scope for v1 but the architecture should not preclude a noise-triggered "confirm".
- Deaf or hard-of-hearing users and noisy or silent environments: earcons must never be the only feedback channel; everything has a visual equivalent. Conversely, low-vision users need more than a small pill: support large HUD text, high contrast, and optional spoken confirmations as a setting even though the default is silent.
- Cognitive load: discoverability commands ("what can I say", "show commands", "help active") are present in every mature system. A context-sensitive cheat sheet listing what is sayable now, including the user's own keybind descriptions, is essential because vocabulary recall is the main barrier for new users.
- Visual fatigue: always-on colored hats overwhelmed a new Cursorless user; Apple dims overlays after inactivity. Overlays should be transient by default.
- Motion and focus safety: overlays must never steal focus (oc-voice, Handy, kevinbadi all call this out).
- Privacy is an accessibility issue for users who must leave the mic open all day: local STT, local gating, only gated transcripts leave the machine, clear mic state indicator.

---

## 3. Jev-specific lessons from the week-old ecosystem

1. Jev's typed outputs line up with established voice UX primitives: `choice` with probabilities is the disambiguation list; `boolean` is the gate (`is_command`, `complete`, `destructive`); `score` handles graded amounts ("scroll a bit", "a lot", "resize a little").
2. Because questions are independent, put every question for one utterance into one request and let code read "only the answers the chosen action needs" (kevinbadi). Plan confidence is the minimum over the answers used.
3. "Select, do not generate": free-text arguments come from code-extracted candidate spans. For window management this is rarely needed (targets are enumerable), which makes our domain an unusually good fit.
4. The 255-option cap matters for the target choice. chris-wozniczek caps the app list at 254 + none; omause ranks and budgets candidates per kind before asking. For us: windows and workspaces are few, but apps (.desktop entries) and keybinds can exceed the cap, so pre-rank with fuzzy matching.
5. Thresholds are model-version-specific ("calibrated on jev-1.13.0"). Pin the model version and keep thresholds in one config file.
6. Hide dangerous options from the model entirely and fail closed (omause). Never rely on a `destructive` boolean alone.
7. State size drives latency and cost: jev-voice-browser caps the snapshot at 100 elements and 60 chars each (3k to 6k tokens). jev-voice-windows had to raise timeouts as the installed-app list grew. Keep the state compact: current windows, workspaces, focused window, recent targets, and a pruned candidate list.
8. None of the Jev projects implements a compositor-level undo journal, Talon-style sleep grammar, repeaters, or a "what can I say" overlay. None runs on Wayland with an accessibility tree. These are open differentiators.

---

## 4. Prioritized UX principles for our app

P0 means ship-blocking, P1 means first release, P2 means soon after.

1. **P0. Act, and let the action be the feedback.** Reversible compositor actions execute immediately with no confirmation and no speech. Extra feedback is reserved for the cases with no visible effect: ignored, unsure, confirm-pending, failed. (Omause: "voice command in, visible action out"; Google/Amazon: "use confirmations sparingly".)
2. **P0. Always show what was heard and what was decided.** Non-focus-stealing layer-shell pill (keyboard interactivity none, click-through) with 5 to 6 states and the heard text plus the chosen action in plain words. Mirror the state in a Waybar module. (Talon subtitles, kevinbadi pill, omause widget.)
3. **P0. Acknowledge within 100 ms, finish within 1 s of end of speech, aim for 400 ms on closed-set commands.** Make hold-to-talk the default so key release is the endpoint. Add a deterministic local fast path for exact commands so that many commands never touch the network. Keep endpointing delay user-configurable. (Nielsen/Miller/Card; Doherty; Talon 300 ms; Microsoft "Wait time before acting".)
4. **P0. Use the vocabulary people already know.** "open", "switch to" / "focus", "close window", "move this to workspace three", "fullscreen", "float", "snap left", "show numbers", "hide numbers", "undo that", "cancel", "go to sleep", "wake up", "what can I say", "repeat that". Put synonyms in the option descriptions; let Jev absorb paraphrase. Users must never need to memorize our words. (Sections 1.5 to 1.8, 2.1.)
5. **P0. Disambiguate with numbers, not questions.** Trigger on low top-1 probability or a small top-1 to top-2 margin. Badge candidate windows with big numbers; resolve the spoken number locally with no model call; auto-dismiss on selection, content change, "cancel", or timeout. For reversible actions use Serenade's variant: act on the top guess and let a spoken number swap it. For in-app controls reuse hypruse `marks` / `click_ui` as "show numbers [here]". (Apple, Microsoft, Google, Dragon, Serenade, jev-voice-browser.)
6. **P0. Risk tiers live in code, and dangerous options are never offered to the model.** Tier 0 reversible: just do it. Tier 1 lossy (close, kill): hold with a HUD card, require a standalone "confirm" or a click, timeout cancels. Tier 2 session or system: hidden by default, executor fails closed. A model `destructive` flag can only raise the tier. (omause, jev-voice-browser caveat, Google guidance.)
7. **P0. A real undo.** Journal the inverse of every reversible compositor action; "undo" / "undo that" pops it; a chain undoes as one unit. No surveyed system has this for window management, and it is what makes principle 1 safe.
8. **P1. Refuse gracefully when unsure.** Below the confidence floor do nothing, show "not sure" with the top 2 to 3 interpretations numbered, play the reject earcon. Doing nothing is always better than doing the wrong thing. (kevinbadi 0.35 floor; Yappy 0.25 escalation.)
9. **P1. Hands-free mode with a sleep grammar, opt-in.** Tap-to-latch and open-mic modes with `go to sleep [anything]`, anchored `wake up`, deep sleep with a longer phrase, idle auto-sleep, optional wake word with an 8 s follow-up window, echo cancellation, and a local gate so that non-command speech never leaves the machine. (Talon, kevinbadi; Dubois et al. misactivation rates.)
10. **P1. Chain two or more commands in one breath.** Split clauses in code, pack all clause questions into one request, execute via hypruse `sequence` + `wait_for`, bind "it" / "this" / "that" to the focused or most recently created window, prefer atomic Hyprland forms (launch directly on a workspace). Stop on first failure and say which step failed. Add "again" / "twice" / "N times" repeaters. (Talon, Serenade, omause, chris-wozniczek; voice strain guidance.)
11. **P1. Make errors cheap to fix at the source.** Bias STT with live window classes, titles, app names, workspace names. Fuzzy-rank candidates before the decision step. Normalize number words in code. Provide `teach --last` style alias learning and let users rename any command word. (omause, Talon wiki.)
12. **P1. Discoverability on demand.** "what can I say" shows a context-sensitive cheat sheet that includes the user's own Hyprland keybind descriptions from hypruse `binds`. Near-miss hints when an utterance almost matched. Visible command history. (Apple "Show hints" / "Show commands", Dragon "What can I say", Talon `help active` and `command history`.)
13. **P1. Four quiet earcons, all optional, never the only channel.** Listening start, accepted, rejected, confirm needed. Related motives with opposite contours for accept and reject. (Blattner et al. 1989; Apple setting; kevinbadi defaults.)
14. **P2. Every path has a voice-only route and a no-voice route.** Activate, confirm, cancel, undo, sleep, wake, help, and dismiss must each be doable by voice alone (motor accessibility) and by key or click alone (noisy rooms, voice strain). Configurable HUD size and contrast; optional spoken confirmations as a setting. (Section 2.9.)
15. **P2. Transient, calm overlays.** Number badges and grids appear on demand, dim after inactivity, and vanish on completion. No always-on decorations by default. Grid drill-down exists only as the fallback when the accessibility tree is empty. (Apple fade setting; Cursorless fatigue report.)
16. **P2. Pin the model, keep thresholds in one file, log every decision.** Confidence gates are model-version-specific; a JSONL history of transcript, answers, probabilities, action, and outcome enables threshold tuning and doubles as the user-visible command history. Transcripts are sensitive, so keep them local with a retention limit. (jev-voice-browser, omause.)

---

## 5. Not found or not confirmed

- numen's README and default phrases file: sourcehut served an anti-bot challenge and a 502. Its chaining grammar, feedback, and any sleep/wake behavior are unconfirmed here. The "X11 only" statement is from numenvoice.org via search snippet.
- The original Talon announcement about removing public Linux support: only OSnews (2026-05-31) was reachable; the cited blog returned 403 and Talon's public changelog does not mention it.
- Funk et al. (2020) exact numbers: ACM returned 403; the "about 4 s" figure is from a search summary.
- Karat et al. (1999), CHI '99, "Patterns of entry and correction in large vocabulary continuous speech recognition systems": bibliographic details confirmed, but I could not retrieve the correction-time numbers, so I did not cite any.
- Apple documentation for what happens on an unrecognized command, and any Apple-published latency figures for Voice Control: not found.
- Any peer-reviewed latency threshold specific to voice command and control of a desktop (as opposed to conversational agents or GUI response time): not found. The thresholds in section 2.6 are general HCI and conversation-analysis results applied by inference.
- Any primary TypeSafe or Vercel documentation that discusses voice or UI control as a use case, or gives guidance on streaming partial transcripts: not found. The vercel.com "What is Jev" page does not mention voice or UI control.
- Any Jev voice project on Linux with numbered overlays, an accessibility tree, a sleep grammar, or an undo journal: none found. omause is the only Hyprland + Jev project found, and it has none of those.
- Established spoken forms for Hyprland-specific concepts (special workspace, groups, pin, pseudo-tile, submaps): none found in any project.
- Serenade company status: repo last pushed 2024-06-11 and not archived; no primary statement about shutdown found.

## 6. Sources

Primary, docs and repos:
- Talon community command set: https://github.com/talonhub/community (files: core/windows_and_tabs/window_management.talon, window_snap.py, core/modes/*.talon, deep_sleep.py, plugin/command_history, plugin/repeater, settings.talon)
- Talon wiki: https://talon.wiki/Customization/talon-files/ ; https://talon.wiki/Basic%20Usage/basic_usage/ ; https://talon.wiki/Resource%20Hub/Speech%20Recognition/improving_recognition_accuracy/ ; https://talon.wiki/Resource%20Hub/Speech%20Recognition/troubleshooting/
- Talon changelog: https://talonvoice.com/dl/latest/changelog.html
- Cursorless docs: https://www.cursorless.org/docs/ ; Rango: https://github.com/david-tejada/rango
- numen: https://numenvoice.org/ ; https://git.sr.ht/~geb/numen (not readable)
- hyprwhspr: https://github.com/goodroot/hyprwhspr ; voxtype: https://github.com/peteonrails/voxtype ; Handy: https://github.com/cjpais/Handy ; nerd-dictation: https://github.com/ideasman42/nerd-dictation ; Speech Note: https://github.com/mkiol/dsnote
- Serenade docs: https://serenade.ai/docs/ ; https://github.com/serenadeai/serenade
- Apple: https://support.apple.com/guide/mac-help/use-voice-control-commands-mh40719/mac ; https://support.apple.com/en-euro/guide/accessibility-mac/item-number-grid-overlays-voice-control-mchl26854b08/11.0/mac/11.0 ; https://support.apple.com/en-is/guide/mac-help/change-voice-control-settings-spc002/26/mac/26 ; https://support.apple.com/en-us/111778
- Microsoft: https://support.microsoft.com/en-us/accessibility/windows/voice-access/voice-access-command-list ; https://support.microsoft.com/en-us/accessibility/windows/voice-access/use-voice-to-interact-with-items-on-the-screen ; https://support.microsoft.com/en-us/accessibility/windows/voice-access/history-of-voice-access-updates
- Google: https://support.google.com/accessibility/android/answer/6151848 ; https://support.google.com/accessibility/android/answer/6151854 ; https://developers.google.com/assistant/conversation-design/confirmations
- Amazon: https://developer.amazon.com/en-US/alexa/alexa-haus/dialogs-for-ac
- Dragon v16 cheat sheet: https://dragon.nuance.com/shared/data-sheets/ct-dragon-professional-v16-and-legal-v16-command-cheat-sheet-en-us.pdf
- Hyprland projects: https://github.com/wombatoperator/omarchy-voice ; https://github.com/nklaveren/oc-voice ; https://github.com/Fiercejojo88862/omarchy-siri ; https://github.com/devfros/omause
- Jev projects: https://github.com/moritzkremb/jev-voice-browser ; https://github.com/kevinbadi/jev-voice ; https://github.com/chris-wozniczek/jev-voice-control ; https://github.com/jonatasperaza/jev-voice-windows ; https://github.com/gaborishka/jev-canvas ; https://yappy.biz/jev/ ; index lists https://github.com/yibie/awesome-jev and https://github.com/cobanov/awesome-jev
- Jev vendor pages consulted: https://vercel.com/i/what-is-jev ; https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway

Primary, papers:
- Stivers et al. 2009, PNAS: https://pmc.ncbi.nlm.nih.gov/articles/PMC2705608/
- Dubois et al. 2020, PoPETs: https://petsymposium.org/popets/2020/popets-2020-0072.php
- Doherty and Thadani 1982: https://www.vm.ibm.com/devpages/jelliott/evrrt8.html
- Blattner, Sumikawa, Greenberg 1989: https://www.tandfonline.com/doi/abs/10.1207/s15327051hci0401_1
- Funk et al. 2020: https://dl.acm.org/doi/10.1145/3409120.3410651 (abstract not retrievable)

Secondary:
- Nielsen, response time limits: https://www.nngroup.com/articles/response-times-3-important-limits/
- Laws of UX, Doherty threshold: https://lawsofux.com/doherty-threshold/
- Fileside, "Four weeks of voice computing": https://www.fileside.app/blog/2025-04-14_voice-computing/
- OSnews on Talon and Linux: https://www.osnews.com/story/145162/
- HN thread on numen: https://news.ycombinator.com/item?id=34816000
- Dictation toolbox, voice strain: https://dictation-toolbox.github.io/dictation-toolbox.org/voice%20strain.html
- handsfree.dev index: https://handsfree.dev/voice-control.html
