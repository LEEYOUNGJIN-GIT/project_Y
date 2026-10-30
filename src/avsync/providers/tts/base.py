"""TTS 공급자 인터페이스 (3.2)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class TTSCapabilities:
    name: str
    version: str
    languages: list[str]
    speed: bool                  # 숫자 속도 파라미터 지원
    word_timestamps: bool
    emotion: bool
    sample_rate: int
    cpu: bool
    # "phoneme_markup": [word](/ipa/) 식 표기, "respell": 철자 치환만 가능, "none"
    pronunciation_mode: str = "respell"
    production_ok: bool = True   # False면 파이프라인 시험 전용
    network: bool = False        # True면 호출마다 외부로 텍스트가 나가고 한도가 있을 수 있음

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TTSRequest:
    text: str
    voice_id: str | None
    speed: float
    lang: str
    out_path: Path


@dataclass
class TTSResult:
    wav_path: Path
    sample_rate: int
    duration_us: int
    meta: dict = field(default_factory=dict)


class TTSProvider(Protocol):
    def capabilities(self) -> TTSCapabilities: ...

    def synthesize(self, request: TTSRequest) -> TTSResult: ...

    def close(self) -> None: ...


class ProviderError(RuntimeError):
    pass


class RateLimitError(ProviderError):
    """한도/일시 차단. 재시도 횟수를 다 써도 계속되면 올린다 (fail closed)."""
