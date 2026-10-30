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


_WORD = re.compile(r"[A-Za-z0-9'\u2019\-]+")


def is_cue_sentence(sentence: str, cue_words: set[str]) -> bool:
    """문장의 모든 단어가 큐 단어(발음 사전 키 등)면 카운트 큐로 본다. 예: 'Ekam.'"""
    words = [w.lower() for w in _WORD.findall(sentence)]
    return bool(words) and all(w in cue_words for w in words)


def split_emphasis(sentence: str, emphasis: list[str]) -> list[tuple[str, bool]]:
    """강조 단어에서 문장을 조각낸다 -> [(조각, 강조 여부)]. 단어 뒤에 붙은 구두점은 조각에 포함한다.

    강조 단어는 문장 안에서 처음 나오는 한 곳만 쓴다. 찾지 못하면 분할하지 않는다.
    """
    cuts: list[tuple[int, int]] = []
    for w in emphasis:
        m = re.search(rf"(?<![\w]){re.escape(w)}(?![\w])[,;:]?", sentence, re.IGNORECASE)
        if m and not any(a < m.end() and m.start() < b for a, b in cuts):
            cuts.append((m.start(), m.end()))
    if not cuts:
        return [(sentence, False)]
    cuts.sort()
    out: list[tuple[str, bool]] = []
    pos = 0
    for a, b in cuts:
        if sentence[pos:a].strip():
            out.append((sentence[pos:a].strip(), False))
        out.append((sentence[a:b].strip(), True))
        pos = b
    if sentence[pos:].strip():
        out.append((sentence[pos:].strip(), False))
    return out
