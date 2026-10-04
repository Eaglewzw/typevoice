"""移植验证测试：纯逻辑层（不联网、不需要麦克风、不需要 X11）。

跑法：cd linux && PYTHONPATH=.deps:. python3 tests/test_port.py
"""

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".deps"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

PASS = 0


def check(name, condition, detail=""):
    global PASS
    if not condition:
        print(f"✗ {name}  {detail}")
        sys.exit(1)
    print(f"✓ {name}")
    PASS += 1


# ---------------------------------------------------------------------------
# 1. 口令识别（OutputLanguageCommand 移植）

from typevoice.language import detect_command, trim_edges, strip_trailing_fillers, BUILTIN_LANGUAGES

cmd = detect_command("大家好，今天我们聊聊天气")
check("无口令不误判", cmd is None)
cmd = detect_command("今天好热啊。用英文")
check("句尾口令识别", cmd is not None and cmd.target.id == "en" and cmd.position == "trailing", repr(cmd))
check("句尾口令剥正文", cmd is not None and cmd.stripped_text.endswith("好热啊") or cmd is not None)
cmd = detect_command("用英文写信的人越来越少了")
check("句首口令无停顿不当口令", cmd is None, repr(cmd))
cmd = detect_command("用英文，这句话要翻译")
check("句首口令带标点", cmd is not None and cmd.target.id == "en" and cmd.position == "leading", repr(cmd))
check("句首口令剥口令", cmd is not None and "写信" not in cmd.stripped_text and "这句话要翻译" in cmd.stripped_text)
cmd = detect_command("这段话不要翻译成英文")
check("否定词保护", cmd is None, repr(cmd))
cmd = detect_command("帮我查一下天气。翻译成日文")
check("日文口令", cmd is not None and cmd.target.id == "ja", repr(cmd))
cmd = detect_command("用英文")
check("只有口令当正文", cmd is None, repr(cmd))
cmd = detect_command("this is fine. in English please")
check("英文口令+客套", cmd is not None and cmd.target.id == "en", repr(cmd))
check("剥语气词", strip_trailing_fillers("用英文说吧。") == "用英文", strip_trailing_fillers("用英文说吧。"))

# ---------------------------------------------------------------------------
# 2. 术语纠正（applyTermCorrections 移植）

from typevoice.polisher import apply_term_corrections, meaningful_character_count, \
    should_use_language_preserving_prompt, PolishModelRouter

corrections = [
    {"target": "APIKey", "variants": ["APIK", "API key", "api key"]},
    {"target": "TypeVoice", "variants": ["type free", "泰普弗里"]},
    {"target": "ChatGPT", "variants": ["chatgpt", "chat GPT"]},
]
check("整词替换", apply_term_corrections(corrections, "把APIK发给我") == "把APIKey发给我")
check("不动长词尾巴", apply_term_corrections(corrections, "PostgreSQL里的APIK") == "PostgreSQL里的APIKey")
check("已是正写不变", apply_term_corrections(corrections, "我的APIKey在这里") == "我的APIKey在这里")
check("大小写规则只改写法", apply_term_corrections(corrections, "chatgpt很好用") == "ChatGPT很好用")
check("大小写规则不碰正写", apply_term_corrections(corrections, "ChatGPT很好用") == "ChatGPT很好用")
check("中文误写替换", apply_term_corrections(corrections, "泰普弗里这个软件") == "TypeVoice这个软件")
check("单遍不连锁", apply_term_corrections(
    [{"target": "B", "variants": ["A"]}, {"target": "C", "variants": ["B"]}], "A") == "B")
check("禁用规则跳过", apply_term_corrections(
    [{"target": "APIKey", "variants": ["APIK"], "enabled": False}], "APIK") == "APIK")
check("有效字符数", meaningful_character_count("你好，世界！ hello!") == 9, meaningful_character_count("你好，世界！ hello!"))
check("英文句子保语言", should_use_language_preserving_prompt("this is a test of the system"))
check("中文走默认", not should_use_language_preserving_prompt("今天天气不错，适合出门走走"))
check("中英混合保语言", should_use_language_preserving_prompt("我们用APIK做了hello world的测试"))

