"""check-script: 문법·시각·글자 수 예산·발음 사전 검증 (9장)."""
from __future__ import annotations

from .domain.script import Script, count_words
from .domain.timebase import fmt_time


def check_script(script: Script, tone_tts: dict, wps: list[float], *, budgets_us: dict[str, int | None],
                 audio_first: bool) -> list[dict]:
    out: list[dict] = []

    def add(level, line_id, msg):
        out.append({"level": level, "id": line_id, "message": msg})

    lo, hi = wps
    max_cue = tone_tts.get("max_cue_words")
    moods = tone_tts.get("moods") or {}
    for ln in script.lines:
        words = count_words(ln.text)
        if max_cue and words > max_cue:
            add("warn", ln.id, f"{words}단어 > 프리셋 max_cue_words({max_cue}). 자세당 큐는 짧게 (4.3)")
        budget = budgets_us.get(ln.id)
        if budget:
            sec = budget / 1e6
            if words > sec * hi:
                add("warn", ln.id, f"{words}단어 / 허용 {sec:.1f}s: 추정 상한({hi}단어/초 -> {int(sec * hi)}단어) 초과, "
                                   f"속도 상한 안에서 맞을 가능성이 낮음")
            elif words > sec * lo:
                add("info", ln.id, f"{words}단어 / 허용 {sec:.1f}s: 추정 범위 경계({lo}-{hi}단어/초). 합성 후 확인")
        if ln.mood not in moods:
            add("warn", ln.id, f"mood '{ln.mood}'은 프리셋에 없음 -> 배율 1.0")
        if ln.emphasis:
            add("warn", ln.id, "emphasis는 MVP에서 미구현이라 무시됨")
        if ln.at_us is not None and audio_first:
            add("info", ln.id, f"audio-first에서는 at({fmt_time(ln.at_us)})이 무시되고 guide.yaml이 시각을 정함")
    used = " ".join(l.text.lower() for l in script.lines)
    for w in script.pronunciations:
        if w.lower() not in used:
            add("info", None, f"발음 사전 항목 '{w}'가 어느 문장에도 쓰이지 않음")
    if not script.approved:
        add("info", None, "approved: false - 미리듣기만 가능, 최종 렌더/촬영용 가이드는 승인 후")
    return out
