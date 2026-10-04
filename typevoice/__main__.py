"""命令行入口。

  python -m typevoice run              运行完整 App（默认；X11 会话）
  python -m typevoice record-once      录一段并走完整流水线，打印结果（测试用）
  python -m typevoice test-asr         只测识别
  python -m typevoice test-polish TXT  只测润色
  python -m typevoice keys             查看已配置的凭证（脱敏）
  python -m typevoice status           配置与数据文件位置
  python -m typevoice history          查看最近的历史记录（识别/润色/失败录音）
  python -m typevoice autostart on|off 开机自启
"""

import argparse
import os
import sys
import threading

from .config import CONFIG_PATH, CONFIG_DIR, DATA_DIR, AUDIO_DIR, LOG_PATH, Config


def _build_components(config: Config):
    from .asr import CloudASRTranscriber
    from .polisher import AIPolisher
    from .pipeline import VoicePolishPipeline
    asr = CloudASRTranscriber(config, log=lambda msg: print(f"  · {msg}"))
    polisher = AIPolisher(config, log=lambda msg: print(f"  · {msg}"))
    return asr, polisher, VoicePolishPipeline(config, asr, polisher)


def cmd_record_once(args) -> int:
    """录 N 秒（或回车结束）→ 识别 → 润色 → 打印；--paste 同时投递。"""
    from .audio import Recorder, RecorderError
    from .config import Config as C
    config = C()
    asr, polisher, pipeline = _build_components(config)
    result = {}

    def on_state(state):
        event = state.get("event")
        if event == "transcribing":
            print("识别中…")
        elif event == "polishing":
            print("整理中…")
        elif event == "empty":
            print("(没有听清内容)")
        elif event == "error":
            print(f"错误：{state.get('message')}")
        elif event == "done":
            result["text"] = state["text"]
            print(f"结果：{state['text']}")

    pipeline.on_state = on_state

    print(f"请说话（按 Enter 提前结束，最长 {args.seconds} 秒）…")
    recorder = Recorder(config, log=lambda msg: None)
    try:
        recorder.start()
    except RecorderError as err:
        print(f"录音失败：{err}", file=sys.stderr)
        return 1
    interrupter = threading.Timer(args.seconds, lambda: None)
    interrupter.start()
    try:
        input()  # 回车提前结束
    except EOFError:
        pass
    interrupter.cancel()
    samples = recorder.stop()
    if len(samples) == 0:
        print("没有录到音频", file=sys.stderr)
        return 1
    print(f"录到 {len(samples) / 16000:.1f}s，开始处理…")
    pipeline.process(samples)

    text = result.get("text", "")
    if text and args.paste:
        from .delivery import TextDelivery
        delivery = TextDelivery(config)
        outcome = delivery.deliver(text)
        print(f"投递：{outcome}")
    return 0 if text else 1


def cmd_test_asr(args) -> int:
    from .audio import Recorder, RecorderError
    config = Config()
    asr, _, _ = _build_components(config)
    if not asr.is_configured():
        print(f"未配置识别凭证：{asr.missing_configuration_hint()}", file=sys.stderr)
        print("编辑 " + CONFIG_PATH + " 填入 bigasr_api_key 或 dashscope_api_key", file=sys.stderr)
        return 1
    print(f"请说话（{args.seconds} 秒）…")
    recorder = Recorder(config, log=lambda msg: None)
    try:
        recorder.start()
    except RecorderError as err:
        print(f"录音失败：{err}", file=sys.stderr)
        return 1
    import time
    time.sleep(args.seconds)
    samples = recorder.stop()
    print(f"录到 {len(samples) / 16000:.1f}s，识别中…")
    try:
        text = asr.transcribe_auto(samples)
    except Exception as err:  # noqa: BLE001
        print(f"识别失败：{err}", file=sys.stderr)
        return 1
    print(f"识别结果：{text}")
    return 0


def cmd_test_polish(args) -> int:
    config = Config()
    _, polisher, _ = _build_components(config)
    if not polisher.is_polish_enabled():
        print("polish_provider 为 none", file=sys.stderr)
        return 1
    try:
        result = polisher.polish(args.text)
    except Exception as err:  # noqa: BLE001
        print(f"润色失败：{err}", file=sys.stderr)
        return 1
    print(result)
    return 0


def cmd_keys(_args) -> int:
    config = Config()
    for key in ("bigasr_api_key", "bigasr_app_id", "bigasr_access_token",
                "dashscope_api_key", "zhipu_api_key", "ark_api_key"):
        value = config.string(key) or os.environ.get(key.upper())
        if value:
            print(f"{key}: {value[:4]}****{value[-4:]}（{len(value)} 字符）")
        else:
            print(f"{key}: 未配置")
    return 0


