"""Run under xvfb-run: real GTK windows/clipboard, no cloud API or microphone.

The clipboard and focus belong to the disposable virtual display.
"""

import threading
import tempfile
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from typevoice.app import App
from typevoice.config import Config
from typevoice.delivery import TextDelivery, PASTED, COPIED_ONLY, PASTED_KEPT_CLIPBOARD, _paste_combo
from typevoice.gtkenv import Gdk, GLib, Gtk
from typevoice.overlay import Capsule
from typevoice.pipeline import VoicePolishPipeline
from typevoice.x11display import open_display
from typevoice.settings import SettingsWindow


def config(**values):
    cfg = Config('/nonexistent/typevoice-test-config.json')
    cfg._data.update(values)
    return cfg


def pump(seconds=0.08):
    end = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.002)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config()
        self.capsule = Capsule(self.cfg)

    def tearDown(self):
        self.capsule.hide()
        self.capsule.window.destroy()
        pump()

    def assert_bottom_position(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo, work = monitor.get_geometry(), monitor.get_workarea()
        x, y = self.capsule.window.get_position()
        w, h = self.capsule.window.get_size()
        self.assertEqual(x, geo.x + (geo.width - w) // 2)
        self.assertEqual(y + h, min(geo.y + geo.height - self.cfg.get('overlay_bottom_margin'),
                                   work.y + work.height - 16))

    def test_recording_processing_error_keep_bottom_anchor(self):
        self.capsule.show_recording(3, 0.5)
        pump()
        self.assert_bottom_position()
        self.capsule.show_processing()
        pump()
        self.assert_bottom_position()
        self.assertFalse(self.capsule.level_bar.get_visible())
        self.capsule.show_message('识别服务暂时不可用，请稍后重试', error=True)
        pump()
        self.assert_bottom_position()

    def test_margin_reload_and_primary_monitor_workarea(self):
        self.cfg._data['overlay_bottom_margin'] = 150
        self.capsule.show_recording()
        pump()
        self.assert_bottom_position()
        # Second monitor has a negative origin and a tall bottom panel.
        monitor = Mock()
        monitor.get_geometry.return_value = SimpleNamespace(x=-1920, y=0, width=1920, height=1080)
        monitor.get_workarea.return_value = SimpleNamespace(x=-1920, y=24, width=1920, height=856)
        display = Mock()
        display.get_primary_monitor.return_value = monitor
        with patch.object(Gdk.Display, 'get_default', return_value=display), \
                patch.object(self.capsule.window, 'move') as move:
            self.capsule._move_to_bottom(120, 40)
        move.assert_called_once_with(-1020, 824)
        display.get_monitor.assert_not_called()

    def test_hide_really_unmaps_window(self):
        self.capsule.show_recording()
        pump()
        self.assertTrue(self.capsule.window.get_mapped())
        self.capsule.hide()
        pump()
        self.assertFalse(self.capsule.window.get_visible())
        self.assertFalse(self.capsule.window.get_mapped())
        self.assertIsNone(self.capsule._anim_timer)

    def test_previous_error_timer_does_not_hide_new_recording_or_processing(self):
        for show in (self.capsule.show_recording, self.capsule.show_processing):
            self.capsule.show_message('旧错误', error=True, auto_hide_ms=30)
            show()
            pump(0.1)
            self.assertTrue(self.capsule.window.get_visible())
            self.assertFalse(self.capsule.window.get_style_context().has_class('capsule-error'))

    def test_processing_from_hidden_does_not_show_level_bar(self):
        self.capsule.show_processing()
        pump()
        self.assertFalse(self.capsule.level_bar.get_visible())
        self.capsule.show_message('已输出原文', auto_hide_ms=30)
        pump(0.1)
        self.assertFalse(self.capsule.window.get_visible())

    def test_volume_smoothing_and_focus_free_window(self):
        self.capsule.show_recording(65, 1)
        pump(.15)
        self.assertGreater(self.capsule._level, 0)
        self.assertLess(self.capsule._level, 1)
        self.assertEqual(self.capsule.label.get_text(), '01:05')
        self.assertFalse(self.capsule.window.get_accept_focus())
        self.capsule.show_recording(65, 0)
        previous = self.capsule._level
        pump(.15)
        self.assertLess(self.capsule._level, previous)

    def test_destroy_cleans_up_animation_and_timeout(self):
        self.capsule.show_recording()
        self.capsule.window.destroy()
        self.assertIsNone(self.capsule._anim_timer)
        self.assertIsNone(self.capsule._hide_timer)


class SettingsTests(unittest.TestCase):
    def test_first_save_is_private_and_preserves_advanced_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = Config(os.path.join(temporary, 'config.json'))
            cfg._data['term_corrections'] = [{'target': 'TypeVoice', 'variants': ['TypeVoice']}]
            on_saved = Mock()
            window = SettingsWindow(cfg, on_saved=on_saved)
            try:
                window.show_all()
                pump()
                self.assertFalse(window.dashscope.get_visibility())
                window.provider.set_active_id('bailian')
                window.dashscope.set_text('test-placeholder')
                window.margin.set_value(120)
                window._save()
                saved = Config(cfg.path)
                self.assertEqual(saved.get('dashscope_api_key'), 'test-placeholder')
                self.assertEqual(saved.get('asr_version'), 'bailian')
                self.assertEqual(saved.get('overlay_bottom_margin'), 120)
                self.assertEqual(len(saved.raw('term_corrections')), 1)
                self.assertEqual(os.stat(cfg.path).st_mode & 0o777, 0o600)
                on_saved.assert_called_once()
                window._hide()
                self.assertFalse(window.get_visible())
            finally:
                window.destroy()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = TextDelivery(config(paste_delay_ms=0))
        self.delivery._run_on_gtk = lambda fn, **kwargs: fn()
        self.delivery._snapshot_text = Mock(return_value='old clipboard')
        self.delivery._set_text = Mock()
        self.delivery._xtest_paste = Mock(return_value=True)

    def test_real_timer_restores_clipboard_after_successful_paste(self):
        restored = threading.Event()
        self.delivery._restore_if_ours = Mock(side_effect=lambda *args: restored.set())
        self.assertEqual(self.delivery.deliver('识别文字'), PASTED)
        self.assertTrue(restored.wait(2))
        self.delivery._restore_if_ours.assert_called_once_with('old clipboard', '识别文字')

    def test_cleanup_failure_does_not_fail_completed_paste(self):
        with patch('typevoice.delivery.threading.Timer', side_effect=RuntimeError('no threads')):
            self.assertEqual(self.delivery.deliver('识别文字'), PASTED_KEPT_CLIPBOARD)

    def test_clipboard_failure_does_not_claim_copied(self):
        self.delivery._set_text.side_effect = TimeoutError('GTK timeout')
        with self.assertRaisesRegex(RuntimeError, '无法写入剪贴板'):
            self.delivery.deliver('识别文字')
        self.delivery._xtest_paste.assert_not_called()

    def test_paste_unavailable_keeps_text_for_manual_paste(self):
        self.delivery._xtest_paste.return_value = False
        self.assertEqual(self.delivery.deliver('识别文字'), COPIED_ONLY)
        self.delivery._set_text.assert_called_once_with('识别文字')

    def test_terminal_shortcut_does_not_match_st_inside_other_app_names(self):
        self.assertEqual(_paste_combo('python3 -m unittest Python3 -m unittest', 1, 2, 3), [1, 3])
        self.assertEqual(_paste_combo('studio Studio', 1, 2, 3), [1, 3])
        self.assertEqual(_paste_combo('st St', 1, 2, 3), [1, 2, 3])
        self.assertEqual(_paste_combo('gnome-terminal-server Gnome-terminal', 1, 2, 3), [1, 2, 3])


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config(restore_clipboard_delay_ms=100)
        # Avoid opening real history/config/audio files or grabbing hotkeys.
        self.app = App.__new__(App)
        self.app.capsule = Capsule(self.cfg)
        self.app._polish_failed = False
        self.app._executor = ThreadPoolExecutor(max_workers=1)
        self.app.delivery = TextDelivery(self.cfg)
        self.window = Gtk.Window()
        self.entry = Gtk.Entry()
        self.window.add(self.entry)
        self.window.show_all()
        self.entry.grab_focus()
        pump()
        from gi.repository import GdkX11  # noqa: F401; registers get_xid
        from Xlib import X
        disp = open_display()
        disp.create_resource_object('window', self.window.get_window().get_xid()).set_input_focus(
            X.RevertToParent, X.CurrentTime)
        disp.sync()
        disp.close()
        self.clipboard = Gtk.Clipboard.get_default(Gdk.Display.get_default())
        self.clipboard.set_text('original clipboard', -1)
        pump()

    def tearDown(self):
        pump(0.3)
        self.app._executor.shutdown(wait=True)
        self.app.capsule.hide()
        self.app.capsule.window.destroy()
        self.window.destroy()
        self.clipboard.clear()
        pump()

    def run_pipeline(self, *, polish_timeout=False, asr_error=False):
        raw = '这是一段已经成功识别并且需要输出的文字'
        asr = Mock()
        asr.current_version.return_value = 'bailian'
        asr.transcribe_auto.return_value = raw
        if asr_error:
            asr.transcribe_auto.side_effect = TimeoutError('ASR timed out')
        polisher = Mock()
        polisher.is_polish_enabled.return_value = True
        polisher.polish.return_value = raw
        if polish_timeout:
            polisher.polish.side_effect = TimeoutError('Time Out')
        polisher.apply_configured_term_corrections.side_effect = lambda text: text
        self.app.pipeline = VoicePolishPipeline(
            self.cfg, asr, polisher, on_state=self.app._on_pipeline_state,
            on_polish_failed=self.app._on_polish_failed)
        future = self.app._executor.submit(self.app._process, np.ones(1600, dtype=np.float32))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            pump(0.02)
            if future.done() and (self.entry.get_text() or asr_error):
                break
        future.result(timeout=1)
        pump(0.3)
        return raw

    def test_success_pastes_into_real_entry_restores_clipboard_and_hides(self):
        raw = self.run_pipeline()
        self.assertEqual(self.entry.get_text(), raw)
        self.assertEqual(self.clipboard.wait_for_text(), 'original clipboard')
        self.assertFalse(self.app.capsule.window.get_visible())

    def test_polish_timeout_outputs_raw_with_non_error_notice(self):
        raw = self.run_pipeline(polish_timeout=True)
        self.assertEqual(self.entry.get_text(), raw)
        self.assertIn('已输出原文', self.app.capsule.label.get_text())
        self.assertNotIn('Time Out', self.app.capsule.label.get_text())
        self.assertFalse(self.app.capsule.window.get_style_context().has_class('capsule-error'))

    def test_actual_asr_failure_still_shows_error(self):
        self.run_pipeline(asr_error=True)
        self.assertEqual(self.entry.get_text(), '')
        self.assertIn('ASR timed out', self.app.capsule.label.get_text())
        self.assertTrue(self.app.capsule.window.get_style_context().has_class('capsule-error'))


if __name__ == '__main__':
    unittest.main()