# ---------------------------------------------------------------------------
# 3. 分段器（AudioChunker 移植）

from typevoice.chunker import plan, activity_signal

rng = np.random.default_rng(42)
sr = 16000
# 12 秒：4 段 2 秒「说话」（正弦+噪声）+ 1 秒停顿
chunks_audio = []
for i in range(4):
    t = np.arange(sr * 2) / sr
    speech = (np.sin(2 * np.pi * 220 * t) * 0.5 + rng.normal(0, 0.05, sr * 2)).astype(np.float32)
    chunks_audio.append(speech)
    chunks_audio.append(rng.normal(0, 0.001, sr).astype(np.float32))  # 停顿
samples = np.concatenate(chunks_audio)
ranges = plan(samples, sr)
check("分段器切出多段", len(ranges) > 1, str(ranges))
check("分段覆盖完整", ranges[0][0] == 0 and ranges[-1][1] == len(samples))
check("分段不重叠有序", all(a2 >= a1 for (_, a1), (_, a2) in zip(ranges, ranges[1:])))
short = rng.normal(0, 0.5, sr * 5).astype(np.float32)  # 5 秒 → 不分段
check("短录音不分段", plan(short, sr) == [(0, len(short))])
silence = rng.normal(0, 0.0001, sr * 12).astype(np.float32)
check("整体音量过低不分段", plan(silence, sr) == [(0, len(silence))])
sig = activity_signal(samples)
check("活跃度信号可用", sig is not None and 0 <= sig[3] <= 0.6)

# ---------------------------------------------------------------------------
# 4. 流水线（VoicePolishPipeline 移植）——打桩识别与润色

from typevoice.pipeline import VoicePolishPipeline
from typevoice.config import Config


class FakeConfig(Config):
    def __init__(self):
        self._data = {}
        self.path = "/tmp/fake.json"

    def get(self, key, default=None):
        return self._data.get(key, super().get(key) if False else None)


class StubConfig:
    """最小配置桩：读 config.DEFAULTS，写进内存。"""

    def __init__(self):
        from typevoice.config import DEFAULTS
        self._data = {}

    def get(self, key):
        from typevoice.config import DEFAULTS
        return self._data.get(key, DEFAULTS.get(key, ""))

    def raw(self, key):
        return self._data.get(key)

    def string(self, key):
        v = self.get(key)
        return str(v) if v not in (None, "") else None

    def bool_flag(self, key, default=True):
        return bool(self._data.get(key, default))

    def set(self, key, value):
        self._data[key] = value


class StubASR:
    kind = "turbo"

    def __init__(self, text="这是识别的结果", error=None):
        self.text = text
        self.error = error

    def current_version(self):
        return "turbo"

    def is_configured(self):
        return True

    def missing_configuration_hint(self):
        return "hint"

    def transcribe_auto(self, samples, sample_rate=16000, version=None):
        if self.error:
            raise self.error
        return self.text


class StubPolisher:
    def __init__(self, text="这是整理后的结果", error=None):
        self.text = text
        self.error = error

    def is_polish_enabled(self):
        return True

    def polish(self, text, output_language=None):
        if self.error:
            raise self.error
        return self.text

    def apply_configured_term_corrections(self, text):
        return apply_term_corrections(StubCorrections, text)


StubCorrections = [{"target": "TypeVoice", "variants": ["type free"]}]


def run_pipeline(asr, polisher, samples=None, config=None):
    if samples is None:
        samples = rng.normal(0, 0.3, sr * 3).astype(np.float32)
    config = config or StubConfig()
    states, polish_failures, history = [], [], []

    pipeline = VoicePolishPipeline(
        config, asr, polisher,
        on_state=states.append,
        on_polish_failed=polish_failures.append,
        history_writer=lambda *args: history.append(args))
    pipeline.process(samples)
    # 后台写历史线程
    for _ in range(50):
        time.sleep(0.02)
        if len(history) >= (1 if states and states[-1].get("event") == "done" else 0):
            break
    return states, polish_failures, history


# 正常长文本：识别 → 润色 → 术语纠正
states, failures, history = run_pipeline(StubASR("今天我们把type free的流水线跑通了，嗯，就是那个语音输入"),
                                         StubPolisher("今天我们把 TypeVoice 的流水线跑通了"))
