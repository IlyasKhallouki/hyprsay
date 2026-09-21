# UI lane research: a Hyprland-native voice HUD overlay

Date: 2026-09-20, second verification pass 2026-09-21. Target: Arch, Hyprland 0.56.2 (hyprlang config manager on this machine), Intel UHD 620, Python 3.14.7, GTK 4.22.4, gtk4-layer-shell 1.3.0, PyGObject 3.56.3.

Source labels used below: [PRIMARY] upstream docs, source code or release notes; [LOCAL] measured or executed on this machine during this research; [SECONDARY] press or third party; [INFERENCE] my reasoning from the above.

Important caveat on every local timing: the machine was under heavy load from parallel jobs while I measured (load average between 13 and 27 on 8 threads). Treat the numbers as pessimistic, and as comparisons between options rather than absolute budgets.

Probe scripts that produced the local results are kept in `/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/uitest/` (`hud_probe.py`, `rings_probe.py`, `toggle_probe.py`, and from the second pass `rerun.py`, `clickthrough_probe.py`, `qt_probe.qml`, `startup_cmp.py`, `aio_probe.py`).

**Second pass, 2026-09-21.** The first draft left seven items open. This pass closed six of them against the Hyprland 0.56.2 source tree (unpacked at `scratchpad/Hyprland-0.56.2/`) and with new live probes. Changes are marked "[pass 2]" where they alter a conclusion:

1. Click-through is now **verified live** (hover test with a control run), and the empty input region survives a resize (section 4).
2. "Only overlay renders above a fullscreen window" is now **confirmed in source** (section 4).
3. Layer rules are **not** map-time only: a runtime `keyword layerrule` re-applies rules to already mapped layers (section 5). The first draft's inference was too strict.
4. `animation = "none"` (Omarchy) is resolved: the rule value is not validated, and any style that is not `slide*` or `popin*` degrades to a plain fade (section 5).
5. `reserved` order is `[left, top, right, bottom]` and the gradient cap is 10 colors, both from source (section 6 and 9).
6. Qt6 QML + layer-shell-qt is now **verified working on this Hyprland session**, with cold start numbers next to GTK (section 3).
7. The renderer benchmark was re-run with CPU accounting; results match the first pass (section 8).

---

## 0. Executive recommendation

Build the HUD as a **separate, resident process written in Python + GTK4 + gtk4-layer-shell via PyGObject**, run by the **system interpreter** (`/usr/bin/python3`), talking to the voice engine over a Unix socket with newline-delimited JSON. Ship a documented, stable HUD protocol so that a Quickshell/QML frontend can be added later for Omarchy 4 style desktops without touching the engine.

Reasons, in order of weight:

1. It already works on this exact machine with zero new packages. I ran a real overlay-layer, click-through, non-focus-stealing, blurred, rounded HUD under Python 3.14.7 and it held the 60 Hz frame cadence (p50 16.6 ms) [LOCAL].
2. Same language as hypruse, so the HUD can reuse hypruse's IPC knowledge (config manager probe, socket2 parser, coordinate contract) instead of reimplementing it in QML/JS.
3. A resident window shows in about 9 ms (p50) from `set_visible(True)` to the compositor's `openlayer` event, even under load [LOCAL]. Cold process start to mapped is 0.46 to 0.84 s at load average 9 and was 1.2 to 2.3 s at load average 13 to 27 [LOCAL], so the HUD must be a daemon, never spawned per utterance.
4. Quickshell is the direction the rice ecosystem has moved (Omarchy 4, end-4, Caelestia, DankMaterialShell, ML4W are all QML now) [PRIMARY], and it is one `pacman -S quickshell` away (extra/quickshell 0.3.1) [LOCAL], but it would add a second language and a second runtime to a Python project for no latency win. Keep it as the planned second frontend, not the first.
5. Qt6 QML + layer-shell-qt works on this machine (verified live in pass 2: overlay layer, namespace through `scope`, no focus taken, cold start 0.17 to 0.36 s against 0.46 to 0.84 s for Python GTK) but has the weakest documentation, no shell-oriented ecosystem, and would still put a second language next to a Python engine. AGS/Astal is not in the Arch official repos (only chaotic-aur / AUR here) and brings a GJS/TypeScript toolchain. Both rejected.

Use **two layer surfaces with two namespaces**:

- `hyprvoice-hud`: small pill (about 520 x 96 logical px), overlay layer, bottom center, blur on, compositor animation off, own 120 to 160 ms in-surface animation.
- `hyprvoice-hints`: full-monitor transparent surface, overlay layer, no blur, no animation, mapped only while numbered hints or rings are on screen.

And use the **compositor itself** for the target-window highlight (`setprop ... active_border_color`), because that is pixel-perfect, follows window animations for free, and costs nothing in the client. Draw client-side rings only for multi-target hint mode.

---

## 1. Verified facts about this machine [LOCAL]

| Item | Value |
| --- | --- |
| Python | 3.14.7 (system) |
| PyGObject | 3.56.3 (`python-gobject 3.56.3-1`), `import gi` works, `gi.events` present |
| GTK | 4.22.4 |
| gtk4-layer-shell | 1.3.0, typelib `Gtk4LayerShell-1.0` and `Gtk4SessionLock-1.0` present, also ships `/usr/lib/liblayer-shell-preload.so` |
| layer-shell-qt | 6.7.4, QML module at `/usr/lib/qt6/qml/org/kde/layershell` |
| Qt | qt6-base/declarative/wayland/svg 6.11.1, `python-pyqt6 6.11.0`, `qml6` binary present, no PySide6 |
| Quickshell | not installed; `extra/quickshell 0.3.1-1`, 6.2 MiB installed size; of its deps only `cpptrace` is missing here |
| AGS / Astal | not in official repos; `aylurs-gtk-shell 3.1.2` and `libastal-*-git` only via chaotic-aur |
| Hyprland | 0.56.2, `hyprctl -j status` reports `"configProvider": "hyprlang"` |
| Vulkan | `vulkan-intel 26.1.6`, GTK picks `VulkanRenderer` by default |
| Monitor | eDP-1, 1920x1080, scale 1, origin 0,0, `reserved: [0,0,0,0]` (waybar is on the bottom layer with no exclusive zone) |
| Live look | `border_size 2`, `gaps_in 2`, `gaps_out 5`, `rounding 10`, `rounding_power 2.0`, active border `ffa4a4a4 ff4e4e4e 45deg`, blur on (size 6, passes 3, xray false), shadows off, `active_opacity 0.9` |
| Rice | HyDE with Wallbash (`~/.config/hypr/themes/{theme,colors,wallbash}.conf`, 160 KB `~/.config/gtk-4.0/gtk.css`) |
| Fonts | JetBrainsMono Nerd Font, CaskaydiaCove Nerd Font, Maple Mono, Mononoki NF, Noto Sans; GNOME interface font is `Noto Sans 10` |

The user's own config already uses the new layer rule grammar: `layerrule = blur true,match:namespace rofi` and `layerrule = ignore_alpha 0,match:namespace rofi` (`~/.config/hypr/windowrules.conf:102-110`, `~/.config/hypr/userprefs.conf:109-110`).

---

## 2. Does PyGObject work on Python 3.14? Does gtk4-layer-shell need load ordering?

