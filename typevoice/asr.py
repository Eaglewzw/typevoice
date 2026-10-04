"""云端语音识别 — CloudASRTranscriber 的移植。

支持四个识别版本：
- turbo     火山极速版（同步 flash）
- standard  火山标准版（异步 submit + 轮询 query）
- v2        火山 2.0 seedasr（异步 submit + 轮询 query）
- bailian   阿里百炼 qwen3-asr-flash（OpenAI 兼容接口，同步一步出结果）

凭证与 mac 版一致：config.json / 环境变量里的 bigasr_api_key（新版单 Key，优先），
或旧版 bigasr_app_id + bigasr_access_token；百炼用 dashscope_api_key。
"""

import base64
import io
import json
import time
import urllib.error
import urllib.request
import uuid
import wave

from .apierrors import extract_api_error_message
from .chunker import plan as chunk_plan

FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
BAILIAN_MODEL = "qwen3-asr-flash"

RESOURCE_IDS = {"turbo": "volc.bigasr.auc_turbo", "standard": "volc.bigasr.auc", "v2": "volc.seedasr.auc"}
SYNC_VERSIONS = {"turbo", "bailian"}
VALID_VERSIONS = {"turbo", "standard", "v2", "bailian"}

SUBMIT_STALL_TIMEOUT = 20.0
QUERY_STALL_TIMEOUT = 10.0


class TranscriptionError(Exception):
    """识别失败。kind 对应 mac 侧 TranscriptionError 的各个 case。"""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind  # missing_credentials / invalid_audio / no_data / parse_error /
        # network / server_busy / server_failed / timeout / no_speech
        self.message = message or self._default_message()
        super().__init__(self.message)

    def _default_message(self) -> str:
        return {
            "missing_credentials": "未配置云端语音识别凭证",
            "invalid_audio": "音频编码失败",
            "no_data": "云端识别未返回数据",
            "parse_error": "云端识别返回无法解析",
            "network": "网络错误",
            "server_busy": "服务器繁忙",
            "server_failed": "云端识别失败",
            "timeout": "识别超时",
            "no_speech": "无内容",
        }.get(self.kind, "云端识别失败")

    @property
    def is_retriable_in_place(self) -> bool:
        return self.kind in ("server_busy", "timeout")


def recognition_budget(audio_seconds: float) -> float:
    """识别等待预算：基础 20s + 时长×0.5，封顶 600s。正常识别一两秒就回来。"""
    return min(600.0, max(20.0, audio_seconds * 0.5 + 20.0))


