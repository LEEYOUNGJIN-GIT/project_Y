"""시험용 합성 박자 신호 생성기 (표준 라이브러리만)."""
import math
import random
import struct
import wave
from pathlib import Path

RATE = 8000


def click_track(bpm, seconds=30, phase=0.37, accent=True, noise=0.0, seed=1, amp_scale=1.0):
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    out = [0.0] * n
    period = 60 / bpm
    k = 0
    while phase + k * period < seconds - 0.1:
        i0 = int((phase + k * period) * RATE)
        lo = (k % 4 == 0) and accent
        f, amp, dec = (80, 0.9, 25) if lo else (1200, 0.5, 90)
        for j in range(int(0.12 * RATE)):
            if i0 + j < n:
                out[i0 + j] += amp * amp_scale * math.sin(2 * math.pi * f * j / RATE) * math.exp(-dec * j / RATE)
        k += 1
    return [int(32767 * max(-1, min(1, v + noise * rnd.uniform(-1, 1)))) for v in out]


def tones(freqs, seconds=30, lfo=0.3):
    n = int(seconds * RATE)
    return [int(9000 * sum(math.sin(2 * math.pi * f * i / RATE) for f in freqs) / len(freqs)
                * (1 + lfo * math.sin(2 * math.pi * 0.2 * i / RATE))) for i in range(n)]


def mix(a, b):
    return [max(-32767, min(32767, x + y)) for x, y in zip(a, b)]


def write(path: Path, samples, rate=RATE) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", v) for v in samples))
    return path
