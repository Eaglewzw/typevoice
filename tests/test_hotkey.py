"""Real X11 shortcut delivery tests; run only inside a disposable Xvfb display.

TYPEVOICE_HOTKEY_TEST=1 xvfb-run -a /usr/bin/python3 -m unittest discover \
    -s tests -p test_hotkey.py -v
No microphone, API calls or user configuration are used.
"""
import os
import time
import unittest

from Xlib import X, XK, display
from Xlib.ext import xtest

from typevoice.x11hotkey import HotkeyManager


class Config:
    keys = "Alt_L,Alt_R"
    toggle = True

    def string(self, key):
        return self.keys

    def bool_flag(self, key, default=True):
        return self.toggle


@unittest.skipUnless(os.environ.get("TYPEVOICE_HOTKEY_TEST") == "1",
                     "requires disposable Xvfb and TYPEVOICE_HOTKEY_TEST=1")
class HotkeyIntegration(unittest.TestCase):
    def setUp(self):
        self.d = display.Display()
        self.root = self.d.screen().root
        self.window = self.root.create_window(
            0, 0, 100, 100, 0, self.d.screen().root_depth,
            event_mask=X.KeyPressMask | X.KeyReleaseMask)
        self.window.map()
        self.window.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()
        self.held = set()
        self.calls = []
        self.errors = []
        self.config = Config()
        self.manager = None
        self.start_manager()

    def start_manager(self):
        self.manager = HotkeyManager(
            self.config, lambda: self.calls.append("start"),
            lambda: self.calls.append("stop"), lambda: self.calls.append("cancel"),
            self.errors.append)
        self.manager.start()
        self.assertTrue(self.manager.ready.wait(3), self.errors)

    def tearDown(self):
        self.manager.stop()
        self.manager.join(3)
        for code in self.held:
            xtest.fake_input(self.d, X.KeyRelease, code)
        self.root.ungrab_key(X.AnyKey, X.AnyModifier)
        self.window.destroy()
        self.d.sync()
        self.d.close()
        self.assertFalse(self.manager.is_alive())
        self.assertEqual(self.errors, [])

    def code(self, name):
        return self.d.keysym_to_keycode(XK.string_to_keysym(name))

    def key(self, name, pressed=True, delay=0.035):
        code = self.code(name)
        if pressed:
            self.held.add(code)
        else:
            self.held.discard(code)
        xtest.fake_input(self.d, X.KeyPress if pressed else X.KeyRelease, code)
        self.d.sync()
        time.sleep(delay)

    def tap(self, name="Alt_L"):
        self.key(name)
        self.key(name, False)

    def expect(self, calls):
        limit = time.monotonic() + 2
        while self.calls != calls and time.monotonic() < limit:
            time.sleep(0.01)
        self.assertEqual(self.calls, calls)

    def grab(self, name, modifiers=X.Mod1Mask):
        # A separate X client owns this grab, just like WeChat/desktop shell.
        self.root.grab_key(self.code(name), modifiers, False,
                           X.GrabModeAsync, X.GrabModeAsync)
        self.d.sync()

    def assert_delivered(self, name):
        self.d.sync()
        events = []
        while self.d.pending_events():
            events.append(self.d.next_event())
        self.assertTrue(any(e.type == X.KeyPress and e.detail == self.code(name)
                            and e.window.id == self.root.id for e in events),
                        f"Other client's passive grab did not receive {name}")

    def chord(self, key="s", alt="Alt_L", delay=0.035):
        self.key(alt, delay=delay)
        self.key(key)
        self.key(key, False)
        self.key(alt, False)

    def test_alt_s_reaches_other_app_without_recording(self):
        self.grab("s")
        self.chord()
        self.assert_delivered("s")
        self.assertEqual(self.calls, [])

    def test_right_alt_s_and_alt_tab(self):
        for key, alt in [("s", "Alt_R"), ("Tab", "Alt_L")]:
            self.grab(key)
            self.chord(key, alt)
            self.assert_delivered(key)
        self.assertEqual(self.calls, [])

    def test_slow_chord_cancels_hold_but_delivers_shortcut(self):
        self.grab("s")
        self.chord(delay=0.65)
        self.assert_delivered("s")
        self.expect(["start", "cancel"])
        time.sleep(0.4)
        self.assertEqual(self.calls, ["start", "cancel"])

    def test_chord_preserves_existing_latched_recording(self):
        self.tap()
        self.expect(["start"])
        self.grab("s")
        self.chord(delay=0.6)
        self.assert_delivered("s")
        self.assertEqual(self.calls, ["start"])
        self.tap()
        self.expect(["start", "stop"])

    def test_hold_and_release(self):
        self.key("Alt_L", delay=0.65)
        self.expect(["start"])
        self.key("Alt_L", False)
        self.expect(["start", "stop"])

    def test_tap_toggle(self):
        self.tap()
        self.expect(["start"])
        time.sleep(0.6)
        self.assertEqual(self.calls, ["start"])
        self.tap()
        self.expect(["start", "stop"])

    def test_other_keys_and_modifiers_in_both_orders(self):
        for other in ("Control_L", "Shift_L", "Super_L", "s", "Alt_R"):
            self.key(other)
            self.tap()
            self.key(other, False)
            self.key("Alt_L")
            self.key(other)
            self.key("Alt_L", False)
            self.key(other, False)
        self.assertEqual(self.calls, [])
        self.tap()
        self.expect(["start"])

    def test_escape_cancels_and_release_does_not_restart(self):
        self.key("Alt_L", delay=0.6)
        self.tap("Escape")
        self.key("Alt_L", False)
        self.expect(["start", "cancel"])
        self.tap()
        self.tap("Escape")
        self.expect(["start", "cancel", "start", "cancel"])

    def test_lock_state_does_not_block_alt(self):
        self.tap("Caps_Lock")
        try:
            self.tap()
            self.tap()
            self.expect(["start", "stop"])
            self.grab("s", X.Mod1Mask | X.LockMask)
            self.chord()
            self.assert_delivered("s")
            self.assertEqual(self.calls, ["start", "stop"])
        finally:
            self.tap("Caps_Lock")

    def test_shutdown_with_pending_start(self):
        self.key("Alt_L", delay=0.03)
        self.manager.stop()
        self.manager.join(2)
        time.sleep(0.25)
        self.assertEqual(self.calls, [])

    def test_hold_only_mode(self):
        self.config.toggle = False
        self.tap()
        self.assertEqual(self.calls, [])
        self.key("Alt_L", delay=0.6)
        self.key("Alt_L", False)
        self.expect(["start", "stop"])

    def test_non_modifier_hotkey_autorepeat(self):
        self.manager.stop()
        self.manager.join(2)
        self.config.keys = "F9"
        self.start_manager()
        self.key("F9", delay=1.5)
        self.assertEqual(self.calls, ["start"])
        self.key("F9", False)
        self.expect(["start", "stop"])


if __name__ == "__main__":
    unittest.main()
