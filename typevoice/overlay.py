"""Compact, focus-free dictation HUD, rendered with original vector artwork."""

import math
import cairo
from .gtkenv import Gdk, GLib, Gtk
import gi

gi.require_version("Pango", "1.0")
from gi.repository import Pango

CSS = b"""
#capsule { background: transparent; }
#capsule-label { color: #ececf0; font: 12px Sans; }
#capsule-label.timer { font: 12px monospace; color: #b6b6c0; }
"""
ANIM_INTERVAL_MS = 33


def rounded_rect(cr, x, y, w, h, r):
    cr.new_sub_path()
    for cx, cy, a in ((x+w-r, y+r, -90), (x+w-r, y+h-r, 0),
                       (x+r, y+h-r, 90), (x+r, y+r, 180)):
        cr.arc(cx, cy, r, math.radians(a), math.radians(a+90))
    cr.close_path()


class Capsule:
    def __init__(self, config=None):
        self.config = config
        self._anim_timer = self._hide_timer = None
        self._anim_kind = None
        self._anim_frame = 0
        self._level = self._target_level = 0.0
        self._state = "recording"
        self._setup_css()
        self.window = self._build_window()
        self._build_body()
        self.window.connect("size-allocate", self._on_size_allocate)
        self.window.connect("destroy", lambda *_: self._cleanup())

    @staticmethod
    def _setup_css():
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_window(self):
        win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        win.set_title("TypeVoice · 语音输入")
        win.set_decorated(False)
        win.set_skip_taskbar_hint(True)
        win.set_skip_pager_hint(True)
        win.set_accept_focus(False)
        win.set_focus_on_map(False)
        win.set_keep_above(True)
        win.set_resizable(False)
        win.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        win.set_name("capsule")
        win.set_app_paintable(True)
        visual = win.get_screen().get_rgba_visual()
        if visual:
            win.set_visual(visual)
        win.connect("draw", self._draw_background)
        return win

    def _build_body(self):
        self.icon = Gtk.DrawingArea()
        self.icon.set_size_request(28, 28)
        self.icon.connect("draw", self._draw_icon)
        self.icon.get_accessible().set_name("麦克风")
        self.level_bar = Gtk.DrawingArea()
        self.level_bar.set_size_request(65, 28)
        self.level_bar.connect("draw", self._draw_wave)
        self.level_bar.get_accessible().set_name("实时音量")
        self.label = Gtk.Label()
        self.label.set_name("capsule-label")
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        self.label.set_max_width_chars(36)
        self.label.set_xalign(0)
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.set_margin_start(18)
        outer.set_margin_end(22)
        outer.set_margin_top(14)
        outer.set_margin_bottom(14)
        for widget in (self.icon, self.level_bar, self.label):
            outer.pack_start(widget, False, False, 0)
        self.window.add(outer)

    def _draw_background(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        for inset, alpha in ((1, .03), (2, .04), (3, .06), (4, .1)):
            rounded_rect(cr, inset, inset+1, w-inset*2, h-inset*2-1, (h-inset*2)/2)
            cr.set_source_rgba(0, 0, 0, alpha)
            cr.fill()
        rounded_rect(cr, 5, 5, w-10, h-10, (h-10)/2)
        cr.set_source_rgba(.085, .085, .105, .97)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, .13)
        cr.set_line_width(1)
        cr.stroke()
        return False

    def _draw_icon(self, widget, cr):
        cr.set_line_width(1.65)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgb(.95, .95, .97)
        if self._state == "recording":
            rounded_rect(cr, 10, 4, 8, 13, 4)
            cr.stroke()
            cr.arc(14, 13, 7, 0, math.pi)
            cr.stroke()
            cr.move_to(14, 20)
            cr.line_to(14, 24)
            cr.move_to(10, 24)
            cr.line_to(18, 24)
            cr.stroke()
        elif self._state == "processing":
            for i in range(8):
                angle = (i/8 * math.tau) + self._anim_frame * .09
                cr.set_source_rgba(.95, .95, .97, .18 + .82 * i/7)
                cr.arc(14 + 8*math.cos(angle), 14 + 8*math.sin(angle), 1.6, 0, math.tau)
                cr.fill()
        elif self._state == "error":
            cr.set_source_rgb(1, .52, .47)
            cr.arc(14, 14, 9, 0, math.tau)
            cr.stroke()
            cr.move_to(14, 9)
            cr.line_to(14, 15)
            cr.stroke()
            cr.arc(14, 19, .9, 0, math.tau)
            cr.fill()
        else:
            cr.set_source_rgb(.69, .88, .76)
            cr.move_to(7, 14)
            cr.line_to(12, 19)
            cr.line_to(21, 9)
            cr.stroke()
        return False

    def _draw_wave(self, widget, cr):
        # Motion follows microphone level; silence stays a quiet row of dots.
        for i in range(9):
            envelope = .45 + .55 * math.sin((i+1)/10 * math.pi)
            motion = .55 + .45 * math.sin(self._anim_frame*.28 + i*.85)**2
            height = 3 + 23 * self._level * envelope * motion
            rounded_rect(cr, i*7+2, (28-height)/2, 3, height, 1.5)
            cr.set_source_rgba(.97, .97, .99, .5 + .5*self._level)
            cr.fill()
        return False

    def _on_size_allocate(self, win, allocation):
        # X11 without a compositor still gets rounded edges (no black corners).
        if win.get_realized() and not win.get_screen().is_composited():
            w, h = allocation.width, allocation.height
            region = cairo.Region()
            r = (h-10) / 2
            for y in range(5, h-5):
                dx = 5 + math.ceil(r - math.sqrt(max(0, r*r - (y-5+.5-r)**2)))
                region.union(cairo.RectangleInt(dx, y, max(0, w-2*dx), 1))
            win.get_window().shape_combine_region(region, 0, 0)
        if win.get_visible():
            self._move_to_bottom(allocation.width, allocation.height)

    def _move_to_bottom(self, w: int | None = None, h: int | None = None):
        """固定在主显示器底部中央，避开面板并为自动隐藏的 Dock 留出空间。

        使用 GDK 逻辑像素，适配缩放；不随鼠标移动或状态变化切换显示器。
        """
        try:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            if monitor is None:
                return
            geo = monitor.get_geometry()
            work = monitor.get_workarea()
            margin = max(0, int(self.config.get("overlay_bottom_margin"))) if self.config else 96
            win_w = w or self.window.get_allocated_width()
            win_h = h or self.window.get_allocated_height()
            if win_w <= 0:
                win_w = 80
            if win_h <= 0:
                win_h = 20
            x = geo.x + (geo.width - win_w) // 2
            bottom = min(geo.y + geo.height - margin, work.y + work.height - 16)
            y = max(work.y, bottom - win_h)
            self.window.move(max(work.x, min(x, work.x + work.width - win_w)), y)
        except Exception:
            pass

    def _present(self, state, text, *, wave=False):
        changed = state != self._state or not self.window.get_visible()
        self._state = state
        self.label.set_text(text)
        self.label.set_tooltip_text(text if state in ("error", "message") else None)
        sc = self.label.get_style_context()
        (sc.add_class if state == "recording" else sc.remove_class)("timer")
        self._set_error_style(state == "error")
        self.icon.get_accessible().set_name({"recording": "录音中", "processing": "处理中",
                                             "error": "失败", "message": "完成"}[state])
        self.window.show_all()
        self.level_bar.set_visible(wave)
        if changed:
            self.window.resize(1, 1)
        self.window.queue_draw()
        self._move_to_bottom()

    def show_recording(self, elapsed=0.0, level=0.0):
        self._schedule_hide(0)
        if self._anim_kind != "recording":
            self._level = 0.0
        self._target_level = min(max(float(level), 0.0), 1.0)
        self._start_anim("recording")
        minutes, seconds = divmod(max(0, int(elapsed)), 60)
        self._present("recording", f"{minutes:02d}:{seconds:02d}", wave=True)

    def show_processing(self):
        self._schedule_hide(0)
        self._start_anim("processing")
        self._present("processing", "正在整理")

    def show_message(self, message, *, error=False, auto_hide_ms=0):
        self._stop_anim()
        self._present("error" if error else "message", message)
        self._schedule_hide(auto_hide_ms)

    def hide(self):
        self._cleanup()
        self._do_hide()

    def _cleanup(self):
        self._stop_anim()
        self._schedule_hide(0)

    def _set_error_style(self, error):
        sc = self.window.get_style_context()
        (sc.add_class if error else sc.remove_class)("capsule-error")

    def _schedule_hide(self, delay_ms):
        if self._hide_timer is not None:
            GLib.source_remove(self._hide_timer)
            self._hide_timer = None
        if delay_ms > 0:
            self._hide_timer = GLib.timeout_add(delay_ms, self._do_hide)

    def _do_hide(self):
        self._hide_timer = None
        self._stop_anim()
        self.window.hide()
        self._set_error_style(False)
        return False

    def _start_anim(self, kind):
        if self._anim_timer is not None and self._anim_kind == kind:
            return
        self._stop_anim()
        self._anim_kind = kind
        self._anim_frame = 0
        self._anim_timer = GLib.timeout_add(ANIM_INTERVAL_MS, self._on_anim_tick)

    def _stop_anim(self):
        if self._anim_timer is not None:
            GLib.source_remove(self._anim_timer)
            self._anim_timer = None
        self._anim_kind = None

    def _on_anim_tick(self):
        if self._anim_kind not in ("recording", "processing"):
            return False
        self._anim_frame += 1
        self._level += (self._target_level-self._level) * .24
        self.icon.queue_draw()
        self.level_bar.queue_draw()
        return True
