"""平台无关的语音处理流水线 — VoicePolishPipeline 的移植。

录音样本 → 识别 → 润色 → 最终文本。UI 通过 on_state 回调收到：
  {"event": "transcribing"|"polishing", "message": str}
  {"event": "done", "text": str}
  {"event": "error", "message": str}
  {"event": "empty"}

沿用 mac 版的关键设计：
- entryID 取消令牌：取消/超时放弃后，晚到的识别、润色结果一律丢弃，不回调、不写历史；
- 临时错误（服务器繁忙/超时）在当前版本原地重试一次，不偷偷换模型；
- 服务端判定无有效语音（静音/太短）→ 当作 empty，安静收起不报错；
- 短文本（≤10 个有效字符）跳过润色直出，聊天场景去句尾句号；
- 润色失败文字照常输出（不丢用户的话），但提醒一次原因；
- 口令（「用英文」等）由程序规则识别，模型只负责翻译。
"""

import threading
import time
import uuid

from . import chunker as audio_chunker
from .language import OutputLanguage, default_language, detect_command, configured_languages


def strip_trailing_punctuation_if_single_sentence(text: str) -> str:
    result = text.strip()
    while result.endswith("。"):
        result = result[:-1].rstrip()
    return result


def meaningful_count(text: str) -> int:
    from .polisher import meaningful_character_count
    return meaningful_character_count(text)


