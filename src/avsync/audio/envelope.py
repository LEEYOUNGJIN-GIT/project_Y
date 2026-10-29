"""구간 기반 이득 곡선(덕킹·원본 소리 제어용). 내레이션 위치를 이미 알고 있으므로 곡선을 직접 만든다.

16-bit 스테레오 WAV로 쓰고 FFmpeg `amultiply`로 곱한다. 값 v는 이득 v/32768 이므로
최대 이득은 약 0.99997(-0.0003 dB)이다.
"""
from __future__ import annotations

from array import array
from pathlib import Path

from ..domain.timebase import us_to_samples
from .wavutil import Pcm, write_wav

_MAX = 32767


def _ramp(a: float, b: float, n: int) -> array:
    if n <= 0:
        return array("h")
    return array("h", (round((a + (b - a) * (i + 1) / n) * _MAX) for i in range(n)))


def merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(windows):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def build_envelope(path: Path, *, duration_us: int, rate: int, base_gain: float, window_gain: float,
                   windows: list[tuple[int, int]], attack_us: int, release_us: int) -> list[tuple[int, int]]:
    """base_gain 위에서 windows 구간만 window_gain이 되는 곡선을 만든다.

    attack은 구간 시작 *직전*에 끝나도록(말이 시작할 때 이미 낮아져 있도록), release는 구간 끝에서 시작한다.
    반환값: 실제 적용된 병합 구간 목록(us).
    """
    total = us_to_samples(duration_us, rate)
    merged = merge_windows([(max(0, a), min(duration_us, b)) for a, b in windows if b > a])
    mono = array("h")
    cursor = 0
    base_v = round(base_gain * _MAX)
    win_v = round(window_gain * _MAX)
    atk = us_to_samples(attack_us, rate)
    rel = us_to_samples(release_us, rate)

    def fill(until: int, v: int) -> None:
        nonlocal cursor
        n = until - cursor
        if n > 0:
            mono.extend(array("h", [v]) * n)
            cursor += n

    for a, b in merged:
        sa, sb = us_to_samples(a, rate), us_to_samples(b, rate)
        atk_start = max(cursor, sa - atk)
        fill(atk_start, base_v)
        n_atk = sa - cursor
        if n_atk > 0:
            r = _ramp(base_gain, window_gain, n_atk)
            mono.extend(r)
            cursor += len(r)
        fill(sb, win_v)
        n_rel = min(rel, total - cursor)
        if n_rel > 0:
            r = _ramp(window_gain, base_gain, n_rel)
            mono.extend(r)
            cursor += len(r)
    fill(total, base_v)
    del mono[total:]
    stereo = array("h", bytes(4 * total))
    stereo[0::2] = mono
    stereo[1::2] = mono
    write_wav(path, Pcm(rate, 2, stereo))
    return merged