**PyGObject on 3.14: yes.** Upstream NEWS: "3.54.0 - 2025-09-06 ... Fix compatibility with Python 3.14 (MR 433)"; 3.55.3 added "asyncio support without EventLoopPolicy" (MR 503, 509), which matters because Python 3.14 deprecates event loop policies [PRIMARY: https://gitlab.gnome.org/GNOME/pygobject/-/raw/main/NEWS]. Locally, 3.56.3 imports, creates windows, subclasses `Gtk.Widget` with `do_snapshot`, and runs tick callbacks under 3.14.7 [LOCAL].

**Load ordering: yes, it is mandatory, and the failure mode is nasty.** Upstream explains that GTK4 has no official support for non-XDG shells, so the library shims libwayland symbols, and "if libwayland gets loaded before us, its calls take precedence and the shim doesn't work" [PRIMARY: https://github.com/wmww/gtk4-layer-shell/blob/main/linking.md]. The documented Python fix:

```python
from ctypes import CDLL
CDLL("libgtk4-layer-shell.so")   # must run before `import gi`
```

or `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so`. Since v1.1.0 the shim uses `RTLD_NEXT` instead of dlopening libwayland [PRIMARY: gtk4-layer-shell v1.1.0 release notes, https://github.com/wmww/gtk4-layer-shell/releases].

What happens without it, measured here [LOCAL]:

- `Gtk4LayerShell.is_supported()` returns `False`, `is_layer_window()` returns `False`, GTK warns "GtkWindow is not a layer surface".
- The window becomes an ordinary xdg toplevel. Hyprland tiled it **and gave it focus**: `hyprctl activewindow` reported class `dev.hyprvoice.probe`. For a voice HUD that is the worst possible bug (it steals the keyboard from the app the user is dictating into).

Mitigations to build in:

1. Put the `CDLL` call at the very top of the HUD entry module, before any import that could pull in GTK, GDK, GStreamer or anything linking libwayland-client.
2. Hard-fail at startup: `if not LS.is_supported(): sys.exit("layer shell shim not active")`. Never call `present()` on a non-layer window.
3. Keep the HUD in its own process so that no other import in the engine (audio, numpy, etc.) can change the load order.
4. Optional belt and braces for the systemd user unit: `Environment=LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so`.

`liblayer-shell-preload.so` (also shipped in 1.3.0) is a different thing: a hack to run arbitrary Wayland apps as layer surfaces via environment variables [PRIMARY: v1.1.0 release notes]. Not needed here.

**Packaging consequence.** hypruse has no `gi` dependency today (`pyproject.toml` depends only on `mcp`; `a11y.py` shells out). PyGObject comes from the distro package, which a default `uv` venv cannot see. Either run the HUD with `/usr/bin/python3` (recommended, zero friction on Arch), or create the venv with `--system-site-packages`. Declare `python-gobject`, `gtk4`, `gtk4-layer-shell` as distro dependencies in the AUR PKGBUILD rather than PyPI dependencies [INFERENCE from LOCAL].

---

## 3. Toolkit comparison

| | Python + GTK4 + gtk4-layer-shell | Quickshell (QML) | Qt6 QML + layer-shell-qt | AGS v3 / Astal |
| --- | --- | --- | --- | --- |
| Installed here | yes, all parts | no (`extra/quickshell 0.3.1`) | yes (no PySide6; PyQt6 and `qml6` present) | no (AUR / chaotic-aur only) |
| Language fit with hypruse | same (Python) | QML + JS, engine talks over socket/IPC | QML + JS or PyQt6 | TypeScript on GJS |
| Layer-shell control | full: layer, namespace, anchors, margins, exclusive zone, keyboard mode, monitor | full: `PanelWindow` + attached `WlrLayershell` (`layer`, `namespace`, `keyboardFocus`), `mask: Region` for click-through | `LayerShellQt.Window` attached props: `layer`, `scope` (namespace), `anchors`, `margins`, `exclusionZone`, `keyboardInteractivity`, `screen` | via Astal GTK4 widgets over gtk4-layer-shell |
| Rendering | GSK, Vulkan by default on Wayland, retained render nodes | Qt Quick scene graph (GPU), very strong for fluid animation and shaders (`ShaderEffect`) | same scene graph | GSK |
| Per-frame cost in our language | Python callback per frame for custom drawing (about 1 ms if done right, see section 8) | none for declarative animations (C++ scene graph) | none | JS per frame |
| Hyprland integration built in | none, but hypruse already has it | `Quickshell.Hyprland` module (workspaces, toplevels, IPC events) | none | Astal Hyprland lib |
| Ecosystem signal | Waybar-era, stable, MIT | what the big rices use in 2026 | niche outside KDE | declining share; AGS repo last pushed 2026-04 |
| Risk | load-order footgun (section 2); user gtk.css leaks in (section 7) | pre-1.0 API; an open upstream issue reports an anchored `PanelWindow` that stops presenting frames on Hyprland (quickshell issue 1130) | thin docs; layer-shell-qt versioned with Plasma (6.7.4) not Qt | extra toolchain, not packaged officially |

Sources: Quickshell docs v0.3.1 for `PanelWindow`, `QsWindow.mask`, `WlrLayershell` [PRIMARY: https://quickshell.org/docs/v0.3.1/types/Quickshell/PanelWindow/ , https://quickshell.org/docs/v0.3.1/types/Quickshell/QsWindow/ , https://quickshell.org/docs/v0.3.1/types/Quickshell.Wayland/WlrLayershell/]; Quickshell changelog, latest v0.3.1 [PRIMARY: https://quickshell.org/changelog/]; layer-shell-qt README and the installed `LayerShellQtQml.qmltypes` [PRIMARY + LOCAL]; repo metadata via GitHub API on 2026-09-20 [PRIMARY]: end-4/dots-hyprland 16.2k stars (QML), caelestia-dots/shell 12.4k (QML), AvengeMedia/DankMaterialShell 8.2k (QML, "built with Quickshell & GO"), mylinuxforwork/dotfiles 5.0k (QML), omacom/omarchy 42.4k, quickshell-mirror/quickshell 3.1k, Aylur/ags 3.1k ("Scaffolding CLI for Astal+Gnim", last push 2026-04-08), Aylur/astal 1.0k.

**[pass 2] Qt6 QML + layer-shell-qt, verified live.** `uitest/qt_probe.qml` run with the stock `qml6` binary mapped a surface that `hyprctl -j layers` reported as level 3 (overlay), namespace `hyprvoice-probe-qt`, `x 700, y 936, w 520, h 96`, the same placement as the GTK probe, and `hyprctl activewindow` stayed on kitty. No warnings on stderr. The working attached properties (names taken from the installed `LayerShellQtQml.qmltypes`):

```qml
import QtQuick
import org.kde.layershell as LS
Window {
    width: 520; height: 96; visible: true; color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
    LS.Window.layer: LS.Window.LayerOverlay
    LS.Window.scope: "hyprvoice-hud"              // scope is the layer-shell namespace
    LS.Window.anchors: LS.Window.AnchorBottom
    LS.Window.margins.bottom: 48
    LS.Window.exclusionZone: -1
    LS.Window.keyboardInteractivity: LS.Window.KeyboardInteractivityNone
    LS.Window.activateOnShow: false
}
```

Cold start, spawn to the compositor's `openlayer` event, three interleaved runs at load average 8.6 to 9.0 [LOCAL, `uitest/startup_cmp.py`]:

| | spawn to mapped | RSS after map |
| --- | --- | --- |
| `python3 hud_probe.py` (GTK4, Vulkan) | 0.46, 0.69, 0.84 s | 73 to 74 MB |
| `qml6 qt_probe.qml` (Qt Quick) | 0.17, 0.27, 0.36 s | 97 to 98 MB |

So QML starts 2 to 3 times faster cold and costs about 25 MB more. Neither number matters for a resident daemon, which is what both would be. It does mean a QML frontend is a real, working option on this machine with zero new packages; what it lacks is not function but fit (second language, no PySide6 here, thin docs). I did not hover-test `Qt.WindowTransparentForInput` for pass-through; Qt documents that flag as making the window transparent for input, and on Wayland it is implemented with an empty input region [not verified here].

**What Omarchy 4 uses.** Omarchy v4.0.0 "Quattro" (2026-08-14) rewrote "the bar, launcher, menus, notifications, on-screen displays, control panels, lock screen, and polkit agent" in Quickshell as plugins inside "a single long-running shell process", removing Waybar, Walker, Mako, SwayOSD, hyprlock, hypridle, swaybg and polkit-gnome, and it "Convert[ed] all Hyprland configs to lua for full 0.56 compatibility" [PRIMARY: https://github.com/omacom/omarchy/releases/tag/v4.0.0]. Its layer namespaces are `omarchy-bar`, `omarchy-menu`, `omarchy-image-selector`, `omarchy-emojis`, `omarchy-clipboard`, `omarchy-keyboard-panel`, and it disables compositor layer animation for all of them, keeping "their own QML opacity transition" [PRIMARY: https://github.com/omacom/omarchy/blob/quattro/default/hypr/apps/omarchy-shell.lua]. Omarchy 4 also ships offline dictation via voxtype (`bin/omarchy-voxtype-*`, `shell/plugins/bar/indicators/Dictation.qml`), which is dictation, not desktop control, but it means an Omarchy user already has a mic indicator convention in the bar.

Useful Quickshell facts if a QML frontend is built later [PRIMARY]: `PanelWindow.focusable` defaults to false and maps to `WlrLayershell.keyboardFocus` (default `None`); `WlrLayershell.layer` defaults to `WlrLayer.Top`; `namespace` "cannot be set after windowConnected"; `mask` with a `Region` decides clickable areas and an `Xor` intersection inverts it; a window whose color is opaque before first show cannot become transparent later unless `surfaceFormat.opaque` is false.

---

## 4. Layer-shell mechanics for a HUD on Hyprland 0.56

Protocol facts [PRIMARY: https://wayland.app/protocols/wlr-layer-shell-unstable-v1]:

- Layers, bottom-most first: `background (0)`, `bottom (1)`, `top (2)`, `overlay (3)`. Normal windows render between bottom and top.
- `keyboard_interactivity`: `none (0)` "no keyboard focus is possible"; `exclusive (1)`; `on_demand (2, since v4)`.
- `set_exclusive_zone`: positive reserves space; `0` means avoid occluding surfaces that have exclusive zones; `-1` means "would not like to be moved to accommodate for other surfaces, and the compositor should extend it all the way to the edges it is anchored to".
- Input: "Layer surfaces receive pointer, touch, and tablet events normally. If you do not want to receive them, set the input region on your surface to an empty region."
- `namespace` is set at `get_layer_surface` time; passing a NULL output lets the compositor choose.
- gtk4-layer-shell on this machine reports protocol version 4 (so `on_demand` exists) [LOCAL].

### Overlay vs top

| | `top` | `overlay` |
| --- | --- | --- |
| Above tiled and floating windows | yes | yes |
| Above fullscreen windows | no. [pass 2] Confirmed in 0.56.2 source: `src/desktop/view/LayerSurface.cpp:330` carries the comment "if in fullscreen, only overlay can be above." and sets the fade alpha of any lower layer to 0 while the monitor has a fullscreen window; `src/managers/fullscreen/handler/FullscreenHandler.cpp:274-277` flips `m_aboveFullscreen` for every layer on that monitor, and `InputManager.cpp:502` stops a `top` layer from taking input then [PRIMARY] | yes |
| Where bars and notifications usually live | yes (this machine's waybar is unusually on `bottom`) | launchers, OSDs, lock-adjacent UI |

Recommendation: **overlay** for both surfaces. A voice HUD must be visible when the user says "exit fullscreen" while a video is fullscreen. The cost is that an overlay surface defeats direct scanout for a fullscreen client while it is mapped, which is why the HUD must unmap when idle and the hint surface must exist only during hint mode [INFERENCE].

### The exact GTK calls (all verified running here)

```python
from ctypes import CDLL
CDLL("libgtk4-layer-shell.so")            # before gi
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gtk4LayerShell as LS
import cairo

assert LS.is_supported()
win = Gtk.ApplicationWindow(application=app)
win.remove_css_class("background")         # see section 7
LS.init_for_window(win)                    # before first present()
LS.set_namespace(win, "hyprvoice-hud")     # before first map; this is what layerrule matches
LS.set_layer(win, LS.Layer.OVERLAY)
LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)   # focus can never be taken
LS.set_anchor(win, LS.Edge.BOTTOM, True)   # one edge anchored = centered on that edge
LS.set_margin(win, LS.Edge.BOTTOM, 48)
LS.set_exclusive_zone(win, -1)             # ignore bars' reserved space, reserve none ourselves
LS.set_monitor(win, gdk_monitor)           # per-monitor placement
win.connect("map", lambda w: w.get_surface().set_input_region(cairo.Region()))  # click-through
win.present()
```

Local verification: `hyprctl -j layers` showed the probe at level 3 on eDP-1 at `x 700, y 936, w 520, h 96` (centered, 48 px off the bottom), and `hyprctl activewindow` still reported the Chrome window that had focus before.

[pass 2] **Click-through verified live, hover only, no clicks** (`uitest/clickthrough_probe.py`). The probe attaches a `Gtk.EventControllerMotion` to the HUD window, moves the pointer into the HUD rectangle with `hyprctl dispatch movecursor`, then restores it. The cursor was idle before the test and sat inside the same tiled window, so keyboard focus could not change for unrelated reasons.

| Variant | `enter` events | `motion` events | Active window before / after |
| --- | --- | --- | --- |
| empty input region (`cairo.Region()`) | 0 | 0 | same address |
| control: no input region set | 1 | 1 | same address |
| empty region, then window resized 520x96 to 600x120 before the hover | 0 | 0 | same address |

So the surface is pointer-transparent, the test is sensitive (the control run does receive the pointer), the region survives a GTK resize, and with `KeyboardMode.NONE` even a HUD that does take the pointer does not take keyboard focus.

Notes:

- Re-apply the empty input region on every `map` (a hidden and re-shown window gets a new `GdkSurface` state) and after size changes. If a future version needs a clickable cancel chip, pass a `cairo.Region(cairo.RectangleInt(x, y, w, h))` for just that chip instead of an empty region.
- **Exclusive zone.** Do not use `auto_exclusive_zone_enable`. A HUD must never push tiled windows around. `-1` also makes the full-monitor hint surface start at the true monitor origin even when a bar reserves space, which keeps the coordinate math in section 6 trivial.
- **Per-monitor.** A layer surface is bound to one output. Map Hyprland's focused monitor (`j/monitors` entry with `"focused": true`, field `name`, e.g. `eDP-1`) to the `GdkMonitor` whose `get_connector()` matches, from `Gdk.Display.get_default().get_monitors()`. Verified here: connector `eDP-1`, geometry `[0,0,1920,1080]`, scale 1.0. gtk4-layer-shell "automatically remap[s] surfaces when monitors change" since 1.1.1 and does not remap unmapped windows since 1.3.0 [PRIMARY: release notes]. Simplest robust design: one hidden HUD window per monitor, created on `monitoraddedv2`, and show only the one on the focused monitor.
- **Never above the lock screen.** Do not set `above_lock`. The engine should also stop listening while locked.
- Set `GTK_A11Y=none` in the HUD process environment so the HUD never appears in the AT-SPI tree that hypruse walks, and startup skips the accessibility bus [PRIMARY for the variable: GTK docs list `GTK_A11Y` backends `atspi`, `accesskit`, `test`, `none` ("Disables the accessibility backend"), https://docs.gtk.org/gtk4/running.html; the benefit for hypruse's tree walk is INFERENCE].

### Side effect to handle in the fork

Showing the HUD emits `openlayer>>hyprvoice-hud` and `closelayer>>hyprvoice-hud` on socket2 [LOCAL], and hypruse's `desktop` snapshot lists every layer. `hyprctl.layer_kind()` would classify our namespace as `unknown` (`/home/ilyask/projects/hypruse/src/hypruse/hyprctl.py:453-467`), it is not in `FOCUS_STEALING_KINDS`, so `sequence` would not abort, but the HUD would still pollute the state string that goes to Jev and the `wait_for layer_open` primitive. The fork should filter namespaces with the `hyprvoice-` prefix in `parse_layers()` and in the event waiter.

---

## 5. Hyprland layer rules, both dialects

### There are two config managers, and the dialect is not a function of the version

- The Lua config manager landed in **0.55.0** (2026-05-09): release notes list "config/lua: init lua config manager, use lua if available (#13817)" and "config: use lua by default, generate lua if no config present" [PRIMARY: https://github.com/hyprwm/Hyprland/releases/tag/v0.55.0]. (The hypruse commit message `ab9db49` says 0.56 added it; the release notes say 0.55. It does not change any conclusion.)
- 0.56.0 (2026-07-20) has "No breaking changes", and 0.56.x fixed "auto-generating hyprlang instead of lua config file by default (#14944)" [PRIMARY: v0.56.0 release notes]. So a fresh 0.56 install is a Lua desktop; an upgraded one with `hyprland.conf` stays hyprlang, like this machine.
- The manager is chosen by config file extension and is visible at runtime through `hyprctl -j status` -> `{"configProvider": "hyprlang" | ...}` [LOCAL + hypruse `ARCHITECTURE.md`, "The two config managers"].
- Under the Lua manager `hyprctl keyword` is refused and `hyprctl eval '<lua>'` exists; under hyprlang it is the reverse: `hyprctl eval 'return 1'` answers "eval is only supported with the lua config manager" [LOCAL]. `hyprctl reload full-reset` can switch managers under a running session [PRIMARY: wiki "Using hyprctl"].
- The official wiki is now **Lua only** from the 0.55.0 version onward. The last wiki version that documents hyprlang layer rules is 0.54.0 [PRIMARY: https://wiki.hypr.land/0.54.0/Configuring/Window-Rules/ vs https://wiki.hypr.land/0.56.0/Configuring/Basics/Window-Rules/].

### Effects (identical names in both dialects)

Straight from the 0.56.2 source, `src/desktop/rule/layerRule/LayerRuleEffectContainer.cpp` [PRIMARY: https://github.com/hyprwm/Hyprland/blob/v0.56.2/src/desktop/rule/layerRule/LayerRuleEffectContainer.cpp]:

`no_anim`, `blur`, `blur_popups`, `ignore_alpha`, `dim_around`, `xray`, `animation`, `order`, `above_lock`, `no_screen_share`. The only match prop is `namespace` (regex).

The old names are gone. On this 0.56.2 machine [LOCAL]:

| Sent via `hyprctl keyword layerrule ...` | Reply |
| --- | --- |
| `blur on, match:namespace ^hyprvoice-probe$` | `ok` |
| `ignore_alpha 0.35, match:namespace ^hyprvoice-probe$` | `ok` |
| `animation slide bottom, match:namespace ^hyprvoice-probe$` | `ok` |
| `xray off, ...` / `dim_around off, ...` / `order 10, ...` / `no_anim on, ...` / `no_screen_share on, ...` | `ok` |
| `blur, hyprvoice-probe` (pre-0.53 grammar) | `invalid field blur: missing a value` |
| `ignorezero, hyprvoice-probe` | `invalid field ignorezero: missing a value` |
| `ignorealpha 0.3, hyprvoice-probe` | `invalid field type ignorealpha` |

So **`ignorezero` no longer exists**; its replacement is `ignore_alpha 0` (the 0.54 wiki: "a = 0 if unspecified"). `ignorealpha` became `ignore_alpha`, `dimaround` became `dim_around`, `noanim` became `no_anim`.

Valid styles for the **`animation = layers...` config keyword**, from `styleValidInConfigVar` in `src/animation/AnimationManager.cpp` at v0.56.2 [PRIMARY]: empty, `fade`, anything starting with `slide` (so `slide`, `slide top|bottom|left|right`), or `popin` with an optional percentage (`popin 80%`). Anything else is "unknown style".

[pass 2] The **layer rule** `animation` effect does not go through that validator. `LayerRuleApplicator.cpp:119` stores the string as is, and `src/desktop/view/animationControllers/LayerSurfaceAnimationController.cpp` only branches on `starts_with("slide")` and `starts_with("popin")`; every other value, including `fade`, `none` or a typo, leaves position and size untouched and animates alpha 0 to 1. `forcedEdgeFromStyle` reads the second word as `top`, `bottom`, `left` or `right`; without it the slide comes from the nearest monitor edge. Live check: `hyprctl keyword layerrule 'animation none, match:namespace ...'` answers `ok` [LOCAL]. So Omarchy's `no_anim = true, animation = "none"` pair is harmless: `no_anim` does the work and `"none"` is an unrecognised style that would mean "fade only" on its own. The animation tree has `layers` -> `layersIn`, `layersOut` and `fade` -> `fadeLayers` -> `fadeLayersIn`, `fadeLayersOut` [PRIMARY: wiki Animations].

### Recommended rules, hyprlang dialect

```ini
# hyprvoice HUD pill: blurred glass, our own animation
layerrule = blur on, match:namespace ^hyprvoice-hud$
layerrule = ignore_alpha 0.35, match:namespace ^hyprvoice-hud$
layerrule = xray off, match:namespace ^hyprvoice-hud$
layerrule = no_anim on, match:namespace ^hyprvoice-hud$

# hyprvoice hint overlay: full monitor, must be instant and never blurred
layerrule = no_anim on, match:namespace ^hyprvoice-hints$
```

[pass 2] One line can carry several effects. The hyprlang parser (`src/config/legacy/ConfigManager.cpp:2060-2093`) splits the value on commas and treats each field as `name value`, a field starting with `match:` being a prop and anything else an effect. Verified live, answer `ok`:

```ini
layerrule = blur on, ignore_alpha 0.35, xray off, no_anim on, match:namespace ^hyprvoice-hud$
```

That makes runtime installation one IPC call per surface instead of four.

Block form also exists in hyprlang [PRIMARY: 0.54 wiki; also used in this user's `~/.config/hypr/workflows/gaming.conf:33-38`]:

```ini
layerrule {
    name = hyprvoice-hud
    match:namespace = ^hyprvoice-hud$
    blur = on
    ignore_alpha = 0.35
    no_anim = on
}
```

### Same rules, Lua dialect

Signature from the stub shipped on this machine, `/usr/share/hypr/stubs/hl.meta.lua:555-568` (`HL.LayerRuleSpec`: `above_lock`, `animation`, `blur`, `blur_popups`, `dim_around`, `enabled`, `ignore_alpha`, `match`, `name`, `no_anim`, `no_screen_share`, `order`, `xray`) and the example in `/usr/share/hypr/hyprland.lua:341-347` [LOCAL, PRIMARY]:

```lua
hl.layer_rule({
  name         = "hyprvoice-hud",
  match        = { namespace = "^hyprvoice-hud$" },
  blur         = true,
  ignore_alpha = 0.35,
  xray         = false,
  no_anim      = true,
})
hl.layer_rule({ name = "hyprvoice-hints", match = { namespace = "^hyprvoice-hints$" }, no_anim = true })
```

`hl.layer_rule` returns a handle with `set_enabled()` / `is_enabled()` [PRIMARY: wiki layer rules]. Omarchy writes `no_anim = true, animation = "none"` together for its shell surfaces [PRIMARY: omarchy-shell.lua]; I could not find `"none"` as an accepted layer style in the 0.56.2 validator, so I would not copy that second key.

### Installing the rules at runtime (no config edit needed)

The HUD can install its own rules at startup, exactly like hypruse's `border_rule()` does for window rules:

- hyprlang: `hyprctl keyword layerrule "blur on, match:namespace ^hyprvoice-hud$"` (verified `ok` here).
- Lua: `hyprctl eval 'hl.layer_rule({ ... })'` (documented; **not executed here**, this machine runs hyprlang). Build the Lua through hypruse's `lua_str()` and never by string interpolation: that argument is code run inside the compositor.
- Runtime rules vanish on `hyprctl reload` (`m_keywordRules.clear()` at `legacy/ConfigManager.cpp:773`, `m_luaLayerRules.clear()` at `lua/ConfigManager.cpp:707`), so re-install on the `configreloaded` socket2 event. Also offer `hyprvoice install-rules` that prints the snippet for the user's dialect.
- [pass 2] **Rules do re-apply to layers that are already mapped.** The first draft inferred the opposite. In source: `hyprctl keyword layerrule ...` runs `mgr->reloadRules()` and damages every monitor (`src/debug/HyprCtl.cpp:1240-1246`); `reloadRules()` rebuilds the rule list, re-registers the keyword rules and calls `ruleEngine()->updateAllRules()` (`legacy/ConfigManager.cpp:946-967`), which calls `propertiesChanged(RULE_PROP_ALL)` on every mapped layer (`src/desktop/rule/Engine.cpp:34-48`). The Lua handle's `set_enabled()` does the same (`lua/objects/LuaLayerRule.cpp:41`). Installing before first map is still the clean order, because it avoids one frame of an unblurred, compositor-animated pill, but a late install is not a bug.
- [pass 2] **Idempotence differs by dialect.** In Lua, a rule with a `name` that already exists is updated in place (`LuaBindingsConfigRules.cpp:1299-1306`), so re-running the installer is safe; always pass `name`. In hyprlang every `keyword layerrule` call appends another entry to `m_keywordRules` until the next reload, so install once per `configreloaded`, not once per utterance.

Housekeeping note: my syntax probes left runtime-only rules for the namespaces `hyprvoice-probe`, `hyprvoice-probe-rings`, `hyprvoice-probe-ring2` and (pass 2) `hyprvoice-probe-multi` in the running compositor. They match nothing else and disappear on the next `hyprctl reload` or logout.

### Which effects to use, and which to avoid

- `blur` + `ignore_alpha`: pick `ignore_alpha` a little below the panel's background alpha and above the peak alpha of any soft shadow you draw. With a 0.72 alpha panel, `0.35` leaves the transparent rounded corners and a shadow unblurred. Verified visually here: panel blurred, surroundings untouched.
- `xray off`: blur samples the real windows behind the HUD, not just the wallpaper. This user's global `blur:xray` is already false.
- `no_anim on` and animate inside the surface. Reason: a layer rule can set the animation **style** but not its **speed**, and this user's `layersIn` inherits `global` = speed 8 (8 deciseconds, 800 ms) with bezier `default` [LOCAL `hyprctl -j animations`; wiki: "speed is the number of deciseconds"]. An 800 ms slide is the opposite of "lightning fast". Omarchy 4 made the same choice.
- `dim_around`: dims everything behind the layer; strength comes from `decoration:dim_around`. Too heavy for every utterance. Reserve it for a destructive-action confirmation state, if any.
- `no_screen_share`: **do not put it on the hint surface.** It "hides the layer from screen sharing by drawing a black rectangle over it" [PRIMARY: 0.54 wiki], and that includes `grim`. With it set on a full-monitor surface my `grim` capture of the whole screen came back solid black [LOCAL]. It would blind hypruse's own `screenshot`, `zoom` and `marks` tools. On the small pill it is merely pointless.
- Never enable `blur` on the full-monitor hint surface: the blur pass would cover the whole screen on a UHD 620 [INFERENCE].
- `order`: only matters for exclusive-zone negotiation; irrelevant with zone `-1`.

---

## 6. Reading the user's live look so the HUD matches the compositor

### Transport

Use the request socket directly, `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket.sock`, with a `[[BATCH]]` prefix and `j/` per command. Measured here [LOCAL]:

| Method | Latency |
| --- | --- |
| socket, one batch of 11 queries (7 getoption + animations + monitors + clients + activewindow, 15 KB reply) | p50 0.60 ms, p95 1.9 ms |
| socket, `j/clients` alone | p50 0.33 ms |
| `hyprctl -j clients` subprocess | p50 16.6 ms, max 20 ms |
| five sequential `hyprctl` subprocesses inside the probe | 111 to 181 ms |

hypruse deliberately shells out to `hyprctl` (`src/hypruse/hyprctl.py:5`). For the HUD and for the pre-Jev state snapshot the fork should add a socket client; it is about 30x faster and removes a fork per call. The wiki warns that hyprctl calls are handled synchronously by the compositor and recommends batching [PRIMARY: wiki "Using hyprctl"].

### Options and their wire format (from `dispatchGetOption`, `src/debug/HyprCtl.cpp` v0.56.2, and tested)

| Option | JSON reply on this machine | Parse as |
| --- | --- | --- |
| `general:border_size` | `{"option": "...", "int": 2, "set": true}` | int |
| `general:gaps_in` | `"custom": "2 2 2 2"` | CSS order: top right bottom left |
| `general:gaps_out` | `"custom": "5 5 5 5"` | same |
| `decoration:rounding` | `"int": 10` | int |
| `decoration:rounding_power` | `"float": 2.000000`, `"set": false` | float; 2.0 is a circular arc, higher is squircle-like |
| `general:col.active_border` | `"custom": "ffa4a4a4 ff4e4e4e 45deg"` | gradient |
| `general:col.inactive_border` | `"custom": "ff0f0f0f ff1a1a1a 45deg"` | gradient |
| `decoration:blur:enabled/size/passes` | ints | if blur is disabled globally, raise the panel alpha to about 0.92 |
| `decoration:active_opacity` | `"float": 0.9` | optional cue for panel alpha |

Details that bite:

- **Option names: use the colon form.** On hyprlang only `general:border_size` works; `general.border_size` returns "no such option" [LOCAL]. Under the Lua manager both work, because `CConfigManager::getConfigValue` falls back to `luaConfigValueName()` which maps `:` to `.` and `-` to `_` [PRIMARY: `src/config/lua/ConfigManager.cpp` v0.56.2, lines 1030-1051 and 1136-1141]. The git wiki now documents the dot form, which is wrong for a 0.56.2 hyprlang session.
- **Gradient format.** `CGradientValueData::toString()` prints each color as `std::format("{:x} ", c.getAsHex())` followed by `"{}deg"` with the angle as an integer [PRIMARY: `src/config/shared/complex/ComplexDataTypes.hpp` v0.56.2]. Colors are `AARRGGBB` hex **without zero padding**, so left-pad every token to 8 digits before parsing (a color with alpha below 0x10 prints with 7 digits). Parse up to 10 stops: [pass 2] the hyprlang parser rejects more with "max colors is 10" (`src/config/legacy/ConfigManager.cpp:129-131`) [PRIMARY]. Angle in integer degrees.
- The `set` flag tells you whether the user overrode the default.

### Animations and beziers

`hyprctl -j animations` returns `[animations[], beziers[]]` [LOCAL]:

- Animation entry: `{name, overridden, bezier, enabled, speed, style}`. An entry with `overridden: false` carries `speed: 0` and `bezier: ""`; walk up the documented tree (`layersIn` -> `layers` -> `global`) until `overridden: true`. Here: `global` = speed 8, bezier `default`; `windows` = speed 6, bezier `wind`, style `slide`; `border` = speed 1, bezier `liner`.
- Bezier entry: `{name, X0, Y0, X1, Y1}`, the two middle control points of a cubic bezier, e.g. `wind = (0.05, 0.9, 0.1, 1.05)`, `default = (0.0, 0.75, 0.15, 1.0)`. The implicit `default` and `linear` are always present.
- 0.55 added **spring** curves (`hl.curve(name, {type = "spring", mass, stiffness, damping})`) [PRIMARY: wiki Animations, 0.55.0 release notes "animations: add springs"]. If the chosen curve is not in the bezier list, fall back to a fixed ease-out.

Use the user's `windows` curve (overshoot and all) as the easing of the HUD's own enter/exit so it moves like their windows, but with a fixed short duration (120 to 160 ms) rather than their speed. Evaluate the cubic bezier in Python once into a 64-entry lookup table; per frame it is a table read.

### Colors beyond the border

Order of preference for accent and surface colors:

1. `general:col.active_border` first stop = accent; last stop = secondary accent. Works on every Hyprland rice with no assumptions.
2. Rice-specific palettes when present, read-only and optional: HyDE Wallbash `~/.config/hypr/themes/colors.conf` (`$wallbash_pry1..4`, `$wallbash_txt1..4`, `$wallbash_Nxa1..9`, all plain hex) [LOCAL]; Omarchy `~/.config/omarchy/current/theme/` (not present here, path from memory of Omarchy 3, not re-verified for 4).
3. `gsettings get org.gnome.desktop.interface color-scheme` for dark or light (here `prefer-light` in gsettings while the HyDE theme file says `prefer-dark`, so do not trust it alone; derive darkness from the luminance of the inactive border or the Wallbash text color instead).

Re-read the look on socket2 `configreloaded` and on HyDE theme switches (which trigger a reload).

---

## 7. Drawing the HUD: styling traps found on this machine

**The user's `~/.config/gtk-4.0/gtk.css` overrides application CSS.** GTK loads that file at `GTK_STYLE_PROVIDER_PRIORITY_USER` (800), above `APPLICATION` (600). HyDE writes a 160 KB theme there which styles `.background`. In my first probe, `window { background: transparent; }` at APPLICATION priority lost: the surface corners outside the rounded panel were filled with the theme's dark navy instead of being transparent [LOCAL, screenshot `uitest/corner.png`].

Fix, verified in the second probe (`uitest/ring_corner.png` shows true transparency):

```python
win.remove_css_class("background")
Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
```

Also: give every HUD widget a unique CSS name or class (`hyprvoice-pill`), set every property you depend on explicitly (font, color, padding, border, box-shadow, min sizes), and do not rely on libadwaita. Do not use `Adw.Application`; plain `Gtk.Application` with `Gtk.ApplicationWindow` avoids dragging in Adwaita's stylesheet and its color-scheme machinery.

Rounded corners: draw the panel with `border-radius` in CSS (or a `GskRoundedRect`), using `decoration:rounding` as the outer radius. For nested elements use `inner = max(outer - padding, 2)`. With `rounding_power` above 2 the compositor draws squircle-like corners that CSS cannot match exactly; a slightly smaller radius hides the mismatch [INFERENCE].

Border: a 1 to 2 px gradient stroke using the user's active border stops and angle is the single strongest "this belongs to my compositor" cue. In CSS: `border: 2px solid transparent; background: linear-gradient(panel, panel) padding-box, linear-gradient(45deg, #a4a4a4, #4e4e4e) border-box;` (GTK4 CSS supports multiple backgrounds with `padding-box` / `border-box` origins) or draw it with `Gtk.Snapshot.append_border` / a stroked `Gsk.Path` filled by `push_stroke` + `append_linear_gradient`.

---

## 8. 60 fps waveform on a UHD 620: what to use

### Renderer

- GTK 4.14 introduced the unified `ngl` and `vulkan` renderers; they add antialiasing, proper fractional scaling, unlimited gradient stops and dmabuf support; GTK 4.16 made Vulkan the default on Wayland [PRIMARY: https://blogs.gnome.org/gtk/2024/01/28/new-renderers-for-gtk/ ; SECONDARY: https://www.phoronix.com/news/GTK-4.16-Released]. On this machine GTK 4.22.4 selects `VulkanRenderer` with no environment override [LOCAL].
- `GSK_RENDERER` accepts `vulkan`, `ngl`, `gl`, `cairo` [PRIMARY: https://docs.gtk.org/gtk4/running.html]. In 4.22 both `ngl` and `gl` resolve to a class named `GLRenderer` here [LOCAL].
- The GTK blog's advice that "very old hardware ... may be better off with the old GL renderer" does not apply: Kaby Lake Gen9 has full Vulkan via ANV.

Measured on the 520 x 96 HUD, one stroked 96-point waveform path per frame, 3 to 5 s runs, machine loaded [LOCAL]:

| Renderer | fps mean | frame p50 | frame p95 | time from spawn to mapped | RSS |
| --- | --- | --- | --- | --- | --- |
| Vulkan (default) | 52.7 to 56.5 | 16.6 ms | 17.1 to 33 ms | 1.25 to 1.57 s | 74 MB |
| ngl | 43.5 to 58.6 | 16.6 ms | 16.8 to 50 ms | 1.37 to 1.92 s | 108 to 112 MB |
| gl | 45.0 to 57.2 | 16.6 ms | 16.9 to 68 ms | 2.27 s | 108 MB |
| cairo | 54.2 to 54.5 | 16.7 ms | 20 to 33 ms | 1.43 s | 67 MB |

All four hold the 16.6 ms vsync cadence at the median; the misses are scheduler noise from the load, not GPU limits. **Recommendation: leave the default (Vulkan)**: lowest GPU-path memory, best startup, and it is the path GTK upstream tests most. Expose `GSK_RENDERER` passthrough in the config for escape hatches; do not hardcode one.

### Drawing API: the cost is Python, not the GPU

Same surface, same renderer, different ways to build the frame [LOCAL]:

| Technique | Python time inside `do_snapshot` (p50 / p95) | fps mean | frame p95 |
| --- | --- | --- | --- |
| 48 bars, each `Gsk.RoundedRect` + `push_rounded_clip` + `append_color` + `pop` | 6.4 to 8.6 ms / 17.8 to 33 ms | 40 to 53 | 33 to 50 ms |
| one `Gsk.PathBuilder` polyline + one `append_stroke` | 0.9 ms / 4.4 ms | 55.4 | 19.5 ms |
| `snapshot.append_cairo()` and 48 rounded bars in pycairo | 1.1 ms / 4.7 ms | 56.8 | 17.3 ms |

[pass 2] Re-run on 2026-09-21 with CPU accounting, 5 s each, load average 11 to 13 from sibling jobs (`uitest/rerun.py`) [LOCAL]:

| GSK renderer | Draw technique | fps mean | frame p50 / p95 / max (ms) | Python in `do_snapshot` p50 / p95 (ms) | CPU user + sys for the whole 5 s run incl. startup |
| --- | --- | --- | --- | --- | --- |
| Vulkan (default) | one Gsk.Path stroke | 59.0 | 16.66 / 16.93 / 33.4 | 0.67 / 2.19 | 1.14 + 0.19 s |
| Vulkan (default) | `append_cairo`, 48 bars | 59.2 | 16.66 / 16.89 / 33.4 | 0.61 / 1.62 | 1.38 + 0.29 s |
| Vulkan (default) | 48 x rounded clip nodes | 56.3 | 16.66 / 30.64 / 82.8 | 3.50 / 10.61 | 1.71 + 0.26 s |
| `ngl` (GLRenderer) | one Gsk.Path stroke | 56.7 | 16.64 / 29.45 / 83.4 | 0.61 / 2.71 | 1.62 + 0.29 s |
| `cairo` (CairoRenderer) | one Gsk.Path stroke | 57.4 | 16.65 / 17.27 / 66.7 | 0.61 / 2.72 | 1.46 + 0.19 s |

Same ranking as the first pass, tighter numbers: Vulkan with a single path or a single cairo context holds 59 fps with a p95 of 16.9 ms on the UHD 620 even while the machine is oversubscribed. Roughly 0.5 s of each CPU figure is interpreter and GTK startup, which leaves on the order of 0.15 to 0.2 CPU-seconds per second of 60 fps animation, that is 15 to 20 percent of one core, only while the waveform is actually moving [INFERENCE from the table]. The per-node variant is the only one that visibly drops frames.

So the rule is: **minimise PyGObject calls per frame.** Each GI call costs tens of microseconds; 48 x 5 boxed-type calls per frame eats half the frame budget, while one path or one cairo context does not. `Gsk.Path`, `Gsk.PathBuilder`, `Gtk.Snapshot.append_fill` and `append_stroke` exist since GTK 4.14.

Ranking for this project:

1. **Gsk.Path + `append_stroke` / `append_fill`** for the waveform: GPU-rasterised, antialiased, one node. For bars, add 24 to 32 rounded rects to a single `PathBuilder` (`add_rounded_rect`) and fill once.
2. **pycairo via `append_cairo`** is an equally good fallback at this size: a 520 x 96 texture upload per frame is trivial. It would not scale to a full-monitor animated surface.
3. **CSS transitions and `@keyframes`** for everything that is not data-driven (state color changes, the listening pulse, opacity, a `Gtk.Revealer` slide). They run on the frame clock in C with zero Python per frame.
4. **GL shaders: avoid.** `GskGLShader` "was deprecated in GTK 4.16 after the new rendering infrastructure introduced in 4.14 did not support it" [PRIMARY: https://docs.gtk.org/gsk4/class.GLShader.html]. The supported route is a `Gtk.GLArea`, which needs PyOpenGL or hand-rolled ctypes, its own GL context and an extra framebuffer blit under the Vulkan renderer. Not worth it for a pill-sized waveform. If a shader-grade visual ever becomes a goal, that is the argument for the Quickshell frontend (`ShaderEffect`), not for GLArea in Python.

Animation plumbing:

- Drive animation with `widget.add_tick_callback(cb)`; read time from `frame_clock.get_frame_time()` (microseconds); call `queue_draw()`; **return `GLib.SOURCE_REMOVE` the moment the HUD is idle or hidden**. With no tick callback and no CSS animation running, GTK schedules no frames and the process sleeps in poll. Hyprland is damage-tracked, so a static HUD costs nothing in the compositor either.
- Feed audio level from the engine at 30 to 60 Hz as a tiny message (`{"t":"level","rms":0.31,"bands":[...]}`), write it into a ring buffer, and let the tick callback interpolate (one-pole smoothing, attack about 30 ms, release about 150 ms). The UI must never block on the socket: use `GLib.io_add_watch` on the socket fd or `Gio.SocketClient` async. [pass 2] asyncio on the GLib loop is available if wanted: under Python 3.14.7 with PyGObject 3.56.3, `gi.events` exports `GLibEventLoop` and `GLibEventLoopPolicy`, `Gio.Application.create_asyncio_task` exists, and `asyncio.sleep(0.05)` ran correctly on a loop obtained from `GLibEventLoopPolicy` (`uitest/aio_probe.py`). Installing the policy raises a `DeprecationWarning` on 3.14 because event loop policies are deprecated there, and my first quick attempt at the policy-free `create_asyncio_task` path hung (I did not investigate; likely my misuse). For a display-only HUD the plain GLib watch is simpler and has no such edge.
- Pre-create everything at daemon start: windows (hidden), `Pango.Layout`s, fonts, the bezier lookup table. My first full-monitor snapshot cost 70 ms (font load and first-time pipeline work); that must happen at startup, not on the first utterance. A warm-up trick: render once into `Gtk.Snapshot` off-screen, or map the hint surface once fully transparent at login.

Latency of showing a resident window: `set_visible(True)` -> `openlayer` event, p50 9.2 ms, min 3.6 ms, max 27 ms over 11 cycles at load average 13 [LOCAL]. That is well inside one or two frames. If even that is too much, keep the pill mapped at all times and animate its opacity to zero when idle (an idle, fully transparent 520 x 96 overlay surface is harmless, but it still defeats direct scanout for fullscreen apps, so prefer unmapping when the active window is fullscreen).

---

## 9. Highlight rings and numbered hint badges

### Geometry model

- `hyprctl -j clients`: `at: [x, y]` and `size: [w, h]` are in **global logical layout coordinates**, the same space as `cursorpos` and hypruse's coordinate contract (`ARCHITECTURE.md`, "The coordinate contract"). The box is the window **without** its border; Hyprland draws the border outside that box. Evidence here: a lone tiled window sits at `at [7,7]`, size `[1906,1066]` on a 1920x1080 monitor with `gaps_out 5` and `border_size 2` (5 + 2 = 7).
- `hyprctl -j monitors`: `x, y` (global logical origin), `width, height` (**physical pixels of the mode**), `scale`, `transform`, `reserved` (four ints, space taken by exclusive zones, in the order **left, top, right, bottom**: [pass 2] `src/debug/HyprCtl.cpp:281-282` prints `m_reservedArea.left(), .top(), .right(), .bottom()` [PRIMARY]; all zero on this machine), `activeWorkspace.id`, `specialWorkspace`, `focused`.
- Logical monitor size = `width / scale` by `height / scale`, swapped for transforms 1, 3, 5, 7.
- A layer surface anchored to all four edges with exclusive zone `-1` has its local origin at the monitor's logical origin, and GTK draws in logical (application) pixels. Therefore:

```
local_x = client.at[0] - monitor.x
local_y = client.at[1] - monitor.y
```

No scale factor enters the drawing code; GTK and the compositor handle buffer scale (including fractional scale, which the unified renderers support natively). Verified at scale 1 only: the ring landed exactly on the compositor's own border with matching corner arcs (`uitest/ring_corner.png`). **Not verified at fractional scale or with a transformed monitor** (single 1x display here); expect up to 1 px rounding disagreement with Hyprland's own border at scales like 1.25.

- If you anchored with zone `0` instead, the surface would start below or beside bars and you would need `reserved` to correct. With `-1` you do not need `reserved` at all, except to avoid placing the pill on top of a bar.

### Which windows get a badge

From one batched read (`j/monitors`, `j/clients`, `j/activewindow`, `j/workspaces`):

- keep clients with `mapped == true`, `hidden == false` (hidden means a non-selected member of a group), and `workspace.id` equal to a monitor's `activeWorkspace.id`, or to its open `specialWorkspace.id`;
- if any client on that workspace has `fullscreen != 0`, only that client is visible;
- `pinned` floating windows show on every workspace of their monitor;
- order badges by reading order (top, then left) for stable numbering, and put floating windows after tiled ones, using `focusHistoryID` to break ties;
- badge position: top-left inside the window at `(x + 10, y + 10)`; if two badges collide (stacked floating windows), nudge along x.

This list is also exactly the option list to hand to Jev as a `choice` question ("which window did the user mean"), so the hint numbers and the Jev option indices should be the same array. Spoken "three" then short-circuits Jev entirely.

### Drawing (verified)

```python
ring_w = border_size + 1
outer = Gsk.RoundedRect()
outer.init_from_rect(Graphene.Rect().init(x - ring_w, y - ring_w, w + 2*ring_w, h + 2*ring_w),
                     rounding + ring_w)
snap.append_border(outer, [ring_w]*4, [rgba]*4)      # one GskBorderNode per window

layout = widget.create_pango_layout("3")             # cache one layout per digit
layout.set_font_description(Pango.FontDescription.from_string("JetBrainsMono Nerd Font Bold 12"))
_, ext = layout.get_pixel_extents()
snap.save(); snap.translate(Graphene.Point().init(bx + (bw-ext.width)/2 - ext.x, by + (bh-ext.height)/2 - ext.y))
snap.append_layout(layout, fg); snap.restore()
```

`append_border` takes one color per side, so a gradient ring needs `push_mask`/`push_stroke` with `append_linear_gradient`, or simply use the accent's first stop as a solid color. Solid reads better for a transient highlight anyway.

The hint surface is static between events: draw once per change, no tick callback. Measured process cost for a 3 s lifetime including startup: 0.8 s user CPU (all startup), 79 MB RSS [LOCAL].

### Keeping rings in sync

socket2 gives `openwindow`, `closewindow`, `movewindow(v2)`, `activewindow(v2)`, `workspace(v2)`, `fullscreen`, `changefloatingmode`, `focusedmon(v2)`, `configreloaded`, `openlayer`, `closelayer` (hypruse's `events.py` already parses most of these). **I found no event that fires when a tiled window's geometry changes** because a neighbour opened, closed or was resized; neither the legacy socket2 list nor the 0.56 Lua event list (`window.open`, `window.close`, `window.active`, `window.fullscreen`, `window.move_to_workspace`, `workspace.active`, `monitor.layout_changed`, ...) contains a move/resize event [PRIMARY: wiki Events page]. [pass 2] Confirmed against the code rather than the docs: the full set of socket2 event names emitted anywhere in the 0.56.2 tree is `activelayout, activespecial(v2), activewindow(v2), bell, changefloatingmode, changeworkspaceid, closelayer, closewindow, configreloaded, createworkspace(v2), custom, destroyworkspace(v2), focusedmon(v2), fullscreen, kill, lockgroups, minimized, monitoradded(v2), monitorremoved(v2), moveintogroup, moveoutofgroup, movewindow(v2), moveworkspace(v2), openlayer, openwindow, pin, renameworkspace, screencast(v2), submap, togglegroup, urgent, windowtitle(v2), workspace(v2)` (grep for `.event = "..."` over `src/`), and the Lua `HL.EventName` union in `/usr/share/hypr/stubs/hl.meta.lua:6-36` has no geometry event either. `movewindow` means "moved to another workspace", not "moved on screen". Consequences:

- Hint mode should be short-lived and modal in spirit: show, wait for the number, act, hide.
- While hints are visible, re-read `j/clients` on every relevant socket2 event and additionally poll at 10 to 20 Hz; at 0.33 ms per read that is free. Hide hints immediately on `workspace`, `fullscreen` or `focusedmon`.
- Hyprland animates window moves over hundreds of ms while `clients` already reports the **goal** geometry. Client-drawn rings will therefore lead the window during animations. Another reason to keep client-side rings for static hint mode only.

### Better for single-target feedback: let the compositor draw the ring

Verified on 0.56.2 [LOCAL]:

```
hyprctl dispatch setprop address:0x55a4... active_border_color rgb(59c8ff)     -> ok
hyprctl dispatch setprop address:0x55a4... border_size 4                         -> ok
hyprctl -j getprop address:0x55a4... border_size                                 -> {"border_size": 4}
hyprctl dispatch setprop address:0x55a4... active_border_color unset             -> ok (reverts to config)
```

`setprop` props are "any of the dynamic effects of Window Rules"; `border_color` expands to `active_border_color` and `inactive_border_color` [PRIMARY: 0.54 wiki Dispatchers; Lua form `hl.dsp.window.set_prop({ window = ..., prop = ..., value = ... })` in the current wiki]. The compositor then animates the color with the user's own `border` animation, follows every move and resize perfectly, respects `rounding_power`, and costs the HUD nothing. Flash pattern: set accent color and `border_size relative 1`, then `unset` both after about 450 ms. For a target that is not the active window, set `inactive_border_color` as well.

One oddity: when I passed a two-stop gradient through `setprop` (`rgba(59c8ffff) rgba(ff00ffff) 90deg`), `getprop` reported only the last color (`ffff00ff 90deg`). Single colors round-trip correctly. Use a single color for the flash, or re-test before relying on gradients [LOCAL].

hypruse already owns a related mechanism (`trust.py` tags owned windows and `hyprctl.border_rule()` installs a `border_color` rule per tag, in both dialects). The fork should route the flash through the same dialect-aware `dispatch()` so it works on Lua desktops.

---

## 10. Visual language: what popular Hyprland rices look like in 2026

Evidence [PRIMARY unless noted]:

- **Omarchy 4**: Quickshell shell; "Switch to the lighter basic JetBrains Mono Nerd font"; a single text-size knob of 9 to 20 px across shell, GTK and terminals; themes expanded "from base 8 to 24" colors; shell surfaces appear without compositor animation.
- **end-4 illogical-impulse** (16k stars, QML): font package lists `otf-space-grotesk`, `ttf-jetbrains-mono-nerd`, `ttf-material-symbols-variable-git`, `ttf-readex-pro`, `ttf-rubik-vf`, `ttf-twemoji` (its `illogical-impulse-fonts-themes` PKGBUILD). Material You, wallpaper-derived palette.
- **Caelestia shell** (12k stars, QML, "A fluid, morphing shell"): depends on `ttf-material-symbols-variable`, `ttf-rubik-vf`, `ttf-cascadia-code-nerd`; config defaults show `Rubik` for clock and workspaces, `CaskaydiaCove NF` mono, `Material Symbols Rounded` icons.
- **HyDE** (9.6k stars, this user's rice): Wallbash wallpaper-derived palette, blurred rofi/swaync/waybar layers with `ignore_alpha`, 2 px gradient borders at 45 degrees, rounding 10, shadows off.
- Shared traits: dark translucent surfaces over compositor blur, large radii (10 to 20 px), 1 to 2 px borders, pill shapes, Nerd Font or Material Symbols glyphs for icons, one accent color derived from the wallpaper or a named palette (Catppuccin, Rose Pine, Gruvbox, Tokyo Night), spring or overshoot easing [the palette names are common knowledge of the scene, SECONDARY/INFERENCE].

Design direction for the HUD (act, do not talk):

- **Form**: one floating pill, bottom center, about 48 px above the edge (or above the bar if the bar is at the bottom; read `reserved`). Height about 44 to 56 px at rest. It grows horizontally with content, never vertically beyond two lines.
- **Content by state**: `listening` = mic glyph + live waveform; `hearing` = partial transcript in the mono font, dimmed; `resolved` = a terse action chip such as `focus  kitty  ws 8` with the confidence as a thin underline whose length is Jev's confidence; `done` = accent flash for 150 ms then fade out by 600 ms; `ambiguous` = hint mode with badges; `error / not understood` = a small horizontal shake and a desaturated border, no text wall.
- **Type**: `JetBrainsMono Nerd Font` for the transcript and action chip (installed here, shipped by Omarchy and end-4), falling back through `CaskaydiaCove Nerd Font`, `monospace`. Use Nerd Font codepoints for the mic and state glyphs so no icon theme is needed. Resolve the family with fontconfig at startup and let the config override it.
- **Color**: panel = the darkest stop of `col.inactive_border` or Wallbash `1xa1`, alpha 0.72 with blur (0.92 when blur is globally disabled); text = Wallbash text color or a luminance-based choice; accent = first stop of `col.active_border`; border = the user's active border gradient at their angle.
- **Motion**: enter = 140 ms scale 0.96 -> 1 plus opacity 0 -> 1 using the user's `windows` bezier; exit = 110 ms opacity; never slide from off-screen (that reads as a notification, and the compositor's 800 ms inherited layer animation is disabled anyway).
- **Speed perception**: show the pill on wake/hotkey within one frame and start the waveform immediately, before any network call. Paint the resolved action the moment Jev's answer lands, and perform the compositor-side border flash in the same batch as the dispatch.

---

## 11. Architecture sketch for the UI lane

```
voice engine (Python, asyncio)  --unix socket, NDJSON-->  hyprvoice-hud (system python, GTK main loop)
        |                                                        |
        |  state: idle|listening|hearing|resolving|resolved|...  |-- window A: ns hyprvoice-hud  (pill, per monitor)
        |  level: rms + bands @ 30-60 Hz                         |-- window B: ns hyprvoice-hints (full monitor, on demand)
        |  hints: [{n, address, at, size, label}]                |
        |  flash: {address}  (engine does setprop itself)        |-- hyprland .socket.sock (look + geometry, batched)
                                                                 |-- hyprland .socket2.sock (configreloaded, monitor*, workspace, fullscreen)
```

- Separate process isolates the load-order requirement, lets the HUD crash or be restarted without losing the mic, and makes the frontend swappable (GTK now, Quickshell later). One-way protocol, HUD is display only, engine never waits on it.
- systemd user unit `hyprvoice-hud.service`, `PartOf=graphical-session.target`, `Environment=GTK_A11Y=none`, restart on failure.
- Startup order: probe config manager -> install layer rules in the right dialect -> read look -> create hidden windows per monitor -> warm fonts and first snapshot -> listen.
- Tests without a compositor: the look parser (gradient padding, gaps, animation tree walk, bezier table), the visible-window filter, and the coordinate mapping are pure functions and should be unit-tested from captured JSON fixtures, matching hypruse's existing `snapshot_from()` style. Live layer-shell behaviour needs a real Hyprland session (the repo memory notes headless Hyprland needs a QEMU virtio-gpu VM).

---

## 12. Not found / not verified

- No primary documentation of hyprlang layer rule syntax for 0.55 or 0.56: the versioned wiki switched to Lua only at 0.55.0. The hyprlang grammar above comes from the 0.54.0 wiki plus live acceptance tests on 0.56.2.
- The Lua path (`hl.layer_rule`, `hyprctl eval`, `getoption` under the Lua manager) was checked against the shipped stub, the wiki and the C++ source, but **not executed**: this machine runs the hyprlang manager.
- No socket2 or Lua event for window geometry changes (move/resize by the layout). Looked in the wiki Events page for both dialects and, in pass 2, in every `.event = "..."` site of the 0.56.2 source and the shipped Lua stub. This is a confirmed absence, not a gap in the search.
- Click-through: resolved in pass 2 by a hover test with a control run (section 4). Still not done: an actual button press through the HUD, and a hover test of the Qt `WindowTransparentForInput` flag.
- Coordinate mapping verified only at scale 1.0, transform 0, single monitor.
- `animation = "none"`: resolved in pass 2 (section 5). It is not a named style; the rule value is unvalidated and anything other than `slide*` or `popin*` means fade only.
- Two-stop gradients through `setprop active_border_color` appeared to lose the first stop; cause not investigated.
- `Gio.Application.create_asyncio_task` hung in one quick test on Python 3.14.7; not investigated. The `GLibEventLoopPolicy` route worked.
- Omarchy 4 theme file locations for palette import were not re-verified.
- No measurement of Hyprland-side GPU cost of blur under the pill on the UHD 620 (no compositor frame-time counters were sampled). Expected to be small for a 520 x 96 region; unmeasured.
- No Quickshell measurement was taken (not installed, and I did not install packages). Plain Qt Quick via `qml6` + layer-shell-qt was measured for cold start and RSS in pass 2 (section 3), but not for animation frame pacing.

---

## 13. Source list

Primary:
- gtk4-layer-shell linking notes: https://github.com/wmww/gtk4-layer-shell/blob/main/linking.md
- gtk4-layer-shell releases (1.1.0 to 1.3.0): https://github.com/wmww/gtk4-layer-shell/releases
- wlr-layer-shell protocol: https://wayland.app/protocols/wlr-layer-shell-unstable-v1
- PyGObject NEWS: https://gitlab.gnome.org/GNOME/pygobject/-/raw/main/NEWS
- Hyprland wiki, layer rules (git, Lua): https://wiki.hypr.land/Configuring/Basics/Window-Rules/ and source https://github.com/hyprwm/hyprland-wiki/blob/main/content/configuring/core/rules/layer-rules.md
- Hyprland wiki 0.56.0 (Lua): https://wiki.hypr.land/0.56.0/Configuring/Basics/Window-Rules/
- Hyprland wiki 0.54.0 (hyprlang): https://wiki.hypr.land/0.54.0/Configuring/Window-Rules/ and https://wiki.hypr.land/0.54.0/Configuring/Dispatchers/
- Hyprland wiki animations, hyprctl, events: https://github.com/hyprwm/hyprland-wiki/tree/main/content/configuring/core
- Hyprland source v0.56.2: `src/desktop/rule/layerRule/LayerRuleEffectContainer.cpp`, `src/animation/AnimationManager.cpp`, `src/debug/HyprCtl.cpp`, `src/config/lua/ConfigManager.cpp`, `src/config/shared/complex/ComplexDataTypes.hpp` under https://github.com/hyprwm/Hyprland/tree/v0.56.2
- Hyprland releases v0.55.0 and v0.56.0: https://github.com/hyprwm/Hyprland/releases
- Shipped files on this machine: `/usr/share/hypr/hyprland.lua`, `/usr/share/hypr/stubs/hl.meta.lua`
- GTK renderers blog: https://blogs.gnome.org/gtk/2024/01/28/new-renderers-for-gtk/
- GskGLShader deprecation: https://docs.gtk.org/gsk4/class.GLShader.html
- GTK running and environment variables: https://docs.gtk.org/gtk4/running.html
- Quickshell docs v0.3.1 and changelog: https://quickshell.org/docs/v0.3.1/types/Quickshell/PanelWindow/ , https://quickshell.org/changelog/
- layer-shell-qt README: https://invent.kde.org/plasma/layer-shell-qt
- Omarchy v4.0.0 release: https://github.com/omacom/omarchy/releases/tag/v4.0.0 ; shell layer rules: https://github.com/omacom/omarchy/blob/quattro/default/hypr/apps/omarchy-shell.lua
- Caelestia shell README: https://github.com/caelestia-dots/shell ; end-4 fonts PKGBUILD: https://github.com/end-4/dots-hyprland/blob/main/sdata/dist-arch/illogical-impulse-fonts-themes/PKGBUILD

Secondary:
- Phoronix on GTK 4.16 Vulkan default: https://www.phoronix.com/news/GTK-4.16-Released
- Phoronix on Omarchy 4.0: https://www.phoronix.com/news/Omarchy-4.0-Released

Local:
- hypruse: `/home/ilyask/projects/hypruse/src/hypruse/hyprctl.py`, `/home/ilyask/projects/hypruse/src/hypruse/events.py`, `/home/ilyask/projects/hypruse/ARCHITECTURE.md`, commit `ab9db49`
- Probes and captures: `/tmp/claude-1000/-home-ilyask-projects-hypruse/4e68274c-d47c-460a-a4ca-e5138b66c779/scratchpad/uitest/`

## Verification

Independent skeptical pass, 2026-09-21. Read-only checks only: no windows mapped, no hyprctl keyword/setprop issued, no Jev calls. Script: scratchpad/v_ui.py.

1. PyGObject on Python 3.14 plus layer-shell preload order: CONFIRMED (partly). `python3 -c "import gi"` gives 3.14.7 / gi 3.56.3; pacman shows gtk4 4.22.4, gtk4-layer-shell 1.3.0. Without preload `Gtk4LayerShell.is_supported()` returned False, with `CDLL('libgtk4-layer-shell.so')` before `import gi` it returned True. NEWS 3.54.0 has "Fix compatibility with Python 3.14 (mr 433)". I did not map a HUD, so "level 3, 520x96, no focus" and the "tiled toplevel took focus" fallback are not re-verified.
2. Layer rule effect names and single match prop: CONFIRMED. LayerRuleEffectContainer.cpp EFFECT_STRINGS lists exactly the ten names. CLayerRule::matches (LayerRule.cpp:95-113) only handles RULE_PROP_NAMESPACE, regex engine (Rule.cpp:69). Nuance: the parser accepts any window match prop (match:class etc.) without error and silently skips it at match time, so a typo in the prop name class is not reported. Error strings "missing a value" and "invalid field type" match handleLayerrule source. The keyword tests were not re-run (would change the desktop).
3. hyprlang and Lua layer rule syntax: CONFIRMED from source. ConfigManager.cpp:2057-2093 loops over comma separated elements, each "name value" or "match:prop value", so several effects per line are valid. hl.meta.lua:555-568 HL.LayerRuleSpec and hyprland.lua:341-347 example match the claim. Lua path remains unexecuted.
4. Lua landed in 0.55.0, machine runs hyprlang: CONFIRMED. GitHub API release v0.55.0 published 2026-05-09T13:41:46Z, body contains "config/lua: init lua config manager, use lua if available (#13817)". `hyprctl -j status` returns configProvider "hyprlang". Wiki claim not checked.
5. Runtime rule install re-applies to mapped layers: CONFIRMED. HyprCtl.cpp:1240-1246 calls reloadRules() and damages monitors; ConfigManager.cpp:946-967 clears rules, re-registers m_keywordRules, calls updateAllRules(); Engine.cpp:34-48 iterates mapped layers with propertiesChanged(RULE_PROP_ALL). handleLayerrule does emplace_back each call, so rules accumulate (build must avoid re-issuing per show). Lua in-place update line not checked.
6. Reading the live look and socket latency: PARTIALLY CORRECT. Colon form works, dot form returns "no such option"; gradient returned "ffa4a4a4 ff4e4e4e 45deg"; animations returns a 2 element list with 'overridden', layersIn not overridden, global speed 8.0. Correction: "gaps come back as 5 5 5 5" is gaps_out; gaps_in is "2 2 2 2" on this machine. My batch of 11 getoption queries measured p50 0.17 ms (researcher 0.60 ms, different query mix), j/clients p50 0.33 ms, hyprctl subprocess p50 17.4 ms, at load average 11. Order of magnitude holds; treat the exact batch number as mix dependent.

Extra spot checks: fullscreen/overlay code lines match (LayerSurface.cpp:330, FullscreenHandler.cpp:274-277, InputManager.cpp:502). `.event =` grep shows no resize/move geometry event. setprop active_border_color and "unset" exist in config/shared/actions/ConfigActions.cpp:704,756 (not HyprCtl.cpp); not executed. quickshell 0.3.1-1 in extra, not installed; layer-shell-qt 6.7.4 installed. Rendering fps, click-through, ring alignment and Qt startup numbers were not re-measured (would require mapping surfaces).
