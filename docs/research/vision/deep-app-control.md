# Lane: deep control of real applications without pre-baked key sequences

Owner example: "pick a song on Spotify". Target machine: i5-8350U, no GPU, 23 GiB RAM,
Arch Linux, Hyprland 0.56.2, French AZERTY. Date 2026-09-22.

Label key:
- **[MEASURED]** I ran the command on this machine, read-only, output quoted.
- **[PRIMARY]** a spec, a vendor's own reference docs, a repository, or a file on this disk.
- **[SECONDARY]** a third-party write-up.
- **[VENDOR]** a vendor's claim about its own product, unverified by me.
- **[INFERENCE]** my reasoning. Never a finding.

---

## 1. The one-paragraph answer

There are exactly three tiers of desktop control on Linux, and they are not close to each
other in power. **Pixels and keystrokes** (what hyprsay does now) work everywhere and
understand nothing. **D-Bus transport interfaces** (MPRIS, the portals, the freedesktop
Application interface) are semantic but tiny: MPRIS on this machine can play, pause, seek
and set volume, and cannot search, cannot list a playlist, cannot even name the tracks in
the current queue. **Service APIs reached through a local client** (the Spotify Web API
through `spotify_player`, already installed here) are fully semantic: search by phrase,
get JSON back, start a named track. The mechanism that answers "pick a song on Spotify"
is the third tier and nothing else. There is no D-Bus path to it, on any Linux desktop,
and the evidence is below.

The second half of the lane, per-application adapters, has a settled answer too, and it is
already running on this machine: the **search-provider protocol**. GNOME Shell's
`org.gnome.Shell.SearchProvider2` and KDE's `org.kde.krunner1` are the same idea, an
extensible set of independent providers that each answer "here are my matches for this
text" and "activate match N". That protocol shape, not a table of key sequences, is what
hyprsay's in-app layer should become.

---

## 2. MPRIS: what it can and cannot do, measured here

### 2.1 What is on the bus right now

**[MEASURED]** `busctl --user list --acquired | grep mpris`:

```
org.mpris.MediaPlayer2.chromium.instance1325064   1325064 chrome
org.mpris.MediaPlayer2.plasma-browser-integration 1992390 plasma-browser-
org.mpris.MediaPlayer2.playerctld                       -  (activatable)
```

`playerctl` v2.4.1 is installed (`/usr/bin/playerctl`), so `playerctld` is available as the
"last active player" proxy.

### 2.2 The full interface surface, introspected

**[MEASURED]** `gdbus introspect --session --dest org.mpris.MediaPlayer2.plasma-browser-integration --object-path /org/mpris/MediaPlayer2`
returns exactly two MPRIS interfaces and no more:

```
interface org.mpris.MediaPlayer2 {
    Raise(); Quit();
    readonly b  HasTrackList  = false
    readonly s  Identity      = 'Google Chrome'
    readonly s  DesktopEntry  = 'google-chrome'
    readonly as SupportedUriSchemes = []
};
interface org.mpris.MediaPlayer2.Player {
    Next(); Previous(); Pause(); PlayPause(); Stop(); Play();
    Seek(in x Offset); SetPosition(in o TrackId, in x Position); OpenUri(in s Uri);
    readonly  s PlaybackStatus = 'Paused'
    readwrite s LoopStatus
    readwrite d Rate, Volume
    readonly  a{sv} Metadata
    readonly  x Position
    readonly  b CanGoNext = false, CanGoPrevious = false, CanPlay = true,
                CanPause = true, CanSeek = true, CanControl = true
};
```

That is the entire vocabulary. Nine methods. Nothing takes a search string. Nothing
returns a list.

The MPRIS spec defines two optional extra interfaces, `org.mpris.MediaPlayer2.TrackList`
and `org.mpris.MediaPlayer2.Playlists` **[PRIMARY: the MPRIS D-Bus Interface
Specification]**, which would give `GetTracksMetadata`, `GoTo`, `GetPlaylists` and
`ActivatePlaylist`. Neither is present here: `HasTrackList = false`, and the whole
introspection above contains neither interface name.

### 2.3 Does Spotify's own Linux client implement them? No.

Spotify's official client is installed on this machine (`pacman -Ql spotify` ->
`/opt/spotify/spotify`, `/opt/spotify/spotify.desktop`). It is not running, so I could not
introspect it live. Instead, **[MEASURED]** I read the shipped binary:

```
$ strings -a /opt/spotify/spotify | grep -oE 'org\.mpris\.MediaPlayer2[A-Za-z.]*' | sort -u
org.mpris.MediaPlayer2
org.mpris.MediaPlayer2.Player
org.mpris.MediaPlayer2.spotify
```

Three strings. The bus name, the root interface, the Player interface. **The literal
strings `org.mpris.MediaPlayer2.TrackList` and `org.mpris.MediaPlayer2.Playlists` do not
occur anywhere in the binary.** A D-Bus service cannot export an interface whose name it
does not contain, so this is close to proof, not an impression: **Spotify on Linux offers
transport control and nothing else.** No search, no playlist enumeration, no queue
inspection, no "play this track".

`OpenUri(s)` is the one semantic-looking method, and on the plasma bridge
`SupportedUriSchemes = []` **[MEASURED]**, meaning the player declares it accepts no
scheme at all. Spotify's own client is widely reported to accept `spotify:track:<id>` on
`OpenUri` **[SECONDARY, and I could not verify it here because the client is not running]**,
but that only moves the problem: you still need the track id, and MPRIS has no way to get
one from a phrase.

### 2.4 What MPRIS is genuinely good for

**[MEASURED]** `playerctl metadata --all-players` right now:

```
plasma-browser-integration  xesam:artist  I Finished A Video Game
plasma-browser-integration  xesam:title   Dino Crisis Series Retrospective | An Exhaustive History and Review
plasma-browser-integration  xesam:url     https://www.youtube.com/watch?v=W98ZEZfBtXQ
plasma-browser-integration  mpris:length  10711521000
```

This is free, instant, local **context**: hyprsay can know what is playing, in which
application, and on the plasma bridge even **the exact URL of the page playing it**, with
one D-Bus property read. For resolving "pause the music", "what is this", "skip this",
"the video I'm watching", MPRIS is the correct and cheapest mechanism. For "pick a song",
it is the wrong tool and no amount of engineering changes that.

**Cost:** one `Get`/`GetAll` on `org.freedesktop.DBus.Properties`, sub-millisecond, no
network, no money, ~50 lines to wrap. hyprsay already depends on `playerctl` for volume
and media **[PRIMARY: hyprsay README install section lists `playerctl` as a requirement]**.

---

## 3. The mechanism that actually answers "pick a song on Spotify"

### 3.1 It is already installed on this machine

**[MEASURED]** `pacman -Ql spotify-player` -> `/usr/bin/spotify_player`,
`spotify_player --version` -> `spotify_player 0.24.1`.

**[MEASURED]** `spotify_player --help`:

```
Commands:
  get           Get Spotify data
  playback      Interact with the playback
  connect       Connect to a Spotify device
  like          Like currently playing track
  authenticate  Authenticate the application
  playlist      Playlist editing
  search        Search spotify
  lyrics        Print lyrics
```

**[MEASURED]** the two subcommands that matter:

```
$ spotify_player search --help
Usage: spotify_player search <query>

$ spotify_player playback start track --help
Usage: spotify_player playback start track <--id <id>|--name <name>>
  -i, --id <id>
  -n, --name <name>

$ spotify_player playback start context --help
Usage: spotify_player playback start context [OPTIONS] <--id <id>|--name <name>> <context_type>
  <context_type>  [possible values: playlist, album, artist]
  -s, --shuffle

$ spotify_player get key --help
  <key>  [possible values: playback, devices, user-playlists, user-liked-tracks,
          user-saved-albums, user-followed-artists, user-top-tracks, queue]

$ spotify_player get item --help
Usage: spotify_player get item <--id <id>|--name <name>> <item_type>
  <item_type>  [possible values: playlist, album, artist, track]
```

`playback start track --name "<phrase>"` is, literally, "pick a song on Spotify". There is
no key sequence, no window, no pixel, no focus. It works with Spotify minimised, on
another workspace, or not open at all.

### 3.2 How it works underneath

**[PRIMARY: `/usr/share/doc/spotify-player/README.md`, shipped by the Arch package]**

- It authenticates against the **Spotify Web API** with OAuth 2.0 authorization code flow
  with PKCE. No client secret. Tokens cached per machine, one-time interactive step.
  `spotify_player authenticate` does both flows up front, which is what you want before
  wiring it to a daemon.
