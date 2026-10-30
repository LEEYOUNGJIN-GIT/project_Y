"""비트/템포 분석 (Phase 5 음악 우선 모드). 순수 Python이며 ML이 아닌 신호처리 휴리스틱이다.

방법: 8 kHz 모노 -> 10 ms 프레임 RMS의 로그 증가량(onset 강도) -> 자기상관으로 템포 후보 -> 비트 위치 최소제곱으로
주기·위상 정밀화 -> 저음역 onset을 4박 주기로 합산해 다운비트 추정.
한계: 박자가 없는 앰비언트/드론은 '박자 없음'으로 거부한다. 변박·텍포 변화·스윙은 다루지 않는다. 합성 클릭 트랙으로만
검증했고 실제 음악 정확도는 미검증이다. 템포 오차 e는 곡 길이에 비례해 박 위치 오차(e x 길이)로 누적된다.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .wavutil import read_wav
from ..platform.ffmpeg import FFmpeg

RATE = 8000
HOP = 80                 # 10 ms
FPS = RATE // HOP        # 100
MIN_BPM, MAX_BPM = 50.0, 180.0
MIN_SECONDS = 8


@dataclass
class BeatInfo:
    bpm: float | None
    period_us: int | None
    phase_us: int | None          # 첫 비트 위치
    confidence: float             # 0-1
    beats_us: list[int] = field(default_factory=list)      # 앞쪽 일부
    downbeat_offset: int | None = None                     # beats_us 중 몇 번째(0-3)가 마디 시작인가
    notes: list[str] = field(default_factory=list)

    @property
    def has_beat(self) -> bool:
        return self.bpm is not None

    def as_dict(self) -> dict:
        return {"bpm": None if self.bpm is None else round(self.bpm, 2), "period_us": self.period_us,
                "phase_us": self.phase_us, "confidence": round(self.confidence, 3), "beats_us": self.beats_us[:16],
                "downbeat_offset": self.downbeat_offset, "notes": self.notes}


def onset_envelope(samples, hop: int = HOP) -> list[float]:
    """10 ms 프레임 에너지를 40 ms 이동평균으로 평활한 뒤 로그 증가량을 onset으로 쓴다.

    평활이 없으면 가까운 두 주파수의 맥놀이가 프레임 RMS에서 앨리어싱되어 지속음 패드에서 가짜 주기성이 생긴다
    (합성 패드가 85 BPM, 신뢰도 0.98로 오판됨 - 시험 중 발견)."""
    n = len(samples) // hop
    energy = []
    for b in range(n):
        blk = samples[b * hop:(b + 1) * hop]
        energy.append(sum(v * v for v in blk) / hop / (32768.0 ** 2))
    smooth = [sum(energy[max(0, i - 3):i + 1]) / len(energy[max(0, i - 3):i + 1]) for i in range(n)]
    logs = [math.log1p(1000.0 * math.sqrt(e)) for e in smooth]
    flux = [0.0] + [max(0.0, logs[i] - logs[i - 1]) for i in range(1, n)]
    return [(flux[max(0, i - 1)] + 2 * flux[i] + flux[min(n - 1, i + 1)]) / 4 for i in range(n)]    # 3점 평활


def _acf(x: list[float], lag: int) -> float:
    n = len(x) - lag
    return sum(x[i] * x[i + lag] for i in range(n)) / n if n > 0 else 0.0


MIN_PEAK = 0.3     # p99 onset 강도 하한(시작값). 합성 패드 <= 0.35, 클릭 >= 0.66에서 나눔


def analyze_envelope(onset: list[float], low: list[float] | None = None, fps: int = FPS, *, min_conf: float = 0.25,
                     min_peak: float = MIN_PEAK) -> BeatInfo:
    n = len(onset)
    if n < MIN_SECONDS * fps:
        return BeatInfo(None, None, None, 0.0, notes=[f"{MIN_SECONDS}초 미만이라 분석하지 않음"])
    m = sum(onset) / n
    x = [v - m for v in onset]
    lo, hi = int(fps * 60 / MAX_BPM), int(fps * 60 / MIN_BPM)
    acf = {lag: _acf(x, lag) for lag in range(max(2, lo // 2), min(n // 2, hi * 2) + 1)}
    zero = _acf(x, 0)
    srt = sorted(onset)
    peak = srt[int(0.99 * (n - 1))]
    if peak < min_peak:
        return BeatInfo(None, None, None, 0.0, notes=[f"onset 강도가 약함(p99 {peak:.2f} < {min_peak}) -> 박자 없음(지속음/앰비언트)으로 판단"])
    if zero <= 1e-12:
        return BeatInfo(None, None, None, 0.0, notes=["onset이 없음(무음/지속음)"])

    def prior(lag: int) -> float:                  # 60-120 BPM 선호 (로그 가우시안, 시작값)
        bpm = 60 * fps / lag
        return math.exp(-0.5 * (math.log2(bpm / 90.0) / 0.9) ** 2)

    cands = [(acf[l] / zero * prior(l), l) for l in range(lo, hi + 1) if l in acf and acf[l - 1] < acf[l] >= acf.get(l + 1, -1e9)] \
        if all((l - 1) in acf for l in range(lo, hi + 1)) else []
    if not cands:
        return BeatInfo(None, None, None, 0.0, notes=["주기적인 onset 후보가 없음"])
    score, lag = max(cands)
    conf = max(0.0, min(1.0, acf[lag] / zero))
    if conf < min_conf:
        return BeatInfo(None, None, None, conf, notes=[f"주기성이 약함(신뢰도 {conf:.2f} < {min_conf}) -> 박자 없음으로 판단"])

    # 포물선 보간으로 주기를 서브프레임 정밀도로
    a, b, c = acf[lag - 1], acf[lag], acf[lag + 1]
    denom = a - 2 * b + c
    period = lag + (0.5 * (a - c) / denom if denom != 0 else 0.0)

    # 위상: 주기 period의 빗(comb)에 onset을 합산해 최대인 시작점
    best_phase, best_val = 0.0, -1.0
    steps = int(period)
    for ph in range(steps):
        t, v = float(ph), 0.0
        while t < n:
            v += onset[int(t)]
            t += period
        if v > best_val:
            best_phase, best_val = float(ph), v

    # 최소제곱 정밀화: 각 예상 비트 주변(±25%)의 onset 최대 위치를 모아 t_k = phase + k*period 로 피팅
    for _ in range(2):
        ks, ts = [], []
        k = 0
        while best_phase + k * period < n - 2:
            c0 = best_phase + k * period
            a0, b0 = max(0, int(c0 - 0.25 * period)), min(n, int(c0 + 0.25 * period) + 1)
            if b0 - a0 >= 3:
                j = max(range(a0, b0), key=lambda i: onset[i])
                if onset[j] > 0.5 * statistics.mean(onset[a0:b0]) + 1e-12:
                    ks.append(k)
                    ts.append(float(j))
            k += 1
        if len(ks) < 6:
            break
        mk, mt = sum(ks) / len(ks), sum(ts) / len(ts)
        sxx = sum((q - mk) ** 2 for q in ks)
        if sxx == 0:
            break
        slope = sum((q - mk) * (t - mt) for q, t in zip(ks, ts)) / sxx
        if not (0.9 * period < slope < 1.1 * period):
            break
        period, best_phase = slope, mt - slope * mk
        while best_phase < 0:
            best_phase += period

    bpm = 60 * fps / period
    notes_alt = [f"템포 옥타브 모호성: {bpm * f:.1f} BPM일 수도 있음 (x{f:g})" for f in (0.5, 2.0) if MIN_BPM <= bpm * f <= MAX_BPM]
    to_us = lambda f: round(f * 1_000_000 / fps)       # noqa: E731
    beats = []
    t = best_phase
    while t < n and len(beats) < 64:
        beats.append(to_us(t))
        t += period
    down = None
    notes: list[str] = list(notes_alt)
    if low is not None and len(beats) >= 8:
        acc = [0.0] * 4
        for i, bt in enumerate(beats):
            c0 = int(bt * fps / 1_000_000)
            acc[i % 4] += sum(low[max(0, c0 - 3):c0 + 4])
        down = max(range(4), key=lambda q: acc[q])
        s = sorted(acc, reverse=True)
        if s[0] < 1.15 * s[1]:
            notes.append("저음역 강세 차이가 작아 다운비트 추정은 불확실함")
            down = None
    return BeatInfo(bpm, to_us(period), to_us(best_phase), conf, beats, down, notes)


BANDS = (("low", "lowpass=f=150"), ("mid", "highpass=f=150,lowpass=f=1000"), ("high", "highpass=f=1000"))


def analyze_file(ff: FFmpeg, path: Path, work: Path) -> BeatInfo:
    """3개 대역(저/중/고)으로 나눠 대역별 onset을 합산한다 - 지속음 패드가 다른 대역의 박을 가리지 못하게."""
    work.mkdir(parents=True, exist_ok=True)
    args = ["-i", str(path)]
    outs = []
    for name, flt in BANDS:
        w = work / f"beats_{name}.wav"
        outs.append(w)
        args += ["-vn", "-ac", "1", "-ar", str(RATE), "-af", flt, "-c:a", "pcm_s16le", str(w)]
    ff.run(args, context="비트 분석용 대역 분리")
    envs = [onset_envelope(read_wav(w).data) for w in outs]
    n = min(len(e) for e in envs)
    total = [sum(e[i] for e in envs) for i in range(n)]
    return analyze_envelope(total, envs[0][:n])
