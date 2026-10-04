"""文本投递 — TextDelivery 的移植。

流程与 mac 版一致：
1. 快照用户原有剪贴板内容；
2. 把最终文本写进剪贴板；
3. 延迟几十毫秒后用 XTEST 合成 Ctrl+V（终端类 App 用 Ctrl+Shift+V）；
4. 过一会儿读回剪贴板：还是我们的文字 → 还原用户原剪贴板；变了（用户又复制了 /
   粘贴没成功）→ 不还原，保留我们的文字。

X11 下无需任何系统权限。剪贴板操作走 GTK（主线程），XTEST 走 python-xlib。
"""

import threading
import time

# 聊天类 App（按 WM_CLASS 小写匹配）：句尾的「。」去掉更自然（mac 的 chatAppNames）
CHAT_APP_HINTS = [
    "wechat", "微信", "wecom", "telegram", "qq", "messages", "whatsapp",
    "discord", "slack", "feishu", "lark", "dingtalk", "line", "signal",
    "messenger", "teams", "wechatwork",
]

# 终端类 App：粘贴要用 Ctrl+Shift+V
TERMINAL_APP_HINTS = [
    "terminal", "gnome-terminal", "konsole", "xterm", "urxvt", "alacritty",
    "kitty", "tilix", "terminator", "wezterm", "st", "guake", "yakuake",
]

PASTED = "pasted"
COPIED_ONLY = "copied_only"
PASTED_KEPT_CLIPBOARD = "pasted_kept_clipboard"


def adjusted_text_for_delivery(text: str, app_class: str | None) -> str:
    """聊天类 App 里去掉句尾的「。」（后面只剩空白时）。"""
    if not app_class:
        return text
    normalized = app_class.strip().lower()
    if not any(hint in normalized for hint in CHAT_APP_HINTS):
        return text
    stripped = text.rstrip()
    if stripped.endswith("。") and not stripped[:-1].rstrip().endswith("。"):
        tail = text[len(stripped):]
        if not tail.strip("。"):
            return stripped[:-1] + tail
    return text


