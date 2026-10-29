"""16-bit PCM WAV 입출력. 외부 의존 없이 표준 라이브러리만 쓴다."""
from __future__ import annotations

import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from ..domain.timebase import samples_to_us, us_to_samples


@dataclass
class Pcm:
    rate: int
    channels: int
    data: array  # 'h', 인터리브

    @property
    def frames(self) -> int:
        return len(self.data) // self.channels

    @property
    def duration_us(self) -> int:
        return samples_to_us(self.frames, self.rate)


def read_wav(path: Path) -> Pcm:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"16-bit PCM WAV만 지원합니다: {path} (sampwidth={w.getsampwidth()})")
        raw = w.readframes(w.getnframes())
        arr = array("h")
        arr.frombytes(raw)
        if sys.byteorder == "big":
            arr.byteswap()
        return Pcm(w.getframerate(), w.getnchannels(), arr)


def write_wav(path: Path, pcm: Pcm) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    arr = array("h", pcm.data)
    if sys.byteorder == "big":
        arr.byteswap()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.channels)
        w.setsampwidth(2)
        w.setframerate(pcm.rate)
        w.writeframes(arr.tobytes())


def silence(rate: int, channels: int, duration_us: int) -> Pcm:
    n = us_to_samples(duration_us, rate)
    return Pcm(rate, channels, array("h", bytes(2 * n * channels)))


def concat(parts: list[Pcm]) -> Pcm:
    if not parts:
        raise ValueError("이어 붙일 오디오가 없습니다")
    rate, ch = parts[0].rate, parts[0].channels
    out = array("h")
    for p in parts:
        if (p.rate, p.channels) != (rate, ch):
            raise ValueError(f"샘플레이트/채널이 다릅니다: {(p.rate, p.channels)} != {(rate, ch)}")
        out.extend(p.data)
    return Pcm(rate, ch, out)


def trim_silence(pcm: Pcm, *, threshold_db: float = -45.0, keep_head_ms: int = 20, keep_tail_ms: int = 60) -> Pcm:
    """앞뒤 무음을 잘라 항목의 `at`이 '발화 시작'을 뜻하게 한다. 공급자마다 앞뒤 무음 길이가 달라서 필요하다."""
    thr = int(32768 * 10 ** (threshold_db / 20))
    ch = pcm.channels
    data = pcm.data
    n = pcm.frames
    first = next((i for i in range(n) if any(abs(data[i * ch + c]) > thr for c in range(ch))), None)
    if first is None:
        return pcm
    last = next(i for i in range(n - 1, -1, -1) if any(abs(data[i * ch + c]) > thr for c in range(ch)))
    head = int(pcm.rate * keep_head_ms / 1000)
    tail = int(pcm.rate * keep_tail_ms / 1000)
    a, b = max(0, first - head), min(n, last + 1 + tail)
    return Pcm(pcm.rate, ch, array("h", data[a * ch:b * ch]))
