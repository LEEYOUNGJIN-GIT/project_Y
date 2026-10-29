"""테스트용 결정적 가짜 TTS: 글자 수에 비례하는 길이의 사인파 + 앞뒤 무음."""
from __future__ import annotations

import math
from array import array
from pathlib import Path

from avsync.audio.wavutil import Pcm, write_wav
from avsync.providers.tts.base import TTSCapabilities, TTSRequest, TTSResult

RATE = 24000


class FakeProvider:
    def __init__(self, sec_per_char: float = 0.06, speed_support: bool = True, lead_ms: int = 100, words: bool = False):
        self.words = words
        self.sec_per_char = sec_per_char
        self.speed_support = speed_support
        self.lead_ms = lead_ms
        self.texts: list[str] = []
        self.speeds: list[float] = []

    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(name="fake", version="1", languages=["en-US"], speed=self.speed_support,
                               word_timestamps=False, emotion=False, sample_rate=RATE, cpu=True,
                               pronunciation_mode="respell")

    def synthesize(self, req: TTSRequest) -> TTSResult:
        self.texts.append(req.text)
        self.speeds.append(req.speed)
        speed = req.speed if self.speed_support else 1.0
        n = round(len(req.text) * self.sec_per_char / speed * RATE)
        body = array("h", (int(9000 * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(n)))
        lead = array("h", bytes(2 * int(RATE * self.lead_ms / 1000)))
        pcm = Pcm(RATE, 1, lead + body + lead)
        write_wav(req.out_path, pcm)
        meta = {}
        if self.words:                                  # 글자 수에 비례해 단어를 배치 (합성 파일 기준 시각, 앞 무음 포함)
            t = self.lead_ms / 1000
            ws = []
            for w in req.text.split():
                d = len(w) * self.sec_per_char / speed
                ws.append({"text": w, "start": t, "end": t + d})
                t += d + self.sec_per_char / speed
            meta["words"] = ws
        return TTSResult(req.out_path, RATE, pcm.duration_us, meta)

    def close(self) -> None:
        pass
