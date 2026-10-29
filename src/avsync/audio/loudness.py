"""음량·피크 측정. FFmpeg 로그를 파싱한다."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..platform.ffmpeg import FFmpeg


@dataclass
class Loudness:
    integrated: float | None     # LUFS
    true_peak: float | None      # dBTP
    lra: float | None


def _f(m) -> float | None:
    if not m:
        return None
    v = float(m.group(1))
    return None if v in (float("-inf"),) else v


def measure_ebur128(ff: FFmpeg, path: Path) -> Loudness:
    res = ff.run_raw(["-i", str(path), "-af", "ebur128=peak=true", "-f", "null", "-"],
                     context="ebur128 측정", loglevel="info")
    tail = res.stderr[res.stderr.rfind("Summary:"):] if "Summary:" in res.stderr else res.stderr
    num = r"(-?inf|-?\d+(?:\.\d+)?)"
    i = re.search(rf"\bI:\s+{num}\s+LUFS", tail)
    pk = re.search(rf"Peak:\s+{num}\s+dBFS", tail)
    lra = re.search(rf"\bLRA:\s+{num}\s+LU\b", tail)

    def conv(m):
        if not m:
            return None
        s = m.group(1)
        return None if "inf" in s else float(s)
    return Loudness(conv(i), conv(pk), conv(lra))


def measure_peak_db(ff: FFmpeg, path: Path) -> float | None:
    """전체 샘플 피크(dBFS). clipping 판정용."""
    res = ff.run_raw(["-i", str(path), "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
                     context="astats 측정", loglevel="info")
    overall = res.stderr[res.stderr.rfind("Overall"):] if "Overall" in res.stderr else ""
    m = re.search(r"Peak level dB:\s+(-?\d+(?:\.\d+)?|-inf)", overall)
    if not m or "inf" in m.group(1):
        return None
    return float(m.group(1))


def measure_rms_db(ff: FFmpeg, path: Path, start_us: int, duration_us: int) -> float | None:
    res = ff.run_raw(["-ss", f"{start_us / 1e6:.6f}", "-t", f"{duration_us / 1e6:.6f}", "-i", str(path),
                      "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
                     context="구간 RMS 측정", loglevel="info")
    overall = res.stderr[res.stderr.rfind("Overall"):] if "Overall" in res.stderr else ""
    m = re.search(r"RMS level dB:\s+(-?\d+(?:\.\d+)?|-inf)", overall)
    if not m or "inf" in m.group(1):
        return None
    return float(m.group(1))


def master_normalize(ff: FFmpeg, src: Path, dst: Path, *, target_i: float, target_tp: float,
                     limiter_budget_db: float, ceiling_offset_db: float = 0.5, codec: str = "pcm_s16le",
                     tolerance_lu: float = 0.15, max_iterations: int = 3) -> dict:
    """측정 후 선형 이득 + 피크 리미터 (5.4-6의 2-pass 정규화).

    FFmpeg loudnorm의 동적 폴백은 이득이 시간에 따라 출렁여 덕킹 깊이를 바꾸므로 쓰지 않는다.
    목표 음량에 필요한 이득이 피크 한도를 넘으면 리미터가 줄일 수 있는 양을 limiter_budget_db로 제한한다.
    예산을 다 써도 목표에 못 미치면 reached_target=False로 보고한다(조용하게 두고 숨기지 않는다).
    """
    m = measure_ebur128(ff, src)
    if m.integrated is None or m.true_peak is None:
        raise RuntimeError("입력이 무음이라 음량 정규화를 할 수 없습니다")
    gain_target = target_i - m.integrated
    gain_cap = (target_tp - m.true_peak) + limiter_budget_db      # 리미터 예산까지 허용한 최대 이득
    gain = min(gain_target, gain_cap)
    ceiling = 10 ** ((target_tp - ceiling_offset_db) / 20)
    out = None
    iters = 0
    for iters in range(1, max_iterations + 1):
        af = f"volume={gain:.3f}dB,alimiter=limit={ceiling:.6f}:attack=5:release=60:level=disabled"
        ff.run(["-i", str(src), "-af", af, "-ar", "48000", "-c:a", codec, str(dst)], context="마스터 정규화")
        out = measure_ebur128(ff, dst)
        err = target_i - (out.integrated if out.integrated is not None else target_i)
        if err <= tolerance_lu or gain >= gain_cap - 1e-6:
            break
        gain = min(gain + err, gain_cap)                              # 리미터가 깎은 만큼 보상
    reached = out.integrated is not None and abs(out.integrated - target_i) <= max(tolerance_lu, 0.5)
    return {"method": "linear_gain_plus_limiter", "input_i": m.integrated, "input_tp": m.true_peak,
            "gain_db": round(gain, 2), "limiter_budget_db": limiter_budget_db,
            "limited": gain > (target_tp - m.true_peak) + 1e-6, "iterations": iters,
            "output_i": out.integrated, "output_tp": out.true_peak, "output_lra": out.lra,
            "reached_target": reached}