class TextDelivery:
    def __init__(self, config, log=None):
        self.config = config
        self.log = log or (lambda msg: None)
        self._gtk_ready = False

    # ---- GTK 主线程调度 ----

    def _schedule_on_gtk(self, fn) -> None:
        from .gtkenv import GLib
        GLib.idle_add(fn)

    def _run_on_gtk(self, fn, timeout: float = 3.0):
        """在 GTK 主线程执行 fn 并等它完成，返回其结果。"""
        result = {}
        done = threading.Event()

        def job():
            try:
                result["value"] = fn()
            except Exception as err:  # noqa: BLE001
                result["error"] = err
            finally:
                done.set()

        self._schedule_on_gtk(job)
        if not done.wait(timeout):
            raise TimeoutError("GTK 主线程响应超时")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    # ---- 剪贴板（GTK 主线程内执行） ----

    def _clipboard(self):
        from .gtkenv import Gtk
        return Gtk.Clipboard.get_default(Gdk_display())

    def _snapshot_text(self) -> str | None:
        cb = self._clipboard()
        try:
            return cb.wait_for_text()
        except Exception:
            return None

    def _set_text(self, text: str | None, store: bool = False):
        cb = self._clipboard()
        if text is None:
            cb.clear()
        else:
            cb.set_text(text, -1)
        if store:
            try:
                cb.store()
            except Exception:
                pass

    # ---- XTEST 粘贴 ----

    def _xtest_paste(self) -> bool:
        try:
            from Xlib import X
            from Xlib.ext import xtest
        except ImportError:
            self.log("TextDelivery: python-xlib 不可用，只能复制不能自动粘贴")
            return False
        try:
            from .x11display import open_display
            disp = open_display()
        except Exception as err:  # noqa: BLE001
            self.log(f"TextDelivery: 打不开 X 显示，无法自动粘贴（{err}）")
            return False
        try:
            app_class = _focused_app_class(disp)
            ctrl = disp.keysym_to_keycode(0xFFE3)   # Control_L
            shift = disp.keysym_to_keycode(0xFFE1)  # Shift_L
            v = disp.keysym_to_keycode(ord("v"))
            combo = list(_paste_combo(app_class, ctrl, shift, v))
            for kc in combo:
                xtest.fake_input(disp, X.KeyPress, kc)
            disp.sync()
            time.sleep(0.02)
            for kc in reversed(combo):
                xtest.fake_input(disp, X.KeyRelease, kc)
            disp.sync()
            self.log(f"TextDelivery: simulate paste app={app_class!r} keys={'ctrl+shift+v' if shift in combo else 'ctrl+v'}")
            return True
        finally:
            disp.close()

    # ---- 对外入口 ----

    def deliver(self, text: str) -> str:
        """投递最终文本到当前焦点输入框（可在工作线程调用）。"""
        from .x11display import open_display
        try:
            disp = open_display()
            app_class = _focused_app_class(disp)
            disp.close()
        except Exception:
            app_class = None
        final_text = adjusted_text_for_delivery(text, app_class)

        # 1. 快照用户原有剪贴板，写入我们的文本
        try:
            saved = self._run_on_gtk(self._snapshot_text)
            self._run_on_gtk(lambda: self._set_text(final_text))
        except Exception as err:  # noqa: BLE001
            self.log(f"TextDelivery: clipboard unavailable ({err})")
            raise RuntimeError("无法写入剪贴板，请重试") from err

        # 2. 合成粘贴键
        time.sleep(max(0, int(self.config.get("paste_delay_ms"))) / 1000.0)
        pasted = self._xtest_paste()
        if not pasted:
            # 粘不了：剪贴板里留着我们的文字，提示用户手动 Ctrl+V，不还原
            self.log("TextDelivery: accessibility-like paste unavailable, copied only")
            return COPIED_ONLY

        # 3. 等粘贴被目标 App 读取后还原用户原剪贴板
        if self.config.bool_flag("restore_clipboard", default=True):
            delay = max(100, int(self.config.get("restore_clipboard_delay_ms") or 300)) / 1000.0
            try:
                timer = threading.Timer(delay, self._restore_if_ours, args=(saved, final_text))
                timer.name = "clipboard-restore"
                timer.daemon = True
                timer.start()
            except Exception as err:  # 粘贴已成功，清理失败不能再报投递失败
                self.log(f"TextDelivery: could not schedule clipboard restore ({err})")
                return PASTED_KEPT_CLIPBOARD
        return PASTED

    def _restore_if_ours(self, saved: str | None, ours: str):
        try:
            def check():
                try:
                    current = self._snapshot_text()
                except Exception:
                    return "error"
                if current is None:
                    return "cleared"
                return "ours" if current == ours else "changed"

            outcome = self._run_on_gtk(check, timeout=2.0)
            if outcome != "ours":
                self.log(f"TextDelivery: clipboard changed by user, skip restore ({outcome})")
                return
            self._run_on_gtk(lambda: self._set_text(saved, store=True))
            self.log("TextDelivery: clipboard restored")
        except Exception as err:  # noqa: BLE001
            self.log(f"TextDelivery: restore failed ({err})")


def _paste_combo(app_class: str | None, ctrl: int, shift: int, v: int) -> list:
    if not v:
        return []
    keys = []
    if ctrl:
        keys.append(ctrl)
    if app_class:
        normalized = app_class.strip().lower()
        # st 是终端的完整类名，不能按子串命中 unittest / studio 等普通窗口。
        if shift and any((hint in normalized.split() if hint == "st" else hint in normalized)
                         for hint in TERMINAL_APP_HINTS):
            keys.append(shift)
    keys.append(v)
    return keys


def _focused_app_class(disp) -> str | None:
    """读当前焦点窗口的 WM_CLASS（沿窗口树上溯几层找 client window）。"""
    try:
        window = disp.get_input_focus().focus
        for _ in range(6):
            if window is None:
                return None
            try:
                wm_class = window.get_wm_class()
            except Exception:
                wm_class = None
            if wm_class:
                return f"{wm_class[0]} {wm_class[1]}".strip()
            parent = window.query_tree().parent
            if parent is None or parent.id == disp.screen().root.id:
                return None
            window = parent
    except Exception:
        return None
    return None


def Gdk_display():
    from .gtkenv import Gdk
    return Gdk.Display.get_default()
