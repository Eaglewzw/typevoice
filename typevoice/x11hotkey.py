"""全局快捷键 — HotkeyManager 的移植（X11 XGrabKey 实现）。

行为与 mac 版对齐（RecordingHotkeyBehavior）：
- 按下立即开始录音；松手即停（长按说话）；
- 按住不足 0.5 秒的「单击」判为点按开关：保持录音，再按一下才停；
- 松手要等 0.08 秒确认（滤掉修饰键信号的瞬间抖动）；刚按下的头 1.5 秒里放宽到
  0.35 秒（按键没压实时会出现「松开零点几秒又按回去」的假信号）；
- 录音期间按 Esc 取消（丢弃本次录音）。

注意：XGrabKey 抓的是「无修饰键按下快捷键本身」，不会影响 Alt+Tab 等组合。
"""

import threading

from .config import Config

# 与 mac 的 RecordingHotkeyBehavior 相同的手势参数
HOLD_THRESHOLD = 0.50
RELEASE_CONFIRM_DELAY = 0.08
EARLY_PRESS_WINDOW = 1.5
EARLY_RELEASE_CONFIRM_DELAY = 0.35

ESC_KEYSYM = 0xFF1B


def lock_modifier_combos(X):
    """XGrabKey 的经典坑：NumLock/CapsLock/ScrollLock 开着时，单独按键的
    modifier mask 不是 0，只抓 modifiers=0 会收不到事件。把锁定修饰键的
    8 种组合全部抓上（xbindkeys 等工具的标准做法）。普通组合键（如 Alt+Tab）
    自带其他修饰键，不受影响。"""
    return [
        0,
        X.Mod2Mask, X.LockMask, X.Mod2Mask | X.LockMask,
        X.Mod5Mask, X.Mod2Mask | X.Mod5Mask, X.LockMask | X.Mod5Mask,
        X.Mod2Mask | X.LockMask | X.Mod5Mask,
    ]


class HotkeyError(Exception):
    pass


