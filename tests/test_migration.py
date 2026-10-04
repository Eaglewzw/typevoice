"""Profile upgrades must preserve encrypted data and existing installations."""

import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from typevoice.history import History
from typevoice.migration import migrate_legacy_data


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = self.root / 'config'
        self.data = self.root / 'data'
        self.old_config = self.config / 'typefree/config.json'
        self.old_config.parent.mkdir(parents=True)
        self.old_config.write_text(json.dumps({'dashscope_api_key': 'dummy-test-key'}))
        self.old_data = self.data / 'typefree'
        self.history = History(str(self.old_data / 'history.db'), str(self.old_data / 'history.key'))
        self.history.add('old', '原始识别', '整理后的内容', 100, 'old.wav')
        (self.old_data / 'audio').mkdir()
        (self.old_data / 'audio/old.wav').write_bytes(b'placeholder audio')

    def tearDown(self):
        self.temporary.cleanup()

    def test_import_preserves_decryption_audio_and_source_backup(self):
        migrate_legacy_data(self.config, self.data)
        profile = self.data / 'typevoice'
        migrated = History(str(profile / 'history.db'), str(profile / 'history.key'))
        self.assertEqual(migrated.recent()[0]['output'], '整理后的内容')
        self.assertEqual((profile / 'audio/old.wav').read_bytes(), b'placeholder audio')
        self.assertEqual((profile / 'history.key').read_bytes(), (self.old_data / 'history.key').read_bytes())
        new_config = self.config / 'typevoice/config.json'
        self.assertEqual(new_config.read_bytes(), self.old_config.read_bytes())
        self.assertEqual(new_config.stat().st_mode & 0o777, 0o600)
        self.assertEqual((profile / 'history.key').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.history.recent()[0]['output'], '整理后的内容')

    def test_existing_profile_never_overwritten_on_repeat_launch(self):
        migrate_legacy_data(self.config, self.data)
        new_config = self.config / 'typevoice/config.json'
        new_config.write_text('{"hotkey_keysym":"F9"}')
        profile = self.data / 'typevoice'
        migrated = History(str(profile / 'history.db'), str(profile / 'history.key'))
        migrated.add('new', 'new', '新的内容', 100)
        migrate_legacy_data(self.config, self.data)
        self.assertEqual(json.loads(new_config.read_text()), {'hotkey_keysym': 'F9'})
        self.assertEqual(len(migrated.recent()), 2)

    def test_backup_includes_committed_wal_rows(self):
        conn = sqlite3.connect(self.old_data / 'history.db')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('INSERT INTO entries (id,ts,output_enc) VALUES (?,?,?)',
                         ('wal', '2026-10-04T12:00:00', self.history._seal('WAL 内容')))
            conn.commit()
            migrate_legacy_data(self.config, self.data)
            profile = self.data / 'typevoice'
            migrated = History(str(profile / 'history.db'), str(profile / 'history.key'))
            self.assertIn('WAL 内容', [r['output'] for r in migrated.recent()])
        finally:
            conn.close()

    def test_missing_key_does_not_publish_broken_database(self):
        (self.old_data / 'history.key').unlink()
        with self.assertRaisesRegex(RuntimeError, '密钥缺失'):
            migrate_legacy_data(self.config, self.data)
        self.assertFalse((self.data / 'typevoice').exists())
        self.assertTrue((self.old_data / 'history.db').exists())

    def test_existing_empty_data_directory_is_respected(self):
        (self.data / 'typevoice').mkdir()
        migrate_legacy_data(self.config, self.data)
        self.assertEqual(list((self.data / 'typevoice').iterdir()), [])

    def test_startup_replaces_legacy_autostart_entry(self):
        from typevoice.__main__ import cmd_run
        autostart = self.config / 'autostart'
        autostart.mkdir()
        old_entry = autostart / 'typefree.desktop'
        old_entry.write_text('[Desktop Entry]\nType=Application\nName=Suiyan\nExec=/usr/share/suiyan/run.sh\n')
        with patch('typevoice.__main__.CONFIG_DIR', str(self.config / 'typevoice')), \
                patch('typevoice.app.run_app', return_value=0):
            self.assertEqual(cmd_run(SimpleNamespace(background=True)), 0)
        new_entry = autostart / 'typevoice.desktop'
        self.assertTrue(new_entry.exists())
        self.assertIn('Name=TypeVoice', new_entry.read_text())
        self.assertIn('run --background', new_entry.read_text())
        self.assertFalse(old_entry.exists())


if __name__ == '__main__':
    unittest.main()
