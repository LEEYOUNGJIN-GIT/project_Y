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
import os
import subprocess
from pathlib import Path
from typing import Sequence

from ...audio.wavutil import read_wav
from ...platform.process import _popen_kwargs, kill_tree
from .base import ProviderError, TTSCapabilities, TTSRequest, TTSResult


def _is_reply(line: str) -> bool:
    """프로토콜 응답은 'ok' 키가 있는 JSON 객체다. 라이브러리가 찍은 `{'a': 1}` 같은 줄은 응답이 아니다."""
    if not line.lstrip().startswith("{"):
        return False
    try:
        obj = json.loads(line)
    except ValueError:
        return False
    return isinstance(obj, dict) and "ok" in obj


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

    def _child_env(self) -> dict:
        """워커 입출력은 항상 UTF-8. Windows는 기본이 시스템 코드페이지(cp1252/cp949)라 한글·IPA가 깨지거나 워커가 죽는다."""
        return {**os.environ, **(self.env or {}), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def _start(self) -> None:
        import time
        t0 = time.perf_counter()
        self._proc = subprocess.Popen(  # noqa: S603 - 인자 배열
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
            cwd=self.cwd, env=self._child_env(), text=True, encoding="utf-8", bufsize=1,
            **_popen_kwargs(low_priority=True),
        )
        reply = self._call({"op": "hello"})
        self._caps = TTSCapabilities(**reply["capabilities"])
        self.cold_start_s = time.perf_counter() - t0

    def _mark_dead(self) -> int | None:
        """워커가 죽었음을 감지하면 즉시 회수하고 핸들을 버린다. EOF를 본 직후에는 자식이 아직 회수되지 않아
        poll()이 None일 수 있어(Windows CI에서 재현) 다음 호출이 죽은 파이프에 쓰는 경쟁 조건이 생긴다."""
        p, self._proc = self._proc, None
        if p is None:
            return None
        try:
            return p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(p)
            return p.poll()

    def _call(self, msg: dict) -> dict:
        p = self._proc
        assert p is not None and p.stdin is not None and p.stdout is not None
        try:
            p.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            p.stdin.flush()
            line = self._read_reply(p)
        except (BrokenPipeError, OSError) as e:
            code = self._mark_dead()
            raise ProviderError(f"공급자 워커와 통신 실패(종료 코드 {code}): {e}") from e
        except KeyboardInterrupt:
            kill_tree(p)
            raise
        if not line:
            code = self._mark_dead()
            raise ProviderError(f"공급자 워커가 응답 없이 종료됨 (종료 코드 {code}). 위 stderr 출력을 확인하세요.")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise ProviderError(f"공급자 오류: {reply.get('error')}")
        return reply

    @staticmethod
    def _read_reply(p) -> str:
        """JSON 객체 줄만 응답으로 취급한다. 라이브러리가 stdout에 섞어 쓴 소음은 stderr로 흘려 보낸다(최대 500줄)."""
        import sys
        for _ in range(500):
            line = p.stdout.readline()
            if not line or _is_reply(line):
                return line
            sys.stderr.write(f"[worker stdout] {line}")
        raise ProviderError("워커가 JSON이 아닌 출력만 계속 보냅니다")

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

    def stats(self) -> dict | None:
        """워커가 보고하는 자원 사용량(최대 메모리 등). 지원하지 않는 워커면 None."""
        self._ensure()
        try:
            return self._call({"op": "stats"})
        except ProviderError:
            return None

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
