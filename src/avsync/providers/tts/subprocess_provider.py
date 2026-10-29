"""별도 환경에서 도는 공급자 워커와 JSON Lines로 통신한다 (3.2).

프로토콜 (한 줄에 JSON 하나, UTF-8):
  -> {"op": "hello"}                              <- {"ok": true, "capabilities": {...}}
  -> {"op": "synthesize", "text", "voice", "speed", "lang", "out"}
                                                  <- {"ok": true, "sample_rate": int, "meta": {...}}
  -> {"op": "bye"}
오류는 {"ok": false, "error": "..."}.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

from ...audio.wavutil import read_wav
from ...platform.process import _popen_kwargs, kill_tree
from .base import ProviderError, TTSCapabilities, TTSRequest, TTSResult


class SubprocessTTSProvider:
    def __init__(self, command: Sequence[str | Path], *, cwd: Path | None = None, env: dict | None = None,
                 startup_timeout: float = 600.0):
        self.command = [str(c) for c in command]
        self.cwd = cwd
        self.env = env
        self.startup_timeout = startup_timeout
        self._proc: subprocess.Popen | None = None
        self._caps: TTSCapabilities | None = None
        self.cold_start_s: float | None = None

    def _start(self) -> None:
        import time
        t0 = time.perf_counter()
        self._proc = subprocess.Popen(  # noqa: S603 - 인자 배열
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            cwd=self.cwd, env=self.env, text=True, encoding="utf-8", bufsize=1,
            **_popen_kwargs(low_priority=True),
        )
        reply = self._call({"op": "hello"})
        self._caps = TTSCapabilities(**reply["capabilities"])
        self.cold_start_s = time.perf_counter() - t0

    def _call(self, msg: dict) -> dict:
        p = self._proc
        assert p is not None and p.stdin is not None and p.stdout is not None
        try:
            p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
        except (BrokenPipeError, OSError) as e:
            raise ProviderError(f"공급자 워커와 통신 실패: {e}") from e
        except KeyboardInterrupt:
            kill_tree(p)
            raise
        if not line:
            raise ProviderError(f"공급자 워커가 응답 없이 종료됨 (종료 코드 {p.poll()}). 위 stderr 출력을 확인하세요.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise ProviderError(f"공급자 오류: {reply.get('error')}")
        return reply

    def _ensure(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            self._start()

    def capabilities(self) -> TTSCapabilities:
        self._ensure()
        assert self._caps is not None
        return self._caps

    def synthesize(self, request: TTSRequest) -> TTSResult:
        self._ensure()
        request.out_path.parent.mkdir(parents=True, exist_ok=True)
        reply = self._call({
            "op": "synthesize", "text": request.text, "voice": request.voice_id,
            "speed": request.speed, "lang": request.lang, "out": str(request.out_path),
        })
        pcm = read_wav(request.out_path)
        return TTSResult(request.out_path, int(reply.get("sample_rate", pcm.rate)), pcm.duration_us,
                         reply.get("meta", {}))

    def close(self) -> None:
        p = self._proc
        if p is None:
            return
        try:
            if p.poll() is None and p.stdin:
                p.stdin.write(json.dumps({"op": "bye"}) + "\n")
                p.stdin.flush()
                p.wait(timeout=5)
        except Exception:
            kill_tree(p)
        finally:
            self._proc = None
