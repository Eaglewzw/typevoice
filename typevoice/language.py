"""语音口令与输出语言 — 对应 mac 的 OutputLanguage / OutputLanguageCommand。

用户在句首或句尾说「用英文」「翻译成日文」，本次输出对应语言。
识别完全用程序规则，不交给模型领会：
- 只认句首和句尾，中间出现一律当正文；
- 句首口令后面要有停顿（标点或空格）；
- 口令前紧跟否定词（不要/别/不用…）的不算；
- 去掉口令后正文要有实质内容，只有口令的整句当正文。
"""

import unicodedata
from dataclasses import dataclass, field
from typing import Optional

LIST_CONFIG_KEY = "output_languages"
DEFAULT_CONFIG_KEY = "default_output_language"
COMMAND_ENABLED_CONFIG_KEY = "output_language_command_enabled"


@dataclass
class OutputLanguage:
    id: str
    name: str
    tag: str
    phrases: list
    enabled: bool = True

    def is_builtin(self) -> bool:
        return not self.id.startswith("custom-")


BUILTIN_LANGUAGES = [
    OutputLanguage("en", "英文", "EN", [
        "用英文", "用英语", "翻译成英文", "翻译成英语", "翻成英文", "翻成英语", "转成英文", "转英文",
        "英文输出", "输出英文", "说英文", "English", "in English",
    ]),
    OutputLanguage("ja", "日文", "JA", [
        "用日文", "用日语", "翻译成日文", "翻译成日语", "翻成日文", "翻成日语", "转日文",
        "日文输出", "输出日文", "说日语", "Japanese", "日本語",
    ]),
    OutputLanguage("ko", "韩文", "KO", [
        "用韩文", "用韩语", "翻译成韩文", "翻译成韩语", "翻成韩文", "翻成韩语", "转韩文",
        "韩文输出", "输出韩文", "说韩语", "Korean", "한국어",
    ]),
    OutputLanguage("zh", "中文", "ZH", [
        "用中文", "翻译成中文", "翻成中文", "转中文", "中文输出", "输出中文", "说中文", "Chinese",
    ]),
    OutputLanguage("fr", "法语", "FR", ["用法语", "翻译成法语", "翻成法语", "法语输出", "French"], enabled=False),
    OutputLanguage("de", "德语", "DE", ["用德语", "翻译成德语", "翻成德语", "德语输出", "German"], enabled=False),
    OutputLanguage("es", "西班牙语", "ES", ["用西班牙语", "翻译成西班牙语", "西班牙语输出", "Spanish"], enabled=False),
]

BUILTIN_BY_ID = {lang.id: lang for lang in BUILTIN_LANGUAGES}


def configured_languages(config) -> list:
    """config.json 的 output_languages：内置项按 id 覆盖，自定义项追加。"""
    stored = config.raw(LIST_CONFIG_KEY)
    if not isinstance(stored, list):
        return list(BUILTIN_LANGUAGES)
    by_id = {}
    customs = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        lang = OutputLanguage(
            id=str(item.get("id", "")).strip(),
            name=str(item.get("name", "")).strip(),
            tag=str(item.get("tag", "")).strip()[:4],
            phrases=[p for p in item.get("phrases", []) if isinstance(p, str)],
            enabled=bool(item.get("enabled", True)),
        )
        if not lang.id or not lang.name or not lang.phrases:
            continue
        if lang.id in BUILTIN_BY_ID:
            by_id[lang.id] = lang
        else:
            customs.append(lang)
    result = [by_id.get(base.id, base) for base in BUILTIN_LANGUAGES]
    return result + customs


def default_language(config) -> Optional[OutputLanguage]:
    lang_id = config.string(DEFAULT_CONFIG_KEY)
    if not lang_id:
        return None
    for lang in configured_languages(config):
        if lang.id == lang_id:
            return lang
    return None


# ---------------------------------------------------------------------------
# 口令识别

