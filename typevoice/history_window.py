"""Local transcript browser and recording playback, with no cloud requests."""

import os
import shutil
import sqlite3
import subprocess

from .gtkenv import Gdk, GLib, Gtk


CSS = b"""
#typevoice-history { background: #f6f5f2; color: #24242b; }
#typevoice-history .title { font-size: 24px; font-weight: bold; }
#typevoice-history .muted { color: #73737e; }
#typevoice-history .section { font-weight: bold; }
#typevoice-history button { border-radius: 8px; padding: 6px 12px; }
#typevoice-history textview text { background: #ffffff; color: #24242b; }
"""


class HistoryWindow(Gtk.Window):
    PAGE_SIZE = 100

    def __init__(self, config, history):
        super().__init__(title="历史记录 · TypeVoice")
        self.config, self.history = config, history
        self._limit = self.PAGE_SIZE
        self._rows = []
        self._selected = None
        self._player = None
        self._play_timer = None
        self._refresh_timer = None
        self._saved_audio_active = config.bool_flag("history_save_audio", default=False)
        self.set_name("typevoice-history")
        self.set_default_size(920, 620)
        self.set_size_request(700, 480)
        icon = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets/typevoice.svg")
        self.set_icon_from_file(icon)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.connect("delete-event", self._hide)
        self.connect("hide", self._cleanup)
        self.connect("destroy", self._cleanup)
        self.connect("map", self._mapped)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        outer.set_border_width(24)
        self.add(outer)
        header = Gtk.Box(spacing=12)
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        heading.pack_start(self._label("历史记录", "title"), False, False, 0)
        heading.pack_start(self._label("查看识别原文、最终文本和已保存的录音。", "muted"), False, False, 0)
        header.pack_start(heading, True, True, 0)
        refresh = Gtk.Button(label="刷新")
        refresh.set_valign(Gtk.Align.CENTER)
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_end(refresh, False, False, 0)
        outer.pack_start(header, False, False, 0)

        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        panes.set_position(290)
        outer.pack_start(panes, True, True, 0)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_size_request(220, -1)
        panes.pack1(sidebar, resize=False, shrink=False)
        self.store = Gtk.ListStore(str, str)
        self.list = Gtk.TreeView(model=self.store)
        self.list.set_headers_visible(False)
        from gi.repository import Pango
        renderer = Gtk.CellRendererText()
        renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        renderer.set_property("ypad", 12)
        column = Gtk.TreeViewColumn("记录", renderer, text=1)
        column.set_expand(True)
        self.list.append_column(column)
        self.list.get_selection().connect("changed", self._selection_changed)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.list)
        sidebar.pack_start(scroll, True, True, 0)
        self.more = Gtk.Button(label="加载更早记录")
        self.more.connect("clicked", self._load_more)
        sidebar.pack_start(self.more, False, False, 0)

        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        detail.set_margin_start(18)
        panes.pack2(detail, resize=True, shrink=False)
        self.timestamp = self._label("请选择一条记录", "muted")
        detail.pack_start(self.timestamp, False, False, 0)
        self.original, self.copy_original = self._text_panel(detail, "识别原文", "asr")
        self.output, self.copy_output = self._text_panel(detail, "最终文本", "output")
        controls = Gtk.Box(spacing=12)
        self.play = Gtk.Button(label="播放录音")
        self.play.connect("clicked", self._toggle_playback)
        self.audio_status = self._label("未选择记录", "muted")
        self.audio_status.set_line_wrap(True)
        controls.pack_start(self.play, False, False, 0)
        controls.pack_start(self.audio_status, True, True, 0)
        detail.pack_start(controls, False, False, 0)

        self.status = self._label("", "muted")
        self.status.set_line_wrap(True)
        outer.pack_start(self.status, False, False, 0)
        outer.pack_start(Gtk.Separator(), False, False, 0)
        saving = Gtk.Box(spacing=16)
        saving_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        saving_text.pack_start(self._label("保存后续录音", "section"), False, False, 0)
        hint = self._label("开启后，后续录音会以 WAV 文件保存在本机，供回听。文字结果会自动保存。", "muted")
        hint.set_line_wrap(True)
        saving_text.pack_start(hint, False, False, 0)
        saving.pack_start(saving_text, True, True, 0)
        self.save_audio = Gtk.Switch()
        self.save_audio.set_valign(Gtk.Align.CENTER)
        self.save_audio.set_active(self._saved_audio_active)
        self.save_audio.connect("notify::active", self._save_audio_changed)
        saving.pack_end(self.save_audio, False, False, 0)
        outer.pack_start(saving, False, False, 0)
        self._show_record(None)

    @staticmethod
    def _label(text, style=None):
        label = Gtk.Label(label=text, xalign=0)
        if style:
            label.get_style_context().add_class(style)
        return label

    def _text_panel(self, parent, title, field):
        heading = Gtk.Box(spacing=8)
        heading.pack_start(self._label(title, "section"), True, True, 0)
        copy = Gtk.Button(label="复制")
        copy.connect("clicked", lambda *_: self._copy(field))
        heading.pack_end(copy, False, False, 0)
        parent.pack_start(heading, False, False, 0)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.set_top_margin(10)
        view.set_bottom_margin(10)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.add(view)
        parent.pack_start(scroll, True, True, 0)
        return view, copy

    def _mapped(self, *_):
        self.refresh(force=True)
        if self._refresh_timer is None:
            self._refresh_timer = GLib.timeout_add_seconds(3, self._poll_history)

    def _poll_history(self):
        self.refresh()
        return True

    def refresh(self, force=False):
        try:
            self.config.reload()
            self._set_audio_switch(self.config.bool_flag("history_save_audio", default=False))
            rows = self.history.recent(self._limit + 1)
        except (OSError, ValueError, sqlite3.Error):
            self.status.set_text("读取历史失败，请检查数据目录和权限，再点击刷新。")
            return
        self.more.set_sensitive(len(rows) > self._limit)
        rows = rows[:self._limit]
        if not force and rows == self._rows:
            return
        selected_id = self._selected["id"] if self._selected else None
        # Block selection callbacks while rebuilding to keep active playback intact.
        selection = self.list.get_selection()
        selection.handler_block_by_func(self._selection_changed)
        self.store.clear()
        self._rows = rows
        selected_index = 0
        for index, row in enumerate(rows):
            preview = " ".join((row["output"] or row["asr"]).split())[:70]
            self.store.append((row["id"], row["ts"].replace("T", " ") + "\n" + preview))
            if row["id"] == selected_id:
                selected_index = index
        if rows:
            selection.select_path(Gtk.TreePath.new_from_indices([selected_index]))
        selection.handler_unblock_by_func(self._selection_changed)
        self._selection_changed(selection)
        self.status.set_text(f"已显示 {len(rows)} 条记录" if rows else
                             "还没有历史记录。完成一次语音输入后，结果会显示在这里。")

    def _load_more(self, *_):
        self._limit += self.PAGE_SIZE
        self.refresh(force=True)

    def _selection_changed(self, selection):
        model, iterator = selection.get_selected()
        record_id = model[iterator][0] if iterator is not None else None
        row = next((row for row in self._rows if row["id"] == record_id), None)
        if row == self._selected:
            return
        self._stop_playback()
        self._show_record(row)

    def _show_record(self, row):
        self._selected = row
        self.timestamp.set_text(row["ts"].replace("T", " ") if row else "请选择一条记录")
        self.original.get_buffer().set_text(row["asr"] if row else "")
        self.output.get_buffer().set_text(row["output"] if row else "")
        self.copy_original.set_sensitive(bool(row and row["asr"]))
        self.copy_output.set_sensitive(bool(row and row["output"]))
        path = row["audio_path"] if row else None
        available = bool(path and os.path.isfile(path))
        self.play.set_sensitive(available)
        self.audio_status.set_text("可播放已保存的录音" if available else
                                   "录音文件已不存在" if path else
                                   "此条记录未保存录音" if row else "未选择记录")

    def _copy(self, field):
        if self._selected and self._selected[field]:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(self._selected[field], -1)
            clipboard.store()
            self.status.set_text("已复制识别原文" if field == "asr" else "已复制最终文本")

    def _set_audio_switch(self, active):
        # GObject may defer notify::active until the current callback returns.
        # Remember the accepted state so programmatic rollback cannot recurse.
        self._saved_audio_active = active
        self.save_audio.set_active(active)

    def _save_audio_changed(self, switch, *_):
        enabled = switch.get_active()
        if enabled == self._saved_audio_active:
            return
        try:
            self.config.reload()
            self.config.set("history_save_audio", enabled)
        except OSError:
            self._set_audio_switch(self._saved_audio_active)
            self.status.set_text("保存设置失败，请检查配置目录权限。")
            return
        actual = self.config.bool_flag("history_save_audio", default=False)
        self._set_audio_switch(actual)
        if actual != enabled:
            self.status.set_text("录音保存设置受 HISTORY_SAVE_AUDIO 环境变量控制。")
        else:
            self.status.set_text("已开启录音保存，从下一次录音生效。" if enabled else
                                 "已关闭后续录音保存，已有录音仍可播放。")

    def _toggle_playback(self, *_):
        if self._player is not None:
            self._stop_playback()
            self.audio_status.set_text("播放已停止")
            return
        path = self._selected["audio_path"] if self._selected else None
        if not path or not os.path.isfile(path):
            self._show_record(self._selected)
            return
        if shutil.which("paplay") is None:
            self.audio_status.set_text("无法播放：请安装 pulseaudio-utils。")
            return
        try:
            self._player = subprocess.Popen(["paplay", path], stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
        except OSError:
            self.audio_status.set_text("无法播放录音，请检查音频服务。")
            return
        self.play.set_label("停止播放")
        self.audio_status.set_text("正在播放…")
        self._play_timer = GLib.timeout_add(100, self._poll_player)

    def _poll_player(self):
        result = self._player.poll() if self._player else 0
        if result is None:
            return True
        self._player = None
        self._play_timer = None
        self.play.set_label("播放录音")
        self.audio_status.set_text("播放结束" if result == 0 else "播放失败，请检查音频服务或录音文件。")
        return False

    def _stop_playback(self):
        if self._play_timer is not None:
            GLib.source_remove(self._play_timer)
            self._play_timer = None
        player, self._player = self._player, None
        if player is not None:
            if player.poll() is None:
                player.terminate()
                try:
                    player.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.wait()
            else:
                player.wait()
        self.play.set_label("播放录音")

    def _cleanup(self, *_):
        self._stop_playback()
        if self._refresh_timer is not None:
            GLib.source_remove(self._refresh_timer)
            self._refresh_timer = None

    def _hide(self, *_):
        self.hide()
        return True


def run_history():
    import signal
    from .config import Config
    from .history import History
    window = HistoryWindow(Config(), History())
    window.connect("hide", lambda *_: Gtk.main_quit())
    signals = [GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, window._hide)
               for sig in (signal.SIGINT, signal.SIGTERM)]
    window.show_all()
    try:
        Gtk.main()
    finally:
        window.destroy()
        for source in signals:
            GLib.source_remove(source)
    return 0
