"""配置与密钥存储 — 对应 mac 的 VoicePolishConfig。

config.json 放在 XDG_CONFIG_HOME/typevoice/ 下，权限 0600（因为里面有 API Key）。
所有键都可用同名大写环境变量覆盖：读取顺序 环境变量 > config.json > 默认值
（对应 mac 的 string(forKey:envKey:)）。
"""

import json
import os
import stat
import threading

APP_NAME = "typevoice"


def _xdg(env_key: str, default_subpath: str) -> str:
    base = os.environ.get(env_key) or os.path.expanduser(default_subpath)
    return base


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config") + "/" + APP_NAME
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share") + "/" + APP_NAME
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache") + "/" + APP_NAME

CONFIG_PATH = CONFIG_DIR + "/config.json"
HISTORY_DB_PATH = DATA_DIR + "/history.db"
HISTORY_KEY_PATH = DATA_DIR + "/history.key"
AUDIO_DIR = DATA_DIR + "/audio"
LOG_PATH = CACHE_DIR + "/typevoice.log"
LOCK_PATH = DATA_DIR + "/lock"

# 各键的默认值；类型决定读取时的转换（bool/int/float/str/list）
DEFAULTS = {
    # 识别（火山引擎 / 阿里百炼）；凭证默认留空，由用户自行申请并填写。
    "asr_version": "turbo",              # turbo / standard / v2 / bailian
    "bigasr_api_key": "",                # 火山新版单 API Key（优先）
    "bigasr_app_id": "",                 # 火山旧版 App ID（向后兼容）
    "bigasr_access_token": "",           # 火山旧版 Access Token
    "dashscope_api_key": "",             # 百炼 DashScope Key（识别 bailian + 千问润色共用）
    "bigasr_boosting_table_name": "",
    "bigasr_boosting_table_id": "",
    "bigasr_correct_table_name": "",
    "bigasr_correct_table_id": "",
    "chunk_asr_max_concurrent": 5,
    # 润色
    "polish_provider": "qwen",           # qwen / zhipu / doubao / none
    "qwen_polish_model": "auto",         # auto = 质量优先队列（见 polisher.PolishModelRouter）
    "zhipu_polish_model": "glm-4.7-flash",
    "doubao_polish_model": "doubao-seed-2-0-pro-260215",
    "zhipu_api_key": "",
    "ark_api_key": "",
    # 输入口令（「用英文」等）
    "output_language_command_enabled": True,
    "default_output_language": "",       # 语言 id；空 = 跟随说话语言
    # 快捷键
    "hotkey_keysym": "Alt_L,Alt_R",            # X11 keysym：Alt_L / Caps_Lock / F9 …
    "hotkey_tap_toggle": True,           # 单击=开始/再按停；长按=按住说话
    # 提示音（paplay）
    "cue_sounds_enabled": True,
    # 主屏底部悬浮窗，逻辑像素；给自动隐藏的 Dock 留空间
    "overlay_bottom_margin": 96,
    # 文本投递
    "paste_delay_ms": 30,                # 写剪贴板后到模拟按键的间隔
    "restore_clipboard_delay_ms": 300,   # 粘贴后多久检查并还原剪贴板
    "restore_clipboard": True,
    # 历史
    "history_retention": "forever",      # forever / oneMonth / oneWeek / oneDay / off
    "history_save_audio": False,
    # 词库
    "term_corrections": [],              # [{"target": "...", "variants": [...], "enabled": true}]
    "hot_words": [],                     # ["词1", "词2"]
    "asr_vocab_mode": "both",            # context / hotwords / both
}


def _coerce(raw, default):
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        return raw if isinstance(raw, list) else default
    return str(raw) if raw is not None else default


# 首次运行生成的模板：让托盘「打开设置」永远有东西可打开
TEMPLATE = {
    "_说明": "TypeVoice 不附带 API Key。请向服务商申请自己的 Key 并填入对应配置项，保存后下次录音生效；"
             "全部配置项见 README.md。此文件含密钥，权限已设为 0600，请勿外传。",
    "bigasr_api_key": "",
    "dashscope_api_key": "",
    "asr_version": "turbo",
    "polish_provider": "qwen",
    "qwen_polish_model": "auto",
    "hotkey_keysym": "Alt_L,Alt_R",
    "hotkey_tap_toggle": True,
    "output_language_command_enabled": True,
    "default_output_language": "",
    "history_retention": "forever",
    "history_save_audio": False,
    "hot_words": [],
    "term_corrections": [],
}


def ensure_template(path: str = CONFIG_PATH) -> bool:
    """配置文件不存在时生成模板；返回是否新生成了。"""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(TEMPLATE, f, ensure_ascii=False, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return True


class Config:
    def __init__(self, path: str = CONFIG_PATH):
        if path == CONFIG_PATH:
            from .migration import migrate_legacy_data
            migrate_legacy_data()
        self.path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, key: str):
        default = DEFAULTS.get(key, "")
        env = os.environ.get(key.upper())
        with self._lock:
            raw = env if env not in (None, "") else self._data.get(key)
        if raw is None or raw == "":
            return default
        return _coerce(raw, default)

    def raw(self, key: str):
        """原样取值（不做默认值兜底），output_languages 这类复杂结构用。"""
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, value):
        with self._lock:
            self._data[key] = value
            self._save()

    def update(self, pairs: dict):
        with self._lock:
            self._data.update(pairs)
            self._save()

    def reload(self):
        """从磁盘重新读取（用户在托盘点「重载配置」后生效）。"""
        with self._lock:
            self._load()

    # ---- 便利读取（对齐 mac 侧常用入口） ----

    def string(self, key: str):
        v = self.get(key)
        return str(v) if v not in (None, "") else None

    def bool_flag(self, key: str, default=True) -> bool:
        env = os.environ.get(key.upper())
        with self._lock:
            raw = env if env is not None else self._data.get(key)
        if raw is None:
            return default
        return _coerce(raw, True)
