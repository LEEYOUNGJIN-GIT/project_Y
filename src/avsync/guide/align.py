"""촬영본 소리에서 가이드의 시작 톤을 찾아 오프셋을 제안한다 (3.5 보조 방법, 12.2 Phase 4).

방법: 8 kHz 모노로 낮춘 신호를 (a) 톤 주파수 좁은 대역(±60 Hz) (b) 넓은 대역(±500 Hz) 두 갈래로 5 ms 블록 에너지를
계산하고, 톤 길이만큼의 창에서 '좁은 대역 에너지가 배경 중앙값보다 크고, 넓은 대역 에너지의 대부분이 거기에 몰린'
구간을 고른다. 전체 에너지 대신 넓은 대역을 쓰는 이유: BGM·방 소음 같은 저주파 에너지가 커도 톤 판정이 흔들리지 않게.
디코드한 오디오의 0초는 영상 시간 0초와 다를 수 있으므로(stream_offset_us) 영상 시간으로 환산해 돌려준다.
순수 Python이라 의존성이 없다. 정밀도는 블록 크기(5 ms) 수준이며 실측 영상으로 검증이 필요하다.
스피커로 가이드를 짧게 틀어 영상 소리에 톤이 남아 있을 때만 동작한다(이어폰만 쓰면 톤이 녹음되지 않는다).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..audio.wavutil import read_wav
from ..platform.ffmpeg import FFmpeg

RATE = 8000
BLOCK = 40                      # 5 ms
BLOCK_US = 5000


@dataclass
class ToneHit:
    time_us: int
    score: float
    tonality: float            # 0..1, 창 안 에너지 중 톤 대역 비율
    contrast_db: float         # 배경 중앙값 대비 대역 에너지 (dB)

    def as_dict(self) -> dict:
        return {"time_us": self.time_us, "score": round(self.score, 3), "tonality": round(self.tonality, 3),
                "contrast_db": round(self.contrast_db, 1)}


def _block_energy(samples, n_blocks: int) -> list[float]:
    out = []
    for b in range(n_blocks):
        blk = samples[b * BLOCK:(b + 1) * BLOCK]
        out.append(sum(x * x for x in blk) / (len(blk) * 32768.0 * 32768.0) if blk else 0.0)
    return out


def detect_tone(band: list[float], total: list[float], tone_ms: int, *, min_score: float = 1.5,
                min_tonality: float = 0.5, floor: float = 1e-8, top: int = 3) -> list[ToneHit]:
    """블록 에너지 배열에서 톤 후보를 찾는다 (순수 계산이라 단위 테스트 가능)."""
    w = max(2, tone_ms * 1000 // BLOCK_US)
    n = len(band)
    if n < w + 2:
        return []
    cs_b = [0.0]
    cs_t = [0.0]
    for x, y in zip(band, total):
        cs_b.append(cs_b[-1] + x)
        cs_t.append(cs_t[-1] + y)
    sums = [(cs_b[i + w] - cs_b[i]) / w for i in range(n - w + 1)]
    tots = [(cs_t[i + w] - cs_t[i]) / w for i in range(n - w + 1)]
    med = statistics.median(sums)
    cands = []
    for i, (s, t) in enumerate(zip(sums, tots)):
        if s < floor:
            continue
        ton = min(1.0, s / t) if t > 0 else 0.0
        if ton < min_tonality:
            continue
        contrast = math.log10((s + floor) / (med + floor))
        cands.append((ton * contrast, i, ton, 10 * contrast))
    cands.sort(reverse=True)
    hits: list[ToneHit] = []
    taken: list[int] = []
    for score, i, ton, c_db in cands:
        if score < min_score:
            break
        if any(abs(i - j) < 100 for j in taken):          # 0.5초 안의 중복 후보 제거
            continue
        peak = max(band[i:i + w])
        onset = next((k for k in range(i, i + w) if band[k] >= 0.25 * peak), i)
        hits.append(ToneHit(onset * BLOCK_US, score, ton, c_db))
        taken.append(i)
        if len(hits) >= top:
            break
    return hits


def find_tone(ff: FFmpeg, media: Path, work: Path, *, freq_hz: float = 880.0, tone_ms: int = 100,
              bandwidth_hz: float = 120.0, wide_hz: float = 1000.0, stream_offset_us: int = 0) -> list[ToneHit]:
    work.mkdir(parents=True, exist_ok=True)
    band_wav, full_wav = work / "align_band.wav", work / "align_full.wav"
    common = ["-i", str(media)]
    ff.run([*common,
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE),
            "-af", f"bandpass=f={freq_hz}:width_type=h:w={bandwidth_hz}", "-c:a", "pcm_s16le", str(band_wav),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(RATE),
            "-af", f"bandpass=f={freq_hz}:width_type=h:w={wide_hz}", "-c:a", "pcm_s16le", str(full_wav)],
           context="align용 오디오 추출 (영상에 오디오 트랙이 필요)")
    b, f = read_wav(band_wav), read_wav(full_wav)
    n = min(b.frames, f.frames) // BLOCK
    hits = detect_tone(_block_energy(b.data, n), _block_energy(f.data, n), tone_ms)
    for h in hits:
        h.time_us += stream_offset_us
    return hits
