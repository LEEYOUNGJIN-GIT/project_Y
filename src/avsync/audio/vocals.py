"""보컬(사람 목소리) 존재 가능성 휴리스틱 (6.4: 보컬 감지 시 폐기).

ML 모델이 아니다. 음성/노래는 300-3400 Hz 대역의 에너지가 음절 속도(3-8 Hz)로 크게 변조되지만, 지속음 패드나 느린
LFO는 그렇지 않다는 점만 이용한다. 알려진 한계:
- 3-8 Hz로 규칙적으로 울리는 악기(빠른 아르페지오, 16분음표 하이햇)는 오탐할 수 있다.
- 잔향이 많거나 가사 없는 허밍·보컬 패드는 놓칠 수 있다.
- 실제 보컬 음악으로는 검증하지 못했다(합성 음성·합성 패드로만 시험). 최종 판단은 청취다.
점수는 0-1이며 '폐기 후보' 선별 용도로만 쓴다.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from .wavutil import read_wav
from ..platform.ffmpeg import FFmpeg

RATE = 8000
BLOCK = 80                 # 10 ms
FPS = RATE // BLOCK        # 엔벨로프 프레임률 100 Hz
WIN, HOP = 2 * FPS, FPS    # 2초 창, 1초 간격


@dataclass
class VocalResult:
    score: float             # 0-1, 높을수록 보컬 가능성
    windows: int
    syllabic_ratio: float
    depth: float

    def as_dict(self) -> dict:
        return {"score": round(self.score, 3), "windows": self.windows,
                "syllabic_ratio": round(self.syllabic_ratio, 3), "depth": round(self.depth, 3)}


def _goertzel_power(x: list[float], f: float) -> float:
    n = len(x)
    w = 2 * math.pi * f / FPS
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    return (s1 * s1 + s2 * s2 - c * s1 * s2) / (n * n)


def score_envelope(env: list[float], *, floor: float = 1e-4) -> VocalResult:
    freqs = [0.5 * k for k in range(1, 41)]                 # 0.5-20 Hz
    wscores, ratios, depths = [], [], []
    for a in range(0, max(0, len(env) - WIN + 1), HOP):
        w = env[a:a + WIN]
        m = sum(w) / len(w)
        if m < floor:
            continue
        d = math.sqrt(sum((v - m) ** 2 for v in w) / len(w)) / m
        x = [v - m for v in w]
        p = {f: _goertzel_power(x, f) for f in freqs}
        tot = sum(p.values())
        if tot <= 0:
            continue
        ratio = sum(v for f, v in p.items() if 3.0 <= f <= 8.0) / tot
        # 변조 스펙트럼이 한 점에 몰리면(규칙적인 박자) 음절 변조가 아니라 리듬일 가능성 -> 감점
        top = max(p[f] for f in freqs if 3.0 <= f <= 8.0) / max(1e-18, sum(v for f, v in p.items() if 3.0 <= f <= 8.0))
        peaky = max(0.0, (top - 0.5) / 0.5)
        wscores.append(ratio * min(1.0, d / 0.5) * (1 - 0.7 * peaky))
        ratios.append(ratio)
        depths.append(d)
    if not wscores:
        return VocalResult(0.0, 0, 0.0, 0.0)
    wscores.sort()
    q75 = wscores[min(len(wscores) - 1, int(0.75 * len(wscores)))]      # 보컬은 곡 일부에만 있을 수 있다
    return VocalResult(q75, len(wscores), statistics.median(ratios), statistics.median(depths))


def vocal_score(ff: FFmpeg, path: Path, work: Path) -> VocalResult:
    work.mkdir(parents=True, exist_ok=True)
    wav = work / "vocal_probe.wav"
    ff.run(["-i", str(path), "-vn", "-ac", "1", "-ar", str(RATE), "-af", "highpass=f=300,lowpass=f=3400",
            "-c:a", "pcm_s16le", str(wav)], context="보컬 분석용 오디오 추출")
    pcm = read_wav(wav)
    env = []
    for b in range(pcm.frames // BLOCK):
        blk = pcm.data[b * BLOCK:(b + 1) * BLOCK]
        env.append(math.sqrt(sum(v * v for v in blk) / BLOCK) / 32768.0)
    return score_envelope(env)
