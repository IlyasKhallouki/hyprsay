#!/usr/bin/python3
"""The hyprsay HUD: a display-only overlay for a push-to-talk voice command.

Run by the SYSTEM interpreter (/usr/bin/python3), never from the engine's venv:
PyGObject is a distro package that a venv cannot see, and the speech model is a pip
wheel that the system cannot. So this file imports nothing from src/hyprsay and nothing
beyond the standard library, gi and pycairo (which PyGObject itself depends on). The few
protocol constants it needs are copied from hyprsay/hudproto.py, and a unit test over
there compares the two copies.

Facts this file is built on, all measured on Hyprland 0.56.2 with GTK 4.22 and
gtk4-layer-shell 1.3 (docs/research/ui.md):

- libgtk4-layer-shell.so must be loaded BEFORE gi. If libwayland wins the race the
  window silently becomes an ordinary toplevel, Hyprland tiles it AND focuses it, and a
  voice HUD that takes the keyboard from the app being dictated into is the worst bug
  this program could have. So: CDLL first, and refuse to start when unsupported.
- Only the overlay layer is drawn above a fullscreen window, and "exit fullscreen" is a
  command. An overlay surface also defeats direct scanout for a fullscreen client, so
  both surfaces are unmapped whenever there is nothing to show.
- An empty input region makes a surface pointer-transparent. It is re-applied on every
  map and on every layout, keyboard mode is NONE, and nothing here can be clicked.
- The user's ~/.config/gtk-4.0/gtk.css is loaded at PRIORITY_USER and paints
  `.background`; without removing that class and loading our CSS one priority higher,
  the transparent corners of the pill come out in the theme's window color.
- A layer rule can choose the compositor's animation style but not its speed, and this
  desktop's layer animation inherits 800 ms. So the rule is `no_anim` and the HUD
  animates inside its own surface: 140 ms in, 110 ms out, on the frame clock, with the
  tick callback removed the moment nothing moves. An idle HUD schedules no frames.
- Badge rectangles arrive in Hyprland global logical pixels. A surface anchored to all
  four edges with exclusive zone -1 starts at its monitor's logical origin, so local
  coordinates are `global - monitor origin` and no scale factor enters the drawing.

Two surfaces: `hyprsay-hud`, the pill, and `hyprsay-hints`, one full-monitor surface
per monitor that is mapped only while numbered badges are up.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import socket
import sys
import time
import tomllib
from collections import deque
from ctypes import CDLL
from dataclasses import dataclass
from pathlib import Path

# keep the HUD out of the AT-SPI tree that the engine's inherited tools walk
os.environ.setdefault("GTK_A11Y", "none")
try:
    CDLL("libgtk4-layer-shell.so")
except OSError:
    sys.exit("hyprsay-hud: libgtk4-layer-shell.so not found; install gtk4-layer-shell")

try:
    import cairo  # noqa: E402
    import gi  # noqa: E402

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Graphene", "1.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, Gio, GLib, Graphene, Gtk, Pango, PangoCairo  # noqa: E402
    from gi.repository import Gtk4LayerShell as LS  # noqa: E402
except (ImportError, ValueError) as exc:
    sys.exit(f"hyprsay-hud: needs the system PyGObject, GTK 4 and gtk4-layer-shell ({exc})")

try:  # GLib 2.80 moved the Unix helpers; PyGObject warns about the old spelling
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # noqa: E402

    unix_signal_add = GLibUnix.signal_add
except (ImportError, ValueError, AttributeError):
    unix_signal_add = GLib.unix_signal_add

# ---- copied from src/hyprsay/hudproto.py; tests/hyprsay/test_hudproto.py compares them
PROTO = 1
RUNTIME_SUBDIR = "hyprsay"
SOCKET_NAME = "hud.sock"
STATES = (
    "hidden",
    "hearing",
    "thinking",
    "still_thinking",
    "heard",
    "swap",
    "hints",
    "countdown",
    "refused",
    "suggest",
    "help",
)
# ----

NAMESPACE_HUD = "hyprsay-hud"
NAMESPACE_HINTS = "hyprsay-hints"
# ignore_alpha sits below the panel's alpha and above zero, so the panel is blurred and
# the transparent corners around it are not. The hints surface is never blurred (a full
# monitor blur pass on integrated graphics) and only needs to appear at once.
LAYER_RULES = (
    f"blur on, ignore_alpha 0.35, no_anim on, match:namespace ^{NAMESPACE_HUD}$",
    f"no_anim on, match:namespace ^{NAMESPACE_HINTS}$",
)
LOOK_QUERY = (
    "j/getoption general:border_size",
    "j/getoption general:col.active_border",
    "j/getoption decoration:rounding",
    "j/getoption general:gaps_out",
    "j/getoption decoration:blur:enabled",
    "j/status",
)

ENTER_MS = 140
EXIT_MS = 110
SLIDE_PX = 8
LIVE_STATES = frozenset({"hearing", "thinking", "still_thinking", "countdown"})
PLACEHOLDER = {
    "hearing": "listening",
    "thinking": "thinking",
    "still_thinking": "still thinking",
    "hints": "which one?",
    "swap": "say a number to switch",
    "countdown": "press the key to cancel",
    "refused": "refused",
    "suggest": "not understood",
    "help": "what you can say",
}
HEADING = {"suggest": "DID YOU MEAN", "help": "TRY", "hints": "ELSEWHERE", "swap": "ELSEWHERE"}
DEFAULTS = {
    "text": "",
    "chip": "",
    "level": 0.0,
    "badges": [],
    "countdown_ms": 0,
    "suggestions": [],
    "ttl_ms": 0,
}
METER_BARS = 9
BACKOFF_FIRST_S = 0.25
BACKOFF_MAX_S = 5.0
BACKOFF_REFUSED_S = 30.0
READ_LIMIT = 1 << 20
FONT = "JetBrainsMono Nerd Font"
WARN = (0.878, 0.467, 0.49)
TEXT = (0.91, 0.91, 0.91)


def log(message: str) -> None:
    """To journald through the unit. Never the words that were spoken, only states."""
    print(f"hyprsay-hud: {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- Hyprland


def hypr(command: str, timeout: float = 1.0) -> str:
    """One request on Hyprland's socket: 0.3 ms, where a hyprctl subprocess costs 17."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        raise OSError("HYPRLAND_INSTANCE_SIGNATURE is not set")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(f"{runtime}/hypr/{signature}/.socket.sock")
        sock.sendall(command.encode())
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def json_stream(text: str) -> list:
    """A [[BATCH]] reply is JSON documents separated by blank lines, not a JSON array."""
    decoder, found, at = json.JSONDecoder(), [], 0
    while at < len(text):
        if text[at].isspace():
            at += 1
            continue
        try:
            value, at = decoder.raw_decode(text, at)
        except ValueError:
            break
        found.append(value)
    return found


