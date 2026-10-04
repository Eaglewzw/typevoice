"""本机加密历史 — HistoryCrypto / AIPolisher 写日志部分的移植。

SQLite 存储，asr/output 两列用 Fernet(AES-128-CBC+HMAC) 加密（对应 mac 的
AES-GCM「vpenc1:」行格式，这里用「tfenc1:」前缀）；密钥文件 0600，
首次运行自动生成。没装 cryptography 时降级为明文（前缀 plain:），功能不受影响。
"""

import base64
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta

from .config import AUDIO_DIR, HISTORY_DB_PATH, HISTORY_KEY_PATH

CIPHER_PREFIX = "tfenc1:"
PLAIN_PREFIX = "plain:"

RETENTION_DAYS = {"oneMonth": 30, "oneWeek": 7, "oneDay": 1}


class History:
    def __init__(self, db_path: str = HISTORY_DB_PATH, key_path: str = HISTORY_KEY_PATH, log=None):
        self.db_path = db_path
        self.key_path = key_path
        self.log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._fernet = self._load_or_create_key()
        self._ensure_schema()

    # ---- 密钥 ----

    def _load_or_create_key(self):
        try:
            from cryptography.fernet import Fernet
        except ImportError:
            self.log("history: 未安装 cryptography，历史将以明文保存")
            return None
        try:
            with open(self.key_path, "rb") as f:
                key = f.read().strip()
            if key:
                return Fernet(key)
        except OSError:
            pass
        key = base64.urlsafe_b64encode(os.urandom(32))
        os.makedirs(os.path.dirname(self.key_path), exist_ok=True)
        with open(self.key_path, "wb") as f:
            f.write(key)
        os.chmod(self.key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self.log(f"history: generated new encryption key at {self.key_path}")
        return Fernet(key)

    def _seal(self, text: str) -> str:
        if self._fernet is None:
            return PLAIN_PREFIX + text
        return CIPHER_PREFIX + self._fernet.encrypt(text.encode()).decode()

    def _open(self, blob: str) -> str:
        if blob.startswith(CIPHER_PREFIX):
            if self._fernet is None:
                return "（密钥丢失，无法解密这条历史）"
            try:
                return self._fernet.decrypt(blob[len(CIPHER_PREFIX):].encode()).decode()
            except Exception:
                return "（解密失败）"
        if blob.startswith(PLAIN_PREFIX):
            return blob[len(PLAIN_PREFIX):]
        return blob

    # ---- 表结构 ----

    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self):
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'polish',
                    asr_enc TEXT NOT NULL DEFAULT '',
                    output_enc TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    audio_file TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries(ts)")

    # ---- 写入 / 读取 / 清理 ----

    def add(self, entry_id: str, asr: str, output: str, duration_ms: int, audio_file=None):
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entries (id, ts, kind, asr_enc, output_enc, duration_ms, audio_file) "
                "VALUES (?, ?, 'polish', ?, ?, ?, ?)",
                (entry_id, datetime.now().isoformat(timespec="seconds"),
                 self._seal(asr), self._seal(output), duration_ms, audio_file))

    def recent(self, limit: int = 50) -> list:
        rows = []
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "SELECT id, ts, asr_enc, output_enc, duration_ms, audio_file "
                "FROM entries WHERE kind = 'polish' ORDER BY ts DESC LIMIT ?", (limit,))
            for row in cursor.fetchall():
                rows.append({
                    "id": row[0], "ts": row[1],
                    "asr": self._open(row[2]), "output": self._open(row[3]),
                    "duration_ms": row[4],
                    "audio_path": os.path.join(AUDIO_DIR, row[5]) if row[5] else None,
                })
        return rows

    def audio_path(self, entry_id: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT audio_file FROM entries WHERE id = ?", (entry_id,)).fetchone()
        return os.path.join(AUDIO_DIR, row[0]) if row and row[0] else None

    def prune(self, retention: str = "forever") -> int:
        """按保留策略删除过期记录；返回删除条数。retention=off 只清音频不留新记录由调用方控制。"""
        days = RETENTION_DAYS.get(retention)
        if days is None:
            return 0
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM entries WHERE ts < ?", (cutoff,))
            return cursor.rowcount
