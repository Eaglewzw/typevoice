"""AI 润色 — AIPolisher 的移植（识别结果 → 人话整理）。

- 提供商：qwen（阿里百炼，默认）/ zhipu（智谱）/ doubao（火山方舟）/ none（不优化）
- 千问支持「自动」模型路由：按质量优先队列取第一个额度未耗尽的模型，
  403 额度类失败自动降级到下一个（冷却 20 小时后自动重试）。
- 术语纠正：润色后按词库把误写换成正写（整词命中、单遍替换、长的优先）。
"""

import json
import re
import time
import urllib.error
import urllib.request
import unicodedata

from .apierrors import extract_api_error_message

QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
DOUBAO_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

DEFAULT_DOUBAO_MODEL = "doubao-seed-2-0-pro-260215"


class PolishError(Exception):
    def __init__(self, kind: str, message: str = ""):
        self.kind = kind  # no_api_key / api_error / network / no_data / parse_error
        self.message = message or {
            "no_api_key": "未配置润色 Key",
            "api_error": "润色失败",
            "network": "网络错误",
            "no_data": "润色服务未返回数据",
            "parse_error": "润色返回无法解析",
        }.get(kind, "润色失败")
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# 模型路由（PolishModelRouter 的移植）

QUALITY_CHAIN = ["qwen3.7-plus", "qwen3.8-max", "qwen3.7-max", "qwen3.7-flash", "qwen3.6-flash"]
SPEED_CHAIN = ["qwen3.7-flash", "qwen3.6-flash", "qwen3.8-max", "qwen3.7-max"]
LAST_RESORT = "qwen3.7-flash"
COOLDOWN = 20 * 3600  # 免费额度当天不会恢复，冷却 20 小时


class PolishModelRouter:
    AUTO = "auto"
    AUTO_SPEED = "auto-speed"

    @staticmethod
    def is_auto(value) -> bool:
        return value in (PolishModelRouter.AUTO, PolishModelRouter.AUTO_SPEED)

    @staticmethod
    def _exhausted_key(model: str) -> str:
        return "polish_auto_exhausted_until." + model

    @staticmethod
    def candidates(value: str | None, config) -> list:
        chain = SPEED_CHAIN if value == PolishModelRouter.AUTO_SPEED else QUALITY_CHAIN
        now = time.time()
        free = []
        for model in chain:
            until = float(config.get(PolishModelRouter._exhausted_key(model)) or 0)
            if now >= until:
                free.append(model)
        return free or [LAST_RESORT]

    @staticmethod
    def mark_exhausted(model: str, config):
        config.set(PolishModelRouter._exhausted_key(model), time.time() + COOLDOWN)


# ---------------------------------------------------------------------------
# 提示词（与 mac 版逐字对齐）

POLISH_SYSTEM_PROMPT = """你是一个语音转文字的整理助手。用户通过语音输入了一段话，你要把它整理成好读的文本。

## 你的角色
想象你是用户的表达优化师，用户口述了一段想法，你帮他整理成用户看到结果时应该觉得"这就是我想说的，只是整理得更清楚、便于阅读和理解"。

## 可以做的事
- 分段：根据语义、语境分段，避免大段文字堆积，让阅读体验更好。
- 标点：修正标点符号，让断句更自然。适当使用冒号、分号来连接关联内容
- 列表：当你觉得用户表达的语义里包含或者就是并列内容时，或用户说“第一、第二、第三”“一个是、另一个是、最后”，或者明显是在口述步骤/清单/多个独立条目时，你要把其中适合并列的列编号显示。
- 去除口语冗余：删掉重复的词、无意义的语气词（"就是"、"然后"、"嗯"等）
- 理顺断句：口语中断裂或不通顺的句子，可以根据语义、语境适当的轻微调整语序使其通顺
- 数字：口语中的汉字数字转为阿拉伯数字（"两到三次"→"2 到 3 次"，"大概五百块"→"大概 500 块"），但成语、固定搭配除外（"一模一样"、"三心二意"不转）
- 明显重复的短语、绕口表达要合并整理，但不要改变用户原意。
- “保留用户用词”不等于原样保留口语里的重复结构。对于明显重复、断裂、不顺的表达，要做轻微合并和理顺。
- 如果一句话本身已经通顺，可以少改；但如果存在明显重复、语序绕、主语不清，要主动整理到自然可读。

## 不能做的事
- 不要回答或回应用户说的内容——你不是对话助手，你是用户的表达转写整理工具。即使用户说的是一个问题，也不要给出答案或建议
- 同义词不替换："不如"不要改成"不妨"，"想要的效果不一样"不要改成"需求各异"
- 不要把口语改成书面语：保留用户自己的说话风格
- 不要添加用户没说过的内容，包括总结句、过渡句、小标题
- 不要使用加粗、标题等富文本格式
- 不要随意更换用户的用词，除非用户用词逻辑不恰当
- 不要翻译用户输入的语言种类

## 输出格式
纯文本，适当分段。并列内容用编号列表。不要加粗、不要加标题。"""


