"""FFmpeg/ffprobe 위치 확인과 호출. PATH 의존은 설정으로 명시했을 때만 허용한다."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from .process import ProcResult, run


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    ffprobe: Path
    from_path_fallback: bool


def _resolve(name: str, configured: str | None, env_var: str, allow_fallback: bool) -> tuple[Path, bool]:
    candidate = configured or os.environ.get(env_var)
    if candidate:
        p = Path(candidate)
        if not p.is_file():
            raise ToolError(f"{name} 경로가 파일이 아닙니다: {p}")
        return p, False
    if allow_fallback:
        found = shutil.which(name)
        if found:
            return Path(found), True
    raise ToolError(
        f"{name}를 찾을 수 없습니다. configs/defaults.yaml의 tools.{name} 또는 환경변수 {env_var}에 경로를 지정하세요."
    )


def find_tools(cfg: Config) -> Tools:
    t = cfg.defaults.get("tools") or {}
    allow = bool(t.get("allow_path_fallback", False))
    ff, a = _resolve("ffmpeg", t.get("ffmpeg"), "AVSYNC_FFMPEG", allow)
    fp, b = _resolve("ffprobe", t.get("ffprobe"), "AVSYNC_FFPROBE", allow)
    return Tools(ff, fp, a or b)


class FFmpeg:
    def __init__(self, tools: Tools):
        self.tools = tools

    def run(self, args, *, context: str = "ffmpeg", timeout: float | None = None) -> ProcResult:
        base = [self.tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        return run([*base, *args], check=True, context=context, timeout=timeout)

    def run_raw(self, args, *, context: str = "ffmpeg", timeout: float | None = None,
                loglevel: str = "info") -> ProcResult:
        """stderr 로그가 필요한 분석 패스(loudnorm, ebur128, astats)용."""
        base = [self.tools.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", loglevel]
        return run([*base, *args], check=True, context=context, timeout=timeout)

    def version(self) -> str:
        return run([self.tools.ffmpeg, "-version"], check=True).stdout

    def has_filter(self, name: str) -> bool:
        """`-filters` 출력은 버전마다 열 형식이 달라(9.x에서 확인) 파싱 대신 `-h filter=NAME`으로 존재를 확인한다."""
        out = run([self.tools.ffmpeg, "-hide_banner", "-h", f"filter={name}"])
        return f"Filter {name}" in out.stdout

    def filters(self) -> set[str]:
        out = run([self.tools.ffmpeg, "-hide_banner", "-filters"], check=True).stdout
        names = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 3:
                names.add(parts[1])
        return names