def wav_bytes(samples, sample_rate: int = 16000) -> bytes:
    """float32 [-1,1] 样本 → 16bit 单声道 WAV。"""
    import numpy as np
    pcm = np.clip(np.asarray(samples, dtype=np.float32) * 32767.0, -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class CloudASRTranscriber:
    def __init__(self, config, log=None):
        self.config = config
        self.log = log or (lambda msg: None)

    # ---- 版本与凭证 ----

    def current_version(self) -> str:
        raw = (self.config.string("asr_version") or "turbo").strip()
        return raw if raw in VALID_VERSIONS else "turbo"

    def _volcano_credentials(self):
        api_key = self.config.string("bigasr_api_key")
        if api_key:
            return ("api_key", api_key)
        app_id = self.config.string("bigasr_app_id")
        access_token = self.config.string("bigasr_access_token")
        if app_id and access_token:
            return ("app_access", app_id, access_token)
        return None

    def _dashscope_api_key(self):
        return self.config.string("dashscope_api_key")

    def is_configured(self, version: str | None = None) -> bool:
        version = version or self.current_version()
        if version == "bailian":
            return bool(self._dashscope_api_key())
        return self._volcano_credentials() is not None

    @staticmethod
    def missing_configuration_hint() -> str:
        return "请配置火山引擎的 bigasr_api_key，或阿里百炼的 dashscope_api_key"

    # ---- 热词（词库直传） ----

    def _current_words(self) -> list:
        words = []
        entries = self.config.raw("term_corrections")
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled") is False:
                    continue
                target = str(item.get("target", "")).strip()
                if target:
                    words.append(target)
        hot = self.config.get("hot_words")
        if isinstance(hot, list):
            words += [str(w).split("|", 1)[0].strip() for w in hot if str(w).strip()]
        seen, out = set(), []
        for w in words:
            k = w.lower()
            if k not in seen:
                seen.add(k)
                out.append(w)
        return out

    def _hot_word_request_options(self) -> dict:
        words = self._current_words()
        corpus = {}
        if words:
            mode = (self.config.string("asr_vocab_mode") or "both").strip()
            context = {}
            if mode in ("hotwords", "both"):
                context["hotwords"] = [{"word": w} for w in words]
            if mode in ("context", "both"):
                context["context_type"] = "dialog_ctx"
                context["context_data"] = [{"text": "用户常说的词：" + "、".join(words)}]
            if context:
                corpus["context"] = json.dumps(context, ensure_ascii=False)
        for key in ("bigasr_boosting_table_name", "bigasr_boosting_table_id",
                    "bigasr_correct_table_name", "bigasr_correct_table_id"):
            value = self.config.string(key)
            if value:
                corpus[key[len("bigasr_"):]] = value
        return {"corpus": corpus} if corpus else {}

    # ---- 识别入口 ----

    def transcribe_auto(self, samples, sample_rate: int = 16000, version: str | None = None) -> str:
        """自动分段：≥10s 且有语音停顿的录音按停顿切段并行识别、按原顺序拼接。"""
        import numpy as np
        samples = np.asarray(samples, dtype=np.float32)
        version = version or self.current_version()
        max_concurrent = min(int(self.config.get("chunk_asr_max_concurrent") or 5),
                             2 if version == "bailian" else 10**9)
        if max_concurrent <= 1:
            return self.transcribe(samples, sample_rate, version)

        ranges = chunk_plan(samples, sample_rate)
        if len(ranges) <= 1:
            return self.transcribe(samples, sample_rate, version)

        chunk_seconds = ", ".join(f"{(e - s) / sample_rate:.1f}s" for s, e in ranges)
        self.log(f"Cloud ASR chunked: {len(ranges)} chunks ({chunk_seconds}), maxConcurrent={max_concurrent}")
        from concurrent.futures import ThreadPoolExecutor
        results = [None] * len(ranges)

        def run_chunk(index, start, end):
            results[index] = self.transcribe(samples[start:end], sample_rate, version)

        with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
            futures = [pool.submit(run_chunk, i, s, e) for i, (s, e) in enumerate(ranges)]
            for f in futures:
                f.result()  # 任一段抛错则整次失败（与 mac 一致：上层做重试/报错）
        return "".join(results)

    def transcribe(self, samples, sample_rate: int = 16000, version: str | None = None) -> str:
        import numpy as np
        samples = np.asarray(samples, dtype=np.float32)
        version = version or self.current_version()
        if len(samples) == 0:
            raise TranscriptionError("no_speech")

        audio_data = wav_bytes(samples, sample_rate)
        audio_seconds = len(samples) / max(sample_rate, 1)
        budget = recognition_budget(audio_seconds)

        if version == "bailian":
            api_key = self._dashscope_api_key()
            if not api_key:
                raise TranscriptionError("missing_credentials")
            if audio_seconds > 300:
                mins = int(round(audio_seconds / 60))
                raise TranscriptionError("server_failed",
                    f"百炼识别最长支持 5 分钟，这段约 {mins} 分钟太长了。请把 asr_version 改用火山 v2，或把录音分短一些。")
            self.log(f"Cloud ASR: version=bailian model={BAILIAN_MODEL} audio=wav/{len(audio_data) // 1024}KB budget={int(budget)}s")
            return self._transcribe_bailian(audio_data, api_key, budget)

        credentials = self._volcano_credentials()
        if not credentials:
            raise TranscriptionError("missing_credentials")
        body = self._make_body(audio_data, "wav")
        resource_id = RESOURCE_IDS[version]
        self.log(f"Cloud ASR: version={version} resource={resource_id} sync={version in SYNC_VERSIONS} "
                 f"hotwords={len(self._current_words())} audio=wav/{len(audio_data) // 1024}KB budget={int(budget)}s")
        if version in SYNC_VERSIONS:
            return self._transcribe_sync(body, credentials, resource_id, budget)
        return self._transcribe_async(body, credentials, resource_id, budget)

    # ---- 火山：请求构造 / 响应解析 ----

    def _make_body(self, audio_data: bytes, fmt: str) -> dict:
        request_dict = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "enable_speaker_info": False,
            "enable_channel_split": False,
            "show_utterances": True,
            "vad_segment": False,
            "sensitive_words_filter": "",
        }
        request_dict.update(self._hot_word_request_options())
        return {
            "user": {"uid": "豆包语音"},
            "audio": {"data": base64.b64encode(audio_data).decode(), "format": fmt, "language": ""},
            "request": request_dict,
        }

    @staticmethod
    def _make_request(url: str, credentials, resource_id: str, request_id: str, timeout: float,
                      body: bytes) -> urllib.request.Request:
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if credentials[0] == "api_key":
            req.add_header("X-Api-Key", credentials[1])
        else:
            req.add_header("X-Api-App-Key", credentials[1])
            req.add_header("X-Api-Access-Key", credentials[2])
        req.add_header("X-Api-Resource-Id", resource_id)
        req.add_header("X-Api-Request-Id", request_id)
        req.add_header("X-Api-Sequence", "-1")
        req.add_header("Authorization", "Bearer;")
        return req

    @staticmethod
    def _post(req: urllib.request.Request):
        """返回 (status_code, headers, body_bytes)。HTTP 非 2xx 也返回而不是抛异常，交给状态码分类。"""
        try:
            with urllib.request.urlopen(req, timeout=req.timeout if hasattr(req, "timeout") else 60) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b""
            return e.code, dict(e.headers or {}), data
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise TranscriptionError("network", f"网络错误：{e}") from e

    @staticmethod
    def _extract_text(json_obj: dict) -> str | None:
        result = json_obj.get("result")
        if not isinstance(result, dict):
            return None
        utterances = result.get("utterances")
        if isinstance(utterances, list) and utterances:
            sentences = [str(u.get("text", "")).strip() for u in utterances if isinstance(u, dict)]
            sentences = [s for s in sentences if s]
            if sentences:
                return "".join(sentences)
        text = result.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return None

    @staticmethod
    def _api_status_code(headers: dict) -> int | None:
        raw = None
        for key, value in headers.items():
            if key.lower() == "x-api-status-code":
                raw = value
                break
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _classify_server_error(self, status: int | None, data: bytes | None) -> TranscriptionError:
        message = "云端识别失败"
        if data:
            try:
                obj = json.loads(data)
                message = obj.get("message") or obj.get("msg") or obj.get("error") or message
            except (json.JSONDecodeError, AttributeError):
                pass
        if status is None:
            return TranscriptionError("server_failed", message)
        if status == 55000031:
            return TranscriptionError("server_busy", f"{message}（服务器繁忙）")
        if status == 20000003:
            return TranscriptionError("no_speech")
        if status == 45000030:
            return TranscriptionError("server_failed",
                                      "火山账号未开通此识别版本，请到控制台开通并领免费额度（45000030）")
        if status == 45000010:
            return TranscriptionError("server_failed", "火山 API Key 无效，请检查是否复制完整（45000010）")
        return TranscriptionError("server_failed", f"{message}（状态码 {status}）")

    # ---- 火山：同步（极速版 flash） ----

    def _transcribe_sync(self, body: dict, credentials, resource_id: str, budget: float) -> str:
        req = self._make_request(FLASH_URL, credentials, resource_id,
                                 uuid.uuid4().hex, budget, json.dumps(body).encode())
        status, headers, data = self._post(req)
        try:
            json_obj = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            raise TranscriptionError("parse_error") from None
        text = self._extract_text(json_obj)
        if text:
            return text
        raise self._classify_server_error(self._api_status_code(headers), data)

    # ---- 火山：异步（标准版 / 2.0：submit + 轮询 query） ----

    def _transcribe_async(self, body: dict, credentials, resource_id: str, budget: float) -> str:
        request_id = uuid.uuid4().hex
        req = self._make_request(SUBMIT_URL, credentials, resource_id, request_id,
                                 min(budget, SUBMIT_STALL_TIMEOUT), json.dumps(body).encode())
        status, headers, _ = self._post(req)
        api_status = self._api_status_code(headers)
        if api_status is not None and api_status not in (20000000, 20000001, 20000002):
            raise self._classify_server_error(api_status, None)

        deadline = time.monotonic() + budget
        while True:
            if time.monotonic() > deadline:
                raise TranscriptionError("timeout")
            qreq = self._make_request(QUERY_URL, credentials, resource_id, request_id,
                                      max(1.0, min(QUERY_STALL_TIMEOUT, deadline - time.monotonic())),
                                      b"{}")
            status, headers, data = self._post(qreq)
            api_status = self._api_status_code(headers)
            if api_status == 20000000:
                try:
                    json_obj = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    raise TranscriptionError("server_failed", "识别完成但未返回文本") from None
                text = self._extract_text(json_obj)
                if text:
                    return text
                raise TranscriptionError("server_failed", "识别完成但未返回文本")
            if api_status in (20000001, 20000002):
                time.sleep(0.3)  # 处理中 / 排队中
                continue
            raise self._classify_server_error(api_status, data)

    # ---- 百炼（DashScope qwen3-asr-flash，同步一步） ----

    def _transcribe_bailian(self, audio_data: bytes, api_key: str, budget: float) -> str:
        data_uri = "data:audio/wav;base64," + base64.b64encode(audio_data).decode()
        system_text = ""
        words = self._current_words()
        if words:
            system_text = "用户常说的词：" + "、".join(words)
        body = {
            "model": BAILIAN_MODEL,
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": system_text}]},
                {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}]},
            ],
        }
        req = urllib.request.Request(BAILIAN_URL, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        status, _, data = self._post(req)
        try:
            json_obj = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            raise TranscriptionError("parse_error") from None

        choices = json_obj.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content", "")).strip()
            if content:
                return content
            # 正常返回但没文字 = 音频里没听出话（静音/太短），与火山 20000003 同等对待
            raise TranscriptionError("no_speech")
        msg = extract_api_error_message(json_obj) or "百炼识别失败"
        raise TranscriptionError("server_failed", msg)

    # ---- 错误文案（与 mac 的 AIPolisher.extractAPIErrorMessage / friendlyProviderError 一致） ----

    # 实现在 apierrors.extract_api_error_message，这里保留一个转发方便测试打桩。
    @classmethod
    def _extract_api_error_message(cls, json_obj):
        return extract_api_error_message(json_obj)
