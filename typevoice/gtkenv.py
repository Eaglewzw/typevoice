"""统一的 GTK3 导入入口。

机器上同时装有 GTK3/GTK4 的 typelib 时，`from gi.repository import Gdk`
不固定版本会加载到 Gdk 4.0，与 Gtk 3.0 冲突（RepositoryError）。所有模块
必须从这里导入 Gdk/Gtk/GLib，禁止直接 `import gi` 后导入。
"""

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402,F401  (re-export)