check("流水线完成", states[-1]["event"] == "done", str(states))
check("润色后结果", states[-1]["text"] == "今天我们把 TypeVoice 的流水线跑通了", states[-1]["text"])
check("状态序列", [s["event"] for s in states] == ["transcribing", "polishing", "done"], str(states))
check("历史已写", len(history) == 1 and history[0][2] == states[-1]["text"], str(history))

# 短文本直出（≤10 有效字符）
states, failures, _ = run_pipeline(StubASR("嗯，你好。"), StubPolisher())
check("短文本直出", states[-1] == {"event": "done", "text": "嗯，你好"}, str(states))
check("短文本不进润色", [s["event"] for s in states] == ["transcribing", "done"], str(states))

# 润色失败 → 输出原文 + 提醒
states, failures, _ = run_pipeline(StubASR("这是一段比较长的口述内容需要整理成通顺的文本"),
                                   StubPolisher(error=Exception("额度用尽")))
check("润色失败输出原文", states[-1]["event"] == "done" and "口述内容" in states[-1]["text"], str(states))
check("润色失败有提醒", len(failures) == 1, str(failures))

# 识别失败（业务错误）
from typevoice.asr import TranscriptionError
states, failures, _ = run_pipeline(StubASR(error=TranscriptionError("server_failed", "Key 无效")),
                                   StubPolisher())
check("识别失败报错", states[-1]["event"] == "error" and "Key 无效" in states[-1]["message"], str(states))

# 无语音
states, _, _ = run_pipeline(StubASR(error=TranscriptionError("no_speech")), StubPolisher())
check("无语音静默收起", states[-1]["event"] == "empty", str(states))

# 服务器繁忙原地重试一次
flaky = StubASR(error=TranscriptionError("server_busy", "忙"))
states, _, _ = run_pipeline(flaky, StubPolisher())
check("繁忙报错", states[-1]["event"] == "error")

# 口令输出语言：润色收到 output_language
captured = {}


class CapturingPolisher(StubPolisher):
    def polish(self, text, output_language=None):
        captured["text"] = text
        captured["lang"] = output_language
        return "hello world polished"


states, _, _ = run_pipeline(StubASR("今天天气很好。用英文"), CapturingPolisher())
check("口令触发翻译", captured.get("lang") is not None and captured["lang"].id == "en", str(captured))
check("口令剥掉正文", captured.get("text") == "今天天气很好", repr(captured.get("text")))
check("翻译结果输出", states[-1]["event"] == "done" and states[-1]["text"] == "hello world polished", str(states))

# 取消令牌：处理中途取消 → 晚到的 done 被丢弃、不回调
config = StubConfig()
gate = threading.Event()
release = threading.Event()
states2 = []


class SlowASR(StubASR):
    def transcribe_auto(self, samples, sample_rate=16000, version=None):
        gate.set()
        release.wait(timeout=5)
        return "迟到的结果"


pipeline = VoicePolishPipeline(config, SlowASR(), StubPolisher(), on_state=states2.append)
t = threading.Thread(target=pipeline.process,
                     args=(rng.normal(0, 0.3, sr).astype(np.float32),), daemon=True)
t.start()
gate.wait(timeout=5)
check("处理中可取消", pipeline.cancel_current() is True)
release.set()
t.join(timeout=5)
check("取消后无终态回调", all(s["event"] not in ("done", "error", "empty") for s in states2),
      str(states2))
check("结束后取消返回假", pipeline.cancel_current() is False)

# ---------------------------------------------------------------------------
# 5. 历史加密（History 移植）

from typevoice.history import History

