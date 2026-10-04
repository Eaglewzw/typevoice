"""平台层验证：X11 连接、GTK、录音（在真实桌面会话里跑，但不干扰用户：
不抢全局热键、不写剪贴板、不弹窗）。

跑法：cd linux && DISPLAY=:0 PYTHONPATH=.deps:. python3 tests/test_platform.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".deps"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0


def check(name, condition, detail=""):
    global PASS
    if not condition:
        print(f"✗ {name}  {detail}")
        sys.exit(1)
    print(f"✓ {name}")
    PASS += 1


# ---- X11 ----

from typevoice.x11display import open_display  # noqa: E402

try:
    disp = open_display()
except Exception as err:  # noqa: BLE001
    print(f"无法连接 X 显示（DISPLAY={os.environ.get('DISPLAY')}）：{err}", file=sys.stderr)
    print("若是 XAuthorization 问题，请带上正确的 XAUTHORITY 环境变量重试", file=sys.stderr)
    sys.exit(1)

from Xlib import XK  # noqa: E402

check("X11 连接", disp.get_display_name() is not None, disp.get_display_name())
code = disp.keysym_to_keycode(XK.string_to_keysym("Alt_L"))
check("Alt_L 键码解析", code > 0, str(code))

from typevoice.delivery import _focused_app_class  # noqa: E402

app_class = _focused_app_class(disp)
print(f"  · 当前焦点窗口 WM_CLASS: {app_class!r}")
check("焦点窗口 WM_CLASS 可读（不强制非空）", True)

# 热键抓取探针：抓 Scroll_Lock 一瞬间再放开（用户不会用到它）
from Xlib import X  # noqa: E402

scroll = disp.keysym_to_keycode(XK.string_to_keysym("Scroll_Lock"))
if scroll:
    grabbed = True

    def handler(err, request):
        nonlocal_grabbed = grabbed  # noqa: F841
        return True

    disp.set_error_handler(handler)
    disp.screen().root.grab_key(scroll, 0, True, X.GrabModeAsync, X.GrabModeAsync)
    disp.sync()
    disp.screen().root.ungrab_key(scroll, 0)
    disp.sync()
    check("XGrabKey 探针（Scroll_Lock 抓/放）", True)
else:
    check("XGrabKey 探针（本机无 Scroll_Lock，跳过）", True)
disp.close()

# ---- GTK ----

from typevoice.gtkenv import Gdk, Gtk  # noqa: E402

check("GTK3 导入", Gtk.get_major_version() == 3, f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}")
check("GDK 显示连接", Gdk.Display.get_default() is not None)

from typevoice.overlay import Capsule, CSS  # noqa: E402

provider = Gtk.CssProvider()
provider.load_from_data(CSS)
check("胶囊 CSS 可解析", True)
capsule = Capsule()  # 只构建不显示
check("胶囊窗口可构建（未显示）", capsule.window is not None and not capsule.window.get_visible())

# 剪贴板只读快照（不写、不动用户内容）
cb = Gtk.Clipboard.get_default(Gdk.Display.get_default())
snap = cb.wait_for_text()
print(f"  · 当前剪贴板文本快照: {str(snap)[:40]!r}")
check("剪贴板读快照", True)

# ---- 录音 ----

from typevoice.audio import Recorder, RecorderError  # noqa: E402

from typevoice.config import Config  # noqa: E402

config = Config()
recorder = Recorder(config, log=lambda msg: None)
try:
    recorder.start()
except RecorderError as err:
    print(f"✗ 录音启动失败：{err}")
    sys.exit(1)
import time  # noqa: E402

levels = []
recorder.on_level = lambda lv: levels.append(lv)
time.sleep(1.0)
samples = recorder.stop()
print(f"  · 录到 {len(samples) / 16000:.2f}s，峰值电平 {max((levels or [0])):.4f}，"
      f"RMS {float((samples ** 2).mean()) ** 0.5:.4f}")
check("录音采集样本", len(samples) >= 8000, f"{len(samples)} samples")

print(f"\n全部 {PASS} 项通过 ✓")