def parse_gradient(custom: str) -> tuple[list[tuple[float, float, float, float]], int]:
    """`ffa4a4a4 ff4e4e4e 45deg` -> stops as (r, g, b, a) and the angle. Hyprland prints
    AARRGGBB without zero padding, so an alpha under 0x10 comes out one digit short."""
    stops, angle = [], 0
    for token in custom.split():
        if token.endswith("deg"):
            with contextlib.suppress(ValueError):
                angle = int(float(token[:-3]))
            continue
        try:
            value = int(token, 16)
        except ValueError:
            continue
        a, r, g, b = ((value >> shift) & 0xFF for shift in (24, 16, 8, 0))
        stops.append((r / 255, g / 255, b / 255, a / 255))
    return stops[:10], angle


def luminance(color: tuple[float, ...]) -> float:
    return 0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]


@dataclass
class Look:
    """What the user's compositor looks like, so the pill looks like one of its windows."""

    border: int = 2
    rounding: int = 10
    gaps_out: int = 5
    stops: tuple = ((0.643, 0.643, 0.643, 1.0), (0.306, 0.306, 0.306, 1.0))
    angle: int = 45
    blur: bool = True
    provider: str = ""

    @classmethod
    def read(cls) -> Look:
        look = cls()
        try:
            replies = json_stream(hypr("[[BATCH]]" + ";".join(LOOK_QUERY)))
        except OSError as exc:
            log(f"could not ask Hyprland for the look, using defaults: {exc}")
            return look
        for reply in replies:
            if isinstance(reply, dict):
                look._take(reply)
        return look

    def _take(self, reply: dict) -> None:
        if "configProvider" in reply:
            self.provider = str(reply["configProvider"])
            return
        option, number, custom = reply.get("option"), reply.get("int"), reply.get("custom", "")
        if option == "general:border_size" and isinstance(number, int):
            self.border = max(0, min(number, 8))
        elif option == "decoration:rounding" and isinstance(number, int):
            self.rounding = max(0, min(number, 28))
        elif option == "decoration:blur:enabled" and isinstance(number, int):
            self.blur = bool(number)
        elif option == "general:gaps_out" and custom:
            with contextlib.suppress(ValueError):
                self.gaps_out = int(str(custom).split()[0])
        elif option == "general:col.active_border" and custom:
            stops, angle = parse_gradient(str(custom))
            if stops:
                self.stops, self.angle = tuple(stops), angle

    @property
    def accent(self) -> tuple[float, float, float]:
        """The brightest border stop, lifted when even that would be unreadable on a
        dark panel (plenty of rices run a near-black border)."""
        r, g, b = max(self.stops, key=luminance)[:3]
        lift = max(0.0, 0.45 - luminance((r, g, b)))
        return tuple(min(1.0, c + lift) for c in (r, g, b))

    @property
    def panel_alpha(self) -> float:
        return 0.74 if self.blur else 0.93

    def css(self, font: str) -> str:
        def rgba(color, alpha=None):
            r, g, b = (round(c * 255) for c in color[:3])
            a = alpha if alpha is not None else color[3] if len(color) > 3 else 1.0
            return f"rgba({r},{g},{b},{a:.3f})"

        stops = list(self.stops) if len(self.stops) > 1 else [self.stops[0]] * 2
        panel = f"rgba(14,14,16,{self.panel_alpha})"
        accent = self.accent
        inner = max(min(self.rounding - 4, 8), 2) if self.rounding else 0
        family = f'"{font}", ' if font else ""
        return f"""
.hs-pill {{
  border: {self.border}px solid transparent;
  border-radius: {self.rounding}px;
  background-image: linear-gradient({panel}, {panel}),
    linear-gradient({(self.angle + 90) % 360}deg, {", ".join(rgba(s) for s in stops)});
  font-family: {family}"JetBrains Mono", "CaskaydiaCove Nerd Font", "Noto Sans Mono", monospace;
}}
.hs-pill label.hs-chip {{
  border-radius: {inner}px;
  border-color: {rgba(accent, 0.55)};
  background-color: {rgba(accent, 0.12)};
  color: {rgba(accent, 1.0)};
}}
.hs-pill label.hs-chip.hs-plain {{
  border-color: rgba(232,232,232,0.18);
  background-color: transparent;
  color: rgba(232,232,232,0.6);
}}
.hs-pill label.hs-digit {{ color: {rgba(accent, 1.0)}; }}
"""


