"""문장 단위 SRT (8장). 시간은 TTS 실제 길이에서 나온다."""
from __future__ import annotations

from pathlib import Path

from ..domain.timebase import fmt_srt


def build_srt(fit_records: list[dict], *, shift_us: int = 0) -> str:
    cues: list[tuple[int, int, str]] = []
    for r in fit_records:
        for seg in r["audio"]["segments"]:
            cues.append((r["at_us"] + seg["start_us"] + shift_us, r["at_us"] + seg["end_us"] + shift_us,
                         seg["original"]))
    cues.sort()
    out = []
    n = 0
    for a, b, text in cues:
        if b <= 0:
            continue
        n += 1
        out.append(f"{n}\n{fmt_srt(max(0, a))} --> {fmt_srt(b)}\n{text}\n")
    return "\n".join(out)


def write_srt(path: Path, fit_records: list[dict], *, shift_us: int = 0) -> None:
    Path(path).write_text(build_srt(fit_records, shift_us=shift_us), encoding="utf-8")