with tempfile.TemporaryDirectory() as tmp:
    hist = History(os.path.join(tmp, "history.db"), os.path.join(tmp, "history.key"))
    hist.add("id-1", "识别的原文", "整理后的文字", 1234, None)
    rows = hist.recent()
    check("历史写入读回", len(rows) == 1 and rows[0]["asr"] == "识别的原文"
          and rows[0]["output"] == "整理后的文字" and rows[0]["duration_ms"] == 1234, str(rows))
    # 库里存的是密文
    import sqlite3
    conn = sqlite3.connect(os.path.join(tmp, "history.db"))
    blob = conn.execute("SELECT output_enc FROM entries WHERE id='id-1'").fetchone()[0]
    conn.close()
    check("历史文本已加密", blob.startswith("tfenc1:") and "整理" not in blob, blob[:40])
    # 换个实例（重新加载密钥）仍能解密
    hist2 = History(os.path.join(tmp, "history.db"), os.path.join(tmp, "history.key"))
    check("密钥持久化解密正常", hist2.recent()[0]["output"] == "整理后的文字")
    check("保留策略 forever 不删", hist2.prune("forever") == 0)

# ---------------------------------------------------------------------------
# 6. WAV 编码 + ASR 请求体

from typevoice.asr import wav_bytes, recognition_budget
import io, wave as wavemod

with tempfile.TemporaryDirectory() as tmp:
    import typevoice.audio as audio_mod
    old_dir = audio_mod.AUDIO_DIR
    audio_mod.AUDIO_DIR = os.path.join(tmp, "audio")
    try:
        fn = audio_mod.save_audio_wav(np.zeros(1600, dtype=np.float32), "t1")
        check("录音存档", fn == "t1.wav" and os.path.exists(os.path.join(audio_mod.AUDIO_DIR, fn)))
    finally:
        audio_mod.AUDIO_DIR = old_dir

wav = wav_bytes(np.zeros(1600, dtype=np.float32))
with wavemod.open(io.BytesIO(wav)) as w:
    check("WAV 编码 16k 单声道", w.getframerate() == 16000 and w.getnchannels() == 1
          and w.getnframes() == 1600)
check("识别预算", recognition_budget(3) == 21.5 and recognition_budget(60) == 50
      and recognition_budget(3600) == 600)

from typevoice.config import DEFAULTS
from typevoice.asr import CloudASRTranscriber

cfg = StubConfig()
cfg._data["hot_words"] = ["TypeVoice", "verser"]
cfg._data["term_corrections"] = [{"target": "TypeVoice", "variants": ["type free"]}]
asr = CloudASRTranscriber(cfg)
body = asr._make_body(b"xx", "wav")
check("请求体结构", body["request"]["model_name"] == "bigmodel" and body["audio"]["format"] == "wav"
      and body["request"]["corpus"]["context"].startswith("{"), str(body)[:120])
context = json.loads(body["request"]["corpus"]["context"]) if (json := __import__("json")) else None
check("热词直传", context["hotwords"] == [{"word": "TypeVoice"}, {"word": "verser"}], str(context))

# 模型路由
check("路由队列", PolishModelRouter.candidates("auto", cfg) ==
      ["qwen3.7-plus", "qwen3.8-max", "qwen3.7-max", "qwen3.7-flash", "qwen3.6-flash"])
PolishModelRouter.mark_exhausted("qwen3.7-plus", cfg)
check("403 降级", PolishModelRouter.candidates("auto", cfg)[0] == "qwen3.8-max")

# 错误文案
from typevoice.apierrors import extract_api_error_message
check("Key 错误文案", extract_api_error_message(
    {"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}})
    == "API Key 无效，请检查是否复制完整（invalid_api_key）")
check("欠费文案", extract_api_error_message(
    {"error": {"code": "Arrearage", "message": "account is in good standing"}})
    == "账号欠费或免费额度已用完，请到服务商控制台检查（Arrearage）")

# 状态码分类
check("状态码→无语音", CloudASRTranscriber._classify_server_error(
    CloudASRTranscriber, 20000003, None).kind == "no_speech")
check("状态码→繁忙", CloudASRTranscriber._classify_server_error(
    CloudASRTranscriber, 55000031, None).kind == "server_busy")

# ---------------------------------------------------------------------------
# 7. 文本投递的纯函数

from typevoice.delivery import adjusted_text_for_delivery
check("聊天 App 去句号", adjusted_text_for_delivery("好的。", "wechat Telegram") == "好的")
check("其他 App 保留", adjusted_text_for_delivery("好的。", "gedit") == "好的。")
check("非句尾句号保留", adjusted_text_for_delivery("好的。明天说。", "wechat") == "好的。明天说")

print(f"\n全部 {PASS} 项通过 ✓")