- Two credentials: a **Web API token** for all REST calls (search, playback control,
  library, playlists) and a **librespot session** for streaming and Spotify Connect device
  registration.
- **Requires a Spotify Premium account.** Quoting the README: "`spotify_player` requires a
  **Spotify Premium** account". This is the hard gate, and it is Spotify's gate, not the
  tool's: the Web API playback endpoints are Premium-only.
- By default it presents **ncspot's client id**, which the README says is registered in
  Spotify's *extended quota mode* and predates the November 2024 Web API changes, so it
  keeps access to browse and personalised endpoints that newly registered apps lost. The
  README is emphatic that you should **not** register your own client id.
- **[PRIMARY, and this is the integration-critical detail]** "CLI commands communicate with
  a client socket on port `client_port` (default: `8080`). If no instance is running, a new
  client is started, which may increase latency." So the second and subsequent calls are a
  localhost socket round trip to an already-warm process; the first one pays a cold start.
- The README's own scripting example is exactly the pattern hyprsay needs:

  ```sh
  spotify_player playback start track --id $(spotify_player search "$query" | jq '.tracks.[0].id' | xargs)
  ```

  `search` returns JSON. That is the seam where an intelligent layer belongs: you get a
  ranked candidate list back and *then* choose, instead of trusting `[0]`.

### 3.3 Why this is the right shape for hyprsay specifically

**[INFERENCE]** The owner's rejection is of *pre-baked routes*, not of code. A recipes table
is pre-baked because it encodes **the path** ("ctrl+L, wait, type, enter"). `spotify_player
search "<phrase>"` encodes **the capability** ("this app can find things by phrase") and
leaves the choice open. The model never sees a key sequence; it sees a list of real songs
that exist in the user's Spotify and picks one. That is the difference between a macro and
an agent, and it is the whole of the owner's complaint 6.

It also directly serves "play the second song by this artist", which MPRIS cannot touch:
`spotify_player get item artist --name "<artist>"` or `search` gives an ordered list, and
"the second" is then an index into a real list rather than a guess.

**Cost on the target machine:**
- Latency: one localhost socket round trip plus one Spotify Web API call. The API call is
  the floor and it is a transatlantic HTTPS request; the README does not state a number
  and I did not measure one (I did not call the API, per the rules). **[NOT MEASURED]**
  For comparison, this project already measures a Jev round trip from Europe at ~315 ms
  median **[PRIMARY: hyprsay README]**, so a Spotify search is plausibly the same order and
  should be assumed to be a few hundred milliseconds, not tens.
- Money: zero. The Spotify Web API is free at this quota; the Premium subscription is the
  real cost and the owner either has it or this whole lane is closed for Spotify.
