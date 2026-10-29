"""항목 길이를 허용 구간에 맞춘다 (5.1). 문구는 절대 자동으로 바꾸지 않는다."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable

from ..domain.script import Line, count_words
from ..domain.timebase import fmt_time
from .synth import LineAudio


@dataclass
class FitInput:
    line: Line
    at_us: int
    until_us: int | None      # 사용자가 지정한 종료 한계 (없으면 None)
    next_at_us: int | None
    hard_end_us: int          # 타임라인(영상) 끝
    base_speed: float


@dataclass
class FitItem:
    id: str
    at_us: int
    end_us: int
    duration_us: int
    allowed_end_us: int
    speed: float
    base_speed: float
    status: str               # ok | speed_adjusted | exceeded
    flags: list[str] = field(default_factory=list)    # exceeded | overlap | past_end
    notes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    audio: dict = field(default_factory=dict)   # LineAudio.as_manifest()

    def as_dict(self) -> dict:
        return asdict(self)


def allowed_end(fi: FitInput, gap_min_ms: int) -> int:
    limits = [fi.hard_end_us]
    if fi.until_us is not None:
        limits.append(fi.until_us)
    if fi.next_at_us is not None:
        gap = max(gap_min_ms, fi.line.pause_after_ms) * 1000
        limits.append(fi.next_at_us - gap)
    return min(limits)


def _suggest(fi: FitInput, audio: LineAudio, allowed_us: int) -> list[str]:
    """규칙 기반 단축 '후보'. 문구는 바꾸지 않고, 수정은 사용자 승인 후 Claude 세션에서 한다."""
    budget_us = allowed_us - fi.at_us
    words = count_words(fi.line.text)
    out = []
    if audio.duration_us > 0 and budget_us > 0:
        ratio = budget_us / audio.duration_us
        target = max(1, math.floor(words * ratio))
        out.append(f"현재 {words}단어 -> 허용 길이에 맞추려면 약 {target}단어 이하")
    sents = [s.original for s in audio.segments]
    if len(sents) > 1:
        out.append(f"마지막 문장 제외: {' '.join(sents[:-1])!r}")
        if len(sents) > 2:
            out.append(f"첫 문장만 사용: {sents[0]!r}")
    return out


def fit_lines(inputs: list[FitInput], synth: Callable[[Line, float], LineAudio], *, speed_supported: bool,
              max_speed: float, speed_step: float, max_iterations: int, gap_min_ms: int) -> list[FitItem]:
    items: list[FitItem] = []
    for fi in inputs:
        limit = allowed_end(fi, gap_min_ms)
        speed = fi.base_speed
        audio = synth(fi.line, speed)
        notes: list[str] = []
        status = "ok"
        it = 0
        while fi.at_us + audio.duration_us > limit and it < max_iterations:
            if not speed_supported:
                notes.append("공급자가 속도 파라미터를 지원하지 않아 속도 재합성을 건너뜀")
                break
            if speed >= max_speed - 1e-9:
                notes.append(f"속도 상한 {max_speed}에 도달")
                break
            needed = speed * audio.duration_us / max(1, (limit - fi.at_us))
            new_speed = min(max_speed, math.ceil(needed / speed_step) * speed_step)
            new_speed = round(max(new_speed, speed + speed_step), 4)
            new_speed = min(new_speed, max_speed)
            speed = new_speed
            audio = synth(fi.line, speed)
            status = "speed_adjusted"
            it += 1
        end = fi.at_us + audio.duration_us
        flags: list[str] = []
        suggestions: list[str] = []
        if end > limit:
            flags.append("exceeded")
            status = "exceeded"
            suggestions = _suggest(fi, audio, limit)
        if fi.next_at_us is not None and end > fi.next_at_us:
            flags.append("overlap")
            notes.append(f"다음 항목 시작({fmt_time(fi.next_at_us)})을 {fmt_time(end - fi.next_at_us)}만큼 침범")
        if end > fi.hard_end_us:
            flags.append("past_end")
            notes.append(f"타임라인 끝({fmt_time(fi.hard_end_us)})을 넘어 문장이 끊김")
        items.append(FitItem(fi.line.id, fi.at_us, end, audio.duration_us, limit, speed, fi.base_speed,
                             status, flags, notes, suggestions, audio.as_manifest()))
    return items


def finalize_flags(records: list[dict], hard_end_us: int) -> None:
    """겹침/영상 끝 초과 플래그를 레코드 목록 전체 기준으로 다시 계산한다 (일부 항목만 재합성했을 때도 사용)."""
    ordered = sorted(records, key=lambda r: r["at_us"])
    for r in ordered:
        r["flags"] = [f for f in r["flags"] if f not in ("overlap", "past_end")]
        r["notes"] = [n for n in r["notes"] if "침범" not in n and "끊김" not in n]
    for r, nxt in zip(ordered, ordered[1:] + [None]):
        end = r["at_us"] + r["duration_us"]
        r["end_us"] = end
        if nxt is not None and end > nxt["at_us"]:
            r["flags"].append("overlap")
            r["notes"].append(f"다음 항목 시작({fmt_time(nxt['at_us'])})을 {fmt_time(end - nxt['at_us'])}만큼 침범")
        if end > hard_end_us:
            r["flags"].append("past_end")
            r["notes"].append(f"타임라인 끝({fmt_time(hard_end_us)})을 넘어 문장이 끊김")