class HotkeyManager(threading.Thread):
    """语义回调（都在 hotkey 线程上调用，UI 侧自行调度）：
      on_record_start()              按下（开始录音）
      on_record_stop()               确认结束（松手确认 / 点按再按）
      on_record_cancel()             Esc 取消
      on_error(message)              无法抓键等致命错误
    """

    def __init__(self, config: Config, on_record_start, on_record_stop,
                 on_record_cancel, on_error=None, log=None):
        super().__init__(name="hotkey", daemon=True)
        self.config = config
        self.on_record_start = on_record_start
        self.on_record_stop = on_record_stop
        self.on_record_cancel = on_record_cancel
        self.on_error = on_error or (lambda msg: None)
        self.log = log or (lambda msg: None)

        self._disp = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # 状态机：idle / recording / stop_pending / latched
        self._state = "idle"
        self._press_time = 0.0
        self._record_start = 0.0
        self._stop_timer = None

    # ---- 生命周期 ----

    def run(self):
        try:
            self._run_loop()
        except Exception as err:  # noqa: BLE001
            self.on_error(str(err))

    def _run_loop(self):
        from Xlib import X, XK, error
        from .x11display import open_display

        disp = open_display()
        self._disp = disp
        # hotkey_keysym 支持逗号分隔多个键：默认左右 Alt 都可按住说话
        keyspec = self.config.string("hotkey_keysym") or "Alt_L,Alt_R"
        names = [n.strip() for n in keyspec.split(",") if n.strip()] or ["Alt_L"]
        keycodes = []
        for name in names:
            keysym = getattr(XK, f"XK_{name}", None) or XK.string_to_keysym(name)
            kc = disp.keysym_to_keycode(keysym) if keysym else 0
            if kc and kc not in keycodes:
                keycodes.append(kc)
            else:
                self.log(f"Hotkey: 键名 {name!r} 在当前键盘布局下不可用，已跳过")
        if not keycodes:
            self.on_error(f"快捷键 {keyspec!r} 一个都解析不到，请在 config.json 里改 hotkey_keysym")
            return
        self._hotkey_codes = frozenset(keycodes)

        def error_handler(err, request):
            self.on_error("全局快捷键已被其他程序占用（如输入法/剪贴板工具），请换 hotkey_keysym")
            self._stop_event.set()
            return True

        disp.set_error_handler(error_handler)
        self.log(f"Hotkey: grabbing keycodes={keycodes} ({keyspec})")
        # XGrabKey 的错误是异步回报的，sync 一下把 AlreadyGrabbed 之类暴露给 error_handler
        for kc in keycodes:
            for modifiers in lock_modifier_combos(X):
                disp.screen().root.grab_key(kc, modifiers, True, X.GrabModeAsync, X.GrabModeAsync)
        disp.sync()

        self._event_loop(disp, keycodes)
        try:
            disp.close()
        except Exception:
            pass

    def _event_loop(self, disp, keycodes):
        from Xlib import X, error
        try:
            while not self._stop_event.is_set():
                event = disp.next_event()
                if self._stop_event.is_set():
                    break
                if event.type in (X.KeyPress, X.KeyRelease):
                    self._handle_key(event.type == X.KeyPress, event.detail)
        except error.ConnectionClosedError:
            if not self._stop_event.is_set():
                self.on_error("X11 连接断开")
        finally:
            self._ungrab_escape()
            try:
                from Xlib import X
                for kc in keycodes:
                    for modifiers in lock_modifier_combos(X):
                        disp.screen().root.ungrab_key(kc, modifiers)
            except Exception:
                pass

    def stop(self):
        self._stop_event.set()
        # next_event() 是阻塞的：关连接让循环退出
        disp = self._disp
        if disp is not None:
            try:
                disp.close()
            except Exception:
                pass

    # ---- Esc 抓取（仅录音期间） ----

    def _grab_escape(self):
        disp = self._disp
        if disp is None:
            return
        from Xlib import X
        esc = disp.keysym_to_keycode(ESC_KEYSYM)
        if esc:
            try:
                for modifiers in lock_modifier_combos(X):
                    disp.screen().root.grab_key(esc, modifiers, True,
                                                X.GrabModeAsync, X.GrabModeAsync)
                disp.sync()
            except Exception:
                pass

    def _ungrab_escape(self):
        disp = self._disp
        if disp is None:
            return
        from Xlib import X
        esc = disp.keysym_to_keycode(ESC_KEYSYM)
        if esc:
            try:
                for modifiers in lock_modifier_combos(X):
                    disp.screen().root.ungrab_key(esc, modifiers)
                disp.sync()
            except Exception:
                pass

    # ---- 按键状态机 ----

    def _handle_key(self, is_press: bool, detail: int):
        import time
        now = time.monotonic()
        esc_code = self._disp.keysym_to_keycode(ESC_KEYSYM) if self._disp else 0

        if detail == esc_code and is_press:
            with self._lock:
                if self._state in ("recording", "stop_pending", "latched"):
                    self.log("Hotkey: Esc pressed → cancel")
                    self._cancel_timer()
                    self._state = "idle"
                    self._ungrab_escape()
                    self.on_record_cancel()
            return

        if detail not in self._hotkey_codes:
            return

        with self._lock:
            if is_press:
                if self._state in ("recording", "stop_pending", "latched"):
                    if self._state == "recording":
                        return  # X 自动重复（按住不放会连发 KeyPress），忽略
                    # stop_pending 里又按回去：当一直按着（早期抖动窗口）
                    if self._state == "stop_pending":
                        self._cancel_timer()
                        self._state = "recording"
                        self._press_time = now
                        return
                    # latched：再按一下 = 停
                    self._state = "idle"
                    self._ungrab_escape()
                    self.on_record_stop()
                    return
                # idle：开始录音
                self._state = "recording"
                self._press_time = now
                self._record_start = now
                self._grab_escape()
                self.on_record_start()
                return

            # KeyRelease
            if self._state != "recording":
                return
            held = now - self._press_time
            tap_toggle = self.config.bool_flag("hotkey_tap_toggle", default=True)
            if tap_toggle and held < HOLD_THRESHOLD:
                # 单击：保持录音（点按开关模式），再按一下才停
                self.log(f"Hotkey: tap ({held:.2f}s) → latch")
                self._state = "latched"
                return
            self._state = "stop_pending"
            early = (now - self._record_start) < EARLY_PRESS_WINDOW
            delay = EARLY_RELEASE_CONFIRM_DELAY if early else RELEASE_CONFIRM_DELAY
            self._cancel_timer()
            self._stop_timer = threading.Timer(delay, self._confirm_stop)
            self._stop_timer.daemon = True
            self._stop_timer.start()

    def _confirm_stop(self):
        with self._lock:
            if self._state != "stop_pending":
                return
            self._state = "idle"
            self._ungrab_escape()
        self.on_record_stop()

    def _cancel_timer(self):
        if self._stop_timer is not None:
            self._stop_timer.cancel()
            self._stop_timer = None

    def force_idle(self):
        """外部（如录音出错）强制回到 idle 态。"""
        with self._lock:
            self._cancel_timer()
            self._state = "idle"
            self._ungrab_escape()
