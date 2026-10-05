"""主程序 — AppDelegate 的移植：把快捷键、录音、流水线、投递、UI 接起来。

线程模型：
- GTK 主线程：胶囊窗口、剪贴板、托盘；
- hotkey 线程：X11 RECORD 监听与组合键判断，产出语义回调；
- recorder 线程：arecord 采集；
- pipeline 线程：识别 + 润色（串行，一次只有一条在跑）。

所有 UI 状态更新经 GLib.idle_add 回到主线程。
"""

import fcntl
import logging
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import CONFIG_PATH, DATA_DIR, LOG_PATH, LOCK_PATH, Config
from .audio import CueSounds, Recorder, RecorderError, save_audio_wav
from .asr import CloudASRTranscriber
from .polisher import AIPolisher
from .pipeline import VoicePolishPipeline
from .history import History
from .delivery import TextDelivery, COPIED_ONLY, PASTED_KEPT_CLIPBOARD
from .x11hotkey import HotkeyManager

log = logging.getLogger("typevoice")


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("[typevoice] %(message)s"))
    root.addHandler(stream)


_log_lock_file = None  # 持有 flock 的文件对象必须常驻，否则 GC 关闭 fd、锁就丢了


def acquire_single_instance() -> bool:
    global _log_lock_file
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    f = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    _log_lock_file = f  # 常驻引用：进程活着，锁就在
    return True


