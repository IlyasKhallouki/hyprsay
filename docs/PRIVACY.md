# What hyprsay sends, and where

hyprsay listens only while you hold the push-to-talk key. It has no wake word and no
always-on microphone. This page says exactly what leaves your machine, when, and how to
make it nothing at all. `hyprsay inspect` prints the real request bodies of your last
command, so none of this has to be taken on trust.

## What leaves the machine

| | Default (`hybrid`) | `stt.backend = "local"`, `jev.enabled = false` and `interpret.enabled = false` |
|---|---|---|
| Your voice (audio) | stays local, **except** when the local recognizer produced words that could not be placed, or produced nothing from real speech: then that one clip is sent to a speech model through the gateway. Not when the transcript was fine and something else was wrong, and never for a dictation | never leaves |
| What you said (text) | sent to Jev when the local grammar cannot resolve the command | never leaves |
| What you said (text), plus your last three commands | sent to a second, small model that tidies a rambling sentence into one command before anything tries to understand it. Only when the local grammar cannot already parse what you said, which covers about three quarters of ordinary commands, and never for a dictation. What goes is the normalized sentence (fillers, casing and punctuation already dropped) and up to three previous commands in the same form, so that "turn it down to 50%" can be read against "play spotify". A command that carried your own text is remembered only as what it did, so it goes as "type text Obsidian" and never as the words. The model returns text and nothing else: it never picks an action | never leaves |
| Dictated text ("type ...") | **never sent anywhere**, never shown on the overlay while waiting, and stored in the journal only as a length and a hash. An utterance that opens with any form of type, say, write or dictate is treated as dictation even when the grammar cannot parse it, so a misheard "typed" does not turn it into a request. The same test runs before the interpreter above, from the same word list rather than a copy of it, so a dictation is not tidied up by anything either | never leaves |
| Names and kinds of your open apps, workspace numbers | sent to Jev with a semantic command: the trusted names of the apps you have open, which workspace you are on, the app and kind of the focused window, and what the last command did. Only names a root-owned `.desktop` file claims, so a window cannot name itself | never leaves |
| Window titles | **not sent**, except to tell apart several windows of the same app, and then only theirs, truncated and redacted | never leaves |
| What a media player is playing (track, artist, album) | **not sent**, except to tell apart several players that would otherwise carry the same name, and then only theirs, truncated and redacted. Same rule, same switch, same reason: it says what you are watching | never leaves |
| Names and kinds of your installed apps | sent to Jev with **every** semantic command, not only when you ask to open something: the questions all go in one round trip, before anything knows which one you meant | never leaves |
| Browser tabs and pages, with the hypruse-browser extension | each tab goes as its site ("youtube.com tab"), and your most visited sites as addresses to open. A tab's title goes only to tell apart two tabs of one site, cut and redacted like a window title. While a browser is in front, the names of the links, buttons and search boxes in view go as options to click. What you typed into a page never goes | never leaves |

Common commands ("workspace three", "close this", "open firefox") are resolved entirely on
your machine by a grammar. Nothing is sent for them in any mode.

## Where it goes, when it goes

Requests go to the Vercel AI Gateway (`ai-gateway.vercel.sh`; from Europe the edge that
answers is typically Paris), which forwards to the model provider:

- **Jev** (`typesafe-ai/jev`, TypeSafe AI): its API resolves to AWS `us-west-2`.
- **Speech rescue**, by default `fish-audio/transcribe-1`. Configurable.
- **The interpreter**, by default `meta/llama-3.1-8b`, chosen because it was the fastest
  model measured that answers with one usable command (395 ms median from this machine).
  Configurable, and `interpret.enabled = false` turns it off entirely.

Every Jev request asks for `zeroDataRetention`. The gateway's catalog lists Jev as
`zdr: all` and `no_training: all`. hyprsay cannot verify what a provider does with data
after it arrives; if that matters, use the local-only settings below.

## Window titles, and what is playing

A window title can hold a mail subject, a chat name, a bank page. So titles are withheld
by default. They are sent only when all of these hold: `privacy.titles = "when_needed"`,
two or more open windows belong to the app you named, and you said extra words that might
tell them apart ("the firefox **with youtube**"). Then only those windows' titles go,
cut to 60 characters. Windows whose class or title matches a redaction rule (password
managers; "private", "incognito", "vault", "password") are sent without a title.

A track title is the same class of thing: it says what you are watching or listening to.
So `privacy.titles` governs it too. An option for a media player is named by the player
("Pause Chromium"); what it is playing is added only when two players would otherwise
wear the same name and could not be told apart, and the same redaction rules run over it.
Nothing is read at all under `"never"`, so there is no string to leak into a request or a
log line.

`privacy.titles = "never"` turns both off. The cost: "the one with youtube" style
references fall back to numbered hints on screen.

## Tabs and pages

The browser extension lets hyprsay see your tabs and the page in front of you, and that is
also what makes "click the second video" possible, so the rule is the window-title rule
again. A tab is named by its site. Its title goes only when another tab is open on the same
site, and then cut to 60 characters, with the same redaction patterns applied. The names of
the links and buttons on a page go only while a browser window is in front, because that is
when you are asking for something on it. Field values are never read, so a password or a
message you are writing cannot end up in a request.

Under `privacy.titles = "never"`, no tab title and no page text goes at all. You can still
switch tabs by site, open sites, scroll, go back, close a tab and search a site with its own
search box; clicking a link by name is what you give up.

## On your disk

`~/.local/state/hyprsay/journal.jsonl`, mode 0600, capped at 5 MB with one rotation. It
records what was heard, what was decided, what was done, and what was sent. It does **not**
record dictated text (only its length and a short hash), audio, window titles, or what a
media player is playing, including either of those inside a request body.
`HYPRSAY_JOURNAL=off` disables it.

The gateway key is read from `AI_GATEWAY_API_KEY` or `~/.config/hyprsay/ai-gateway.key`
(mode 0600). It is never logged, printed, or included in an error message.

## Fully local

```toml
[stt]
backend = "local"      # audio never leaves the machine

[jev]
enabled = false        # grammar only: nothing is ever sent

[interpret]
enabled = false        # no sentence is sent anywhere to be tidied up
```

You keep every canonical command. You lose paraphrases ("bring up my notes") and
references by description ("the music"), which become numbered hints or "not understood".

## What this does not protect against

Voice is an unauthenticated channel. Anyone who can speak while you hold the key, or a
video playing nearby, can issue a command. Push to talk is the main defense. Beyond it:
nothing destructive runs on a model's say-so, closing a window takes a cancellable
countdown, session actions take a physical key tap, typing is refused in terminals,
launchers and authentication dialogs, and nothing runs while the session is locked.
