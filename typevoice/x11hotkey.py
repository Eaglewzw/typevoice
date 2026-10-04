"""Observe X11 keys without grabbing Alt or stealing other apps' shortcuts.

A short chord window precedes hold recording. A tap acts on release; any other
key held before/during Alt suppresses that gesture. A late chord cancels the
new hold recording, never submits it. Existing latched recording is preserved.
"""

import select
import threading
import time
from collections import deque

from .config import Config

CHORD_DELAY = 0.18
HOLD_THRESHOLD = 0.50
RELEASE_CONFIRM_DELAY = 0.08
EARLY_PRESS_WINDOW = 1.5
EARLY_RELEASE_CONFIRM_DELAY = 0.35
REPEAT_RELEASE_DELAY = 0.01
ESC_KEYSYM = 0xFF1B


class HotkeyError(Exception):
    pass


class HotkeyManager(threading.Thread):
    """All semantic callbacks and X11 operations run on this worker thread."""

    def __init__(self, config: Config, on_record_start, on_record_stop,
                 on_record_cancel, on_error=None, log=None):
        super().__init__(name="hotkey", daemon=True)
        self.config = config
        self.on_record_start = on_record_start
        self.on_record_stop = on_record_stop
        self.on_record_cancel = on_record_cancel
        self.on_error = on_error or (lambda msg: None)
        self.log = log or (lambda msg: None)
        self._stop_event = threading.Event()
        self._reset_event = threading.Event()
        self.ready = threading.Event()
        self._state = "idle"
        self._down = set()
        self._gesture = None
        self._blocked = False
        self._deadline = None
        self._events = deque()
        self._release = None

    def run(self):
        try:
            self._run_loop()
        except Exception as err:  # noqa: BLE001
            if not self._stop_event.is_set():
                self.on_error(str(err))
        finally:
            self._deadline = None
            self.ready.clear()

    def _run_loop(self):
        from Xlib import X, XK
        from Xlib.ext import record
        from .x11display import open_display

        control = open_display()
        observer = None
        context = None
        try:
            if not control.has_extension("RECORD"):
                raise HotkeyError("X11 服务未启用 RECORD 扩展，无法监听录音快捷键")
            keyspec = self.config.string("hotkey_keysym") or "Alt_L,Alt_R"
            codes = set()
            for name in keyspec.split(","):
                name = name.strip()
                sym = getattr(XK, f"XK_{name}", None) or XK.string_to_keysym(name)
                code = control.keysym_to_keycode(sym) if sym else 0
                if code:
                    codes.add(code)
            if not codes:
                raise HotkeyError(f"快捷键 {keyspec!r} 无法解析，请修改 hotkey_keysym")
            self._hotkey_codes = codes
            self._esc_code = control.keysym_to_keycode(ESC_KEYSYM)
            keymap = control.query_keymap()
            self._down = {code for code in range(256)
                          if keymap[code // 8] & (1 << (code % 8))}
            context = control.record_create_context(0, [record.AllClients], [{
                "core_requests": (0, 0), "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0), "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.KeyRelease),
                "errors": (0, 0), "client_started": False, "client_died": False,
            }])
            control.sync()
            observer = open_display()

            def receive(reply):
                if reply.category == record.StartOfData:
                    self.ready.set()
                elif reply.category == record.FromServer and not reply.client_swapped:
                    # Only physical key events, not delivered copies or text. Keep
                    # keycode/time in memory solely to recognize gestures/repeats.
                    from Xlib.protocol import rq
                    data = reply.data
                    while data:
                        event, data = rq.EventField(None).parse_binary_value(
                            data, observer.display, None, None)
                        if event.type in (X.KeyPress, X.KeyRelease):
                            self._events.append((event.type == X.KeyPress,
                                                 event.detail, event.time))

            # Deferred reply lets this same thread service deadlines and shutdown;
            # no timer thread shares an Xlib connection or starts audio after quit.
            record.EnableContext(
                display=observer.display,
                opcode=observer.display.get_extension_major(record.extname),
                context=context, callback=receive, defer=True)
            observer.flush()
            self.log(f"Hotkey: observing {keyspec} (Alt combinations pass through)")
            while not self._stop_event.is_set():
                observer.pending_events()  # dispatch RECORD replies without blocking
                if self._reset_event.is_set():
                    self._reset_event.clear()
                    self._state = "idle"
                    self._deadline = None
                    self._blocked = True
                while self._events and not self._stop_event.is_set():
                    self._dispatch(*self._events.popleft())
                now = time.monotonic()
                if self._release and now >= self._release[3]:
                    self._flush_release()
                if self._deadline is not None and now >= self._deadline:
                    self._deadline = None
                    if self._state == "armed":
                        self._start(now)
                    elif self._state == "stop_pending":
                        self._state = "idle"
                        self.on_record_stop()
                select.select([observer], [], [], 0.01)
        finally:
            if context is not None:
                control.record_disable_context(context)
                control.sync()
            if observer is not None:
                observer.close()
            if context is not None:
                control.record_free_context(context)
                control.sync()
            control.close()

    def stop(self):
        self._stop_event.set()

    def force_idle(self):
        self._reset_event.set()

    def _dispatch(self, pressed, code, server_time):
        if self._release:
            if pressed and (code, server_time) == self._release[1:3]:
                self._release = None  # X11 synthetic autorepeat release/press pair
                return
            self._flush_release()
        if pressed:
            self._handle_key(True, code)
        else:
            self._release = (False, code, server_time,
                             time.monotonic() + REPEAT_RELEASE_DELAY)

    def _flush_release(self):
        _, code, _, _ = self._release
        self._release = None
        self._handle_key(False, code)

    def _start(self, now):
        self._state = "recording"
        self._record_start = now
        self.on_record_start()

    def _handle_key(self, pressed, code):
        now = time.monotonic()
        if pressed:
            if code in self._down:
                return
            others_down = bool(self._down)
            self._down.add(code)
            if code == self._esc_code and self._state != "idle":
                active = self._state != "armed"
                self._state = "idle"
                self._deadline = None
                self._blocked = True
                if active:
                    self.on_record_cancel()
                return
            if self._gesture is not None:
                self._blocked = True
                if self._state in ("armed", "recording"):
                    active = self._state == "recording"
                    self._state = "idle"
                    self._deadline = None
                    if active:
                        self.on_record_cancel()
                return
            if code not in self._hotkey_codes:
                return
            self._gesture = code
            self._blocked = others_down
            self._press_time = now
            if others_down:
                return
            if self._state == "idle":
                self._state = "armed"
                self._deadline = now + CHORD_DELAY
            elif self._state == "stop_pending":
                self._state = "recording"
                self._deadline = None
            # In latched state, wait for a clean release before stopping: Alt+S
            # must not stop a recording that was already running.
            return

        self._down.discard(code)
        if code != self._gesture:
            return
        self._gesture = None
        if self._blocked:
            return
        self._deadline = None
        if self._state == "latched":
            self._state = "idle"
            self.on_record_stop()
        elif self._state in ("armed", "recording"):
            tap = (now - self._press_time < HOLD_THRESHOLD and
                   self.config.bool_flag("hotkey_tap_toggle", default=True))
            if self._state == "armed":
                if not tap:
                    self._state = "idle"
                    return
                self._start(now)
            if tap:
                self._state = "latched"
            else:
                self._state = "stop_pending"
                early = now - self._record_start < EARLY_PRESS_WINDOW
                self._deadline = now + (EARLY_RELEASE_CONFIRM_DELAY if early
                                        else RELEASE_CONFIRM_DELAY)
