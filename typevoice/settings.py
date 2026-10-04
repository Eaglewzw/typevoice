"""Small desktop control panel; keys stay local and are masked by default."""

import os
from .gtkenv import Gtk, Gdk
from .config import ensure_template

CSS = b"""
#typevoice-settings { background: #f6f5f2; color: #24242b; }
#typevoice-settings .title { font-size: 28px; font-weight: bold; }
#typevoice-settings .muted { color: #73737e; }
#typevoice-settings .card { background: #ffffff; border: 1px solid #e5e4e0; border-radius: 16px; padding: 20px; }
#typevoice-settings entry, #typevoice-settings combobox button { border-radius: 8px; min-height: 30px; }
#typevoice-settings button { border-radius: 8px; padding: 6px 16px; }
#typevoice-settings button.suggested-action { background: #25252d; color: white; border-color: #25252d; }
"""


class SettingsWindow(Gtk.Window):
    def __init__(self, config, on_saved=None, on_quit=None, on_history=None):
        super().__init__(title="TypeVoice")
        self.config, self.on_saved = config, on_saved
        self.on_history = on_history
        self._history_window = None
        self.connect("destroy", self._close_history)
        self.set_name("typevoice-settings")
        self.set_default_size(470, 560)
        self.set_resizable(False)
        icon = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "typevoice.svg")
        self.set_icon_from_file(icon)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.connect("delete-event", self._hide)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_border_width(28)
        self.add(outer)
        header = Gtk.Box(spacing=14)
        from gi.repository import GdkPixbuf
        header.pack_start(Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(
            icon, 52, 52, True)), False, False, 0)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title_box.add(self._label("TypeVoice", "title"))
        title_box.add(self._label("随口说，自然成文。", "muted"))
        header.add(title_box)
        history_button = Gtk.Button(label="历史记录")
        history_button.set_valign(Gtk.Align.CENTER)
        history_button.connect("clicked", self._open_history)
        header.pack_end(history_button, False, False, 0)
        outer.add(header)

        guide = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        guide.get_style_context().add_class("card")
        hotkey = config.get("hotkey_keysym")
        key_label = "Alt" if hotkey == "Alt_L,Alt_R" else hotkey.replace(
            "_L", "（左）").replace("_R", "（右）").replace(",", " / ")
        guide.add(self._label(f"按住 {key_label} 说话，松开后自动输入"))
        guide.add(self._label("轻按开始 / 再按结束     ·     Esc 取消", "muted"))
        outer.add(guide)

        grid = Gtk.Grid(column_spacing=16, row_spacing=12)
        grid.get_style_context().add_class("card")
        outer.add(grid)
        self.provider = Gtk.ComboBoxText()
        for key, name in (("bailian", "阿里百炼"), ("turbo", "火山引擎 · 极速"),
                          ("standard", "火山引擎 · 标准"), ("v2", "火山引擎 · V2")):
            self.provider.append(key, name)
        self.provider.set_active_id(config.get("asr_version"))
        self.dashscope = self._secret(config.get("dashscope_api_key"))
        self.volc = self._secret(config.get("bigasr_api_key"))
        self.polish = Gtk.ComboBoxText()
        for key, name in (("qwen", "通义千问"), ("zhipu", "智谱（使用已有配置）"),
                          ("doubao", "豆包（使用已有配置）"), ("none", "关闭，直接输出原文")):
            self.polish.append(key, name)
        self.polish.set_active_id(config.get("polish_provider"))
        self.margin = Gtk.SpinButton.new_with_range(16, 500, 4)
        self.margin.set_value(config.get("overlay_bottom_margin"))
        for row, (title, widget) in enumerate((("语音识别", self.provider),
                ("百炼 API Key", self.dashscope), ("火山 API Key", self.volc),
                ("文字润色", self.polish), ("底部留白", self.margin))):
            grid.attach(self._label(title), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
        self.status = self._label("请向服务商申请并填写自己的 API Key，软件不附带密钥。"
                                  "密钥保存在本机，语音发送至所选服务商，费用由服务商收取。", "muted")
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(48)
        outer.add(self.status)
        actions = Gtk.Box(spacing=8)
        advanced = Gtk.Button(label="高级设置")
        advanced.connect("clicked", self._open_config)
        actions.pack_start(advanced, False, False, 0)
        if on_quit:
            quit_button = Gtk.Button(label="退出TypeVoice")
            quit_button.connect("clicked", lambda *_: on_quit())
            actions.pack_start(quit_button, False, False, 0)
        save = Gtk.Button(label="保存设置")
        save.get_style_context().add_class("suggested-action")
        save.connect("clicked", self._save)
        actions.pack_end(save, False, False, 0)
        outer.add(actions)

    @staticmethod
    def _label(text, style=None):
        label = Gtk.Label(label=text, xalign=0)
        if style:
            label.get_style_context().add_class(style)
        return label

    @staticmethod
    def _secret(value):
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        entry.set_text(value or "")
        entry.set_placeholder_text("填入你自行申请的 API Key")
        return entry

    def _save(self, *_):
        try:
            self.config.update({"asr_version": self.provider.get_active_id(),
                                "dashscope_api_key": self.dashscope.get_text().strip(),
                                "bigasr_api_key": self.volc.get_text().strip(),
                                "polish_provider": self.polish.get_active_id(),
                                "overlay_bottom_margin": self.margin.get_value_as_int()})
            if self.on_saved:
                self.on_saved()
        except OSError:
            self.status.set_text("保存失败，请检查配置目录是否可写。")
            return
        self.status.set_text("已保存，下次录音生效。关闭此窗口后仍可使用快捷键。")

    def _open_config(self, *_):
        import subprocess
        ensure_template(self.config.path)
        subprocess.Popen(["xdg-open", self.config.path])

    def _open_history(self, *_):
        if self.on_history:
            self.on_history()
            return
        from .history import History
        from .history_window import HistoryWindow
        if self._history_window is None:
            self._history_window = HistoryWindow(self.config, History())
        self._history_window.show_all()
        self._history_window.present()

    def _close_history(self, *_):
        if self._history_window is not None:
            self._history_window.destroy()
            self._history_window = None

    def _hide(self, *_):
        self.hide()
        return True


def run_settings():
    from .config import Config
    window = SettingsWindow(Config())
    window.connect("hide", lambda *_: Gtk.main_quit())
    window.show_all()
    Gtk.main()
    window.destroy()
    return 0
