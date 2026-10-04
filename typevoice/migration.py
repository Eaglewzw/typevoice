"""One-time import from the old Linux port; keep source data as a backup."""

import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def migrate_legacy_data(config_base=None, data_base=None):
    from .config import CONFIG_DIR, DATA_DIR
    config_base = Path(config_base) if config_base else Path(CONFIG_DIR).parent
    data_base = Path(data_base) if data_base else Path(DATA_DIR).parent
    # Both historical releases used typefree for their private data directory.
    legacy_config = config_base / 'typefree/config.json'
    legacy_data = data_base / 'typefree'
    if not legacy_config.is_file() and not legacy_data.is_dir():
        return
    config_base.mkdir(parents=True, exist_ok=True)
    fd = os.open(config_base / '.typevoice-migration.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target_config = config_base / 'typevoice/config.json'
        if legacy_config.is_file() and not target_config.exists():
            target_config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target_config.parent, delete=False) as output:
                temporary = Path(output.name)
                try:
                    output.write(legacy_config.read_bytes())
                    output.flush()
                    os.fchmod(output.fileno(), 0o600)
                    os.replace(temporary, target_config)
                finally:
                    temporary.unlink(missing_ok=True)

        target_data = data_base / 'typevoice'
        if not legacy_data.is_dir() or target_data.exists():
            return
        # Publish the database and its encryption key together; don't import an
        # encrypted database whose key is missing, or overwrite a new profile.
        if (legacy_data / 'history.db').exists() and not (legacy_data / 'history.key').exists():
            raise RuntimeError('旧版历史密钥缺失，请先恢复 history.key；原文件未修改。')
        data_base.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.typevoice-import-', dir=data_base) as directory:
            staged = Path(directory) / 'profile'
            staged.mkdir(mode=0o700)
            key = legacy_data / 'history.key'
            if key.exists():
                shutil.copy2(key, staged / key.name)
                (staged / key.name).chmod(0o600)
            database = legacy_data / 'history.db'
            if database.exists():
                # SQLite backup includes committed data still in the WAL.
                source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
                destination = sqlite3.connect(staged / database.name)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                    source.close()
                (staged / database.name).chmod(0o600)
            audio = legacy_data / 'audio'
            if audio.is_dir():
                shutil.copytree(audio, staged / 'audio')
            os.replace(staged, target_data)


def legacy_autostart_entries(config_base):
    """Only recognize entries created by the previous Linux applications."""
    directory = Path(config_base) / 'autostart'
    for name in ('typefree.desktop', 'suiyan.desktop'):
        path = directory / name
        if path.is_file():
            content = path.read_text(encoding='utf-8')
            if any(line in content.splitlines() for line in ('Name=Typefree', 'Name=Suiyan')):
                yield path
