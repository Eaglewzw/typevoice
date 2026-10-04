"""托盘图标 — StatusBarController 的移植。

Ubuntu GNOME 默认带 AppIndicator 支持；gir1.2-ayatanaappindicator3-0.1
未安装时返回 None（App 照常工作，退出靠 Ctrl+C 或 systemctl --user stop）。
"""

import os
import subprocess

from .config import DATA_DIR


def create_tray(app) -> object | None:
    """创建托盘；失败返回 None。提供设置、历史、数据目录和退出入口。"""
    try:
        import gi
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator3
        except (ValueError, ImportError):
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3  # type: ignore
        from .gtkenv import Gtk
    except (ImportError, ValueError):
        return None

    icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "assets", "typevoice-symbolic.svg")
    indicator = AppIndicator3.Indicator.new(
        "typevoice", icon_path if os.path.exists(icon_path) else "audio-input-microphone",
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    menu = Gtk.Menu()

    def add_item(label, callback, sensitive=True):
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if callback is not None:
            item.connect("activate", lambda *_: callback())
        menu.append(item)
        return item

    add_item("TypeVoice · 随时可以说话", None, sensitive=False)
    add_item("设置…", app.show_settings)
    add_item("历史记录…", app.show_history)
    add_item("打开数据目录（历史/音频）", lambda: subprocess.Popen(["xdg-open", DATA_DIR]))
    add_item("重载配置（改完 config.json 后点这里）", app.reload_config)
    add_item("退出", app.quit)

    menu.show_all()
    indicator.set_menu(menu)

    return indicator
