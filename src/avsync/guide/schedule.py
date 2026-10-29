"""호흡 격자 시간표 (4.4). 순수 계산이며 모든 시각은 정수 us다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..domain.script import LINE_KEY, Line, ScriptError, _LineLoader
from ..domain.timebase import TimeError, parse_time

HOLD_MODES = ("first_only", "every_breath")


@dataclass
class StepSpec:
    line: str
    counts: int
    hold: str = ""
    hold_mode: str = "first_only"
    hold_cues: list[str] = field(default_factory=list)
    src_line: int = 0


@dataclass
class GuideSpec:
    count_in_text: str
    count_in_us: int
    start_tone: bool
    count_us: int
    steps: list[StepSpec]
    tail_us: int
    lead_in_ms: int | None


@dataclass
class PlannedStep:
    line: str
    counts: int
    start_us: int        # 호흡(카운트) 시작
    end_us: int
    speak_at_us: int     # 내레이션 시작 = start + lead_in
    kind: str = "step"   # step | hold_cue


@dataclass
class GuidePlan:
    count_in_us: int
    count_us: int
    tone_at_us: int | None
    lead_in_us: int
    steps: list[PlannedStep]          # 메인 단계
    items: list[dict]                 # 내레이션 타임라인 항목 (id, text는 호출자가 채움), at_us 순서
    total_us: int
    tail_us: int


def load_guide(path: Path) -> GuideSpec:
    import yaml as _y
    try:
        data = _y.load(Path(path).read_text(encoding="utf-8"), Loader=_LineLoader)  # noqa: S506
    except FileNotFoundError:
        raise ScriptError([f"{path}: 파일이 없습니다"]) from None
    except _y.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise ScriptError([f"{path}:{mark.line + 1 if mark else 1}: YAML 문법 오류: {getattr(e, 'problem', e)}"]) from None
    problems: list[str] = []

    def err(line, msg):
        problems.append(f"{path}:{line}: {msg}")

    if not isinstance(data, dict):
        raise ScriptError([f"{path}:1: 최상위가 매핑이어야 합니다"])
    if data.get("mode") != "audio_first":
        err(data.get(LINE_KEY, 1), "mode는 audio_first여야 합니다")
    ci = data.get("count_in") or {}
    try:
        count_in_us = parse_time(ci.get("count_in_s"))
    except (TimeError, TypeError):
        err(ci.get(LINE_KEY, 1), f"count_in.count_in_s가 필요합니다 (초): {ci.get('count_in_s')!r}")
        count_in_us = 0
    br = data.get("breath") or {}
    try:
        count_us = parse_time(br.get("count_s"))
        if count_us <= 0:
            raise TimeError("0 이하")
    except (TimeError, TypeError) as e:
        err(br.get(LINE_KEY, 1), f"breath.count_s가 필요합니다 (양수 초): {e}")
        count_us = 0
    tail_us = parse_time(data["tail_s"]) if data.get("tail_s") is not None else 0
    steps = []
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        err(data.get(LINE_KEY, 1), "steps는 비어 있지 않은 목록이어야 합니다")
        raw_steps = []
    for s in raw_steps:
        ln = s.get(LINE_KEY, 0) if isinstance(s, dict) else 0
        if not isinstance(s, dict) or "line" not in s:
            err(ln, "steps 항목에는 line이 필요합니다")
            continue
        counts = s.get("counts", 1)
        if not isinstance(counts, int) or counts < 1:
            err(ln, f"{s['line']}: counts는 1 이상의 정수여야 합니다")
            continue
        mode = s.get("hold_mode", "first_only")
        if mode not in HOLD_MODES:
            err(ln, f"{s['line']}: hold_mode는 {HOLD_MODES} 중 하나여야 합니다")
            continue
        cues = [str(c) for c in (s.get("hold_cues") or [])]
        if mode == "every_breath" and counts > 1 and not cues:
            err(ln, f"{s['line']}: every_breath에는 hold_cues(반복할 짧은 큐 목록)가 필요합니다")
            continue
        steps.append(StepSpec(str(s["line"]), counts, str(s.get("hold", "")), mode, cues, ln))
    if problems:
        raise ScriptError(problems)
    lead = data.get("lead_in_ms")
    return GuideSpec(
        count_in_text=str(ci.get("text", "")).strip(), count_in_us=count_in_us,
        start_tone=bool(ci.get("start_tone", True)), count_us=count_us, steps=steps, tail_us=tail_us,
        lead_in_ms=int(lead) if lead is not None else None,
    )


def build_plan(spec: GuideSpec, lines: dict[str, Line], *, lead_in_ms: int, count_in_speak_ms: int = 300) -> GuidePlan:
    """각 단계의 시작 = count_in + (앞 단계 counts 합) x count_s. 내레이션은 시작 + lead_in."""
    missing = [s.line for s in spec.steps if s.line not in lines]
    if missing:
        raise ScriptError([f"guide.yaml의 line이 script.yaml에 없습니다: {missing}"])
    lead = lead_in_ms * 1000
    cursor = spec.count_in_us
    items: list[dict] = []
    if spec.count_in_text:
        items.append({"id": "count_in", "text": spec.count_in_text, "at_us": count_in_speak_ms * 1000,
                      "until_us": spec.count_in_us, "mood": "calm", "pause_after_ms": 0, "kind": "count_in"})
    planned: list[PlannedStep] = []
    for st in spec.steps:
        start = cursor
        end = start + st.counts * spec.count_us
        ln = lines[st.line]
        items.append({"id": ln.id, "text": ln.text, "at_us": start + lead, "until_us": None, "mood": ln.mood,
                      "pause_after_ms": ln.pause_after_ms, "kind": "step"})
        planned.append(PlannedStep(ln.id, st.counts, start, end, start + lead))
        if st.hold_mode == "every_breath" and st.counts > 1:
            for k in range(1, st.counts):
                cue = st.hold_cues[(k - 1) % len(st.hold_cues)]
                at = start + k * spec.count_us + lead
                items.append({"id": f"{ln.id}_h{k}", "text": cue, "at_us": at, "until_us": None, "mood": ln.mood,
                              "pause_after_ms": 0, "kind": "hold_cue"})
        cursor = end
    items.sort(key=lambda i: i["at_us"])
    total = cursor + spec.tail_us
    return GuidePlan(count_in_us=spec.count_in_us, count_us=spec.count_us,
                     tone_at_us=spec.count_in_us if spec.start_tone else None, lead_in_us=lead,
                     steps=planned, items=items, total_us=total, tail_us=spec.tail_us)
