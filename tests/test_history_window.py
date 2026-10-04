"""History UI and recording persistence integration; run with xvfb-run."""

import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from typevoice.app import App
from typevoice.asr import TranscriptionError
from typevoice.config import Config
from typevoice.gtkenv import Gdk, GLib, Gtk
from typevoice.history import History
from typevoice.history_window import HistoryWindow
from typevoice.pipeline import VoicePolishPipeline
from typevoice.settings import SettingsWindow


def pump(seconds=0.08):
    end = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.002)


def text(view):
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)


class HistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(str(self.root / 'config.json'))
        self.history = History(str(self.root / 'history.db'), str(self.root / 'history.key'))
        self.window = HistoryWindow(self.config, self.history)
        self.window.show_all()
        pump()

    def tearDown(self):
        self.window.destroy()
        pump()
        self.temp.cleanup()

    def test_empty_state_then_full_original_and_output_copy(self):
        self.assertIn('还没有', self.window.status.get_text())
        self.assertFalse(self.window.play.get_sensitive())
        raw = '识别原文\n' * 100
        output = '整理后的完整结果\n' * 100
        self.history.add('one', raw, output, 1000)
        self.window.refresh()
        self.assertEqual(text(self.window.original), raw)
        self.assertEqual(text(self.window.output), output)
        self.assertIn('未保存录音', self.window.audio_status.get_text())
        self.window.copy_output.clicked()
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.assertEqual(clipboard.wait_for_text(), output)
        self.window.copy_original.clicked()
        self.assertEqual(clipboard.wait_for_text(), raw)

    def test_pagination_and_new_records_preserve_selection(self):
        for i in range(105):
            self.history.add(f'row-{i}', f'原文{i}', f'结果{i}', 10)
        self.window.refresh()
        self.assertEqual(len(self.window.store), 100)
        self.assertTrue(self.window.more.get_sensitive())
        self.window.more.clicked()
        self.assertEqual(len(self.window.store), 105)
        self.assertFalse(self.window.more.get_sensitive())
        selection = self.window.list.get_selection()
        selection.select_path(Gtk.TreePath.new_from_indices([104]))
        original = text(self.window.output)
        selected_id = self.window._selected['id']
        self.history.add('new-record', '新原文', '新结果', 10)
        self.window.refresh()
        self.assertEqual(self.window._selected['id'], selected_id)
        self.assertEqual(text(self.window.output), original)

    def test_toggle_persists_without_overwriting_other_settings(self):
        self.config.update({'dashscope_api_key': 'test-placeholder'})
        other = Config(self.config.path)
        other.set('overlay_bottom_margin', 180)
        self.window.save_audio.set_active(True)
        saved = Config(self.config.path)
        self.assertTrue(saved.get('history_save_audio'))
        self.assertEqual(saved.get('dashscope_api_key'), 'test-placeholder')
        self.assertEqual(saved.get('overlay_bottom_margin'), 180)
        self.assertEqual(os.stat(saved.path).st_mode & 0o777, 0o600)
        self.window.save_audio.set_active(False)
        self.assertFalse(Config(self.config.path).get('history_save_audio'))

    def test_toggle_reports_failed_save(self):
        with patch.object(self.config, 'set', side_effect=PermissionError):
            self.window.save_audio.set_active(True)
        self.assertFalse(self.window.save_audio.get_active())
        self.assertIn('保存设置失败', self.window.status.get_text())

    def test_environment_override_is_shown_without_switch_recursion(self):
        with patch.dict(os.environ, {'HISTORY_SAVE_AUDIO': '1'}):
            self.window.refresh()
            self.assertTrue(self.window.save_audio.get_active())
            self.window.save_audio.set_active(False)
            self.assertTrue(self.window.save_audio.get_active())
            self.assertIn('环境变量', self.window.status.get_text())

    def test_missing_and_unsafe_audio_paths_are_not_played(self):
        self.history.add('missing', '原文', '结果', 1, 'missing.wav')
        self.window.refresh()
        self.assertFalse(self.window.play.get_sensitive())
        self.assertIn('已不存在', self.window.audio_status.get_text())
        self.history.add('unsafe', '原文', '结果', 1, '../outside.wav')
        self.assertIsNone(self.history.audio_path('unsafe'))

    def test_play_stop_selection_and_close_reap_player(self):
        directory = self.root / 'audio'
        directory.mkdir()
        (directory / 'one.wav').write_bytes(b'fixture')
        self.history.add('one', '原文', '结果', 1000, 'one.wav')
        self.window.refresh()
        self.assertTrue(self.window.play.get_sensitive())
        for stop in ('button', 'selection', 'hide'):
            process = subprocess.Popen(['/bin/sleep', '30'])
            try:
                with patch('typevoice.history_window.shutil.which', return_value='/usr/bin/paplay'), \
                     patch('typevoice.history_window.subprocess.Popen', return_value=process) as start:
                    self.window.play.clicked()
                    self.assertEqual(self.window.play.get_label(), '停止播放')
                    self.assertEqual(start.call_args.args[0], ['paplay', str(directory / 'one.wav')])
                    if stop == 'button':
                        self.window.play.clicked()
                    elif stop == 'selection':
                        self.window.list.get_selection().unselect_all()
                    else:
                        self.window.hide()
                self.assertIsNotNone(process.poll())
                self.assertIsNone(self.window._play_timer)
                self.window.show_all()
                self.window.refresh(force=True)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=2)
        self.window.hide()
        self.assertIsNone(self.window._refresh_timer)

    def test_playback_errors_are_visible(self):
        directory = self.root / 'audio'
        directory.mkdir()
        (directory / 'one.wav').write_bytes(b'fixture')
        self.history.add('one', '', '失败记录', 10, 'one.wav')
        self.window.refresh()
        with patch('typevoice.history_window.shutil.which', return_value=None):
            self.window.play.clicked()
        self.assertIn('pulseaudio-utils', self.window.audio_status.get_text())
        player = Mock()
        player.poll.return_value = 1
        with patch('typevoice.history_window.shutil.which', return_value='/usr/bin/paplay'), \
             patch('typevoice.history_window.subprocess.Popen', return_value=player):
            self.window.play.clicked()
            pump(0.2)
        self.assertIn('播放失败', self.window.audio_status.get_text())
        self.assertIsNone(self.window._player)

    def test_pipeline_saves_text_and_optional_audio_visible_in_browser(self):
        app = App.__new__(App)
        app.config = self.config
        asr = Mock()
        asr.current_version.return_value = 'bailian'
        asr.transcribe_auto.return_value = '这是识别原文'
        polisher = Mock()
        polisher.is_polish_enabled.return_value = False
        polisher.apply_configured_term_corrections.side_effect = lambda value: value
        written = threading.Event()

        def save(*args):
            self.history.add(*args)
            written.set()

        pipeline = VoicePolishPipeline(self.config, asr, polisher,
                                       audio_saver=app._audio_saver, history_writer=save)
        samples = np.ones(1600, dtype=np.float32) * 0.01
        for enabled in (False, True):
            written.clear()
            self.window.save_audio.set_active(enabled)
            with patch('typevoice.audio.AUDIO_DIR', str(self.root / 'audio')):
                pipeline.process(samples)
                self.assertTrue(written.wait(2))
            row = self.history.recent(1)[0]
            self.assertEqual(row['asr'], '这是识别原文')
            self.assertEqual(bool(row['audio_path']), enabled)
            if enabled:
                self.assertTrue(Path(row['audio_path']).is_file())
                self.assertEqual(os.stat(row['audio_path']).st_mode & 0o777, 0o600)
        written.clear()
        asr.transcribe_auto.side_effect = TranscriptionError('network')
        with patch('typevoice.audio.AUDIO_DIR', str(self.root / 'audio')):
            pipeline.process(samples)
            self.assertTrue(written.wait(2))
        self.window.refresh()
        self.assertEqual(text(self.window.original), '')
        self.assertIn('识别失败', text(self.window.output))
        self.assertTrue(self.window.play.get_sensitive())

    def test_settings_history_entry(self):
        callback = Mock()
        settings = SettingsWindow(self.config, on_history=callback)
        try:
            settings._open_history()
            callback.assert_called_once()
        finally:
            settings.destroy()

    def test_standalone_settings_can_open_and_close_history(self):
        settings = SettingsWindow(self.config)
        try:
            self.history.add('standalone', '原文', '结果', 1)
            with patch('typevoice.history.History', return_value=self.history):
                settings._open_history()
            owned = settings._history_window
            pump()
            self.assertEqual(text(owned.output), '结果')
            self.assertTrue(owned.get_visible())
        finally:
            settings.destroy()
        self.assertIsNone(owned._refresh_timer)


if __name__ == '__main__':
    unittest.main()
