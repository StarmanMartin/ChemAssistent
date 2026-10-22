"""Record docs/assets/screencast.mp4: the real app window, driven by a script.

Runs an AssistantWindow (no top-bar icon, no socket, so the installed app
can keep running) against the configured Chemotion and AI, types the demo
questions, clicks Run on every code card and captures the window ~20 times
a second. Each frame is composed onto a 16:9 stage with a simple top bar,
a pointer and captions and encoded with GStreamer (x264, no ffmpeg needed).
Waits for the model are fast-forwarded.

    .venv/bin/python docs/screencast.py [output.mp4]

The window really opens on the screen; keep the mouse away from it. The
demo creates a sample, so point the config at a test instance.
"""
from __future__ import annotations

import math
import re
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import app  # noqa: E402  (sets GDK_BACKEND=x11 before Gtk)
from app import Gdk, GLib, Gtk  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
import cairo  # noqa: E402
from gi.repository import GdkPixbuf, Gst  # type: ignore  # noqa: E402
from PIL import Image  # noqa: E402

OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "assets" / "screencast.mp4"
WIDTH, HEIGHT = 1920, 1080  # video pixels
STAGE_W, STAGE_H = 1440, 810  # logical (GDK) pixels shown in the video
TOP_BAR = 28
FPS = 20
FAST = 6  # fast-forward factor while the model works
CAPTURE_S = 0.12
TYPE_CPS = 22

QUESTION = "Which samples are in my collection CO2 reduction?"
FOLLOW_UP = "Add a new sample “Formic acid” (SMILES OC=O) to that collection."
INSTALL = [
    "Ubuntu:   curl -fsSL https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.sh | bash",
    "Windows:  irm https://raw.githubusercontent.com/StarmanMartin/ChemAssistent/main/install.ps1 | iex",
]

BACKGROUND = (0.10, 0.11, 0.13)
ACCENT = (0.16, 0.62, 0.85)


def monitor_origin() -> tuple[int, int]:
    """Where the stage sits on the real screen (centered in the work area)."""
    area = Gdk.Display.get_default().get_primary_monitor().get_workarea()
    return area.x + max(0, (area.width - STAGE_W) // 2), max(0, area.y - TOP_BAR)


STAGE_X, STAGE_Y = 0, 0


def demo_work_area() -> Gdk.Rectangle:
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = STAGE_X, STAGE_Y + TOP_BAR, STAGE_W, STAGE_H - TOP_BAR
    return rect


class DemoApp:
    """What AssistantWindow needs from ChemAssistentApp."""

    def __init__(self) -> None:
        self.window: app.AssistantWindow | None = None
        provider = Gtk.CssProvider()
        provider.load_from_data(app.CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

    def new_conversation(self) -> None:
        pass

    def window_closed(self, _window) -> None:
        self.window = None


class Recorder:
    def __init__(self) -> None:
        Gst.init(None)
        self.pipeline = Gst.parse_launch(
            f"appsrc name=src format=time caps=video/x-raw,format=BGRA,width={WIDTH},height={HEIGHT},"
            f"framerate={FPS}/1 ! videoconvert ! video/x-raw,format=I420 ! "
            "x264enc speed-preset=veryfast pass=qual quantizer=21 key-int-max=100 ! "
            f"video/x-h264,profile=high ! mp4mux faststart=true ! filesink location={OUTPUT}"
        )
        self.src = self.pipeline.get_by_name("src")
        self.pipeline.set_state(Gst.State.PLAYING)
        self.frames = 0

    def push(self, surface: cairo.ImageSurface, count: int = 1) -> None:
        """The frame `count` times (a capture can take longer than a frame)."""
        surface.flush()
        data = bytes(surface.get_data())
        for _ in range(count):
            buffer = Gst.Buffer.new_wrapped(data)
            buffer.pts = self.frames * Gst.SECOND // FPS
            buffer.duration = Gst.SECOND // FPS
            self.src.emit("push-buffer", buffer)
            self.frames += 1

    def finish(self) -> None:
        self.src.emit("end-of-stream")
        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        self.pipeline.set_state(Gst.State.NULL)


def pixbuf_surface(pixbuf: GdkPixbuf.Pixbuf, scale: float = 1.0) -> cairo.ImageSurface:
    """A pixbuf as a cairo surface without GI's cairo bridge (python3-gi-cairo
    is often missing): premultiplied BGRA through Pillow."""
    width, height = pixbuf.get_width(), pixbuf.get_height()
    if not pixbuf.get_has_alpha():
        image = Image.frombuffer("RGB", (width, height), pixbuf.get_pixels(), "raw", "RGB",
                                 pixbuf.get_rowstride(), 1)
        data = bytearray(image.tobytes("raw", "BGRX"))
        surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_RGB24, width, height, width * 4)
        surface.set_device_scale(scale, scale)
        return surface
    image = Image.frombuffer("RGBA", (width, height), pixbuf.get_pixels(), "raw", "RGBA",
                             pixbuf.get_rowstride(), 1).convert("RGBa")
    red, green, blue, alpha = image.split()
    data = bytearray(Image.merge("RGBa", (blue, green, red, alpha)).tobytes())
    surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, width, height, width * 4)
    surface.set_device_scale(scale, scale)
    return surface


def ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


class Stage:
    """Draws one video frame: background, top bar, window, menu, pointer, caption."""

    def __init__(self) -> None:
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
        zoom = WIDTH / STAGE_W
        self.logo = pixbuf_surface(GdkPixbuf.Pixbuf.new_from_file_at_size(
            str(app.DARK_UI_ICON_FILE), round(160 * zoom), round(160 * zoom)), zoom)
        self.panel_icon = pixbuf_surface(GdkPixbuf.Pixbuf.new_from_file_at_size(
            str(app.DARK_UI_ICON_FILE), round(18 * zoom), round(18 * zoom)), zoom)
        self.icons = {}
        self.caption = ""
        self.caption_since = 0.0
        self.caption_until = 0.0
        self.caption_place = "chat"
        self.card: tuple[str, str, str] | None = None  # title card: title, subtitle, footer
        self.menu_open = False
        self.menu_hover = -1
        self.pointer = (STAGE_W * 0.7, STAGE_H * 0.6)
        self.pointer_visible = False
        self.click_at = -10.0
        self.fast = False
        self.expanded = False
        self.window_ready = False  # placed where the prompt belongs
        self.window_shot: tuple[float, cairo.ImageSurface] | None = None  # (time, capture)

    def panel_icon_pos(self) -> tuple[float, float]:
        return STAGE_W - 120, TOP_BAR / 2

    def menu_items(self) -> list[tuple[str, float, float, float, float]]:
        x, _y = self.panel_icon_pos()
        items = ["Ask Chemotion…", "New conversation", None, "Settings…", "Quit"]
        rows, top = [], TOP_BAR + 8
        for text in items:
            height = 10 if text is None else 34
            rows.append((text, x - 150, top, 220, height))
            top += height
        return rows

    def draw(self, window: Gtk.Window | None, now: float) -> cairo.ImageSurface:
        cr = cairo.Context(self.surface)
        cr.scale(WIDTH / STAGE_W, HEIGHT / STAGE_H)
        if self.card:
            self._draw_card(cr)
        else:
            self._draw_desktop(cr)
            if window is not None and self.window_ready and window.get_visible() and window.get_window():
                self._draw_window(cr, window, now)
            if self.menu_open:
                self._draw_menu(cr)
            if self.pointer_visible:
                self._draw_pointer(cr, now)
        if self.caption:
            self._draw_caption(cr, now)
        if self.fast:
            self._text(cr, f"▶▶  fast-forward {FAST}×", 13, 16, TOP_BAR / 2, (1, 1, 1, 0.85), align="left", bold=True)
        return self.surface

    def _draw_desktop(self, cr) -> None:
        gradient = cairo.LinearGradient(0, 0, STAGE_W, STAGE_H)
        gradient.add_color_stop_rgb(0, 0.13, 0.18, 0.26)
        gradient.add_color_stop_rgb(1, 0.06, 0.07, 0.10)
        cr.set_source(gradient)
        cr.paint()
        cr.set_source_rgb(0, 0, 0)
        cr.rectangle(0, 0, STAGE_W, TOP_BAR)
        cr.fill()
        self._text(cr, time.strftime("%a %-d %b  %H:%M"), 13, STAGE_W / 2, TOP_BAR / 2,
                   (1, 1, 1, 0.9), bold=True)
        x, y = self.panel_icon_pos()
        if self.menu_open:
            cr.set_source_rgba(1, 1, 1, 0.18)
            self._rounded(cr, x - 14, 3, 28, TOP_BAR - 6, 6)
            cr.fill()
        cr.set_source_surface(self.panel_icon, x - 9, y - 9)
        cr.paint()
        for index, icon in enumerate(("network-wireless-symbolic", "audio-volume-high-symbolic",
                                      "battery-full-symbolic")):
            self._icon(cr, icon, STAGE_W - 80 + index * 22, y)

    def _icon(self, cr, name: str, x: float, y: float) -> None:
        if name not in self.icons:
            try:
                info = Gtk.IconTheme.get_default().lookup_icon(name, 32, 0)
                white = Gdk.RGBA()
                white.parse("white")
                pixbuf, _ = info.load_symbolic(white, None, None, None)
                self.icons[name] = pixbuf_surface(pixbuf, pixbuf.get_width() / 16)
            except (AttributeError, GLib.Error, TypeError):
                self.icons[name] = None
        if self.icons[name] is not None:
            cr.set_source_surface(self.icons[name], x - 8, y - 8)
            cr.paint()

    def _draw_window(self, cr, window: Gtk.Window, now: float) -> None:
        """The window as it is on screen; captured at most every CAPTURE_S
        (a capture takes longer than a frame), drawn every frame."""
        gdk_window = window.get_window()
        _ok, x, y = gdk_window.get_origin()
        width, height = gdk_window.get_width(), gdk_window.get_height()
        shot = self.window_shot
        if shot is None or now - shot[0] >= CAPTURE_S or shot[1].get_width() != round(width * WIDTH / STAGE_W):
            pixbuf = Gdk.pixbuf_get_from_window(gdk_window, 0, 0, width, height)
            if pixbuf is None:
                return
            zoom = WIDTH / STAGE_W
            pixbuf = pixbuf.scale_simple(round(width * zoom), round(height * zoom), GdkPixbuf.InterpType.BILINEAR)
            self.window_shot = shot = (now, pixbuf_surface(pixbuf, zoom))
        cr.set_source_surface(shot[1], x - STAGE_X, y - STAGE_Y)
        cr.paint()

    def _draw_menu(self, cr) -> None:
        rows = self.menu_items()
        left, top = rows[0][1], rows[0][2]
        bottom = rows[-1][2] + rows[-1][4]
        cr.set_source_rgba(0.16, 0.16, 0.17, 0.98)
        self._rounded(cr, left - 6, top - 6, 232, bottom - top + 12, 12)
        cr.fill()
        for index, (text, x, y, w, h) in enumerate(rows):
            if text is None:
                cr.set_source_rgba(1, 1, 1, 0.12)
                cr.rectangle(x + 8, y + h / 2, w - 16, 1)
                cr.fill()
                continue
            if index == self.menu_hover:
                cr.set_source_rgba(1, 1, 1, 0.12)
                self._rounded(cr, x, y, w, h, 7)
                cr.fill()
            self._text(cr, text, 14, x + 14, y + h / 2, (1, 1, 1, 0.92), align="left")

    def _draw_pointer(self, cr, now: float) -> None:
        x, y = self.pointer
        since = now - self.click_at
        if 0 <= since < 0.45:  # click ripple
            cr.set_source_rgba(*ACCENT, 0.55 * (1 - since / 0.45))
            cr.arc(x, y, 8 + 26 * since / 0.45, 0, 2 * math.pi)
            cr.fill()
        cr.save()
        cr.translate(x, y)
        cr.move_to(0, 0)
        for px, py in ((0, 19), (4.5, 14.8), (8, 22), (11, 20.6), (7.6, 13.6), (13.5, 13.6)):
            cr.line_to(px, py)
        cr.close_path()
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_source_rgb(0, 0, 0)
        cr.set_line_width(1.2)
        cr.stroke()
        cr.restore()

    def _draw_caption(self, cr, now: float) -> None:
        alpha = ease((now - self.caption_since) / 0.35) * ease((self.caption_until - now) / 0.35)
        if alpha <= 0:
            return
        size, line_height = 22, 31
        chat = STAGE_W * app.CHAT_SHARE
        if self.expanded and self.caption_place == "results":  # the empty lower part of the results
            center, max_width, bottom = chat + (STAGE_W - chat) / 2, STAGE_W - chat - 80, 120
        elif self.expanded:  # in the chat column above the input
            center, max_width, bottom = chat / 2, chat - 80, 120
        else:
            center, max_width, bottom = STAGE_W / 2, STAGE_W - 200, 34
        lines = []
        for line in self.caption.split("\n"):
            lines.extend(self._wrap(cr, self._runs(line), size, max_width))
        widths = [sum(self._width(cr, text, size, bold) for text, bold in runs) for runs in lines]
        width, height = max(widths) + 56, len(lines) * line_height + 24
        x, y = center - width / 2, STAGE_H - height - bottom
        cr.set_source_rgba(0.05, 0.06, 0.08, 0.9 * alpha)
        self._rounded(cr, x, y, width, height, 14)
        cr.fill()
        cr.set_source_rgba(*ACCENT, alpha)
        self._rounded(cr, x, y, 6, height, 3)
        cr.fill()
        for index, (runs, line_width) in enumerate(zip(lines, widths)):
            left = center - line_width / 2
            for text, bold in runs:
                self._text(cr, text, size, left, y + 12 + line_height * (index + 0.5), (1, 1, 1, alpha),
                           align="left", bold=bold)
                left += self._width(cr, text, size, bold)

    def _wrap(self, cr, runs: list[tuple[str, bool]], size: float, max_width: float) -> list[list]:
        """Word-wrap runs of (text, bold) into lines no wider than max_width."""
        words = [(word, bold) for text, bold in runs for word in re.findall(r"\S+\s*|\s+", text)]
        lines, line, width = [], [], 0.0
        for word, bold in words:
            word_width = self._width(cr, word.rstrip(), size, bold)
            if line and width + word_width > max_width:
                lines.append(line)
                line, width = [], 0.0
            line.append((word, bold))
            width += self._width(cr, word, size, bold)
        if line:
            line[-1] = (line[-1][0].rstrip(), line[-1][1])
            lines.append(line)
        return lines

    @staticmethod
    def _runs(line: str) -> list[tuple[str, bool]]:
        """'a <b>b</b> c' -> [('a ', False), ('b', True), (' c', False)]"""
        parts = re.split(r"</?b>", line)
        return [(part, index % 2 == 1) for index, part in enumerate(parts) if part]

    def _draw_card(self, cr) -> None:
        title, subtitle, footer = self.card
        cr.set_source_rgb(*BACKGROUND)
        cr.paint()
        cr.set_source_surface(self.logo, STAGE_W / 2 - 80, STAGE_H / 2 - 250)
        cr.paint()
        self._text(cr, title, 60, STAGE_W / 2, STAGE_H / 2 - 20, (1, 1, 1, 1), bold=True)
        self._text(cr, subtitle, 28, STAGE_W / 2, STAGE_H / 2 + 50, (0.75, 0.78, 0.82, 1))
        for index, line in enumerate(footer.split("\n") if footer else []):
            self._text(cr, line, 16, STAGE_W / 2, STAGE_H / 2 + 150 + index * 30, (*ACCENT, 1), mono=True)

    @staticmethod
    def _rounded(cr, x, y, w, h, r) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    @staticmethod
    def _font(cr, size: float, bold: bool, mono: bool) -> None:
        cr.select_font_face("Monospace" if mono else "Cantarell", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(size)

    def _width(self, cr, text: str, size: float, bold: bool = False, mono: bool = False) -> float:
        self._font(cr, size, bold, mono)
        return cr.text_extents(text).x_advance

    def _text(self, cr, text, size, x, y, rgba, align="center", bold=False, mono=False) -> None:
        """One line of text, vertically centered on y."""
        self._font(cr, size, bold, mono)
        ascent, descent = cr.font_extents()[:2]
        left = x - cr.text_extents(text).x_advance / 2 if align == "center" else x
        cr.move_to(left, y + (ascent - descent) / 2)
        cr.set_source_rgba(*rgba)
        cr.show_text(text)
        cr.new_path()


def stage_center(widget: Gtk.Widget) -> tuple[float, float]:
    toplevel = widget.get_toplevel()
    x, y = widget.translate_coordinates(toplevel, 0, 0)
    _ok, ox, oy = toplevel.get_window().get_origin()
    allocation = widget.get_allocation()
    return ox - STAGE_X + x + allocation.width / 2, oy - STAGE_Y + y + allocation.height / 2


class Demo:
    def __init__(self) -> None:
        self.app = DemoApp()
        self.stage = Stage()
        self.recorder = Recorder()
        self.window: app.AssistantWindow | None = None
        self.script = self.run()
        self.wait_until = 0.0
        self.condition = None
        self.condition_deadline = 0.0
        self.started = self.last_tick = time.monotonic()
        self.video_time = 0.0  # runs slower than the clock while fast-forwarding
        self.draw_time = 0.0
        # Below GTK's redraws, or the captured window lags behind the real one.
        GLib.timeout_add(1000 // FPS, self.tick, priority=GLib.PRIORITY_LOW)

    # --- driving ----------------------------------------------------------------
    def tick(self) -> bool:
        now = time.monotonic()
        self.video_time += (now - self.last_tick) / (FAST if self.stage.fast else 1)
        self.last_tick = now
        try:
            if self.condition is not None:
                if self.condition() or now > self.condition_deadline:
                    self.condition = None
            if self.condition is None and now >= self.wait_until:
                step = next(self.script)
                if callable(step):
                    self.condition, self.condition_deadline = step, now + 600
                else:
                    self.wait_until = now + step
            missing = int(self.video_time * FPS) - self.recorder.frames
            if missing > 0:
                began = time.monotonic()
                self.recorder.push(self.stage.draw(self.window, now), missing)
                self.draw_time += time.monotonic() - began
        except Exception as exc:
            if not isinstance(exc, StopIteration):
                traceback.print_exc()
            print(f"{self.recorder.frames} frames, {self.video_time:.0f} s of video, "
                  f"drawing took {self.draw_time:.0f} s of {now - self.started:.0f} s", flush=True)
            self.recorder.finish()
            if self.window is not None:
                self.window.close_conversation()
            Gtk.main_quit()
            return False
        return True

    def say(self, markup: str, seconds: float = 6.0, place: str = "chat") -> None:
        self.stage.caption = markup
        self.stage.caption_place = place
        self.stage.caption_since = time.monotonic()
        self.stage.caption_until = self.stage.caption_since + seconds

    def move(self, target, seconds: float = 0.8):
        start = self.stage.pointer
        self.stage.pointer_visible = True
        began = time.monotonic()
        while True:
            t = ease((time.monotonic() - began) / seconds)
            end = target() if callable(target) else target
            self.stage.pointer = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            if t >= 1:
                return
            yield 0

    def click(self):
        self.stage.click_at = time.monotonic()
        yield 0.15

    def type_entry(self, entry: Gtk.Entry, text: str):
        for index in range(len(text)):
            entry.set_text(text[:index + 1])
            entry.set_position(-1)
            yield 1 / TYPE_CPS

    def type_view(self, view: Gtk.TextView, text: str):
        buffer = view.get_buffer()
        for char in text:
            buffer.insert_at_cursor(char)
            yield 1 / TYPE_CPS

    def prompt_placed(self) -> bool:
        width, _height = self.window.get_size()
        area = demo_work_area()
        _ok, x, _y = self.window.get_window().get_origin()
        return abs(x - (area.x + (area.width - width) // 2)) <= 2

    def log_events(self) -> None:
        handler = self.window._on_agent_event
        self.text_chars = 0

        def logged(kind, data):
            if kind == "text":
                self.text_chars += len(data)
                return handler(kind, data)
            if kind == "done":
                print(f"        ({self.text_chars} text characters)", flush=True)
            print(f"{time.monotonic() - self.started:7.1f} {kind}: {str(data)[:100]!r}", flush=True)
            return handler(kind, data)

        self.window._on_agent_event = logged

    def chat_idle(self) -> bool:
        chat = self.window.chat
        return not chat.busy

    def waiting_card(self):
        results = self.window.results
        return next((card for card in results.cards if card.state == "waiting"), None)

    def model_turn(self, first_card_caption: str, later_card_caption: str):
        """Until the agent answered: fast-forward its thinking, run every card."""
        cards = 0
        while not self.chat_idle():
            status = self.window.chat.status.get_text()
            card = self.waiting_card()
            if card is not None:
                self.stage.fast = False
                cards += 1
                self.say(first_card_caption if cards == 1 else later_card_caption)
                yield 4.5 if cards == 1 else 2.0
                yield from self.move(lambda: stage_center(card.run_button))
                yield 0.3
                yield from self.click()
                card.run()
                yield 0.5
                self.say("It runs in its own process; the output opens as a tab below the code.")
                continue
            self.stage.fast = status not in ("Writing…",)
            yield 0
        self.stage.fast = False

    def run(self):
        stage = self.stage
        stage.card = ("ChemAssistent", "Ask your Chemotion ELN in plain language", "")
        yield 3.5
        stage.card = None
        self.say("ChemAssistent lives in the top bar.\nClick the Chemotion icon (or press <b>Ctrl+Space</b> twice).", 5.0)
        yield 1.0
        yield from self.move(stage.panel_icon_pos(), 1.0)
        yield from self.click()
        stage.menu_open = True
        yield 0.6
        _text, x, y, w, h = stage.menu_items()[0]
        yield from self.move((x + 60, y + h / 2), 0.5)
        stage.menu_hover = 0
        yield 0.6
        yield from self.click()
        stage.menu_open = False
        stage.menu_hover = -1

        self.window = app.AssistantWindow(self.app)
        self.app.window = self.window
        self.window.open_prompt()
        self.log_events()
        stage.pointer_visible = False
        yield self.prompt_placed
        stage.window_ready = True
        self.say("Ask a question about your data.")
        yield 1.0
        yield from self.type_entry(self.window.entry, QUESTION)
        yield 0.8
        self.say("Enter starts the agent; the prompt becomes a workspace.")
        yield 0.5
        self.window.entry.emit("activate")
        stage.expanded = True
        yield 1.5
        self.say("The agent looks up the Chemotion API and writes Python for it.")
        yield 1.0
        yield from self.model_turn(
            "The code shows up on the right. <b>Nothing runs before you click Run</b>;\n"
            "you can edit it first, or Reject it with a reason.",
            "Every further step needs your OK as well.",
        )
        self.say("Results are tables; a click on a row opens the element in Chemotion.")
        yield 4.0

        self.say("Ask follow-up questions in the chat. Files and folders can be attached with the paperclip.")
        yield from self.move(lambda: stage_center(self.window.chat.input), 0.9)
        yield from self.click()
        stage.pointer_visible = False
        yield 0.3
        yield from self.type_view(self.window.chat.input, FOLLOW_UP)
        yield 0.8
        self.window.chat._send()
        yield 1.5
        self.say("It also changes data, but again only after your confirmation.")
        yield from self.model_turn(
            "Review what it will write: purpose and code are shown before anything happens.",
            "Every further step needs your OK as well.",
        )
        yield 5.0  # time to read the answer
        self.say("Conversations are saved: the prompt offers to continue recent ones.", 4.0,
                 place="results")
        yield 4.5
        stage.caption = ""
        stage.card = ("Install ChemAssistent", "Ubuntu (GNOME) · Windows 10/11",
                      # Padded to one length: centered, the monospace lines then start in one column.
                      "\n".join(line.ljust(max(map(len, INSTALL))) for line in INSTALL)
                      + "\n\ngithub.com/StarmanMartin/ChemAssistent")
        yield 6.0


def main() -> None:
    global STAGE_X, STAGE_Y
    STAGE_X, STAGE_Y = monitor_origin()
    app.work_area = demo_work_area
    # Keep the video's 16:9 stage: no maximizing to the real (wide) work area.
    app.AssistantWindow.maximize = lambda self: None
    app.history.save = lambda *args, **kwargs: None
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    Demo()
    Gtk.main()
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
