"""Render the actual GTK widgets into a shareable preview on a virtual display."""

import argparse
from pathlib import Path
import tempfile
import time
import cairo
import gi
gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo
from typevoice.config import Config
from typevoice.gtkenv import GLib
from typevoice.overlay import Capsule
from typevoice.settings import SettingsWindow


def pump():
    end = time.monotonic() + .15
    context = GLib.MainContext.default()
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(.002)


def render(output):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1320, 880)
    cr = cairo.Context(surface)
    cr.set_source_rgb(.91, .91, .90)
    cr.paint()

    def text(x, y, message, size=15, color=(.40, .40, .45)):
        cr.set_source_rgb(*color)
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription(f'Sans {size}'))
        layout.set_text(message, -1)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    text(52, 34, 'TypeVoice', 28, (.12, .12, .16))
    text(53, 89, 'Linux 语音输入 · 界面实机渲染', 13)
    with tempfile.TemporaryDirectory(prefix='typevoice-preview-') as temporary:
        config = Config(str(Path(temporary) / 'config.json'))
        settings = SettingsWindow(config)
        settings.show_all()
        pump()
        cr.save()
        cr.translate(52, 152)
        settings.draw(cr)
        cr.restore()
        capsule = Capsule(config)
        states = [('录音 · 随声音起伏', lambda: capsule.show_recording(12, .8)),
                  ('处理 · 轻量等待动画', capsule.show_processing),
                  ('提示 · 原文仍然保留', lambda: capsule.show_message('已输出原文（本次未润色）')),
                  ('失败 · 明确反馈', lambda: capsule.show_message('识别暂时不可用，请重试', error=True))]
        for i, (label, show) in enumerate(states):
            y = 182 + i * 146
            text(755, y, label, 13)
            show()
            pump()
            cr.save()
            cr.translate(750, y + 38)
            cr.push_group()
            capsule.window.draw(cr)
            cr.pop_group_to_source()
            cr.paint()
            cr.restore()
        capsule.window.destroy()
        settings.destroy()
    output.parent.mkdir(parents=True, exist_ok=True)
    surface.write_to_png(str(output))
    print(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist/ui-preview.png'))
    render(parser.parse_args().output)