def install_layer_rules(look: Look) -> None:
    """Runtime rules, so no config file is edited. They vanish on a config reload, which
    is why `configreloaded` brings us back here; every call APPENDS in hyprlang, so this
    runs once per reload and never per utterance."""
    if look.provider == "lua":
        # `keyword` is refused under the Lua manager, and the `hyprctl eval` form has
        # never been executed by anyone on this project. Code run inside the compositor
        # is not the place to guess. `hyprsay setup` prints the snippet instead.
        log("config provider is lua: layer rules not installed (no blur, 800 ms fade)")
        return
    for rule in LAYER_RULES:
        try:
            reply = hypr(f"/keyword layerrule {rule}").strip()
        except OSError as exc:
            log(f"layer rule not installed: {exc}")
            return
        if reply != "ok":
            log(f"layer rule refused by Hyprland: {reply[:120]}")


def focused_monitor_name() -> str:
    try:
        monitors = json.loads(hypr("j/monitors"))
    except (OSError, ValueError):
        return ""
    return next((str(m.get("name", "")) for m in monitors if m.get("focused")), "")


# --------------------------------------------------------------------------- animation


class Fade:
    """A value easing toward a target over a fixed time, stepped from the frame clock."""

    def __init__(self) -> None:
        self.value = 0.0
        self.target = 0.0
        self._from = 0.0
        self._start: int | None = None
        self._span = 1

    def go(self, target: float, ms: int) -> None:
        # The clock starts at the first frame that is actually drawn. A window that was
        # just mapped still reports the frame time of the last time it was visible, and
        # an animation started from that would already be over.
        self._from, self.target = self.value, target
        self._start, self._span = None, max(ms, 1) * 1000

    def step(self, now_us: int) -> bool:
        """True while still moving."""
        if self._start is None:
            self._start = now_us
        t = min(max((now_us - self._start) / self._span, 0.0), 1.0)
        eased = 1 - (1 - t) ** 3
        self.value = self._from + (self.target - self._from) * eased
        return t < 1.0