def output_request_marker(language) -> str:
    return f"【本次要求：用{language.name}输出】"


def output_language_system_section(language) -> str:
    return f"""


## 目标语言
如果待整理文本后面带有{output_request_marker(language)}，说明用户要求这段话用{language.name}输出：先按上面的规则整理，再把整理结果翻译成自然、地道的{language.name}，只输出{language.name}译文；人名、产品名、专有名词保留原样；不要输出其他语言，不要加任何说明或翻译标注。如果整理后的文本本来就是{language.name}，直接输出整理结果。此时「不要翻译用户输入的语言种类」这条不适用。
"""


def should_use_language_preserving_prompt(text: str) -> bool:
    latin_letters = 0
    cjk_characters = 0
    english_words = 0
    current_word_length = 0

    def finish_word():
        nonlocal english_words, current_word_length
        if current_word_length >= 2:
            english_words += 1
        current_word_length = 0

    for ch in text:
        code = ord(ch)
        if 65 <= code <= 90 or 97 <= code <= 122:
            latin_letters += 1
            current_word_length += 1
        else:
            finish_word()
            if 0x4E00 <= code <= 0x9FFF:
                cjk_characters += 1
    finish_word()

    has_english_sentence = english_words >= 3 or latin_letters >= max(8, cjk_characters)
    has_mixed = cjk_characters > 0 and english_words >= 2
    return has_english_sentence or has_mixed


def make_polish_user_prompt(text: str, output_language=None) -> str:
    if output_language is not None:
        return f"待整理文本：\n{text}\n\n{output_request_marker(output_language)}"
    if should_use_language_preserving_prompt(text):
        return ("Keep the original language. Do not translate. Preserve Chinese and English as they appear. "
                f"Polish this speech transcript only:\n{text}")
    return f"待整理文本：\n{text}"


def meaningful_character_count(text: str) -> int:
    """去掉空白、标点、符号后的字符数（短文本直出的门槛）。"""
    return sum(1 for ch in text if not ch.isspace()
               and not unicodedata.category(ch).startswith(("P", "S")))


# ---------------------------------------------------------------------------
# 术语纠正（applyTermCorrections 的移植）

LATIN_ALNUM = re.compile(r"[0-9A-Za-z]")


def apply_term_corrections(corrections: list, text: str) -> str:
    """按词库把误写换成正写：
    - 已是正写的地方不动（误写 APIK、正写 APIKey 时，原文里的 APIKey 不能变 APIKeyey）；
    - 英文/数字误写整词命中（SQL 不改 PostgreSQL 的尾巴）；中文没有词边界，照旧；
    - 单遍替换：所有命中都在原文上找，长的误写优先、互不重叠。
    """
    pairs = []
    for correction in corrections or []:
        target = str(correction.get("target", "")).strip()
        if not target:
            continue
        if correction.get("enabled") is False:
            continue
        for variant in correction.get("variants", []):
            variant = str(variant).strip()
            if variant and variant != target:
                pairs.append((variant, target))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    if not pairs or not text:
        return text

    accepted = []  # (start, end, target)
    for variant, target in pairs:
        case_only = variant.lower() == target.lower()
        correct_spots = [] if case_only else _occurrences(text, target)
        for start, end in _occurrences(text, variant):
            if not _is_whole_latin_word(text, start, end, variant):
                continue
            if case_only and text[start:end] == target:
                continue
            if any(_overlap(start, end, s, e) for s, e, _ in correct_spots):
                continue
            if any(_overlap(start, end, s, e) for s, e, _ in accepted):
                continue
            accepted.append((start, end, target))
    if not accepted:
        return text

    result = text
    for start, end, target in sorted(accepted, key=lambda a: a[0], reverse=True):
        result = result[:start] + target + result[end:]
    return result


def _overlap(s1, e1, s2, e2) -> bool:
    return max(s1, s2) < min(e1, e2)


def _occurrences(text: str, needle: str):
    """不分大小写地找出 needle 的所有不重叠位置。"""
    if not needle:
        return []
    ranges = []
    lower_text = text.lower()
    lower_needle = needle.lower()
    pos = 0
    while True:
        idx = lower_text.find(lower_needle, pos)
        if idx < 0:
            break
        ranges.append((idx, idx + len(needle)))
        pos = idx + len(needle)
    return ranges


