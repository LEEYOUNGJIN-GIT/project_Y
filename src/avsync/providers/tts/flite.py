"""FFmpeg libflite 공급자. 오프라인 파이프라인 시험용이며 제작·품질 평가에는 쓰지 않는다."""
from __future__ import annotations

from ...audio.wavutil import read_wav
from ...platform.ffmpeg import FFmpeg
from .base import ProviderError, TTSCapabilities, TTSRequest, TTSResult

_RATE = 16000


class FliteProvider:
    def __init__(self, ffmpeg: FFmpeg):
        self.ff = ffmpeg
        if not ffmpeg.has_filter("flite"):
            raise ProviderError("이 FFmpeg 빌드에는 flite 필터가 없습니다")

    def capabilities(self) -> TTSCapabilities:
        return TTSCapabilities(
            name="flite", version="ffmpeg-libflite", languages=["en-US"], speed=False,
            word_timestamps=False, emotion=False, sample_rate=_RATE, cpu=True,
            pronunciation_mode="respell", production_ok=False,
        )

    def synthesize(self, request: TTSRequest) -> TTSResult:
        # lavfi 필터 인자에서 ' : \ 는 이스케이프가 필요하다. 텍스트는 파일로 넘겨 이스케이프를 피한다.
        txt = request.out_path.with_suffix(".txt")
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        txt.write_text(request.text, encoding="utf-8")
        try:
            # 경로의 특수문자를 막기 위해 작업 디렉터리를 파일 위치로 바꿔 상대 이름만 쓴다.
            from ...platform.process import run
            res = run(
                [self.ff.tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", f"flite=textfile={txt.name}:voice=slt",
                 "-ar", str(_RATE), "-ac", "1", "-c:a", "pcm_s16le", request.out_path.name],
                cwd=request.out_path.parent,
            )
        finally:
            txt.unlink(missing_ok=True)
        if not res.ok:
            raise ProviderError(f"flite 합성 실패: {res.stderr.strip()[-300:]}")
        pcm = read_wav(request.out_path)
        return TTSResult(request.out_path, pcm.rate, pcm.duration_us, {"voice": "slt"})

    def close(self) -> None:
        pass
