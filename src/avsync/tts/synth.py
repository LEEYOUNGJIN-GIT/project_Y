"""문장 단위 합성 + 항목 단위 캐시. 한 항목을 바꿔도 다른 항목 파일의 해시는 유지된다 (12.3).

- 문장 사이에는 `pause_ms` 무음을 넣는다.
- 강조 단어(emphasis)는 그 앞뒤에서 문장을 조각내 `emphasis_pause_ms` 무음을 넣는다 (pause_only, 5.6).
- 카운트 큐 문장(모든 단어가 큐 단어)은 `cue_speed`로 합성한다 (5.6).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..audio.wavutil import Pcm, concat, read_wav, silence, trim_silence, write_wav
from ..domain.hashing import sha256_file, sha256_json
from ..domain.script import Line
from ..domain.timebase import samples_to_us
from ..providers.tts.base import ProviderError, TTSProvider, TTSRequest
from .normalize import Spoken, apply_pronunciations, is_cue_sentence, split_emphasis, split_sentences


@dataclass
class Segment:
    original: str
    normalized: str
    spoken: str
    start_us: int   # 항목 시작 기준 상대 시각
    end_us: int
    fragments: int = 1
    is_cue: bool = False
    speed: float = 1.0


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


@dataclass
class _Frag:
    sentence_index: int
    spoken: Spoken
    speed: float
    emphasized: bool


class Synthesizer:
    def __init__(self, provider: TTSProvider, cache_dir: Path, *, voice_id: str | None, lang: str,
                 pronunciations: dict[str, str], sentence_pause_ms: int, emphasis_pause_ms: int | None = None,
                 cue_speed: float | None = None, cue_words: list[str] | None = None, max_speed: float = 1.06):
        self.provider = provider
        self.cache_dir = Path(cache_dir)
        self.voice_id = voice_id
        self.lang = lang
        self.pron = pronunciations
        self.pause_ms = sentence_pause_ms
        self.emphasis_pause_ms = emphasis_pause_ms        # None이면 강조 분할 안 함
        self.cue_speed = cue_speed                        # None이면 큐 문장도 본문 속도
        self.cue_words = {w.lower() for w in list(pronunciations) + list(cue_words or [])}
        self.max_speed = max_speed
        self.caps = provider.capabilities()
        self.calls = 0       # 실제 합성 호출 수 (캐시 적중 제외). 테스트·벤치마크용
        self.synth_seconds = 0.0

    def _fragments(self, line: Line, speed: float) -> tuple[list[_Frag], list[str]]:
        frags: list[_Frag] = []
        warnings: list[str] = []
        for si, sent in enumerate(split_sentences(line.text)):
            cue = self.cue_speed is not None and is_cue_sentence(sent, self.cue_words)
            sp = min(self.cue_speed, self.max_speed) if cue else speed
            parts = [(sent, False)]
            if self.emphasis_pause_ms is not None and line.emphasis:
                parts = split_emphasis(sent, line.emphasis)
            for text, emph in parts:
                frags.append(_Frag(si, apply_pronunciations(text, self.pron, self.caps.pronunciation_mode), sp, emph))
        missing = [w for w in line.emphasis if w.lower() not in line.text.lower()]
        warnings += [f"{line.id}: emphasis '{w}'가 문구에 없음" for w in missing]
        for f in frags:
            warnings += f.spoken.warnings
        return frags, warnings

    def _key(self, frags: list[_Frag]) -> str:
        return sha256_json({
            "provider": self.caps.name, "version": self.caps.version, "voice": self.voice_id,
            "lang": self.lang, "pause_ms": self.pause_ms, "emph_ms": self.emphasis_pause_ms, "trim": "v1",
            "frags": [(f.sentence_index, f.spoken.spoken, round(f.speed, 4), f.emphasized) for f in frags],
        })[:24]

    def synth_line(self, line: Line, speed: float) -> LineAudio:
        frags, warnings = self._fragments(line, speed)
        key = self._key(frags)
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
        sent_start: dict[int, int] = {}
        sent_frags: dict[int, list[_Frag]] = {}
        sent_end: dict[int, int] = {}
        prev: _Frag | None = None
        for i, fr in enumerate(frags):
            out = tmp_dir / f"{key}_{i}.wav"
            t0 = time.perf_counter()
            res = self.provider.synthesize(TTSRequest(fr.spoken.spoken, self.voice_id, fr.speed, self.lang, out))
            self.synth_seconds += time.perf_counter() - t0
            self.calls += 1
            pcm = trim_silence(read_wav(res.wav_path))
            out.unlink(missing_ok=True)
            if pcm.frames == 0:
                raise ProviderError(f"{line.id}: 빈 오디오가 합성됨 ({fr.spoken.spoken!r})")
            if rate is None:
                rate = pcm.rate
            if prev is not None:
                same_sentence = prev.sentence_index == fr.sentence_index
                gap_ms = (self.emphasis_pause_ms or 0) if same_sentence else self.pause_ms
                gap = silence(rate, pcm.channels, gap_ms * 1000)
                parts.append(gap)
                cursor += gap.frames
            sent_start.setdefault(fr.sentence_index, cursor)
            cursor += pcm.frames
            sent_end[fr.sentence_index] = cursor
            sent_frags.setdefault(fr.sentence_index, []).append(fr)
            parts.append(pcm)
            prev = fr
        for si in sorted(sent_start):
            fs = sent_frags[si]
            cue = self.cue_speed is not None and is_cue_sentence(" ".join(f.spoken.original for f in fs), self.cue_words)
            segs.append(Segment(
                original=" ".join(f.spoken.original for f in fs), normalized=" ".join(f.spoken.normalized for f in fs),
                spoken=" ".join(f.spoken.spoken for f in fs), start_us=samples_to_us(sent_start[si], rate),
                end_us=samples_to_us(sent_end[si], rate), fragments=len(fs), is_cue=cue, speed=fs[0].speed))
        merged = concat(parts)
        write_wav(wav, merged)
        sha = sha256_file(wav)
        meta.write_text(json.dumps({
            "line_id": line.id, "duration_us": merged.duration_us, "sample_rate": merged.rate,
            "segments": [asdict(s) for s in segs], "wav_sha256": sha, "speed": speed,
            "provider": self.caps.name, "voice": self.voice_id,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return LineAudio(line.id, wav, merged.duration_us, merged.rate, speed, segs, key, sha, False, warnings)
