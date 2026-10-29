"""문장 단위 합성 + 항목 단위 캐시. 한 항목을 바꿔도 다른 항목 파일의 해시는 유지된다 (12.3)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..audio.wavutil import Pcm, concat, read_wav, silence, trim_silence, write_wav
from ..domain.hashing import sha256_file, sha256_json
from ..domain.script import Line
from ..domain.timebase import samples_to_us
from ..providers.tts.base import ProviderError, TTSProvider, TTSRequest
from .normalize import Spoken, apply_pronunciations, split_sentences


@dataclass
class Segment:
    original: str
    normalized: str
    spoken: str
    start_us: int   # 항목 시작 기준 상대 시각
    end_us: int


@dataclass
class LineAudio:
    line_id: str
    wav_path: Path
    duration_us: int
    sample_rate: int
    speed: float
    segments: list[Segment]
    cache_key: str
    wav_sha256: str
    from_cache: bool
    warnings: list[str] = field(default_factory=list)

    def as_manifest(self) -> dict:
        d = asdict(self)
        d["wav_path"] = str(self.wav_path)
        return d


class Synthesizer:
    def __init__(self, provider: TTSProvider, cache_dir: Path, *, voice_id: str | None, lang: str,
                 pronunciations: dict[str, str], sentence_pause_ms: int):
        self.provider = provider
        self.cache_dir = Path(cache_dir)
        self.voice_id = voice_id
        self.lang = lang
        self.pron = pronunciations
        self.pause_ms = sentence_pause_ms
        self.caps = provider.capabilities()
        self.calls = 0       # 실제 합성 호출 수 (캐시 적중 제외). 테스트·벤치마크용
        self.synth_seconds = 0.0

    def _key(self, spoken: list[Spoken], speed: float) -> str:
        return sha256_json({
            "provider": self.caps.name, "version": self.caps.version, "voice": self.voice_id,
            "lang": self.lang, "speed": round(speed, 4), "pause_ms": self.pause_ms, "trim": "v1",
            "sentences": [s.spoken for s in spoken],
        })[:24]

    def synth_line(self, line: Line, speed: float) -> LineAudio:
        sentences = split_sentences(line.text)
        spoken = [apply_pronunciations(s, self.pron, self.caps.pronunciation_mode) for s in sentences]
        warnings = [w for s in spoken for w in s.warnings]
        key = self._key(spoken, speed)
        wav = self.cache_dir / f"{key}.wav"
        meta = self.cache_dir / f"{key}.json"
        if wav.is_file() and meta.is_file():
            m = json.loads(meta.read_text(encoding="utf-8"))
            segs = [Segment(**s) for s in m["segments"]]
            return LineAudio(line.id, wav, m["duration_us"], m["sample_rate"], speed, segs, key,
                             m["wav_sha256"], True, warnings)

        import time
        parts: list[Pcm] = []
        segs: list[Segment] = []
        cursor = 0                      # 샘플 단위로 누적한다 (반올림된 us를 더하면 오차가 쌓인다)
        rate = None
        tmp_dir = self.cache_dir / "_tmp"
        for i, sp in enumerate(spoken):
            out = tmp_dir / f"{key}_{i}.wav"
            t0 = time.perf_counter()
            res = self.provider.synthesize(TTSRequest(sp.spoken, self.voice_id, speed, self.lang, out))
            self.synth_seconds += time.perf_counter() - t0
            self.calls += 1
            pcm = trim_silence(read_wav(res.wav_path))
            out.unlink(missing_ok=True)
            if pcm.frames == 0:
                raise ProviderError(f"{line.id}: 빈 오디오가 합성됨 ({sp.spoken!r})")
            if rate is None:
                rate = pcm.rate
            if i > 0:
                gap = silence(rate, pcm.channels, self.pause_ms * 1000)
                parts.append(gap)
                cursor += gap.frames
            start = cursor
            cursor += pcm.frames
            segs.append(Segment(sp.original, sp.normalized, sp.spoken, samples_to_us(start, rate),
                                samples_to_us(cursor, rate)))
            parts.append(pcm)
        merged = concat(parts)
        write_wav(wav, merged)
        sha = sha256_file(wav)
        meta.write_text(json.dumps({
            "line_id": line.id, "duration_us": merged.duration_us, "sample_rate": merged.rate,
            "segments": [asdict(s) for s in segs], "wav_sha256": sha, "speed": speed,
            "provider": self.caps.name, "voice": self.voice_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return LineAudio(line.id, wav, merged.duration_us, merged.rate, speed, segs, key, sha, False, warnings)