def rounded(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


class Stage(Gtk.Box):
    """Draws its children shifted by `offset` pixels. A margin would move them too, but
    in whole pixels and through a relayout per frame; this is one transform node."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.offset = 0.0

    def do_snapshot(self, snapshot) -> None:
        snapshot.save()
        snapshot.translate(Graphene.Point().init(0, self.offset))
        child = self.get_first_child()
        while child is not None:
            self.snapshot_child(child, snapshot)
            child = child.get_next_sibling()
        snapshot.restore()


# --------------------------------------------------------------------------- surfaces


def _set_class(widget, name: str, on: bool) -> None:
    (widget.add_css_class if on else widget.remove_css_class)(name)


def _load_css(provider, css: str) -> None:
    if hasattr(provider, "load_from_string"):  # GTK 4.12
        provider.load_from_string(css)
    else:
        provider.load_from_data(css.encode())


def pass_through(window) -> None:
    surface = window.get_surface()
    if surface is not None:
        surface.set_input_region(cairo.Region())


def layer_window(app, namespace: str):
    window = Gtk.ApplicationWindow(application=app)
    window.remove_css_class("background")
    window.add_css_class("hs-window")
    window.set_decorated(False)
    window.set_focusable(False)
    window.set_can_focus(False)
    LS.init_for_window(window)
    LS.set_namespace(window, namespace)
    LS.set_layer(window, LS.Layer.OVERLAY)
    LS.set_keyboard_mode(window, LS.KeyboardMode.NONE)
    LS.set_exclusive_zone(window, -1)
    window.connect("map", pass_through)
    window.connect(
        "realize", lambda w: w.get_surface().connect("layout", lambda *_: pass_through(w))
    )
    return window


class Pill:
    def __init__(self, app, hud: Hud) -> None:
        self.hud = hud
        self.fade = Fade()
        self.level = 0.0
        self.history: deque[float] = deque([0.0] * METER_BARS, maxlen=METER_BARS)
        self._history_at = 0
        self._tick_id = 0
        self._countdown_from = 0.0
        self._countdown_ms = 0
        self.monitor = None
        self._from_top = hud.options.position == "top"

        self.window = layer_window(app, NAMESPACE_HUD)
        # A resizable GTK window grows with its content and then keeps that size; seen
        # live, the pill stayed 355x94 after the badges legend was gone. A window that is
        # not resizable is always exactly its natural size.
        self.window.set_resizable(False)
        edge = LS.Edge.TOP if self._from_top else LS.Edge.BOTTOM
        LS.set_anchor(self.window, edge, True)
        # the stage keeps SLIDE_PX of empty room on the side the pill slides in from
        LS.set_margin(self.window, edge, max(hud.options.margin - SLIDE_PX, 0))

        self.stage = Stage()
        # a widget is born opaque; without this the first frame would flash at full
        # strength before the fade from zero begins
        self.stage.set_opacity(0.0)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.add_css_class("hs-pill")
        self.box.set_halign(Gtk.Align.CENTER)
        self.box.set_margin_top(SLIDE_PX if self._from_top else 0)
        self.box.set_margin_bottom(0 if self._from_top else SLIDE_PX)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        row.add_css_class("hs-row")
        self.indicator = Gtk.DrawingArea()
        self.indicator.set_content_width(38)
        self.indicator.set_content_height(20)
        self.indicator.set_valign(Gtk.Align.CENTER)
        self.indicator.set_draw_func(self._draw_indicator)
        self.text = Gtk.Label(xalign=0)
        self.text.add_css_class("hs-text")
        self.text.set_single_line_mode(True)
        self.text.set_ellipsize(Pango.EllipsizeMode.END)
        self.text.set_max_width_chars(56)
        self.chip = Gtk.Label()
        self.chip.add_css_class("hs-chip")
        self.chip.set_single_line_mode(True)
        self.chip.set_valign(Gtk.Align.CENTER)
        for widget in (self.indicator, self.text, self.chip):
            row.append(widget)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.list.add_css_class("hs-list")
        self.bar = Gtk.DrawingArea()
        self.bar.add_css_class("hs-bar")
        self.bar.set_content_height(2)
        self.bar.set_draw_func(self._draw_bar)
        for widget in (row, self.list, self.bar):
            self.box.append(widget)
        self.stage.append(self.box)
        self.window.set_child(self.stage)
        self.window.connect(
            "realize", lambda w: w.get_surface().connect("layout", lambda *_: hud.pill_moved())
        )

    # ------------------------------------------------------------------ content

    def show(self, view: dict, entering: bool) -> None:
        state = view["state"]
        _set_class(self.box, "hs-refused", state == "refused")
        self.text.set_text(view["text"] or PLACEHOLDER.get(state, ""))
        # a placeholder is not something the speaker said, so it is drawn quieter
        _set_class(self.text, "hs-quiet", not view["text"])
        self.chip.set_text(view["chip"])
        self.chip.set_visible(bool(view["chip"]))
        # an accent chip is an ACTION; a reason or an instruction is plain
        _set_class(self.chip, "hs-plain", state in ("suggest", "hints", "refused"))
        self._fill_list(view)
        self.bar.set_visible(state == "countdown" and view["countdown_ms"] > 0)
        if state == "countdown" and (entering or view["countdown_ms"] != self._countdown_ms):
            self._countdown_from, self._countdown_ms = time.monotonic(), view["countdown_ms"]
        if entering and state == "hearing":
            self.level = 0.0
            self.history.extend([0.0] * METER_BARS)

        if not self.window.get_visible():
            self._place()
            self.window.present()
        if self.fade.target != 1.0:
            self.fade.go(1.0, ENTER_MS)
        self.indicator.queue_draw()
        self._ensure_ticking()

    def _fill_list(self, view: dict) -> None:
        while (child := self.list.get_first_child()) is not None:
            self.list.remove(child)
        state = view["state"]
        rows: list[tuple[str, str]] = []
        if state in ("suggest", "help"):
            rows = [("", phrase) for phrase in view["suggestions"]]
        elif view["badges"]:
            # a badge without a rectangle is not on screen; its number is still speakable
            rows = [
                (str(b["n"]), str(b.get("label", "")))
                for b in view["badges"]
                if not b["w"] or not b["h"]
            ]
        self.list.set_visible(bool(rows))
        if not rows:
            return
        heading = Gtk.Label(label=HEADING.get(state, "ELSEWHERE"), xalign=0)
        heading.add_css_class("hs-heading")
        self.list.append(heading)
        for digit, phrase in rows:
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            item.add_css_class("hs-item")
            if digit:
                number = Gtk.Label(label=digit, xalign=0)
                number.add_css_class("hs-digit")
                item.append(number)
            label = Gtk.Label(label=phrase, xalign=0)
            label.add_css_class("hs-phrase")
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(56)
            item.append(label)
            self.list.append(item)

    def _place(self) -> None:
        """Only while unmapped: the pill belongs on the monitor the user is looking at."""
        name = focused_monitor_name()
        monitors = Gdk.Display.get_default().get_monitors()
        for index in range(monitors.get_n_items()):
            monitor = monitors.get_item(index)
            if name and monitor.get_connector() == name:
                LS.set_monitor(self.window, monitor)
                self.monitor = monitor
                return

    def rectangle(self, monitor) -> tuple[float, float, float, float] | None:
        """Where the pill's surface sits on `monitor`, in that monitor's own pixels. A
        layer surface is never told its position, but one anchored edge centers it."""
        if not self.window.get_visible() or self.monitor not in (None, monitor):
            return None
        box = monitor.get_geometry()
        w, h = self.window.get_width(), self.window.get_height()
        gap = max(self.hud.options.margin - SLIDE_PX, 0)
        return (box.width - w) / 2, gap if self._from_top else box.height - gap - h, w, h

    def hide(self) -> None:
        if not self.window.get_visible() or self.fade.target == 0.0:
            return
        self.fade.go(0.0, EXIT_MS)
        self._ensure_ticking()

    # ------------------------------------------------------------------ frames

    def _ensure_ticking(self) -> None:
        if not self._tick_id:
            self._tick_id = self.stage.add_tick_callback(self._tick)

    def _tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        moving = self.fade.step(now)
        offset = (1 - self.fade.value) * SLIDE_PX * (-1 if self._from_top else 1)
        self.stage.set_opacity(self.fade.value)  # GTK ignores a value that did not change
        if offset != self.stage.offset:
            self.stage.offset = offset
            self.stage.queue_draw()
        state = self.hud.view["state"]
        if not moving and self.fade.target == 0.0:
            self._tick_id = 0
            self.window.set_visible(False)
            return GLib.SOURCE_REMOVE
        if state == "hearing":
            # one pole: fast attack so a plosive shows, slow release so the bars breathe
            target = self.hud.view["level"]
            self.level += (target - self.level) * (0.55 if target > self.level else 0.12)
            if now - self._history_at > 45_000:
                self._history_at = now
                self.history.append(self.level)
        if state in LIVE_STATES:
            self.indicator.queue_draw()
            if self.bar.get_visible():
                self.bar.queue_draw()
            return GLib.SOURCE_CONTINUE
        if moving:
            return GLib.SOURCE_CONTINUE
        # nothing moves: no callback, no frames, the process sleeps in poll
        self._tick_id = 0
        return GLib.SOURCE_REMOVE

    def countdown_left(self) -> float:
        total = self.hud.view["countdown_ms"] / 1000
        if total <= 0:
            return 1.0
        return max(0.0, 1.0 - (time.monotonic() - self._countdown_from) / total)

    # ------------------------------------------------------------------ drawing

    def _draw_bar(self, _area, cr, width, height) -> None:
        cr.set_source_rgba(*self.hud.look.accent, 0.9)
        rounded(cr, 0, 0, width * self.countdown_left(), height, height / 2)
        cr.fill()

    def _draw_indicator(self, _area, cr, width, height) -> None:
        state = self.hud.view["state"]
        accent = self.hud.look.accent
        cx, cy = width / 2, height / 2
        phase = time.monotonic()
        if state == "hearing":
            bar, gap = 2.6, 1.6
            left = cx - (METER_BARS * (bar + gap) - gap) / 2
            cr.set_source_rgba(*accent, 1.0)
            for i, value in enumerate(self.history):
                tall = 3 + (height - 3) * min(1.0, value * 1.15)
                rounded(cr, left + i * (bar + gap), cy - tall / 2, bar, tall, bar / 2)
            cr.fill()
        elif state in ("thinking", "still_thinking"):
            speed = 5.0 if state == "thinking" else 2.6
            for i in range(3):
                pulse = 0.5 + 0.5 * math.sin(phase * speed - i * 0.9)
                cr.set_source_rgba(*(accent if state == "thinking" else TEXT), 0.3 + 0.7 * pulse)
                cr.arc(cx + (i - 1) * 9, cy, 2.4, 0, 2 * math.pi)
                cr.fill()
        elif state == "countdown":
            left = self.countdown_left()
            timed = self.hud.view["countdown_ms"] > 0
            alpha = 1.0 if timed else 0.55 + 0.45 * math.sin(phase * 4.0)
            cr.set_line_width(2.4)
            cr.set_source_rgba(*TEXT, 0.16)
            cr.arc(cx, cy, 7.5, 0, 2 * math.pi)
            cr.stroke()
            cr.set_source_rgba(*accent, alpha)
            cr.arc(cx, cy, 7.5, -math.pi / 2, -math.pi / 2 + 2 * math.pi * left)
            cr.stroke()
        elif state == "refused":
            cr.set_line_width(2.2)
            cr.set_source_rgba(*WARN, 1.0)
            cr.arc(cx, cy, 7.5, 0, 2 * math.pi)
            cr.move_to(cx - 5.3, cy + 5.3)
            cr.line_to(cx + 5.3, cy - 5.3)
            cr.stroke()
        elif state == "heard":
            cr.set_line_width(2.4)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.set_source_rgba(*accent, 1.0)
            cr.move_to(cx - 6, cy)
            cr.line_to(cx - 1.5, cy + 4.5)
            cr.line_to(cx + 6.5, cy - 4.5)
            cr.stroke()
        elif state in ("hints", "swap"):
            cr.set_line_width(1.6)
            cr.set_source_rgba(*accent, 1.0)
            rounded(cr, cx - 8, cy - 8, 16, 16, 4)
            cr.stroke()
            self._glyph(cr, "#", cx, cy, accent, 10)
        else:  # suggest, help
            self._glyph(cr, "?", cx, cy, TEXT, 13)

    def _glyph(self, cr, text: str, cx: float, cy: float, color, size: int) -> None:
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(
            Pango.FontDescription.from_string(f"{self.hud.options.font} Bold {size}px")
        )
        layout.set_text(text, -1)
        ink, _logical = layout.get_pixel_extents()
        cr.set_source_rgba(*color[:3], 1.0)
        cr.move_to(cx - ink.width / 2 - ink.x, cy - ink.height / 2 - ink.y)
        PangoCairo.show_layout(cr, layout)


class Hints:
    """One full-monitor surface: a ring around each candidate window and its number."""

    BOX = 34

    def __init__(self, app, hud: Hud, monitor) -> None:
        self.hud = hud
        self.monitor = monitor
        self.badges: list[dict] = []
        self.fade = Fade()
        self._tick_id = 0
        self.window = layer_window(app, NAMESPACE_HINTS)
        for edge in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(self.window, edge, True)
        LS.set_monitor(self.window, monitor)
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.set_opacity(0.0)
        self.window.set_child(self.area)

    def owns(self, badge: dict) -> bool:
        box = self.monitor.get_geometry()
        cx, cy = badge["x"] + badge["w"] / 2, badge["y"] + badge["h"] / 2
        return box.x <= cx < box.x + box.width and box.y <= cy < box.y + box.height

    def show(self, badges: list[dict]) -> None:
        changed = badges != self.badges
        self.badges = badges
        if not badges:
            self.hide()
            return
        if changed:
            # drawn once per change: the cairo surface is cached and only its opacity animates
            self.area.queue_draw()
        if not self.window.get_visible():
            self.window.present()
        if self.fade.target != 1.0:
            self.fade.go(1.0, ENTER_MS)
            self._ensure_ticking()

    def hide(self) -> None:
        if not self.window.get_visible() or self.fade.target == 0.0:
            return
        self.fade.go(0.0, EXIT_MS)
        self._ensure_ticking()

    def _ensure_ticking(self) -> None:
        if not self._tick_id:
            self._tick_id = self.area.add_tick_callback(self._tick)

    def _tick(self, _widget, clock) -> bool:
        moving = self.fade.step(clock.get_frame_time())
        self.area.set_opacity(self.fade.value)
        if moving:
            return GLib.SOURCE_CONTINUE
        self._tick_id = 0
        if self.fade.target == 0.0:
            self.window.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _draw(self, _area, cr, width, height) -> None:
        look = self.hud.look
        accent = look.accent
        origin = self.monitor.get_geometry()
        hole = self.hud.pill.rectangle(self.monitor)
        if hole is not None:
            # Two tiled windows meet exactly where the pill sits, and which overlay
            # surface is on top depends on which was mapped last. A ring never crosses
            # the pill if it is simply not drawn there.
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(0, 0, width, height)
            cr.rectangle(*hole)
            cr.clip()
            cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        ring = look.border + 1
        taken: list[tuple[float, float]] = []
        for badge in self.badges:
            x, y = badge["x"] - origin.x, badge["y"] - origin.y
            w, h = badge["w"], badge["h"]
            # Hyprland draws its border OUTSIDE the window box; the ring sits on top of it
            cr.set_line_width(ring)
            cr.set_source_rgba(*accent, 0.95)
            rounded(cr, x - ring / 2, y - ring / 2, w + ring, h + ring, look.rounding + ring / 2)
            cr.stroke()

            bx, by = x + 10.0, y + 10.0
            while any(
                abs(bx - tx) < self.BOX + 4 and abs(by - ty) < self.BOX + 4 for tx, ty in taken
            ):
                bx += self.BOX + 6  # stacked floating windows: step aside, never overlap
            taken.append((bx, by))
            rounded(cr, bx, by, self.BOX, self.BOX, max(min(look.rounding - 2, 9), 0))
            cr.set_source_rgba(0.055, 0.055, 0.063, 0.92)
            cr.fill_preserve()
            cr.set_line_width(1.5)
            cr.set_source_rgba(*accent, 0.8)
            cr.stroke()
            self._digit(cr, str(badge["n"]), bx + self.BOX / 2, by + self.BOX / 2, accent)

    def _digit(self, cr, text: str, cx: float, cy: float, color) -> None:
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(
            Pango.FontDescription.from_string(f"{self.hud.options.font} ExtraBold 21px")
        )
        layout.set_text(text, -1)
        ink, _logical = layout.get_pixel_extents()
        cr.set_source_rgba(*color, 1.0)
        cr.move_to(cx - ink.width / 2 - ink.x, cy - ink.height / 2 - ink.y)
        PangoCairo.show_layout(cr, layout)


# --------------------------------------------------------------------------- the HUD


class Hud:
    def __init__(self, app, options) -> None:
        self.app = app
        self.options = options
        self.look = Look.read()
        self.view: dict = {"state": "hidden", **DEFAULTS}
        self._ttl = 0
        self._hints: dict[str, Hints] = {}
        self._dynamic = Gtk.CssProvider()
        self._load_css()
        self.pill = Pill(app, self)

    # ------------------------------------------------------------------ look

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        sheet = Path(__file__).resolve().with_name("style.css")
        if sheet.exists():
            static = Gtk.CssProvider()
            static.load_from_path(str(sheet))
            # one above the user's gtk.css, which otherwise wins over any application
            Gtk.StyleContext.add_provider_for_display(
                display, static, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
            )
        else:
            log(f"{sheet} is missing; the pill will look unstyled")
        _load_css(self._dynamic, self.look.css(self.options.font))
        Gtk.StyleContext.add_provider_for_display(
            display, self._dynamic, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2
        )

    def reload_look(self, reinstall_rules: bool) -> None:
        self.look = Look.read()
        _load_css(self._dynamic, self.look.css(self.options.font))
        if reinstall_rules and not self.options.no_rules:
            install_layer_rules(self.look)
        self.pill.indicator.queue_draw()
        for hints in self._hints.values():
            hints.area.queue_draw()
        log("look re-read from Hyprland")

    # ------------------------------------------------------------------ messages

    def apply(self, message: object) -> None:
        """Draw what the engine says. The engine has already checked the transition
        (hudproto.next_state), so the only rule here is how an update merges."""
        if not isinstance(message, dict) or message.get("t") != "state":
            return
        state = message.get("state")
        if state not in STATES:
            return
        entering = state != self.view["state"]
        known = {k: v for k, v in message.items() if k in DEFAULTS}
        if not entering and set(known) == {"level"}:
            self.view["level"] = _number(known["level"])
            return  # the meter reads it on the next frame; no widget is touched
        self.view = {**(DEFAULTS if entering else self.view), **known, "state": state}
        self.view["level"] = _number(self.view["level"])
        self.view["countdown_ms"] = int(_number(self.view["countdown_ms"]))
        self.view["badges"] = [b for b in self.view["badges"] if _is_badge(b)]
        self.view["suggestions"] = [str(s) for s in self.view["suggestions"]][:12]
        self.view["text"], self.view["chip"] = str(self.view["text"]), str(self.view["chip"])
        self._render(entering)
        if "ttl_ms" in known or entering:
            self._arm_ttl(int(_number(self.view["ttl_ms"])))

    def _render(self, entering: bool) -> None:
        if self.view["state"] == "hidden":
            self.pill.hide()
            self._show_badges([])
            return
        self.pill.show(self.view, entering)
        self._show_badges([b for b in self.view["badges"] if b["w"] > 0 and b["h"] > 0])

    def _show_badges(self, badges: list[dict]) -> None:
        if badges:
            monitors = Gdk.Display.get_default().get_monitors()
            for index in range(monitors.get_n_items()):
                monitor = monitors.get_item(index)
                name = monitor.get_connector() or str(index)
                if name not in self._hints or self._hints[name].monitor is not monitor:
                    if name in self._hints:
                        self._hints.pop(name).window.destroy()
                    self._hints[name] = Hints(self.app, self, monitor)
        for hints in self._hints.values():
            hints.show([b for b in badges if hints.owns(b)])

    def pill_moved(self) -> None:
        for hints in self._hints.values():
            if hints.window.get_visible():
                hints.area.queue_draw()

    def _arm_ttl(self, ms: int) -> None:
        if self._ttl:
            GLib.source_remove(self._ttl)
            self._ttl = 0
        if ms > 0:
            self._ttl = GLib.timeout_add(ms, self._expired)

    def _expired(self) -> bool:
        self._ttl = 0
        self.apply({"t": "state", "state": "hidden"})
        return GLib.SOURCE_REMOVE

    def engine_gone(self) -> None:
        self.apply({"t": "state", "state": "hidden"})


def _number(value: object) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _is_badge(badge: object) -> bool:
    return isinstance(badge, dict) and all(
        isinstance(badge.get(key), int) for key in ("n", "x", "y", "w", "h")
    )


# --------------------------------------------------------------------------- the engine


class EngineLink:
    """The client end of the engine's socket. One `hello`, then we only listen. The
    engine owns the socket file, so a missing file just means the engine is not up yet."""

    def __init__(self, path: Path, hud: Hud) -> None:
        self.path = path
        self.hud = hud
        self._sock: socket.socket | None = None
        self._watch = 0
        self._buffer = b""
        self._backoff = BACKOFF_FIRST_S
        self._quiet = False

    def connect(self) -> bool:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.connect(str(self.path))
            sock.sendall(json.dumps({"t": "hello", "proto": PROTO}).encode() + b"\n")
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            if not self._quiet:
                log(f"engine not reachable at {self.path} ({exc.strerror or exc}); retrying")
                self._quiet = True
            self._retry()
            return GLib.SOURCE_REMOVE
        self._sock, self._buffer, self._quiet = sock, b"", False
        self._watch = GLib.io_add_watch(
            sock.fileno(),
            GLib.PRIORITY_DEFAULT,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._readable,
        )
        log("connected to the engine")
        return GLib.SOURCE_REMOVE

    def _retry(self, after: float | None = None) -> None:
        delay = self._backoff if after is None else after
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
        GLib.timeout_add(int(delay * 1000), self.connect)

    def _readable(self, _fd, condition) -> bool:
        assert self._sock is not None
        gone = ""
        try:
            while chunk := self._sock.recv(65536):
                self._buffer += chunk
            gone = "the engine closed the connection"
        except BlockingIOError:
            pass
        except OSError as exc:
            gone = exc.strerror or str(exc)
        # lines first: the last thing a refusing engine says is why
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if self._handle(line) is False:
                return GLib.SOURCE_REMOVE
        if len(self._buffer) > READ_LIMIT:
            gone = "a line without an end"
        if not gone and condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            gone = "the engine went away"
        return self._lost(gone) if gone else GLib.SOURCE_CONTINUE

    def _handle(self, line: bytes) -> bool:
        try:
            message = json.loads(line)
        except ValueError:
            return True
        if isinstance(message, dict) and message.get("t") == "refused":
            # the reason is the engine's own sentence, never something that was spoken
            reason = str(message.get("reason", ""))[:200]
            self._lost(f"the engine refused this HUD: {reason}", BACKOFF_REFUSED_S)
            return False
        # a full connection that delivers means the next outage starts from a short wait
        self._backoff = BACKOFF_FIRST_S
        self.hud.apply(message)
        return True

    def _lost(self, why: str, retry_after: float | None = None) -> bool:
        log(f"{why}; hiding")
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._watch = 0
        self.hud.engine_gone()
        self._retry(retry_after)
        return GLib.SOURCE_REMOVE


class ReloadWatch:
    """socket2 `configreloaded`: the look may have changed and our runtime layer rules
    are certainly gone. Nothing else on that socket concerns a display."""

    def __init__(self, hud: Hud) -> None:
        self.hud = hud
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._open()

    def _open(self) -> bool:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(f"{runtime}/hypr/{signature}/.socket2.sock")
            sock.setblocking(False)
        except OSError:
            sock.close()
            return GLib.SOURCE_REMOVE  # SIGHUP still works
        self._sock, self._buffer = sock, b""
        GLib.io_add_watch(
            sock.fileno(),
            GLib.PRIORITY_DEFAULT_IDLE,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._readable,
        )
        return GLib.SOURCE_REMOVE

    def _readable(self, _fd, _condition) -> bool:
        assert self._sock is not None
        try:
            chunk = self._sock.recv(65536)
        except BlockingIOError:
            return GLib.SOURCE_CONTINUE
        except OSError:
            chunk = b""
        if not chunk:
            self._sock.close()
            self._sock = None
            return GLib.SOURCE_REMOVE  # Hyprland is going down, and so are we
        *lines, self._buffer = (self._buffer + chunk).split(b"\n")
        self._buffer = self._buffer[-4096:]
        if any(line.startswith(b"configreloaded>>") for line in lines):
            self.hud.reload_look(reinstall_rules=True)
        return GLib.SOURCE_CONTINUE


# --------------------------------------------------------------------------- demo


def demo_badges() -> list[dict]:
    """Real rectangles when Hyprland answers (read-only), thirds of a 1080p screen if not."""
    try:
        monitors = json.loads(hypr("j/monitors"))
        clients = json.loads(hypr("j/clients"))
        active = {m["activeWorkspace"]["id"] for m in monitors}
        shown = [
            c
            for c in clients
            if c.get("mapped") and not c.get("hidden") and c["workspace"]["id"] in active
        ]
        shown.sort(key=lambda c: (c["floating"], c["at"][1], c["at"][0]))
        badges = [
            {
                "n": n,
                "label": c["class"],
                "x": c["at"][0],
                "y": c["at"][1],
                "w": c["size"][0],
                "h": c["size"][1],
            }  # fmt: skip
            for n, c in enumerate(shown[:4], start=1)
        ]
    except (OSError, ValueError, KeyError, TypeError):
        badges = []
    if not badges:
        badges = [
            {"n": i + 1, "label": name, "x": 7 + i * 636, "y": 7, "w": 630, "h": 1066}
            for i, name in enumerate(("firefox", "kitty", "code"))
        ]
    return [
        *badges,
        {"n": len(badges) + 1, "label": "Firefox (workspace 4)", **dict.fromkeys("xywh", 0)},
    ]


def demo_script(step_ms: int) -> list[tuple[int, dict]]:
    badges = demo_badges()

    def say(state: str, **fields) -> dict:
        return {"t": "state", "state": state, **DEFAULTS, **fields}

    return [
        (step_ms, say("hearing")),
        (step_ms, say("hearing", text="focus fire")),
        (step_ms // 2, say("thinking", text="focus firefox")),
        (step_ms // 2, say("still_thinking", text="focus firefox")),
        (step_ms, say("heard", text="focus firefox", chip="focus window firefox")),
        (step_ms, say("swap", text="focus firefox", chip="focus window firefox", badges=badges)),
        (step_ms, say("hints", text="close the browser", chip="say a number", badges=badges)),
        (
            step_ms,
            say("countdown", text="close this", chip="close window kitty", countdown_ms=step_ms),
        ),  # fmt: skip
        (step_ms, say("countdown", text="tap the key to confirm", chip="lock screen")),
        (step_ms, say("refused", text="the session is locked")),
        (
            step_ms,
            say(
                "suggest",
                text="move it over there",
                chip="say which workspace",
                suggestions=["move this to workspace 3", "move firefox to workspace 2"],
            ),
        ),  # fmt: skip
        (
            step_ms,
            say(
                "help",
                suggestions=[
                    "focus firefox",
                    "close this",
                    "workspace 2",
                    "open terminal",
                    "fullscreen",
                    "volume up",
                    "undo",
                ],
            ),
        ),  # fmt: skip
        (600, say("hidden")),
    ]


class Demo:
    def __init__(self, hud: Hud, step_ms: int, only: str) -> None:
        self.hud = hud
        self.script = demo_script(step_ms)
        if only:
            # hold one state for a screenshot; a state with two variants alternates
            self.script = [(ms, m) for ms, m in self.script if m["state"] == only]
            if len(self.script) == 1:
                self.script = [(0, self.script[0][1])]
        self.at = 0
        GLib.timeout_add(33, self._meter)
        GLib.idle_add(self._next)

    def _next(self) -> bool:
        if not self.script:
            log("no such demo state")
            self.hud.app.quit()
            return GLib.SOURCE_REMOVE
        ms, message = self.script[self.at % len(self.script)]
        self.at += 1
        self.hud.apply(message)
        if ms:
            GLib.timeout_add(ms, self._next)
        return GLib.SOURCE_REMOVE

    def _meter(self) -> bool:
        if self.hud.view["state"] == "hearing":
            t = time.monotonic()
            level = 0.45 + 0.35 * math.sin(t * 7.3) * math.sin(t * 2.1) + 0.2 * math.sin(t * 23)
            self.hud.apply({"t": "state", "state": "hearing", "level": max(0.0, min(1.0, level))})
        return GLib.SOURCE_CONTINUE


# --------------------------------------------------------------------------- main


def configured() -> dict:
    """[hud] from the engine's config file, then HYPRSAY_HUD_* like the engine reads them.
    The unit starts this script without arguments, so the file is where position lives."""
    found: dict = {}
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
        text = Path("~/.config/hyprsay/config.toml").expanduser().read_text()
        section = tomllib.loads(text).get("hud", {})
        found = dict(section) if isinstance(section, dict) else {}
    position = os.environ.get("HYPRSAY_HUD_POSITION", found.get("position", "bottom"))
    margin = os.environ.get("HYPRSAY_HUD_MARGIN", found.get("margin", 48))
    try:
        margin = int(margin)
    except (TypeError, ValueError):
        margin = 48
    return {"position": position if position in ("bottom", "top") else "bottom", "margin": margin}


def arguments(argv: list[str]):
    defaults = configured()
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    parser = argparse.ArgumentParser(prog="hyprsay-hud", description="The hyprsay overlay.")
    parser.add_argument("--position", choices=("bottom", "top"), default=defaults["position"])
    parser.add_argument(
        "--margin", type=int, default=defaults["margin"], help="pixels from the edge"
    )
    parser.add_argument("--socket", type=Path, default=Path(runtime) / RUNTIME_SUBDIR / SOCKET_NAME)
    parser.add_argument("--font", default=FONT, help="font family for the pill and the badges")
    parser.add_argument("--no-rules", action="store_true", help="do not install layer rules")
    parser.add_argument("--demo", action="store_true", help="cycle through every state, no engine")
    parser.add_argument("--demo-state", default="", help="with --demo: hold this one state")
    parser.add_argument("--demo-step-ms", type=int, default=2200)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = arguments(sys.argv[1:] if argv is None else argv)
    Gtk.init()
    if not LS.is_supported():
        # never present() without it: the window would be tiled and FOCUSED
        log("the compositor does not support layer shell, or libwayland loaded first; not starting")
        return 2

    # NON_UNIQUE: the demo may run next to the real HUD, and D-Bus uniqueness is not
    # what decides who is the HUD anyway; the engine serves exactly one client
    app = Gtk.Application(application_id="app.hyprsay.hud", flags=Gio.ApplicationFlags.NON_UNIQUE)
    parts: list = []

    def activate(_app) -> None:
        if parts:
            return
        hud = Hud(app, options)
        if not options.no_rules:
            # before the first map, so the pill never shows one unblurred, animated frame
            install_layer_rules(hud.look)
        parts.extend([hud, ReloadWatch(hud)])
        if options.demo or options.demo_state:
            parts.append(Demo(hud, options.demo_step_ms, options.demo_state))
        else:
            link = EngineLink(options.socket, hud)
            parts.append(link)
            link.connect()
        app.hold()

    def sighup() -> bool:
        if parts:
            parts[0].reload_look(reinstall_rules=False)
        return GLib.SOURCE_CONTINUE

    def stop() -> bool:
        app.quit()
        return GLib.SOURCE_REMOVE

    app.connect("activate", activate)
    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGHUP, sighup)
    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