- Complexity: one subprocess call and a JSON parse. Around 150 lines for a complete
  adapter including candidate ranking. The `spotify_player` daemon should be kept warm
  (a systemd user unit alongside hyprsay's own) so the cold start is never on the voice path.
- Risk: it is a third-party AUR/community package. If it stops being maintained, the
  fallback is to talk to the Spotify Web API directly, which is the same endpoints minus
  the credential handling.

### 3.4 librespot and go-librespot

**[MEASURED]** Neither `librespot` nor `go-librespot` nor `spotifyd` nor `ncspot` is
installed here; only `spotify` and `spotify-player`.

**[INFERENCE, stated as reasoning not finding]** librespot is a playback backend: it makes
the machine a Spotify Connect target. It solves "where does the audio come out", not
"which song". A hyprsay that wants to play a named song still needs the Web API search
regardless of which backend renders the audio. So librespot is not on the critical path
for this lane, and adding it would be scope the owner did not ask for.

---

## 4. What a typical application actually exposes on the session bus, enumerated here

I swept every well-known name on this session bus for action-style interfaces. This is the
honest picture of how much semantic control a Linux desktop gives you for free.

### 4.1 The sweep

**[MEASURED]** 41 well-known names scanned; object trees walked; each path introspected for
`org.gtk.Actions`, `org.gtk.Application`, `org.freedesktop.Application`,
`org.kde.KDBusService`, `org.mpris.MediaPlayer2.TrackList`, `org.mpris.MediaPlayer2.Playlists`.

Result: **13 names expose `org.freedesktop.Application`**, **11 of those also expose
`org.gtk.Actions`**, **2 expose `org.kde.KDBusService`** (dolphin, kdeconnectd).
**Zero expose TrackList or Playlists.**

The apps that expose it here are: `ca.desrt.dconf`, `dev.hyprvoice.aio`,
`fr.arouillard.waybar`, `org.blueman.Applet`, `org.blueman.Tray`, `org.erikreider.swaync`
(plus `/window/1` and `/window/2` sub-objects), `org.freedesktop.network-manager-applet`,
`org.kde.dolphin-892392`, `org.kde.kdeconnect.daemon`.

### 4.2 `org.freedesktop.Application` is the real cross-toolkit action interface

**[MEASURED]** `busctl --user introspect org.kde.dolphin-892392 /org/kde/dolphin`:

```
org.freedesktop.Application  interface
.Activate                    method  a{sv}
.ActivateAction              method  sava{sv}
.Open                        method  asa{sv}
org.kde.KDBusService         interface
.CommandLine                 method  assa{sv}  i
```

`ActivateAction(string action_name, array<variant> parameter, dict platform_data)` is the
freedesktop D-Bus Activation interface **[PRIMARY: the freedesktop Desktop Entry
Specification, section on D-Bus Activation]**. It is implemented by every GApplication
(GTK) and every KDBusService (KDE Frameworks) app, regardless of toolkit. It is the one
genuinely universal "do a named thing in this app" call on Linux.

Note the discrepancy worth knowing: dolphin exports `org.freedesktop.Application` at
runtime even though its `.desktop` file does not set `DBusActivatable=true`
(see 4.4). **[INFERENCE]** The runtime surface is therefore larger than the static manifest,
so a system that only reads `.desktop` files will under-report what it can drive.

### 4.3 `org.gtk.Actions.DescribeAll` gives runtime enumeration, and it is nearly empty here

`org.gtk.Actions` has `DescribeAll() -> a{s(bgav)}`: for every action, its name, whether
it is enabled, its parameter type signature, and its current state. That is a *self-describing
capability list*, exactly what an adapter system wants.

**[MEASURED]** what the running GTK apps here actually publish:

```
swaync            /org/erikreider/swaync          -> {}            (0 actions)
swaync            /org/erikreider/swaync/window/1 -> {}            (0 actions)
waybar            /fr/arouillard/waybar           -> {}            (0 actions)
nm-applet         /org/freedesktop/network_manager_applet
                                                  -> {'enable-pref': (true, signature 's', [])}   (1 action)
```

**This is the finding that matters, and it is negative.** The mechanism is real, it is
enumerable, it is free, and on a normal Hyprland desktop almost nobody populates it.
GNOME's own applications (Files, Text Editor, Calculator) do populate GActions richly
because their menus are built from them **[SECONDARY: GNOME application development
convention; I have no GNOME app installed here to verify]**, but a Hyprland user's app mix
is browsers, terminals, editors and Electron, and those publish nothing.

### 4.4 `.desktop` action manifests: real, standard, and thin

Every `.desktop` file may declare `Actions=a;b;c` with a `[Desktop Action a]` group giving
a localised `Name` and an `Exec` **[PRIMARY: Desktop Entry Specification, "Additional
applications actions"]**. That is a machine-readable, already-localised, per-app capability
manifest present on every Linux machine at zero cost.

**[MEASURED]** I scanned all four application directories on this machine (249 `.desktop`
files):

```
desktop files scanned : 249
  DBusActivatable=true :   1     (org.kde.spectacle)
  declare Actions=     :  10     (33 actions total)
```

The ten, with their actions:

```
chrome-agimnkij...-Profile_2   Search / Shorts / Subscriptions      (a YouTube PWA)
code                            new-empty-window
com.anthropic.Claude            NewChat / NewCode
com.google.Chrome               new-window / new-private-window
firefox                         new-window / new-private-window / open-profile-manager
google-chrome                   new-window / new-private-window
org.kde.konsole                 NewWindow / NewTab
org.kde.plasma-systemmonitor    overview / history / processes / applications
org.kde.spectacle               FullScreenScreenShot / CurrentMonitorScreenShot /
                                ActiveWindowScreenShot / RectangularRegionScreenShot /
                                WindowUnderCursorScreenShot / RecordRegion
systemsettings                  kcm-lookandfeel / kcm-users / kcm-screenlocker / ...
```

**Honest reading:** 33 actions across 249 apps, and most are "open a new window". The
`.desktop` `Actions=` list is a **launcher menu**, not a control surface. It is worth
reading (it is free, localised, and it is exactly what a right-click on a dock icon shows,
so users already have a mental model for it) but it will not make hyprsay deep.

Where it *does* pay off immediately is `org.kde.spectacle`: six named, parameterless,
D-Bus-activatable screenshot actions. **[INFERENCE]** "take a screenshot of this window"
becomes `ActivateAction("ActiveWindowScreenShot", [], {})` instead of a keystroke, on any
machine with spectacle installed, with zero per-app code.

### 4.5 Qt applications: control of the framework, not of the app

**[MEASURED]** `busctl --user introspect org.kde.dolphin-892392 /MainApplication` exposes
`org.qtproject.Qt.QApplication` (`closeAllWindows`, `setStyleSheet`, cursor flash time,
double click interval), `org.qtproject.Qt.QCoreApplication` (`quit`, `exit`,
`applicationName = "dolphin"`, `applicationVersion = "26.04.3"`) and
`org.qtproject.Qt.QGuiApplication` (`setBadgeNumber`, `desktopFileName = "org.kde.dolphin"`,
`platformName = "wayland"`).

This is Qt's debug/introspection surface, automatic for any `QApplication` with
`QDBusConnection` registration. It gives you **identity** (a trustworthy application name
and `.desktop` id, straight from the process, which a window title cannot give you) and
one dangerous verb (`quit`). It gives you no application semantics.

**[INFERENCE]** The identity part is genuinely useful to hyprsay's safety model: its README
says a window title "can never authorize anything" and a target must be corroborated against
a system `.desktop` file. `org.qtproject.Qt.QGuiApplication.desktopFileName` read from the
process itself is a *stronger* corroboration than a title, obtained over the bus, for free.

### 4.6 `org.freedesktop.FileManager1`: a real cross-desktop semantic interface

**[MEASURED]** dolphin owns `org.freedesktop.FileManager1` and exports:

```
.ShowFolders          method  ass
.ShowItems            method  ass
.ShowItemProperties   method  ass
.SortOrderForUrl      method  s -> ss
```

**[PRIMARY: the freedesktop FileManager1 D-Bus interface]** Any file manager may own this
name. "Show me that file in the file manager" is `ShowItems(["file:///path"], "")`, one
call, no window, no keystroke, and it works whether the file manager is open or not.

**[INFERENCE]** This is the template for what a good adapter looks like: a *capability*
named in the abstract ("show these items"), owned by whichever app currently provides it,
with a stable well-known bus name so the caller never needs to know which app it is.

---

## 5. The web page problem, measured

The owner's complaint 2 is "it cannot click inside web pages. It has no idea what is ON a
page." I measured every route into a page on this machine.

### 5.1 AT-SPI is alive here, which corrects a claim in the code

`hyprsay/src/hyprsay/recipes.py` says, in its module docstring:

> "Measured on the development machine: the AT-SPI registry will not even activate
> (`busctl --address=... org.a11y.atspi.Registry` answers "Could not activate remote peer
> ... unit failed")"

**[MEASURED] That is no longer true on this machine today:**

```
$ busctl --user call org.a11y.Bus /org/a11y/bus org.a11y.Bus GetAddress
s "unix:path=/run/user/1000/at-spi/bus_1"
$ busctl --user get-property org.a11y.Bus /org/a11y/bus org.a11y.Status IsEnabled
b true
$ busctl --user get-property org.a11y.Bus /org/a11y/bus org.a11y.Status ScreenReaderEnabled
b false
```

The registry answers, and `GetChildren` on the root returns 15 entries. Their names:

```
xdg-desktop-portal-gtk, nm-applet, waybar, udiskie, blueman-tray, blueman-applet,
google-chrome, "Google Chrome", google-chrome
```

So the accessibility tree route is open again. The stale comment in `recipes.py` should be
corrected; it is currently the stated justification for the whole key-sequence design.

### 5.2 But Chrome's tree is an empty stub

This is the decisive measurement. I walked all three Chrome AT-SPI roots read-only:

**[MEASURED]**

```
:1.7157  "google-chrome"   children: 0
:1.7158  "Google Chrome"   children: 1
             frame  "about:blank - Google Chrome"   kids = 0
:1.7159  "google-chrome"   children: 0
```

Chrome **registers** with AT-SPI and advertises one frame titled `about:blank`, whose
child list is empty. No document, no links, no buttons, no tab strip. A bounded
breadth-first walk to depth 9 visited exactly 2 nodes.

**[MEASURED]** `tr '\0' ' ' < /proc/1325064/cmdline` -> `/opt/google/chrome/chrome` with no
flags, and there is no `~/.config/chrome-flags.conf`, `~/.config/google-chrome-flags.conf`
or `/etc/chromium-flags.conf` on this machine. `ScreenReaderEnabled` is `false`, which is
the signal Chromium watches to turn its accessibility engine on **[SECONDARY: Chromium's
documented AT-SPI activation behaviour; I did not read the source]**.

**Conclusion, firmly:** on a default Hyprland desktop, the accessibility tree gives you
**nothing whatsoever** inside a web page. Not a degraded view. Nothing. Any plan that says
"use the a11y tree for the browser" is wrong unless it also changes how the browser is
launched, and changing how the user launches their browser is a big ask for a voice tool.

### 5.3 The route that does work here is a browser extension with native messaging

**[MEASURED]** native messaging hosts installed on this machine:

```
/etc/chromium/native-messaging-hosts/org.kde.plasma.browser_integration.json
/etc/opt/chrome/native-messaging-hosts/org.kde.plasma.browser_integration.json
~/.config/google-chrome/NativeMessagingHosts/com.anthropic.claude_browser_extension.json
~/.config/google-chrome/NativeMessagingHosts/com.anthropic.claude_code_browser_extension.json
```

And the KDE bridge is running and working: it publishes the playing page's **exact URL** on
MPRIS (section 2.4), and it exports two KRunner providers:

**[MEASURED]** `busctl --user tree org.kde.plasma.browser_integration`:

```
/HistoryRunner
/TabsRunner
/org/mpris/MediaPlayer2
```

**[MEASURED]** `busctl --user introspect org.kde.plasma.browser_integration /TabsRunner`:

```
org.kde.krunner1  interface
.Actions   method  -   -> a(sss)
.Match     method  s   -> a(sssuda{sv})
.Run       method  ss
.Teardown  method
```

**[MEASURED]** `Actions()` answers:

```
([('MUTE', 'Mute Tab', 'audio-volume-muted'), ('UNMUTE', 'Unmute Tab', 'audio-volume-high')],)
```

So the interface is live and responding. **[MEASURED, negative]** `Match("youtube")`,
`Match("dino")` and `Match("chrome")` each returned an empty array here, even though a
YouTube tab is demonstrably open (MPRIS knows its title and URL). I could not determine why
from read-only inspection; plausible causes are the extension's tab permission or the
runner being disabled in the plugin config, and I am not going to claim which.

**[PRIMARY]** `/usr/share/krunner/dbusplugins/plasma-runner-browsertabs.desktop` shows how a
provider declares itself:

```
X-Plasma-API=DBus
X-Plasma-DBusRunner-Service=org.kde.plasma.browser_integration*
X-Plasma-DBusRunner-Path=/TabsRunner
X-Plasma-Request-Actions-Once=true
X-Plasma-Runner-Syntaxes=:q:
X-Plasma-Runner-Syntax-Descriptions=Finds open browser tabs whose title or URL match :q:
Comment=Find and activate browser tabs
```

That file is a **capability manifest in the form hyprsay needs**: a bus name, an object
path, a declared query syntax, and a one-line natural-language description of what the
provider can serve. Read section 7.

There is also `/usr/share/krunner/dbusplugins/kwin-runner-windows.desktop`,
`plasma-runner-browserhistory.desktop`, `plasma-runner-baloosearch.desktop` and
`plasma-runnners-activities.desktop` installed here, and
`/usr/share/gnome-shell/search-providers/firefox.search-provider.ini`.

---

## 6. Everything else the lane asked about, short and honest

### 6.1 Wayland and the portals

**[PRIMARY: hyprsay's own inherited README]** `xdg-desktop-portal-hyprland` does not
implement the RemoteDesktop portal, so anything built on portals or libei for *input*
degrades on Hyprland. That is settled and hyprsay already avoids it.

For *semantics*, the portals give almost nothing: `org.freedesktop.portal.OpenURI` opens a
URI in the user's default handler, the file chooser and screenshot portals are dialogs. A
portal is a sandbox escape hatch, not an application control surface. **[INFERENCE]** There
is no portal path to in-app control and there will not be one, because the portal design
goal is the opposite (isolating apps from each other).

### 6.2 Emacs and vim servers

**[MEASURED]** Neither `emacs` nor `emacsclient` is on this machine's bus; I did not find
either installed in the package sweep I ran.

**[PRIMARY, general knowledge of the mechanism, verifiable by anyone]** `emacsclient --eval
'<elisp>'` against `emacs --daemon` is the single most powerful application-control
mechanism that exists on Linux: arbitrary evaluation in the app's own language, full
introspection, synchronous result. Vim/Neovim's equivalent is the msgpack-RPC socket at
`$NVIM` (`nvim --server <path> --remote-expr`), which is similarly total.

**[INFERENCE]** These are the proof that the adapter idea works and the warning about where
it ends: an adapter for an app with a scripting server is trivially deep and trivially
dangerous, because "do anything" is exactly what it offers. In hyprsay's tier model an
Emacs adapter cannot be tier 0 or 1 no matter how convenient it is.

### 6.3 GNOME and KDE D-Bus interfaces

**[MEASURED]** No GNOME Shell on this machine (no `org.gnome.Shell` on the bus). KDE is
partially present: `kded6` is running with `org.kde.plasma.browser.integration`,
`org.kde.KScreen`, `org.kde.kappmenu`, `org.kde.plasmanetworkmanagement`; `org.kde.krunner`
is activatable but no Plasma shell is running.

**[INFERENCE]** For a Hyprland user, "the KDE D-Bus interfaces" mostly means the KDE
*framework* services a user happens to have installed (spectacle, dolphin, kdeconnect,
the browser bridge), not a shell. Those are worth adapters individually; there is no
Plasma-wide control surface to lean on. The same for GNOME: hyprsay cannot assume
`org.gnome.Shell` exists.

`org.kde.kdeconnect.daemon` is on the bus here **[MEASURED]** and is a genuinely rich
semantic surface (send a file to the phone, find my phone, share a URL, run a remote
command) that a voice tool would enjoy, but it is off this lane's path.

---

## 7. Per-application adapters: what a good one looks like

This is the second half of the lane. The question is not "how do we write adapters" but
"how does the system decide which adapter serves an utterance, without a hardcoded table".

### 7.1 The prior art that is actually on this machine

Two production systems answer exactly this question with the same protocol shape.

**KDE: `org.kde.krunner1`** **[MEASURED, introspected above]**
```
Match(s query)          -> a(sssuda{sv})   id, text, icon, type, relevance, properties
Run(s matchId, s actionId)
Actions()               -> a(sss)          id, text, icon
Teardown()
```
Registration is a file in `/usr/share/krunner/dbusplugins/` naming the bus name, the object
path, a query syntax and a human-readable description **[PRIMARY, quoted in 5.3]**.

**GNOME: `org.gnome.Shell.SearchProvider2`** **[PRIMARY: the interface is a published GNOME
Shell D-Bus API; on this machine `/usr/share/gnome-shell/search-providers/firefox.search-provider.ini`
is installed, which is the registration half of it]**
```
GetInitialResultSet(as terms)          -> as  result ids
GetSubsearchResultSet(as prev, as terms) -> as
GetResultMetas(as ids)                 -> aa{sv}  id, name, description, icon
ActivateResult(s id, as terms, u timestamp)
LaunchSearch(as terms, u timestamp)
```

The two designs agree on every important point, which is strong evidence that the shape is
right:

1. **A provider is a separate process** that already owns the domain. It is not a plugin
   loaded into the shell. It cannot crash the caller and it needs no permissions the app
   does not already have.
2. **The contract is two calls: match, then activate.** Matching is read-only and cheap and
   returns *candidates with relevance*. Acting takes an opaque id the provider itself
   minted. The caller never constructs an action; it picks one the provider offered.
3. **Registration is a declarative file**, not code in the caller. Adding an app means
   dropping a file, not editing the shell.
4. **The provider ranks its own matches** because only it knows its domain. The caller
   merges ranked lists across providers.
5. **Subsearch refinement** (GNOME's `GetSubsearchResultSet`) lets a provider narrow its own
   previous result set as the user keeps talking. That is directly relevant to the owner's
   complaint 5 about acting while the person is still speaking.

### 7.2 The other prior art the lane named

**Talon** **[SECONDARY; I did not have Talon to inspect and did not verify source]** scopes
commands by application context: a `.talon` file carries a header like `app: firefox` or
`win.title: /Gmail/` and its commands only exist while that context matches. The important
structural idea is **context as a predicate on the live window**, with the grammar itself
being partitioned by it, so the recognizer's search space shrinks to what the focused app
can do. This is a good idea hyprsay can copy with no model involved: the set of *offerable*
intents is a pure function of the focused window.

**Serenade** **[SECONDARY]** used a per-editor plugin that exposed the editor's own buffer
and selection to the speech engine, rather than driving the editor by keystrokes. Same
lesson as Emacs: where the app has a real API, use it and you get semantics for free.

**Apple Shortcuts / App Intents** **[VENDOR: Apple's developer documentation]** is the most
complete version of this idea in production. An app declares an `AppIntent`: a typed
parameter list, a title, and crucially `AppShortcutPhrases` (natural-language phrase
templates with parameter slots), plus `AppEntity` types with a `DefaultEntityQuery` so the
system can *resolve a spoken phrase to one of the app's objects*. The system, not the app,
does the disambiguation dialogue when a phrase matches several entities. The three-part
structure is the thing to steal: **capability (intent) + typed parameters + a query that
turns spoken text into the app's own objects.**

**Home Assistant** **[SECONDARY/PRIMARY: its intent architecture is public]** splits the
same way: *intents* are abstract (`HassTurnOn`), *entities* are the concrete things, and an
intent handler per domain knows how to serve the intent for its domain. Sentence templates
map speech to intent plus slots; the handler resolves slots against the live entity
registry. Its most transferable idea is the **area/context slot**: an utterance with no
explicit target inherits the target from where the speaker is. That is the direct analogue
of the owner's complaint 3, where saying an app's name should mean "the one already open",
resolved from context rather than defaulted to "make a new one".

**GNOME Shell extensions** **[INFERENCE]** are the counter-example worth naming: they are
in-process JavaScript with full access to the shell, which makes them powerful and makes
them the reason GNOME extensions break on every release. Do not copy this. The D-Bus
provider model in 7.1 is GNOME's *own* answer for third parties, and it is the one that
survived.

### 7.3 What the adapter interface should be for hyprsay

**[INFERENCE, this is a design proposal, not a finding]**

Synthesising 7.1 and 7.2, an adapter is a small object with four members:

```
can_serve(context) -> bool
    Pure, local, no network. A predicate over the live desktop: is this app's process
    running, does it own its bus name, is its window focused, is the required binary
    installed. This is what fixes complaint 3: the adapter for "Spotify" knows whether
    Spotify is already running and where, before anything is launched.

capabilities(context) -> list[Capability]
    Each capability has: a stable id, a tier (hyprsay already has 0..3), a typed parameter
    list, and a one-line natural-language description. This list is what gets offered to
    Jev as a CHOICE over up to 255 options, which is exactly the question type Jev answers.
    The model picks a capability id; it never writes a command.

resolve(capability, slots, context) -> list[Candidate]
    The match half of the KRunner/SearchProvider contract. Turns "the second song by this
    artist" into real candidates from the app's own domain, each with a display name and
    an opaque handle. Read-only. This is where spotify_player search lands, where
    TabsRunner.Match lands, where "which of your open windows" lands.

perform(capability, candidate_handle, context) -> Outcome
    The activate half. Takes only a handle the adapter itself minted. Never takes free text
    from the model.
```

The two-phase split is the whole safety argument, and it maps onto hyprsay's existing
tier system without changing it. `resolve` is always tier 0 because it only reads.
`perform` carries the capability's tier. The model's output is constrained to a capability
id plus a candidate index, which are both choices over enumerated sets, which is precisely
the shape Jev was built for.

**How the system decides which adapter serves an utterance.** Not by asking a model to
route. Three stages, cheapest first:

1. **Filter by `can_serve`.** Pure local predicates over the live desktop. On this machine
   that is a handful of adapters, not forty.
2. **Union the surviving adapters' capabilities into one candidate list**, each with its
   one-line description. This is a choice over a few dozen options.
3. **One Jev choice question over that list.** The options are real, they are built from
   the live desktop, and the model cannot invent one. This is the same discipline hyprsay
   already applies to window selection.

**[INFERENCE]** Note what this deletes: the string split on the literal word "and"
(complaint 1) becomes unnecessary for the common case, because a capability with typed
parameters absorbs "open chrome and go to youtube" as one capability (`open_url`) with one
slot, rather than two clauses. Compounds that really are two capabilities become a
*sequence* question, which is still a choice over enumerated capability ids.

### 7.4 Which adapters are worth writing first, on the evidence

Ranked by (owner's stated pain) x (mechanism actually exists on this machine):

1. **Spotify, via `spotify_player`.** The owner's own example. Fully semantic. Installed.
   Requires Premium and a one-time `spotify_player authenticate`. Section 3.
2. **Media, via MPRIS/playerctl.** Trivial, instant, already a dependency, and it gives
   *context* (what is playing, in which app, at which URL) that improves resolution for
   many other utterances. Section 2.4.
3. **Windows and workspaces, via the existing hyprctl layer.** Reframed as an adapter with
   `can_serve` and a `resolve` that returns *existing* windows before any launch. This is
   the direct fix for complaint 3 and needs no new mechanism at all, only the ordering
   change: resolve before launch, always.
4. **Browser, via a native-messaging extension.** The only route into page content on this
   machine (5.2 proves the a11y route is dead, 5.3 proves the extension route works and is
   already installed twice over). This is the largest piece of work in the lane and the
   only one that needs a component that does not exist yet.
5. **File manager, via `org.freedesktop.FileManager1`.** Four methods, cross-desktop,
   already owned by dolphin here. Cheap win.
6. **Screenshots, via `org.kde.spectacle`'s six declared `.desktop` actions.** Free, if
   spectacle is installed.

---

## 8. What I could not establish

- Spotify's official client's `OpenUri` scheme support: not verified, client not running,
  and the binary is stripped. The MPRIS interface list from the binary is solid; the
  `OpenUri` behaviour is not.
- Why `TabsRunner.Match` returns empty on this machine while the same bridge's MPRIS works.
- Any Spotify Web API latency number from this machine: I did not call the API.
- Whether `spotify_player`'s Arch build includes the `daemon` feature (it is off by default
  upstream); `spotify_player features` would say, but running it starts a client.
- Talon and Serenade internals: no installation here, and I did not read their source.

---

## 9. Where this leaves the owner's six complaints

| # | Complaint | What this lane found |
|---|---|---|
| 1 | Split on the literal "and" | Dissolved by typed capabilities with parameter slots; compounds become a choice over capability ids, not a string split. 7.3. |
| 2 | Cannot click in web pages | The a11y route is measurably dead (5.2). The native-messaging extension route works and is already installed on this machine (5.3). This is the one piece that must be built. |
| 3 | Ignores context, opens a new window | Fixed by adapter ordering: `can_serve` and `resolve` see the live desktop before `perform` ever launches. Home Assistant's area-slot pattern is the precedent. 7.2, 7.3. |
| 4 | Nowhere near intent | The model choosing among real candidates from real apps is a different problem from the model choosing among hardcoded routes. 7.3. |
| 5 | Should act while speaking | `resolve` is read-only and cheap, so it can run on partial text and refine, exactly like `GetSubsearchResultSet`. 7.1, point 5. |
| 6 | Deep in-app ability, no pre-baked routes | `spotify_player playback start track --name` is the existence proof that the deep route exists and is not a key sequence. 3. |

### 9.1 Complaint 3, located exactly in the code

**[PRIMARY: read in the repository today]** The "opens a new one instead of going to the
existing one" behaviour is one branch, and it is a routing decision made before the desktop
is consulted.

`src/hyprsay/nlu/understand.py:450`:

```python
        if intent is Intent.LAUNCH_APP:
            return self._launch_locally(parse, phrase, template, heard, state)
```

`_launch_locally` (line 694 onward) resolves `phrase` against the **installed application
lexicon** and builds `Action(Intent.LAUNCH_APP, app=app, window=None)`. It never asks
`state` whether a window of that app is already open. The verb the speaker used ("open",
"launch", "start", all in the same verb set at `src/hyprsay/nlu/resolve.py:33` alongside
"go", "focus" and "show") decides the intent, and the live desktop never gets a vote.

**[INFERENCE]** This is not a model failure and Jev cannot fix it, because the grammar
already committed to `LAUNCH_APP` before any model is consulted. The fix is the adapter
ordering from 7.3: `resolve` runs first and returns *both* existing windows and the
launchable app as candidates of one list, and only if no window exists does `perform`
launch. That is Home Assistant's implicit-area slot (10.8) applied to windows.

---

## 10. Appendix A: verified interface definitions

Everything here I fetched from the defining source during this lane, or introspected on
this machine. Nothing is reconstructed from memory.

### 10.1 MPRIS TrackList, the interface Spotify does not implement

**[PRIMARY: freedesktop MPRIS specification, `Track_List_Interface.html`]**

```
GetTracksMetadata (ao TrackIds) -> aa{sv} Metadata
AddTrack    (s Uri, o AfterTrack, b SetAsCurrent)
RemoveTrack (o TrackId)
GoTo        (o TrackId)
properties: Tracks (ao, read only), CanEditTracks (b, read only)
```

This is the *closest* MPRIS ever gets to "pick a track", and note what it still is not: it
lists "a short list of tracks which were recently played or will be played shortly". It is
a queue view. There is no search anywhere in MPRIS, in any interface, optional or not.
Combined with the binary evidence in 2.3, the conclusion holds: no D-Bus route to
"pick a song".

### 10.2 The Spotify Web API calls an adapter would make

**[PRIMARY: Spotify Web API reference, fetched 2026-09-22]**

```
GET  https://api.spotify.com/v1/search
     required query params: q (supports field filters artist:, album:, track:, year:,
     genre:, isrc:, upc:, tag:new, tag:hipster) and type (album|artist|playlist|track|
     show|episode|audiobook, comma separated)
     optional: market, limit, offset, include_external
     auth: OAuth 2.0

PUT  https://api.spotify.com/v1/me/player/play
     scope: user-modify-playback-state
     account: Spotify Premium only
     body: context_uri (album/artist/playlist), uris (array of track URIs),
           offset (object, "indicates from where in the context playback should start",
                   only valid when context_uri is an album or playlist),
           position_ms
     query: device_id (optional; defaults to the user's currently active device)
     success: 204 No Content
```

**[INFERENCE]** `offset` inside a `context_uri` is literally the mechanism for "play the
second song on that album" or "the second song in that playlist". For "the second song by
this artist" (an artist context has no stable ordering the API guarantees), the honest path
is `GET /v1/search?q=artist:<name>&type=track` or the artist's top tracks, then `uris` with
the chosen track. Either way the pattern is identical: **search returns an ordered list,
the ordinal in the utterance indexes that list, and the play call names an exact URI.**
None of this is a key sequence and none of it is pre-baked.

Note the `q` field filters. `artist:Radiohead track:Creep` is the API's own structured
query language, which means an adapter's `resolve` step can turn a parsed utterance into a
*precise* query rather than a bag of words. That is a real quality lever.

### 10.3 `org.freedesktop.Application`, verbatim

**[PRIMARY: freedesktop Desktop Entry Specification, D-Bus Activation section]**

```xml
<method name='Activate'>
  <arg type='a{sv}' name='platform_data' direction='in'/>
</method>
<method name='Open'>
  <arg type='as'    name='uris'          direction='in'/>
  <arg type='a{sv}' name='platform_data' direction='in'/>
</method>
<method name='ActivateAction'>
  <arg type='s'     name='action_name'   direction='in'/>
  <arg type='av'    name='parameter'     direction='in'/>
  <arg type='a{sv}' name='platform_data' direction='in'/>
</method>
```

The spec states that an application supporting D-Bus launching must implement this
interface with a desktop file name matching its D-Bus service name, and that
`ActivateAction` corresponds to the Desktop Actions of the spec's "Additional applications
actions" section. This exactly matches what I introspected on dolphin (4.2).

### 10.4 `org.gnome.Shell.SearchProvider2`, verbatim

**[PRIMARY: the interface XML as published in GNOME's search provider documentation and
example provider]**

```xml
<interface name="org.gnome.Shell.SearchProvider2">
  <method name="GetInitialResultSet">
    <arg type="as" name="terms"            direction="in" />
    <arg type="as" name="results"          direction="out" />
  </method>
  <method name="GetSubsearchResultSet">
    <arg type="as" name="previous_results" direction="in" />
    <arg type="as" name="terms"            direction="in" />
    <arg type="as" name="results"          direction="out" />
  </method>
  <method name="GetResultMetas">
    <arg type="as"     name="identifiers"  direction="in" />
    <arg type="aa{sv}" name="metas"        direction="out" />
  </method>
  <method name="ActivateResult">
    <arg type="s"  name="identifier"       direction="in" />
    <arg type="as" name="terms"            direction="in" />
    <arg type="u"  name="timestamp"        direction="in" />
  </method>
  <method name="LaunchSearch">
    <arg type="as" name="terms"            direction="in" />
    <arg type="u"  name="timestamp"        direction="in" />
  </method>
</interface>
```

Three details worth stealing wholesale:

- **`terms` is `as`, an array of words, not a string.** The provider receives the tokens,
  not a sentence. That is the right boundary for a voice system too: the router owns the
  sentence, the adapter owns its domain's vocabulary.
- **`GetInitialResultSet` returns only ids.** Metadata is a *separate* call for only the ids
  the caller decided to show. Two round trips by design, so a provider that matches a
  thousand things does not serialise a thousand descriptions. For hyprsay this maps to:
  resolve cheaply, describe only the handful you will offer to Jev.
- **`GetSubsearchResultSet` takes the previous result set.** The provider narrows its own
  earlier answer instead of starting over. This is the mechanism for acting while someone
  is still speaking (complaint 5), and it is a protocol feature, not an optimisation.

### 10.5 `org.kde.krunner1`, verbatim from this machine

**[MEASURED]** (5.3) plus **[SECONDARY]** for the field meanings of the match tuple:

```
Match(s query) -> a(sssuda{sv})
        id, text, iconName, type (u), relevance (d), properties (a{sv})
Run(s matchId, s actionId)
Actions() -> a(sss)      id, text, iconName
Teardown()
```

A real wrinkle worth knowing before implementing: **the published interface says the type
field is `u` but real runners answer with `i`.** I hit this on this machine: introspection
reported `a(sssuda{sv})` and the actual reply from `TabsRunner` was `a(sssida{sv})`
**[MEASURED]**, and this signed/unsigned discrepancy is a known reported KDE bug
**[SECONDARY]**. A client must be lenient about it.

### 10.6 Talon's context and tag model

**[SECONDARY: the Talon community wiki]**

Context header syntax: `[and] [not] <requirement>: (<literal match> | /<regex>/<flags>)`,
with `app:`, `title:` and `tag:` as requirements. Same-type lines OR together, different
types AND together. Below the dash, `tag(): user.my_tag` activates a tag when the header
matches.

**This is the important architectural idea and it costs nothing to copy.** A generic
command file is gated on a *tag*, and each application's context file simply declares which
tags it supports. So "next tab" is written once, against `tag: user.tabs`, and Firefox,
Chrome and a terminal multiplexer each say `tag(): user.tabs` when focused. The table of
per-app key sequences disappears into a table of *which capabilities each app claims*,
which is a much smaller and much more extensible thing.

**[INFERENCE]** hyprsay's `recipes.py` already half-invented this: it keys on a derived app
*kind* rather than a class, for exactly the reason Talon uses tags ("keying on class would
need one entry per browser and would still miss the next browser the owner installs", its
own docstring). The difference is that a Talon tag is *declared by the app context*, and
can be many per app, while a hyprsay kind is one label the lexicon guesses. Moving from
"one kind per window" to "a set of capability tags per window" is a small refactor with a
large payoff.

### 10.7 Apple App Intents

**[VENDOR: Apple developer documentation and WWDC sessions]**

The pieces, named as Apple names them:

- **`AppIntent`**: one capability, with `@Parameter` properties, a title and a `perform()`.
- **`AppEntity`**: a type representing one of the app's own objects, usable as a parameter.
- **`EntityQuery`**: how the system finds entities; `entities(for:)` takes identifiers and
  returns entities. **`EntityStringQuery`** is the variant that resolves entities **by a
  string the user said**, and `suggestedEntities()` supplies the browsable list.
- **`AppShortcutsProvider`** returns **`AppShortcut`**s, each carrying `phrases` with
  interpolation, for example `"Navigate to \(\.$navigationOption) in \(.applicationName)"`.

**[INFERENCE]** The generalisable lesson for hyprsay: Apple does **not** ask the model to
produce a command. It asks the app to declare (a) its capabilities, (b) its object types,
and (c) *a way to turn a spoken string into one of its objects*. The disambiguation dialogue
when a phrase matches several entities is run by the **system**, from the app's own
candidate list. That is the same two-phase resolve/perform contract as GNOME and KDE, from
a completely different tradition, which is about as strong a convergence argument as this
kind of design question ever gets.

### 10.8 Home Assistant

**[SECONDARY: Home Assistant developer documentation for intent recognition and hassil]**

Sentence templates in HassIL YAML map speech to an intent plus slots. Two slot lists are
built in and supplied by Home Assistant at recognition time: **`<name>`** (an entity name)
and **`<area>`**, with `floor` also available. A handler per domain serves the abstract
intent (`HassTurnOn`) against the live entity registry. Hassil returns the *first* match,
so ambiguity has to be resolved by adding context.

**[INFERENCE]** The `<area>` slot is the pattern that fixes the owner's complaint 3. In
Home Assistant, "turn on the light" said in the kitchen means the kitchen light, because
the area is an implicit slot filled from where the speaker is. hyprsay's equivalent
implicit slots are: the focused window, the active workspace, the most recently used window
of that app, and (newly available, 2.4) what is currently playing. Saying an app's name
should fill "which window" from those before anything is launched.

---

## 11. Appendix B: measured costs on this machine

All read-only, on the i5-8350U, on a live session with 8 windows open.

| mechanism | measured | note |
|---|---|---|
| MPRIS `GetAll` on `org.mpris.MediaPlayer2.Player` via `busctl` | 9, 9, 13, 13, 13 ms | dominated by process spawn; an in-process D-Bus client is sub-millisecond |
| `playerctl metadata` one field | 12, 14, 23, 29, 31 ms | same, plus playerctl's own startup |
| `spotify_player --help` (process start floor) | 16, 22, 22 ms | 32 MB binary; this is the spawn cost only, no network |
| AT-SPI walk of the whole Chrome tree | 2 nodes, under 0.1 s | because the tree is empty (5.2) |
| Jev round trip from Europe | ~315 ms median | **[PRIMARY: hyprsay README]**, for comparison |
| local Parakeet decode of a 1 s command | 93 ms idle | **[PRIMARY: hyprsay README]**, for comparison |

**[NOT MEASURED]** Any Spotify Web API call. I did not call it, per the rules, and there is
no cached credential on this machine to call it with (see below). Treat a search as a
transatlantic HTTPS round trip and budget it like the Jev call, not like a D-Bus read.

**[MEASURED]** `~/.config/spotify-player` and `~/.cache/spotify-player` **do not exist**.
So `spotify_player` has never been run here: the plan must include a one-time
`spotify_player authenticate` (two browser OAuth approvals, per 3.2) before the Spotify
adapter can work at all, and that step needs a Premium account.

**[INFERENCE] Budget for a full "play X on Spotify" utterance**, with a warm client
process and the daemon already authenticated:

```
key release
  -> local Parakeet decode                ~93 ms   [measured, existing]
  -> grammar or one Jev capability choice  1 ms or ~315 ms
  -> adapter resolve: spotify_player search
         process spawn ~20 ms  +  Spotify Web API round trip (unmeasured, assume 200-500 ms)
  -> one Jev choice over the returned candidates, or grammar ordinal   ~315 ms or 1 ms
  -> adapter perform: playback start track --id   (spawn + API, again a few hundred ms)
```

That is roughly **0.6 s best case and around 1.5 s** if both model questions are asked.
**[INFERENCE]** The two levers that matter are (a) keeping a persistent `spotify_player`
client so the socket is warm rather than spawning, and (b) starting `resolve` on partial
speech so the search is already in flight when the key is released. Both are architecture,
not optimisation, and both are what section 7.3's read-only `resolve` is designed to allow.

---

## 12. The single sentence to take away

Stop writing key sequences and start writing **adapters with a read-only `resolve` and an
acting `perform`**, because that is independently what GNOME, KDE, Apple, Talon and Home
Assistant all converged on, and because on this exact machine the one command that already
answers the owner's own example is `spotify_player playback start track --name`, which is a
`resolve` and a `perform` and not a keystroke.


## Verification

Skeptic pass, 2026-09-22. Method: re-check the six findings a plan would lean on hardest,
each with a DIFFERENT source or a DIFFERENT check than the researcher used. In progress.

### V1. Spotify's Linux client is transport-only: CONFIRMED, and by a stronger method

The researcher's evidence was the *absence* of the strings `org.mpris.MediaPlayer2.TrackList`
and `org.mpris.MediaPlayer2.Playlists`. Absence-of-string is weak evidence on its own: a
binary can build an interface name by concatenation at runtime, and `strings` misses
anything compressed or in a sibling library. Both holes are real here, and both close.

**[MEASURED, different check]** The substring `TrackList` alone *does* occur 5 times in
`/opt/spotify/spotify`:

```
Failed to convert TrackListQuery to collection query
HasTrackList
!CollectionOfflineTrackListRequest
"CollectionOfflineTrackListResponse
spotify.collection_cosmos.proto.CollectionOfflineTrackListRequestidsortfilter
```

None of these is a D-Bus interface name. `HasTrackList` is the boolean property on the
root interface that the spec requires a player to publish, and Spotify publishes it, which
is precisely a player declaring it does *not* implement TrackList.

**[MEASURED, and this is the positive proof the researcher did not have]** The binary
contains a hand-rolled MPRIS dispatcher whose error strings enumerate the interfaces it
knows:

```
Unrecognized or unimplemented org.mpris.MediaPlayer2 method '%s'
Unrecognized or unimplemented org.mpris.MediaPlayer2.Player method '%s'
Unrecognized or unimplemented org.mpris.MediaPlayer2 property '%s'
Unrecognized or unimplemented org.mpris.MediaPlayer2.Player property '%s'
Received MPRIS MediaPlayer2.Player 'Get' request, but no metadata is available
```

Two interfaces are dispatched. A third would need a third message. That is evidence *for*
the ceiling, not merely absence of evidence against it.

**[MEASURED]** Spotify's Linux client is a CEF application (`/opt/spotify/libcef.so`,
249 MB; `Apps/xpui.spa`, `Apps/login.spa`). I scanned `libcef.so` separately in case the
MPRIS implementation lived there: it yields `org.mpris.MediaPlayer2`,
`org.mpris.MediaPlayer2.Player` and `org.mpris.MediaPlayer2.chromium.instance` and nothing
more. That is Chromium's own unused MPRIS, and it has the same ceiling.

**Verdict: CONFIRMED.** The conclusion stands and the reasoning behind it should be
replaced with the dispatcher strings, which are much harder to argue with.

### V2. `spotify_player` CLI surface: CONFIRMED, but `--name` is the blind `[0]` the report warns against

**[MEASURED]** `spotify_player --version` -> `spotify_player 0.24.1`; `pacman -Qi
spotify-player` -> `0.24.1-1`, built 2026-07-20, installed 2026-08-13, upstream
`https://github.com/aome510/spotify-player`, packaged by an Arch Linux staff packager
(Orhun Parmaksiz), not an AUR drop. The report calls it "a third-party AUR/community
package"; it is in the official `extra` repository with a named Arch packager, which is a
materially lower supply-chain risk than the report implies. The CLI help text reproduces
exactly as quoted.

**[PRIMARY, upstream source at tag v0.24.1, `spotify_player/src/cli/client.rs`]** The
important thing the report did not check is what `--name` actually does. It calls
`client.search_specific_type(&name, SearchType::Track)` and then takes:

```rust
if !page.items.is_empty() && page.items[0].id.is_some() {
    ItemId::Track(page.items[0].id.clone().unwrap())
} else {
    anyhow::bail!("Cannot find track with name='{name}'");
}
```

Corroborated locally: **[MEASURED]** the binary contains the literals `Cannot find track
with name='`, `Cannot find album with name='`, `Cannot find artist with name='` and
`Cannot find playlist with name='`.

So `--name` is a global Spotify search that discards everything except result zero. The
report elsewhere says, correctly, "that is the seam where an intelligent layer belongs:
you get a ranked candidate list back and *then* choose, instead of trusting `[0]`", and
then its own headline claim recommends the code path that trusts `[0]`. A plan that wires
`playback start track --name "<phrase>"` to speech has reinstated a blind heuristic under
a nicer name.

**Verdict: CONFIRMED on the mechanism, OVERSTATED as an answer to complaint 6.** The
usable path is `spotify_player search "<query>"` (JSON, ranked, multiple types), then the
model or a ranker picks an id, then `playback start track --id <id>`. That is also the
only shape that can serve "the second one" or "the live version".

### V3. The Premium gate and the unauthenticated state: CONFIRMED, and the gate is Spotify's own

The report sourced Premium to the packaged README. I checked the vendor instead.

**[PRIMARY: Spotify Web API reference, "Start/Resume Playback", fetched 2026-09-22]**
`PUT /me/player/play`, scope `user-modify-playback-state`, success `204 No Content`, body
fields `context_uri`, `uris`, `offset`, `position_ms`, `device_id`, and the flat sentence:
"This API only works for users who have Spotify Premium." So the gate is not
`spotify_player`'s; it is the endpoint's, and the direct-Web-API fallback the report
proposes inherits it unchanged. If the owner is on the free tier, this entire lane is shut
for Spotify and no amount of adapter design reopens it. That should be the first question
asked of the owner, before any code.

**[MEASURED, independent of `ls`]** `ss -ltnp` shows no listener on 8080 or 8989, and
neither `~/.config/spotify-player` nor `~/.cache/spotify-player` exists. Never run, never
authenticated. Two separate browser OAuth approvals are required (Web API token under
ncspot's client id, librespot session under Spotify's own), per the shipped README.

Two costs the report understates:
- The "warm client" on `client_port` 8080 is not a thin helper. It is the full
  `spotify_player` application, which starts a **librespot streaming session** and
  registers this laptop as a **Spotify Connect device**. Keeping it warm changes what
  appears in the owner's device list on every other Spotify client they own.
- 8080 is the single most contended port on a developer laptop. A voice daemon that
  silently depends on it will fail in a way that looks like "Spotify is broken".

**Verdict: CONFIRMED, with two costs the plan must carry.**

### V4. Chrome's accessibility tree is empty: CONFIRMED, and worse than reported

The report measured three Chrome AT-SPI roots, one advertising a frame titled
`about:blank - Google Chrome` with zero children, and a depth-9 walk that visited 2 nodes.

**[MEASURED, re-run now, same bus, different queries]** The registry root today has 13
child entries resolving to 7 unique bus names, and exactly one of them is Chrome:

```
:1.0    xdg-desktop-portal-gtk   ChildCount 0
:1.1    nm-applet                ChildCount 0
:1.2    waybar                   ChildCount 1
:1.3    udiskie                  ChildCount 0
:1.4    blueman-tray             ChildCount 0
:1.5    blueman-applet           ChildCount 0
:1.7157 google-chrome            ChildCount 0
```

Chrome's ChildCount is now **0**, with no frame at all, while `hyprctl clients` shows the
same Chrome pid 1325064 owning three real windows: `Research Hypruse Online - Google
Chrome`, a YouTube app window in Profile 2, and `about:blank - Google Chrome`. So the tree
is not merely thin, it does not even track the windows that exist.

**Verdict: CONFIRMED, and the negative is stronger than stated.**

### V5. "The AT-SPI registry is alive, so recipes.py is stale": CONFIRMED on the letter, OVERSTATED on the consequence

**[MEASURED]** The quoted docstring is accurate: `/home/ilyask/projects/hyprsay/src/hyprsay/recipes.py`
lines 5 to 8 do say the registry "will not even activate".

**[MEASURED, a source the report did not use]** The registry is alive for a durable,
configured reason, not a transient one: `gsettings get org.gnome.desktop.interface
toolkit-accessibility` -> `true`, and `at-spi-bus-launcher`, `at-spi2-registryd` and the
`dbus-broker-launch` for `accessibility.conf` have all been up since Mon Sep 21 14:02:15,
about 18 hours. Nothing in this session started them. So the stale-comment half of the
finding is solid.

**But the consequence does not follow.** The finding says "The premise no longer holds for
GTK and Qt apps". Nothing measured here shows that. Of the seven applications on the tree,
six are a portal backend and tray or panel icons, five of which have `ChildCount 0`; the
sixth, waybar, has one child. There is **not one GTK or Qt application window** on the
tree. The desktop's actual windows are six `kitty` terminals, which do not speak AT-SPI at
all, plus Chrome, which stubs. The registry being reachable and the registry being useful
are different claims, and only the first is measured.

The same docstring carries a second stale number worth fixing in the same diff: it says
"234 files in /usr/share/applications". **[MEASURED]** `/usr/share/applications` holds
exactly 234 `.desktop` files today, so that one is still right; the report's own "249" is
the count across four directories, which is 252 today. Both numbers are fine, they just
measure different sets, and the plan should not treat the drift as meaningful.

**Verdict: CONFIRMED that the registry is alive and the comment is stale. OVERSTATED that
this reopens the a11y route for applications: no application was tested.**

### V6. The KDE browser bridge: CONFIRMED live, and I found the answer the report could not

The report reached `org.kde.krunner1` on `/TabsRunner`, got `Actions()` back, and then got
empty arrays for `Match("youtube")`, `Match("dino")` and `Match("chrome")`, saying "I could
not determine why from read-only inspection".

**[MEASURED]** I probed the same method with different terms:

```
Match("about") -> [('1321207040', 'about:blank', 'google-chrome', 70, 0.95,
                   {'actions': <@as []>, 'subtext': <'about:blank'>,
                    'urls': <['about:blank']>})]
Match("blank") -> the same one result, relevance 0.90
Match("youtube")  -> []
Match("dino")     -> []
Match("research") -> []
Match("")         -> Error org.freedesktop.DBus.Error.InvalidArgs: "Search term too short"
```

The runner is not broken and the permission is not missing. **Its entire tab index on this
machine is one tab, `about:blank`.** The three queries the report tried all failed honestly,
because nothing matching them is in the index. The YouTube tab that MPRIS reports is in a
different Chrome profile (`chrome-agimnkijcaahngcdmfeangaknmldooml-Profile_2` in
`hyprctl clients`), and the extension instance answering KRunner indexes one profile.

This is worse for the plan than an unexplained empty result. It means the browser-bridge
route, measured today, covers **1 of at least 3 open tabs**, and its coverage is a function
of which Chrome profile the extension happens to be installed in. Any design that says
"the bridge gives us the tab list" must first solve per-profile installation.

Separately, the report's framing that this is "the only route into page content" is
**[INFERENCE, and refuted by this machine's own contents]**: two Anthropic native-messaging
hosts are installed here
(`~/.config/google-chrome/NativeMessagingHosts/com.anthropic.claude_browser_extension.json`
and `..._claude_code_browser_extension.json`), and the Chrome DevTools Protocol is a third
route that a sibling lane in this same scratchpad measured (`cdp_bench.py`, `browser.md`).
"The only route I found on the session bus" is the defensible version.

**Verdict: CONFIRMED that the interface is live; the unexplained-empty-result caveat is
RESOLVED (one-tab index, wrong profile); the "only route" framing is OVERSTATED.**

### V7. The krunner signature mismatch: CONFIRMED, reproduced exactly

**[MEASURED]** `busctl --user introspect ... --xml-interface` declares
`<arg direction="out" type="a(sssuda{sv})" name="matches"/>`, and the live reply above
carries `@a(sssida{sv})`. Unsigned in the contract, signed on the wire. A strict client
rejects every match. Reproduced independently.

### V8. The search-provider protocol as the adapter contract: signatures CONFIRMED, "acts while you speak" REFUTED

**[PRIMARY, fetched from the canonical repository at
`GNOME/gnome-shell:data/dbus-interfaces/org.gnome.ShellSearchProvider2.xml`, main branch]**
Every signature in the finding is exact: `GetInitialResultSet(as terms) -> as`,
`GetSubsearchResultSet(as previous_results, as terms) -> as`,
`GetResultMetas(as identifiers) -> aa{sv}`, `ActivateResult(s identifier, as terms, u timestamp)`,
`LaunchSearch(as terms, u timestamp)`. The id-then-metadata split is real and documented.

The interpretation is not. The finding says subsearch makes "acting while the person is
still speaking a protocol feature in this design, not an optimisation bolted on", and calls
it "the direct answer to the owner's complaint 5". Three things in the primary source say
otherwise.

1. **The protocol never acts.** The doc comment on `ActivateResult` reads: "Called when
   the users chooses a given result." GNOME's own tutorial says the same: "ActivateResult
   is called when the user clicks on an individual result." The shell calls the provider;
   the provider answers; a human commits. Every method except `ActivateResult` and
   `LaunchSearch` is read-only, and both of those take a `u timestamp` of "the user
   interaction that triggered this call". There is no speculative execution anywhere in it.

2. **Subsearch is explicitly a re-query optimisation, in those words.** Verbatim: "Called
   when a search is performed which is a 'subsearch' of the previous search, e.g. the
   method may return less results, but not more or different results. This allows search
   providers to only search through the previous result set, rather than possibly
   performing a full re-query." "Bolted on" is the wrong verdict; it is an optimisation by
   its own definition.

3. **Its monotonic-narrowing contract is violated by streaming speech.** "Not more or
   different results" assumes the query only ever grows, which is true of a typed search
   entry and false of an ASR partial hypothesis, where a later token routinely rewrites an
   earlier word. hyprsay's own prefix experiments in this scratchpad
   (`prefix_parakeet.json`, `prefix_moonshine.json`) exist because of exactly that
   instability. Honouring the contract with speech means falling back to
   `GetInitialResultSet` on every revision, which is the full re-query the mechanism was
   built to avoid.

A fourth, quieter problem with adopting the shape wholesale: `GetResultMetas` returns
`name`, `id`, `icon`, `description`. Free text for a human to read. There are **no typed
parameters** anywhere in SearchProvider2. Apple's App Intents, which the report cites
alongside it as convergent prior art, has typed `@Parameter`s and is a different contract
in the one respect that matters for "play the second live version". Calling the three
traditions convergent flattens that difference. Convergence on "provider returns ranked
opaque ids, caller activates one" is real; convergence on a capability-and-parameter
contract is not, and only App Intents has the second.

**Verdict: signatures CONFIRMED verbatim from primary source; "protocol feature, not an
optimisation" REFUTED by the spec's own wording; "direct answer to complaint 5"
OVERSTATED.**

### V9. `understand.py` commits to LAUNCH_APP before any model: CONFIRMED end to end

The report located this in the grammar path only. I traced it through execution.

**[MEASURED]** `src/hyprsay/nlu/understand.py:450` is `if intent is Intent.LAUNCH_APP:`
followed by `return self._launch_locally(...)`. `_launch_locally` (line 694) calls
`resolve.resolve_app(phrase, heard.text, self.lexicon, self.cfg.gates)`, whose protocol
`LexiconLike` (resolve.py:44) offers `match_windows(phrase, state, ...)` and it is never
called on this path. Line 736 builds `replace(base, intent=Intent.LAUNCH_APP, app=app,
window=None)`.

**[MEASURED, and this closes the loop the report left open]** The Jev path does the same
thing. `_launch_remote` (line 1429) resolves via `self._ranked_apps(fan, answers)`, a
ranking over **installed apps** only; no open window is a candidate there either. And the
executor is unconditional: `ops.py:297 _launch_app` builds the `.desktop` `Exec` argv and
calls `server.launch(command, workspace=...)` with no lookup of existing windows at any
point.

So complaint 3 is not a model failure at any of the three layers. There is no "is it
already open" question asked in the grammar, in the remote path, or in the operation.

**Verdict: CONFIRMED, and stronger than stated.**

### V10. The `.desktop` and GActions negatives: CONFIRMED by independent re-count

**[MEASURED, my own sweep]** Across `/usr/share/applications`,
`~/.local/share/applications` and both flatpak export dirs: 252 `.desktop` files today, 10
declaring `Actions=`, 33 action entries in total, and exactly one
`DBusActivatable=true` (`/usr/share/applications/org.kde.spectacle.desktop`). Matches the
report except 252 versus 249, which is three files added since.

**[MEASURED, my own `org.gtk.Actions.DescribeAll` sweep over every well-known name]** 17
(name, path) pairs export `org.gtk.Actions`, across 9 unique well-known names.
`DescribeAll` returns **0** actions for `ca.desrt.dconf`, `dev.hyprvoice.aio`,
`fr.arouillard.waybar`, `org.blueman.Tray`, and all three `swaync` paths under all three of
its names; **1** for `org.blueman.Applet` and **1** for
`org.freedesktop.network-manager-applet`. Two actions on the entire session bus.

**Verdict: CONFIRMED.** The negative result is real and is the most useful thing in the
report: it stops an architecture being built on GActions.

### What I could not check

- I did not call the Spotify Web API or authenticate `spotify_player`, so no latency number
  for `search` exists and none should be quoted. The report's "plausibly the same order as
  Jev's 315 ms" is an inference and is labelled as one there; it must not be promoted to a
  budget line.
- I could not confirm the claim that Spotify's own client accepts `spotify:track:<id>` on
  `OpenUri`, because the client is not running. It remains secondary.
- I did not verify the Talon, App Intents or Home Assistant claims; they are cited to
  vendor docs and community wikis and are labelled accordingly in the report.
