"""발음 사전 적용과 문장 분리. 원문·정규화문·발음문을 모두 보존한다 (4.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_IPA = re.compile(r"^/.+/$")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text.strip())]
    return [p for p in parts if p]


@dataclass
class Spoken:
    original: str
    normalized: str            # 공백 정리만 한 문장
    spoken: str                # 공급자에 실제로 보내는 문장
    applied: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _respell(value: str) -> str:
    """철자 치환용 정규화: 'EH-kum' -> 'ehkum'. 이 변환이 실제로 올바른 발음을 내는지는 청취로만 확인할 수 있다."""
    return value.replace("-", "").lower()


def apply_pronunciations(sentence: str, pron: dict[str, str], mode: str) -> Spoken:
    normalized = " ".join(sentence.split())
    spoken = normalized
    applied: dict[str, str] = {}
    warnings: list[str] = []
    for word in sorted(pron, key=len, reverse=True):
        value = pron[word]
        pat = re.compile(rf"(?<![\w]){re.escape(word)}(?![\w])", re.IGNORECASE)
        if not pat.search(spoken):
            continue
        if _IPA.match(value):
            if mode == "phoneme_markup":
                repl = lambda m, v=value: f"[{m.group(0)}]({v})"  # noqa: E731
            else:
                warnings.append(f"{word}: 이 공급자는 음소 표기를 지원하지 않아 사전 항목을 건너뜀")
                continue
        else:
            repl = lambda m, v=value: _respell(v)  # noqa: E731
        spoken = pat.sub(repl, spoken)
        applied[word] = value
    return Spoken(sentence, normalized, spoken, applied, warnings)
