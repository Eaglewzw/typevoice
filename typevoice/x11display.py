"""X11 显示连接的小工具。"""

import os


def open_display():
    """给工作线程用：新建一条 X 连接（Xlib 的 Display 不是线程安全的，每线程各开各的）。"""
    from Xlib.display import Display
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("未设置 DISPLAY 环境变量（当前不是 X11 会话？）")
    return Display()
