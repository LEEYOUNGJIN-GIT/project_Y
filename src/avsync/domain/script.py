"""script.yaml 파서. 오류는 줄 번호와 함께 보고한다 (4.1)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import yaml

from .timebase import TimeError, parse_time

LINE_KEY = "__line__"


class ScriptError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


class _LineLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _LineLoader, node: yaml.MappingNode, deep=False):
    mapping = yaml.SafeLoader.construct_mapping(loader, node, deep=deep)
    mapping[LINE_KEY] = node.start_mark.line + 1
    return mapping


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass
class Line:
    id: str
    text: str
    at_us: int | None = None
    until_us: int | None = None
    mood: str = "calm"
    emphasis: list[str] = field(default_factory=list)
    pause_after_ms: int = 0
    src_line: int = 0


@dataclass
class VoiceSpec:
    provider: str | None = None
    voice_id: str | None = None
    base_speed: float | None = None   # None이면 톤 프리셋의 speed를 쓴다


@dataclass
class Script:
    approved: bool
    voice: VoiceSpec
    pronunciations: dict[str, str]
    lines: list[Line]
    no_narration: list[tuple[int, int]]

    def by_id(self) -> dict[str, Line]:
        return {ln.id: ln for ln in self.lines}


_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
_WORD_RE = re.compile(r"[A-Za-z0-9'’\-]+")


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def load_script(path: Path, *, require_at: bool, fps: Fraction | None = None) -> Script:
    """`require_at`: video-first는 True (모든 항목에 at 필요), audio-first는 False."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ScriptError([f"{path}: 파일이 없습니다"]) from None
    try:
        data = yaml.load(raw, Loader=_LineLoader)  # noqa: S506 - SafeLoader 하위 클래스
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"{path}:{mark.line + 1}" if mark else str(path)
        raise ScriptError([f"{where}: YAML 문법 오류: {getattr(e, 'problem', e)}"]) from None
    if not isinstance(data, dict):
        raise ScriptError([f"{path}:1: 최상위가 매핑이어야 합니다"])

    problems: list[str] = []

    def err(line: int, msg: str):
        problems.append(f"{path}:{line}: {msg}")

    approved = data.get("approved", False)
    if not isinstance(approved, bool):
        err(data.get(LINE_KEY, 1), "approved는 true/false여야 합니다")
        approved = False

    voice_raw = data.get("voice") or {}
    voice = VoiceSpec(
        provider=voice_raw.get("provider"),
        voice_id=voice_raw.get("voice_id"),
        base_speed=float(voice_raw["base_speed"]) if voice_raw.get("base_speed") is not None else None,
    )

    pron_raw = data.get("pronunciations") or {}
    pronunciations = {str(k): str(v) for k, v in pron_raw.items() if k != LINE_KEY}

    lines: list[Line] = []
    seen: dict[str, int] = {}
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list) or not raw_lines:
        err(data.get(LINE_KEY, 1), "lines는 비어 있지 않은 목록이어야 합니다")
        raw_lines = []
    for item in raw_lines:
        if not isinstance(item, dict):
            err(1, f"lines 항목이 매핑이 아닙니다: {item!r}")
            continue
        ln = item.get(LINE_KEY, 0)
        lid = str(item.get("id", "")).strip()
        if not lid or not _ID_RE.match(lid):
            err(ln, f"id가 없거나 형식이 잘못되었습니다: {item.get('id')!r}")
            continue
        if lid in seen:
            err(ln, f"id {lid!r}가 {seen[lid]}번째 줄과 중복됩니다")
            continue
        seen[lid] = ln
        text = str(item.get("text", "")).strip()
        if not text:
            err(ln, f"{lid}: text가 비어 있습니다")
            continue
        at_us = until_us = None
        try:
            if item.get("at") is not None:
                at_us = parse_time(item["at"], fps=fps)
            if item.get("until") is not None:
                until_us = parse_time(item["until"], fps=fps)
        except TimeError as e:
            err(ln, f"{lid}: {e}")
            continue
        if require_at and at_us is None:
            err(ln, f"{lid}: video-first 모드에서는 at이 필요합니다")
        if at_us is not None and until_us is not None and until_us <= at_us:
            err(ln, f"{lid}: until({item['until']})이 at({item['at']})보다 뒤여야 합니다")
        pause = item.get("pause_after_ms", 0)
        if not isinstance(pause, int) or pause < 0:
            err(ln, f"{lid}: pause_after_ms는 0 이상의 정수여야 합니다")
            pause = 0
        emphasis = item.get("emphasis") or []
        lines.append(
            Line(
                id=lid, text=text, at_us=at_us, until_us=until_us,
                mood=str(item.get("mood", "calm")), emphasis=[str(e) for e in emphasis],
                pause_after_ms=pause, src_line=ln,
            )
        )

    if require_at:
        ordered = [ln for ln in lines if ln.at_us is not None]
        for a, b in zip(ordered, ordered[1:]):
            if b.at_us <= a.at_us:
                err(b.src_line, f"{b.id}: at이 앞 항목 {a.id}보다 뒤여야 합니다 (항목은 시간순으로 적습니다)")

    windows: list[tuple[int, int]] = []
    for w in data.get("no_narration") or []:
        try:
            a, b = parse_time(w[0], fps=fps), parse_time(w[1], fps=fps)
        except (TimeError, TypeError, IndexError) as e:
            err(data.get(LINE_KEY, 1), f"no_narration 항목 오류 {w!r}: {e}")
            continue
        if b <= a:
            err(data.get(LINE_KEY, 1), f"no_narration {w!r}: 끝이 시작보다 뒤여야 합니다")
            continue
        windows.append((a, b))
    if require_at:
        for ln in lines:
            if ln.at_us is None:
                continue
            for a, b in windows:
                if a <= ln.at_us < b:
                    err(ln.src_line, f"{ln.id}: no_narration 구간 안에서 시작합니다")

    if problems:
        raise ScriptError(problems)
    return Script(approved=approved, voice=voice, pronunciations=pronunciations,
                  lines=lines, no_narration=windows)