class VoicePolishPipeline:
    def __init__(self, config, transcriber, polisher, on_state=None,
                 on_polish_failed=None, on_output_language_applied=None,
                 audio_saver=None, history_writer=None, log=None):
        self.config = config
        self.transcriber = transcriber
        self.polisher = polisher
        self.on_state = on_state or (lambda state: None)
        self.on_polish_failed = on_polish_failed
        self.on_output_language_applied = on_output_language_applied
        self.audio_saver = audio_saver      # (samples, entry_id) -> 文件名 or None
        self.history_writer = history_writer  # (entry_id, asr, output, duration_ms, audio_file) -> None
        self.log = log or (lambda msg: None)
        self._active_lock = threading.Lock()
        self._active_entry_id = None
        self._last_pipeline_start = 0.0

    # ---- 取消 ----

    def cancel_current(self) -> bool:
        """放弃正在处理的这一次：之后它的结果都不再回调、不写历史。"""
        with self._active_lock:
            had = self._active_entry_id is not None
            self._active_entry_id = None
            return had

    def _begin(self, entry_id):
        with self._active_lock:
            self._active_entry_id = entry_id

    def _is_active(self, entry_id) -> bool:
        with self._active_lock:
            return self._active_entry_id == entry_id

    def _emit(self, state: dict, entry_id):
        if not self._is_active(entry_id):
            self.log(f"Pipeline result dropped (cancelled): {state.get('event')}")
            return
        if state.get("event") in ("done", "error", "empty"):
            self.cancel_current()
        self.on_state(state)

    # ---- 主入口 ----

    def process(self, samples, sample_rate: int = 16000):
        import numpy as np
        samples = np.asarray(samples, dtype=np.float32)
        pipeline_start = time.monotonic()

        if len(samples) == 0:
            self.log("No audio samples!")
            self.on_state({"event": "empty"})
            return

        entry_id = str(uuid.uuid4())  # 该条历史的稳定 ID
        self._begin(entry_id)
        self._last_pipeline_start = pipeline_start
        self.log(f"Pipeline started: samples={len(samples)} "
                 f"({len(samples) / 16000.0:.1f}s)")
        self._emit({"event": "transcribing", "message": "识别中…"}, entry_id)
        self._transcribe(samples, sample_rate, pipeline_start, entry_id)

    # ---- 识别 ----

    def _transcribe(self, samples, sample_rate: int, pipeline_start, entry_id):
        version = self.transcriber.current_version()
        self._transcribe_cloud(samples, sample_rate, version, retried_in_place=False,
                               pipeline_start=pipeline_start, entry_id=entry_id)

    def _transcribe_cloud(self, samples, sample_rate, version, retried_in_place,
                          pipeline_start, entry_id):
        transcribe_start = time.monotonic()
        try:
            raw_text = self.transcriber.transcribe_auto(samples, sample_rate, version)
        except Exception as err:  # noqa: BLE001 —— 统一转成状态回调
            if not self._is_active(entry_id):
                self.log("Cloud ASR failure ignored (cancelled)")
                return
            self.log(f"Cloud ASR failed in {time.monotonic() - transcribe_start:.1f}s "
                     f"(version={version}): {err}")
            self._handle_cloud_failure(err, samples, sample_rate, version, retried_in_place,
                                       pipeline_start, entry_id)
            return
        self.log(f"Cloud ASR result (version={version}): chars={len(raw_text)}, "
                 f"took {time.monotonic() - transcribe_start:.1f}s")
        self._handle_cloud_only_text(raw_text, pipeline_start, samples, entry_id)

    def _handle_cloud_failure(self, err, samples, sample_rate, version, retried_in_place,
                              pipeline_start, entry_id):
        kind = getattr(err, "kind", "unknown")
        # 临时性错误（服务器繁忙/超时）：原地重试同一版本一次（不换版本）
        if getattr(err, "is_retriable_in_place", False) and not retried_in_place:
            self.log("Transient failure, retrying same version once")
            self._transcribe_cloud(samples, sample_rate, version, True,
                                   pipeline_start, entry_id)
            return
        if kind == "no_speech":
            self.log("No speech detected, treating as empty result")
            self._emit({"event": "empty"}, entry_id)
            return
        message = str(err)
        if kind == "missing_credentials":
            message = self.transcriber.missing_configuration_hint()
        # 识别失败的录音静默存进历史（开启 history_save_audio 时），提示里不堆细节
        self._preserve_failed_attempt(samples, entry_id)
        self._emit({"event": "error", "message": message}, entry_id)

    # ---- cloudOnly 后处理 ----

    def _handle_cloud_only_text(self, raw_text, pipeline_start, samples, entry_id):
        # 目标语言：语音口令优先，其次是「默认输出语言」
        command = self._detect_output_language_command(raw_text)
        target = None
        if command is not None:
            target = command.target
        else:
            target = default_language(self.config)
        if target is not None:
            stripped = command.stripped_text if command is not None else raw_text
            self._apply_output_language(target, command, stripped, raw_text,
                                        pipeline_start, samples, entry_id)
            return

        char_count = meaningful_count(raw_text)
        if char_count <= 10:
            output = strip_trailing_punctuation_if_single_sentence(raw_text)
            self.log(f"Cloud-only short text ({char_count} chars), direct output chars={len(output)}")
            self._finish(output, raw_text, pipeline_start, samples, entry_id)
            return

        if not self.polisher.is_polish_enabled():
            output = strip_trailing_punctuation_if_single_sentence(raw_text)
            self.log(f"Polish disabled by user (provider=none), output chars={len(output)}")
            self._finish(output, raw_text, pipeline_start, samples, entry_id)
            return

        self.log(f"Cloud-only long text ({char_count} chars), polishing")
        self._emit({"event": "polishing", "message": "整理中…"}, entry_id)
        polish_start = time.monotonic()
        try:
            polished = self.polisher.polish(raw_text)
        except Exception as err:  # noqa: BLE001
            self.log(f"Cloud ASR polish failed in {time.monotonic() - polish_start:.1f}s: {err}, fallback to raw")
            if self._is_active(entry_id) and getattr(err, "kind", None) != "no_api_key":
                self.on_polish_failed and self.on_polish_failed(str(err))
            self._finish(raw_text, raw_text, pipeline_start, samples, entry_id)
            return
        self.log(f"Cloud ASR polish done in {time.monotonic() - polish_start:.1f}s, chars={len(polished)}")
        self._finish(polished or raw_text, raw_text, pipeline_start, samples, entry_id)

    # ---- 口令输出语言 ----

    def _detect_output_language_command(self, text: str):
        if not self.config.bool_flag("output_language_command_enabled", default=True):
            return None
        return detect_command(text, configured_languages(self.config))

    def _apply_output_language(self, target: OutputLanguage, command, text, raw_asr,
                               pipeline_start, samples, entry_id):
        if command is not None:
            self.log(f"Output language command: {command.matched_phrase} ({command.position}) → {target.id}")
        else:
            self.log(f"Default output language → {target.id}")
        if not self.polisher.is_polish_enabled():
            # 用户关了润色：没有模型可翻译，去掉口令后照常输出
            self.log("Polish disabled, output language ignored")
            self._finish(text, raw_asr, pipeline_start, samples, entry_id)
            return
        self._emit({"event": "polishing", "message": f"→ {target.tag}"}, entry_id)
        polish_start = time.monotonic()
        try:
            polished = self.polisher.polish(text, output_language=target)
        except Exception as err:  # noqa: BLE001
            self.log(f"Output in {target.id} failed in {time.monotonic() - polish_start:.1f}s: {err}, fallback to original")
            if self._is_active(entry_id) and getattr(err, "kind", None) != "no_api_key":
                self.on_polish_failed and self.on_polish_failed(str(err))
            self._finish(text, raw_asr, pipeline_start, samples, entry_id)
            return
        if not polished:
            self._finish(text, raw_asr, pipeline_start, samples, entry_id)
            return
        self.log(f"Output in {target.id} done in {time.monotonic() - polish_start:.1f}s, chars={len(polished)}")
        if self._is_active(entry_id):
            self.on_output_language_applied and self.on_output_language_applied(target, command)
        self._finish(polished, raw_asr, pipeline_start, samples, entry_id)

    # ---- 完成处理 ----

    def _finish(self, final_text, raw_asr, pipeline_start, samples, entry_id):
        if not self._is_active(entry_id):
            self.log("Pipeline result dropped (cancelled): done")
            return
        if not final_text:
            self.log(f"Pipeline took {(time.monotonic() - pipeline_start) * 1000:.0f} ms, no output")
            self._emit({"event": "empty"}, entry_id)
            return

        # 应用术语纠正
        corrected = self.polisher.apply_configured_term_corrections(final_text)
        if corrected != final_text:
            self.log(f"Term corrections applied: before chars={len(final_text)}, after chars={len(corrected)}")

        duration_ms = int((time.monotonic() - pipeline_start) * 1000)
        self.log(f"Pipeline completed in {duration_ms} ms")
        self._emit({"event": "done", "text": corrected}, entry_id)

        # 后台存音频 + 写历史，避免拖慢粘贴
        saved_asr = raw_asr or corrected

        def background():
            audio_file = None
            if self.audio_saver is not None:
                try:
                    audio_file = self.audio_saver(samples, entry_id)
                except Exception as err:  # noqa: BLE001
                    self.log(f"audio saver failed: {err}")
            if self.history_writer is not None:
                try:
                    self.history_writer(entry_id, saved_asr, corrected, duration_ms, audio_file)
                except Exception as err:  # noqa: BLE001
                    self.log(f"history write failed: {err}")

        threading.Thread(target=background, name="pipeline-finish", daemon=True).start()

    # ---- 失败保留 ----

    def _preserve_failed_attempt(self, samples, entry_id) -> bool:
        """识别失败时把录音存进历史。返回是否真的保留了：
        没开 history_save_audio（默认）或保存失败 → False，错误提示里不能谎称已保存。
        """
        if self.audio_saver is None:
            return False
        try:
            audio_file = self.audio_saver(samples, entry_id)
        except Exception as err:  # noqa: BLE001
            self.log(f"preserve failed attempt error: {err}")
            return False
        if not audio_file:
            return False
        duration_ms = int((time.monotonic() - self._last_pipeline_start) * 1000)
        if self.history_writer is not None:
            try:
                self.history_writer(entry_id, "",
                                    "（这段录音识别失败，可播放音频找回当时说的话）",
                                    duration_ms, audio_file)
            except Exception as err:  # noqa: BLE001
                self.log(f"history write failed: {err}")
        self.log(f"Preserved failed recording to history ({audio_file})")
        return True