class App:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._recording = False
        self._level = 0.0
        self._recorder: Recorder | None = None
        self._timer_id = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline")
        self._hotkey: HotkeyManager | None = None
        self._tray = None
        self.capsule = None
        self._polish_failed = False
        self._settings = None
        self._history_window = None

        self.cue = CueSounds(self.config)
        self.history = History(log=log.info)
        self.asr = CloudASRTranscriber(self.config, log=log.info)
        self.polisher = AIPolisher(self.config, log=log.info)
        self.pipeline = VoicePolishPipeline(
            self.config, self.asr, self.polisher,
            on_state=self._on_pipeline_state,
            on_polish_failed=self._on_polish_failed,
            audio_saver=self._audio_saver,
            history_writer=self._history_writer,
            log=log.info)
        self.delivery = TextDelivery(self.config, log=log.info)

    # ---- 启动 / 退出 ----

    def start(self, *, background=True):
        from .overlay import Capsule
        self.capsule = Capsule(self.config)

        # 首次运行只生成配置；设置窗口由用户从托盘主动打开。
        from .config import ensure_template
        if ensure_template(self.config.path):
            log.info(f"config: 已生成模板配置 {self.config.path}，填好 Key 后点托盘「重载配置」")

        self._hotkey = HotkeyManager(
            self.config,
            on_record_start=self._on_record_start,
            on_record_stop=self._on_record_stop,
            on_record_cancel=self._on_record_cancel,
            on_error=self._on_hotkey_error,
            log=log.info)
        self._hotkey.start()

        self._tray = self._create_tray_safely()
        if self._tray is None:
            log.info("tray: AppIndicator 不可用，跳过托盘（sudo apt install "
                     "gir1.2-ayatanaappindicator3-0.1 可启用）")

        retention = self.config.string("history_retention") or "forever"
        removed = self.history.prune(retention)
        if removed:
            log.info(f"history: pruned {removed} expired entries")

        log.info(f"TypeVoice started. config={CONFIG_PATH} data={DATA_DIR}")
        log.info(f"hotkey: hold/tap {self.config.string('hotkey_keysym')}, "
                 f"asr_version={self.asr.current_version()}, "
                 f"asr_configured={self.asr.is_configured()}, "
                 f"polish_provider={self.config.string('polish_provider')}")

    def quit(self, *_args):
        log.info("TypeVoice quitting")
        if self._history_window is not None:
            self._history_window.destroy()
            self._history_window = None
        # 每步都兜住异常，保证 Gtk.main_quit() 一定执行——
        # 退不干净的代价是进程残留在后台、还占着全局快捷键
        if self._hotkey is not None:
            try:
                self._hotkey.stop()
            except Exception:
                log.exception("hotkey.stop failed")
        if self._recorder is not None and self._recording:
            try:
                self._recorder.cancel()
            except Exception:
                log.exception("recorder.cancel failed")
        try:
            self.pipeline.cancel_current()
        except Exception:
            log.exception("pipeline.cancel failed")
        try:
            self._executor.shutdown(wait=False)
        except Exception:
            pass
        from .gtkenv import Gtk
        Gtk.main_quit()

    def install_signal_handlers(self):
        """SIGINT/SIGTERM 挂进 GLib 主循环。

        不能用 Python 的 signal.signal：主线程在 Gtk.main() 的 C 循环里时，
        Python 层的信号处理器要等解释器重新拿到字节码才会跑，主循环空闲时
        可能永远轮不到——进程就杀不死了。
        """
        from .gtkenv import GLib
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.quit)

    def _create_tray_safely(self):
        try:
            from . import tray as tray_mod
            return tray_mod.create_tray(self)
        except Exception as err:  # noqa: BLE001
            log.info(f"tray: 创建失败（{err}），跳过")
            return None

    # ---- 设置与历史 ----

    def show_settings(self):
        from .settings import SettingsWindow
        if self._settings is None:
            self._settings = SettingsWindow(self.config, on_saved=self.reload_config,
                                             on_quit=self.quit, on_history=self.show_history)
        self._settings.show_all()
        self._settings.present()

    def show_history(self):
        from .history_window import HistoryWindow
        if self._history_window is None:
            self._history_window = HistoryWindow(self.config, self.history)
        self._history_window.show_all()
        self._history_window.present()

    def reload_config(self):
        """托盘菜单：用户改完 config.json 后点这里，Key/模型等即刻生效（快捷键改动仍需重启）。"""
        self.config.reload()
        log.info(f"config reloaded. asr_configured={self.asr.is_configured()}, "
                 f"polish_provider={self.config.string('polish_provider')}, "
                 f"asr_version={self.asr.current_version()}")
        self._show_message("配置已重载", auto_hide_ms=1500)

    # ---- 录音控制（hotkey 线程回调） ----

    def _on_record_start(self):
        if self._recording:
            return
        self.config.reload()
        log.info("recording start")
        # 上一条还在处理就放弃它：晚到的结果不能打进来
        self.pipeline.cancel_current()
        self.cue.play("start")
        self._recorder = Recorder(self.config, on_level=self._on_level, log=log.info)
        try:
            self._recorder.start()
        except RecorderError as err:
            self._recorder = None
            self._show_message(str(err), error=True, auto_hide_ms=3000)
            return
        self._recording = True
        from .gtkenv import GLib
        self._timer_id = GLib.timeout_add(200, self._tick_recording)

    def _on_record_stop(self):
        if not self._recording:
            return
        self._recording = False
        log.info("recording stop")
        self._stop_recording_timer()
        recorder, self._recorder = self._recorder, None
        if recorder is None:
            return
        samples = recorder.stop()
        self.cue.play("stop")
        self._show_processing()
        self._executor.submit(self._process, samples)

    def _on_record_cancel(self):
        if not self._recording:
            return
        self._recording = False
        log.info("recording cancelled")
        self._stop_recording_timer()
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.cancel()
        self._hide_capsule()

    def _on_hotkey_error(self, message: str):
        log.error(f"hotkey: {message}")
        self._show_message(message, error=True, auto_hide_ms=5000)

    # ---- UI 调度（全部回 GTK 主线程） ----

    def _on_level(self, level: float):
        self._level = level

    def _tick_recording(self):
        if not self._recording or self._recorder is None:
            self._timer_id = None
            return False
        from .gtkenv import GLib

        def update():
            if self.capsule is not None and self._recording and self._recorder is not None:
                self.capsule.show_recording(self._recorder.elapsed, self._level)

        GLib.idle_add(update)
        return True

    def _stop_recording_timer(self):
        if self._timer_id is not None:
            from .gtkenv import GLib
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _show_message(self, message: str, *, error: bool = False, auto_hide_ms: int = 0):
        from .gtkenv import GLib

        def update():
            if self.capsule is not None:
                self.capsule.show_message(message, error=error, auto_hide_ms=auto_hide_ms)

        GLib.idle_add(update)

    def _show_processing(self):
        from .gtkenv import GLib

        def update():
            if self.capsule is not None:
                self.capsule.show_processing()

        GLib.idle_add(update)

    def _hide_capsule(self):
        from .gtkenv import GLib

        def update():
            if self.capsule is not None:
                self.capsule.hide()

        GLib.idle_add(update)

    # ---- 流水线回调（pipeline 线程） ----

    def _on_pipeline_state(self, state: dict):
        event = state.get("event")
        if event in ("transcribing", "polishing"):
            # Typeless 风格：处理中只显示流动圆点动画，不显示文字细节
            self._show_processing()
        elif event == "empty":
            # 没听清内容：安静收起，不打扰
            self._hide_capsule()
        elif event == "error":
            self._show_message(state.get("message", "识别失败"), error=True, auto_hide_ms=4000)
        elif event == "done":
            text = state.get("text", "")
            self._executor.submit(self._deliver, text, polish_failed=self._polish_failed)

    def _on_polish_failed(self, reason: str):
        # 原文仍会投递成功；完成后给普通提示，不能抢先显示整次操作失败。
        self._polish_failed = True
        log.warning("Polish unavailable; delivering original transcript: %s", reason)

    def _process(self, samples):
        self._polish_failed = False
        try:
            self.pipeline.process(samples)
        except Exception as err:  # noqa: BLE001
            log.exception("pipeline crashed")
            self._show_message(f"处理出错：{err}", error=True, auto_hide_ms=4000)

    def _deliver(self, text: str, *, polish_failed: bool = False):
        try:
            result = self.delivery.deliver(text)
        except Exception as err:  # noqa: BLE001
            log.exception("deliver crashed")
            self._show_message(f"投递失败：{err}", error=True, auto_hide_ms=4000)
            return
        if result == COPIED_ONLY:
            self._show_message("已复制，请 Ctrl+V 粘贴", auto_hide_ms=2500)
        elif result == PASTED_KEPT_CLIPBOARD:
            self._show_message("已复制", auto_hide_ms=2000)
        elif polish_failed:
            self._show_message("已输出原文（本次未润色或翻译）", auto_hide_ms=2500)
        else:
            self._hide_capsule()

    # ---- 历史 / 音频 ----

    def _audio_saver(self, samples, entry_id) -> str | None:
        if not self.config.bool_flag("history_save_audio", default=False):
            return None
        return save_audio_wav(samples, entry_id)

    def _history_writer(self, entry_id, asr, output, duration_ms, audio_file):
        self.history.add(entry_id, asr, output, duration_ms, audio_file)


def run_app(*, background=True) -> int:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not os.environ.get("DISPLAY"):
        print("TypeVoice目前需要 X11 会话；请在登录界面选择 Ubuntu on Xorg。", file=sys.stderr)
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            from .gtkenv import Gtk
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
                                       text="TypeVoice目前需要 X11 桌面")
            dialog.format_secondary_text("请注销，在登录界面选择 Ubuntu on Xorg 后重新打开。")
            dialog.run()
            dialog.destroy()
        return 1
    if not acquire_single_instance():
        return 0
    setup_logging()
    app = App()
    from .gtkenv import Gtk  # noqa: F401  导入失败即缺 GTK，尽早暴露
    app.start(background=background)
    app.install_signal_handlers()
    Gtk.main()
    return 0