def cmd_status(_args) -> int:
    print(f"配置文件: {CONFIG_PATH}（{'存在' if os.path.exists(CONFIG_PATH) else '不存在'}）")
    print(f"数据目录: {DATA_DIR}（历史库 / 密钥 / 音频）")
    print(f"日志文件: {LOG_PATH}")
    print(f"音频目录: {AUDIO_DIR}")
    return 0


def cmd_history(args) -> int:
    if args.window:
        from .history_window import run_history
        return run_history()
    from .history import History
    rows = History().recent(limit=args.limit)
    if not rows:
        print("还没有历史记录")
        return 0
    for row in rows:
        asr = row["asr"].replace("\n", " ")
        output = row["output"].replace("\n", " ")
        line = f"{row['ts']}  输出: {output[:60]}"
        if asr and asr != output:
            line += f"  (识别原文: {asr[:40]})"
        print(line)
        if row.get("audio_path"):
            print(f"    └ 音频: {row['audio_path']}")
    return 0


def cmd_autostart(args) -> int:
    from .migration import legacy_autostart_entries
    autostart_dir = os.path.join(os.path.dirname(CONFIG_DIR), "autostart")
    legacy_entries = list(legacy_autostart_entries(os.path.dirname(CONFIG_DIR)))
    desktop_path = os.path.join(autostart_dir, "typevoice.desktop")
    if args.action == "off":
        for legacy in legacy_entries:
            legacy.unlink()
        if os.path.exists(desktop_path):
            os.remove(desktop_path)
            print("已关闭开机自启")
        else:
            print("本来就未开启")
        return 0
    os.makedirs(autostart_dir, exist_ok=True)
    package_dir = os.path.dirname(os.path.abspath(__file__))
    launcher = os.path.join(os.path.dirname(package_dir), "run.sh")
    if not os.path.exists(launcher):
        print(f"找不到启动脚本 {launcher}", file=sys.stderr)
        return 1
    with open(desktop_path, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\n"
                "Type=Application\n"
                "Name=TypeVoice\n"
                "Name[zh_CN]=TypeVoice\n"
                "Icon=typevoice\n"
                "Comment=按住快捷键说话，松手后整理好的文字直接输入到光标处\n"
                f"Exec={_desktop_quote(launcher)} run --background\n"
                "X-GNOME-Autostart-enabled=true\n")
    for legacy in legacy_entries:
        legacy.unlink()
    print(f"已开启开机自启：{desktop_path}")
    return 0


def _desktop_quote(value):
    # Desktop Entry Exec quoting is different from shell quoting.
    escaped = value.replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return '"' + escaped.replace("\\", "\\\\") + '"'


def cmd_run(args) -> int:
    from .migration import legacy_autostart_entries
    if list(legacy_autostart_entries(os.path.dirname(CONFIG_DIR))):
        from types import SimpleNamespace
        if not os.path.exists(os.path.join(os.path.dirname(CONFIG_DIR), "autostart/typevoice.desktop")):
            cmd_autostart(SimpleNamespace(action="on"))
        else:
            for legacy in legacy_autostart_entries(os.path.dirname(CONFIG_DIR)):
                legacy.unlink()
    from .app import run_app
    return run_app(background=args.background)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="typevoice", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    from . import __version__
    parser.add_argument("--version", action="version", version=f"TypeVoice {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="运行完整 App（默认）")
    run.add_argument("--background", action="store_true", help="隐藏设置窗口，在后台运行")
    parser.set_defaults(background=False)

    p = sub.add_parser("record-once", help="录一段并走完整流水线，打印结果")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--paste", action="store_true", help="结果同时投递到光标处")

    p = sub.add_parser("test-asr", help="只测云端识别")
    p.add_argument("--seconds", type=float, default=3.0)

    p = sub.add_parser("test-polish", help="只测润色")
    p.add_argument("text")

    sub.add_parser("keys", help="查看已配置的凭证（脱敏）")
    sub.add_parser("status", help="配置与数据文件位置")
    p = sub.add_parser("history", help="查看最近的历史记录")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--window", action="store_true", help="打开历史记录窗口")
    p = sub.add_parser("autostart", help="开机自启 on|off")
    p.add_argument("action", choices=["on", "off"])

    args = parser.parse_args(argv)
    from .migration import migrate_legacy_data
    migrate_legacy_data()
    command = args.command or "run"
    return {
        "run": cmd_run,
        "record-once": cmd_record_once,
        "test-asr": cmd_test_asr,
        "test-polish": cmd_test_polish,
        "keys": cmd_keys,
        "status": cmd_status,
        "history": cmd_history,
        "autostart": cmd_autostart,
    }[command](args)


if __name__ == "__main__":
    sys.exit(main())
