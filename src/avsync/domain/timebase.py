"""시간 표현. 모든 시간은 정수 마이크로초(us), 샘플 위치는 정수 샘플로 다룬다."""
from __future__ import annotations

import re
from fractions import Fraction

US = 1_000_000

_TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+)(?:\.(\d{1,6}))?$")
_SEC_RE = re.compile(r"^(\d+)(?:\.(\d{1,6}))?$")
_FRAME_RE = re.compile(r"^(\d+)f$")


class TimeError(ValueError):
    pass


def _frac_to_us(frac: str | None) -> int:
    if not frac:
        return 0
    return int(frac.ljust(6, "0"))


def parse_time(value, *, fps: Fraction | None = None) -> int:
    """`MM:SS.s`, `HH:MM:SS.s`, `SS.s`, 숫자(초), `97f`(프레임, fps 필요) -> 정수 us.

    음수는 앞에 `-`를 붙인다. 소수부는 최대 6자리까지 받는다(그 이상은 오류).
    """
    if isinstance(value, bool):
        raise TimeError(f"시간 형식이 아닙니다: {value!r}")
    if isinstance(value, int):
        return value * US
    if isinstance(value, float):
        return round(value * US)
    s = str(value).strip()
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    m = _FRAME_RE.match(s)
    if m:
        if fps is None:
            raise TimeError(f"프레임 표기 {value!r}에는 fps가 필요합니다")
        us = round(Fraction(int(m.group(1))) / fps * US)
        return -us if neg else us
    m = _TIME_RE.match(s)
    if m:
        h, mi, se, frac = m.groups()
        total = (int(h or 0) * 3600 + int(mi) * 60 + int(se)) * US + _frac_to_us(frac)
        return -total if neg else total
    m = _SEC_RE.match(s)
    if m:
        total = int(m.group(1)) * US + _frac_to_us(m.group(2))
        return -total if neg else total
    raise TimeError(f"시간 형식이 아닙니다: {value!r} (예: 00:04.5, 12.5, 97f)")


def fmt_time(us: int) -> str:
    sign = "-" if us < 0 else ""
    us = abs(us)
    total_ms, rem = divmod(us, 1000)
    if rem >= 500:
        total_ms += 1
    s, ms = divmod(total_ms, 1000)
    m, s = divmod(s, 60)
    return f"{sign}{m:02d}:{s:02d}.{ms:03d}"


def fmt_srt(us: int) -> str:
    total_ms = max(0, (us + 500) // 1000)
    s, ms = divmod(total_ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def us_to_samples(us: int, rate: int) -> int:
    """반올림. 정수 연산만 사용한다."""
    sign = -1 if us < 0 else 1
    return sign * ((abs(us) * rate + US // 2) // US)


def samples_to_us(n: int, rate: int) -> int:
    sign = -1 if n < 0 else 1
    return sign * ((abs(n) * US + rate // 2) // rate)


def seconds_to_us(sec: float) -> int:
    return round(sec * US)


def us_to_s(us: int) -> float:
    return us / US


def parse_fps(text: str) -> Fraction:
    """ffprobe의 `30000/1001` 형식."""
    if "/" in text:
        n, d = text.split("/", 1)
        d_i = int(d)
        return Fraction(int(n), d_i) if d_i else Fraction(0)
    return Fraction(text)