NEGATIONS = ["不要", "别", "不用", "不能", "无需", "不需要", "不必", "不会", "没有", "不是", "不", "没"]
MINIMUM_CONTENT_CHARACTERS = 3
TRAILING_FILLERS = sorted(
    ["可以吗", "好吗", "好么", "行吗", "谢谢", "一下", "就行", "吧", "呗", "哦", "啊", "呀", "哈", "说", "please"],
    key=len, reverse=True,
)
SEPARATORS = set("，。、！？；：,.!?;:\"“”‘’'()（）[]【】…—-~～ \t\n\r")


@dataclass
class OutputLanguageCommand:
    target: OutputLanguage
    position: str          # leading / trailing
    matched_phrase: str
    stripped_text: str     # 去掉口令后的正文（已去掉口令旁边的标点）


def _is_separator(ch: str) -> bool:
    return ch in SEPARATORS


def _is_ascii_letter(ch: str) -> bool:
    return "a" <= ch <= "z" or "A" <= ch <= "Z"


def has_punctuation_boundary(s: str) -> bool:
    """跳过空白后，紧邻的第一个字符是标点。"""
    for ch in s:
        if ch in (" ", "\t"):
            continue
        return _is_separator(ch)
    return False


def trim_edges(s: str) -> str:
    start, end = 0, len(s)
    while start < end and _is_separator(s[start]):
        start += 1
    while end > start and _is_separator(s[end - 1]):
        end -= 1
    return s[start:end]


def strip_trailing_fillers(s: str) -> str:
    """反复剥掉句尾的语气词/客套及其旁边的标点（「用英文说吧。」→「用英文」）。"""
    current = trim_edges(s)
    while True:
        lower = current.lower()
        filler = next((f for f in TRAILING_FILLERS if lower.endswith(f.lower())), None)
        if filler is None:
            return current
        current = trim_edges(current[: len(current) - len(filler)])


def is_negated(body: str) -> bool:
    tail = trim_edges(body)
    return any(tail.endswith(neg) for neg in NEGATIONS)


def valid_body(body: str) -> Optional[str]:
    """正文要有实质内容。"""
    meaningful = sum(1 for ch in body if ch.isalpha() or ch.isnumeric())
    return body if meaningful >= MINIMUM_CONTENT_CHARACTERS else None


def detect_command(text: str, languages: Optional[list] = None) -> Optional[OutputLanguageCommand]:
    if languages is None:
        languages = BUILTIN_LANGUAGES
    trimmed = trim_edges(text)
    if not trimmed:
        return None
    lower = trimmed.lower()
    without_fillers = strip_trailing_fillers(trimmed)
    trailing_candidates = [trimmed] if without_fillers == trimmed else [trimmed, without_fillers]

    pairs = []
    for language in languages:
        if not language.enabled:
            continue
        for phrase in language.phrases:
            p = phrase.strip()
            if p:
                pairs.append((p, language))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)  # 长口令优先

    for phrase, language in pairs:
        p = phrase.lower()
        # 英文口令（English / in English）在英文句子里本来就是普通单词，空格不算停顿，必须有标点隔开
        needs_punctuation = any(_is_ascii_letter(c) for c in phrase)

        # 句尾（先试原句，再试剥掉语气词后的句子）
        for candidate in trailing_candidates:
            if not candidate.lower().endswith(p):
                continue
            body = candidate[: len(candidate) - len(phrase)]
            boundary_ok = (not needs_punctuation) or has_punctuation_boundary(body[::-1])
            if boundary_ok and not is_negated(body):
                stripped = valid_body(trim_edges(body))
                if stripped is not None:
                    return OutputLanguageCommand(language, "trailing", phrase, stripped)

        # 句首：口令后面必须有停顿
        if lower.startswith(p):
            rest = trimmed[len(phrase):]
            boundary_ok = has_punctuation_boundary(rest) if needs_punctuation \
                else bool(rest) and _is_separator(rest[0])
            if boundary_ok:
                stripped = valid_body(trim_edges(rest))
                if stripped is not None:
                    return OutputLanguageCommand(language, "leading", phrase, stripped)
    return None