def _is_whole_latin_word(text: str, start: int, end: int, variant: str) -> bool:
    """以英文字母/数字开头（结尾）的误写，前（后）一个字符不能也是英文字母/数字。"""
    if LATIN_ALNUM.match(variant[0]) and start > 0 and LATIN_ALNUM.match(text[start - 1]):
        return False
    if LATIN_ALNUM.match(variant[-1]) and end < len(text) and LATIN_ALNUM.match(text[end]):
        return False
    return True


# ---------------------------------------------------------------------------


class AIPolisher:
    def __init__(self, config, log=None):
        self.config = config
        self.log = log or (lambda msg: None)

    def is_polish_enabled(self) -> bool:
        provider = (self.config.string("polish_provider") or "qwen").strip().lower()
        return provider != "none"

    def _provider(self):
        """返回 (name, url, model, api_key)；未配置返回 None。"""
        provider = (self.config.string("polish_provider") or "qwen").strip().lower()
        if provider == "qwen":
            key = self.config.string("dashscope_api_key")
            if not key:
                return None
            saved = self.config.string("qwen_polish_model")
            model = saved if saved else PolishModelRouter.AUTO
            return ("qwen", QWEN_URL, model, key)
        if provider == "zhipu":
            key = self.config.string("zhipu_api_key")
            if not key:
                return None
            return ("zhipu", ZHIPU_URL, self.config.string("zhipu_polish_model") or "glm-4.7-flash", key)
        if provider == "doubao":
            key = self.config.string("ark_api_key")
            if not key:
                return None
            saved = self.config.string("doubao_polish_model")
            return ("doubao", DOUBAO_URL, saved or DEFAULT_DOUBAO_MODEL, key)
        return None

    # ---- 润色入口 ----

    def polish(self, text: str, output_language=None) -> str:
        provider = self._provider()
        if provider is None:
            raise PolishError("no_api_key")

        system_prompt = POLISH_SYSTEM_PROMPT
        if output_language is not None:
            system_prompt += output_language_system_section(output_language)
        user_prompt = make_polish_user_prompt(text, output_language)

        name, url, model, api_key = provider

        def make_body(m: str) -> dict:
            body = {
                "model": m,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            if name == "qwen":
                body.update(top_p=0.8, temperature=0.7, result_format="message", enable_thinking=False)
            else:  # zhipu / doubao
                body.update(temperature=0.1, max_tokens=2000, thinking={"type": "disabled"})
            return body

        self.log(f"Cloud ASR polish provider={name} model={model}")

        if name == "qwen":
            candidates = (PolishModelRouter.candidates(model, self.config)
                          if PolishModelRouter.is_auto(model) else [model])
            return self._attempt_qwen(candidates, url, api_key, make_body)

        content, _, _ = self._call_chat_completions(url, api_key, make_body(model))
        return content

    def _attempt_qwen(self, candidates: list, url: str, api_key: str, make_body) -> str:
        if not candidates:
            raise PolishError("api_error", "润色模型均不可用（额度用完）")
        model = candidates[0]
        self.log(f"Cloud ASR polish qwen attempt model={model}")
        try:
            content, _, _ = self._call_chat_completions(url, api_key, make_body(model))
            return content
        except PolishError as err:
            if err.kind != "quota_exhausted":
                raise
            PolishModelRouter.mark_exhausted(model, self.config)
            rest = candidates[1:]
            if rest:
                self.log(f"Cloud ASR polish qwen: {model} 额度类失败(403)，自动降级到 {rest[0]}")
                return self._attempt_qwen(rest, url, api_key, make_body)
            raise

    # ---- HTTP ----

    def _call_chat_completions(self, url: str, api_key: str, body: dict):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        status, _, data = self._post(req, timeout=60)
        if status is None:
            raise PolishError("network", "网络错误")
        try:
            json_obj = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            raise PolishError("parse_error") from None

        choices = json_obj.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content", "")).strip()
            usage = json_obj.get("usage") or {}
            return (content, int(usage.get("prompt_tokens", 0) or 0),
                    int(usage.get("completion_tokens", 0) or 0))

        # 非 2xx：403/额度类 → quota_exhausted（触发自动降级）；其余按文案报错
        if status == 403:
            raise PolishError("quota_exhausted",
                              extract_api_error_message(json_obj) or "额度不足（403）")
        msg = extract_api_error_message(json_obj) or f"润色失败（{status}）"
        raise PolishError("api_error", msg)

    @staticmethod
    def _post(req: urllib.request.Request, timeout: float):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            try:
                data = e.read()
            except Exception:
                data = b""
            return e.code, dict(e.headers or {}), data
        except (urllib.error.URLError, OSError, TimeoutError):
            return None, {}, b""

    # ---- 术语纠正 ----

    def apply_configured_term_corrections(self, text: str) -> str:
        return apply_term_corrections(self.config.raw("term_corrections") or [], text)
