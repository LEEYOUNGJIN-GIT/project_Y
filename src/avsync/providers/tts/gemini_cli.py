"""Gemini TTS 공급자: 공식 `gemini-api` CLI(google-gemini/gemini-api-cli, Apache-2.0)를 감싼다 (5.7).

왜 REST가 아니라 CLI인가: 이 개발 환경에서 ai.google.dev 문서를 열 수 없어 현재 요청 형식을 확인하지 못했다.
CLI README는 요청 형식을 CLI가 책임지고, 명령(`gemini-api tts`), 플래그(`-f`, `--voice`, `--out`), 종료 코드
(0 성공, 1 런타임/API 오류, 2 사용법 오류, 3 인증 오류)를 문서화한다. 이 계약만 사용한다.

지키는 것:
- API 키는 이 도구가 읽거나 저장하지 않는다. CLI가 환경변수/키체인에서 읽는다. 오류 문구의 키 모양 문자열은 지운다.
- 전송되는 것은 합성할 대본 문장뿐이다. 영상·음성·brief는 보내지 않는다.
- free_only(기본 true): 사용자가 `avsync gemini confirm-free --yes`로 '결제 미연결 프로젝트의 키'임을 확인하기 전에는 거부.
- 한도 오류는 재시도를 제한하고 RateLimitError로 중단한다. 한 영상 중간에 음성을 바꾸지 않기 위해 자동 폴백은 하지 않는다.
- 숫자 속도 파라미터가 없으므로 capabilities.speed=False. 길이 조절은 문구 단축 제안으로 대체된다.
- 출력 형식(WAV/MP3 등)과 샘플레이트는 가정하지 않고 FFmpeg로 PCM16 WAV로 변환해 읽는다.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Sequence

from ...audio.wavutil import read_wav
from ...platform.ffmpeg import FFmpeg
from ...platform.process import run
from .base import ProviderError, RateLimitError, TTSCapabilities, TTSRequest, TTSResult

_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{16,}|(?i:api[_-]?key)\s*[=:]\s*\S+")
_RATE_RE = re.compile(r"429|rate.?limit|quota|resource.?exhausted|too many requests|unavailable|overloaded", re.I)


def redact(text: str) -> str:
    return _KEY_RE.sub("[REDACTED]", text)


def find_cli(configured: str | None) -> list[str]:
    cand = configured or os.environ.get("AVSYNC_GEMINI_CLI") or shutil.which("gemini-api")
    if not cand:
        raise ProviderError("gemini-api CLI를 찾을 수 없습니다. 설치: https://github.com/google-gemini/gemini-api-cli "
                            "(README의 설치 안내) 후 PATH에 두거나 환경변수 AVSYNC_GEMINI_CLI에 경로를 지정하세요")
    return [cand]


class GeminiCliTTSProvider:
    def __init__(self, ffmpeg: FFmpeg, *, command: Sequence[str], ack_path: Path | None, free_only: bool = True,
                 model: str | None = None, style_prefix: str = "", max_retries: int = 2, timeout_s: float = 120.0,
                 backoff_s: float = 2.0):
        self.ff = ffmpeg
        self.command = [str(c) for c in command]
        self.ack_path = ack_path
        self.free_only = free_only
        self.model = model
        self.style_prefix = style_prefix
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.backoff_s = backoff_s
        self._version: str | None = None
        self.attempts = 0

    def _check_ack(self) -> None:
        if self.free_only and not (self.ack_path and Path(self.ack_path).is_file()):
            raise ProviderError(
                "Gemini 무료 등급 확인이 없습니다 (free_only). 결제가 연결되지 않은 프로젝트에서 발급한 키인지 확인한 뒤 "
                "`avsync gemini confirm-free --yes`를 실행하세요. 결제 연결 프로젝트의 키를 쓰면 과금될 수 있습니다")

    def capabilities(self) -> TTSCapabilities:
        if self._version is None:
            r = run([*self.command, "version"], timeout=30)
            self._version = (r.stdout.strip().splitlines() or ["unknown"])[0] if r.ok else "unknown"
        return TTSCapabilities(
            name="gemini-cli", version=f"{self._version}|model={self.model or 'cli-default'}|style={self.style_prefix!r}",
            languages=["en-US"], speed=False, word_timestamps=False, emotion=False, sample_rate=24000, cpu=True,
            pronunciation_mode="respell", network=True)

    def synthesize(self, request: TTSRequest) -> TTSResult:
        self._check_ack()
        if not request.voice_id:
            raise ProviderError("Gemini voice_id가 정해지지 않았습니다. `gemini-api tts --help`로 음성을 확인하고 script.yaml에 지정하세요")
        work = Path(tempfile.mkdtemp(prefix="avsync_gemini_"))
        try:
            txt = work / "in.txt"
            txt.write_text(self.style_prefix + request.text, encoding="utf-8")
            outdir = work / "out"
            outdir.mkdir()
            args = [*self.command, "tts", "-f", str(txt), "--voice", request.voice_id, "--out", str(outdir)]
            if self.model:
                args += ["-m", self.model]
            written = self._call_with_retries(args, outdir)
            request.out_path.parent.mkdir(parents=True, exist_ok=True)
            # 형식·샘플레이트를 가정하지 않는다: 어떤 입력이든 PCM16 모노 WAV로 (원래 샘플레이트 유지)
            self.ff.run(["-i", str(written), "-vn", "-ac", "1", "-c:a", "pcm_s16le", str(request.out_path)],
                        context="Gemini 출력 변환")
            pcm = read_wav(request.out_path)
            return TTSResult(request.out_path, pcm.rate, pcm.duration_us,
                             {"voice": request.voice_id, "cli_attempts": self.attempts})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _call_with_retries(self, args: list[str], outdir: Path) -> Path:
        last = ""
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            r = run(args, timeout=self.timeout_s)
            err = redact(r.stderr.strip()[-400:])
            if r.returncode == 0:
                return self._written_file(r.stdout, outdir)
            if r.returncode == 3:
                raise ProviderError(f"Gemini 인증 실패(종료 코드 3). 키 설정을 확인하세요 (`gemini-api whoami`): {err}")
            if r.returncode == 2:
                raise ProviderError(f"gemini-api 사용법/검증 오류(종료 코드 2): {err}")
            last = err or redact(r.stdout.strip()[-200:])
            if r.returncode == 1 and _RATE_RE.search(last) and attempt < self.max_retries:
                time.sleep(self.backoff_s * (2 ** attempt))
                continue
            break
        if _RATE_RE.search(last):
            raise RateLimitError(
                f"Gemini 한도/일시 차단으로 중단: {last}\n이미 합성된 항목은 캐시에 남아 있어, 나중에 같은 명령을 다시 실행하면 이어서 합니다. "
                f"다른 공급자로 바꾸려면 한 영상의 음성이 섞이지 않도록 전체를 다시 합성해야 합니다 (--provider kokoro).")
        raise ProviderError(f"gemini-api 실패(종료 코드 {r.returncode}): {last}")

    @staticmethod
    def _written_file(stdout: str, outdir: Path) -> Path:
        for line in reversed(stdout.strip().splitlines()):
            p = Path(line.strip())
            if p.is_file():
                return p
        files = [p for p in outdir.iterdir() if p.is_file()]
        if len(files) == 1:
            return files[0]
        raise ProviderError(f"gemini-api가 쓴 파일을 찾지 못했습니다 (stdout={redact(stdout.strip()[-200:])!r})")

    def close(self) -> None:
        pass
