"""환경 점검 (Phase 0). 판정할 수 없는 것은 '미확인'으로 남긴다."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from .config import Config, load_config
from .platform.ffmpeg import FFmpeg, ToolError, find_tools
from .platform.process import run

REQUIRED_FILTERS = {
    "loudnorm": "2-pass 음량 정규화",
    "adelay": "샘플 단위 배치",
    "amix": "스템 합산",
    "amultiply": "이득 곡선 곱(덕킹)",
    "acrossfade": "BGM 루프 크로스페이드",
    "aresample": "48 kHz 변환",
    "ebur128": "음량 측정",
    "astats": "피크/RMS 측정",
    "apad": "길이 맞춤",
}
OPTIONAL_FILTERS = {
    "sidechaincompress": "문서 5.4의 사이드체인 덕킹 (이 구현은 이득 곡선 방식을 쓰므로 필수 아님)",
    "subtitles": "burn-in 자막(후순위, libass 필요)",
    "flite": "오프라인 시험용 TTS(제작에는 쓰지 않음)",
}


def _ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            return ms.tp / 1024 ** 3
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (OSError, AttributeError, ValueError):
        return None


def _onedrive(path: Path) -> bool:
    low = [p.lower() for p in path.resolve().parts]
    if any("onedrive" in p for p in low):
        return True
    env = os.environ.get("OneDrive")
    return bool(env and str(path.resolve()).lower().startswith(env.lower()))


def run_doctor(cfg: Config | None = None, workdir: Path | None = None, write_reports: bool = True) -> dict:
    cfg = cfg or load_config()
    workdir = workdir or Path.cwd()
    checks: list[dict] = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    add("os", "info", f"{platform.platform()} / Python {platform.python_version()} / CPU {os.cpu_count()}코어")
    ram = _ram_gb()
    add("ram", "info" if ram is None or ram >= 8 else "warn", f"{ram:.1f} GB" if ram else "확인 불가")
    free = shutil.disk_usage(workdir).free / 1024 ** 3
    add("disk", "pass" if free > 5 else "warn", f"작업 드라이브 여유 {free:.1f} GB")
    add("python", "pass" if sys.version_info >= (3, 11) else "fail", platform.python_version() + " (필요 3.11+)")
    add("uv", "pass" if shutil.which("uv") else "warn", shutil.which("uv") or "uv를 찾지 못함 (공급자 환경 설치에 필요)")

    ff = None
    try:
        tools = find_tools(cfg)
        ff = FFmpeg(tools)
        add("ffmpeg", "warn" if tools.from_path_fallback else "pass",
            f"{tools.ffmpeg}" + (" (PATH 폴백 사용 - Windows에서는 defaults.yaml에 경로 지정 권장)" if tools.from_path_fallback else ""))
    except ToolError as e:
        add("ffmpeg", "fail", str(e))

    reports = Path(cfg.root) / "reports"
    if ff:
        ver = ff.version()
        filters = ff.filters()
        first = ver.splitlines()[0]
        lic = "GPL" if "--enable-gpl" in ver else "LGPL/기타"
        nonfree = "--enable-nonfree" in ver
        add("ffmpeg_version", "info", first)
        add("ffmpeg_license", "warn" if nonfree else "info",
            f"{lic} 구성{' + nonfree(재배포 불가, 개인 사용만)' if nonfree else ''} - 개인 사용 기록용 (문서 10장)")
        for f, why in REQUIRED_FILTERS.items():
            add(f"filter:{f}", "pass" if f in filters else "fail", why)
        for f, why in OPTIONAL_FILTERS.items():
            add(f"filter:{f}", "pass" if f in filters else "info", f"{'있음' if f in filters else '없음'} - {why}")
        amix_h = run([ff.tools.ffmpeg, "-hide_banner", "-h", "filter=amix"]).stdout
        add("amix:normalize", "pass" if "normalize" in amix_h else "fail", "amix normalize=0 옵션 필요 (FFmpeg 4.4+)")
        for enc in ("libx264", "aac"):
            out = run([ff.tools.ffmpeg, "-hide_banner", "-encoders"]).stdout
            add(f"encoder:{enc}", "pass" if f" {enc} " in out else "fail", "출력 H.264/AAC")
        if write_reports:
            reports.mkdir(exist_ok=True)
            (reports / "ffmpeg_build.txt").write_text(
                ver + "\n\n=== -filters ===\n" + run([ff.tools.ffmpeg, "-hide_banner", "-filters"]).stdout,
                encoding="utf-8")

    cache = cfg.model_cache()
    add("model_cache", "info", str(cache))
    if _onedrive(cache) or _onedrive(workdir):
        add("onedrive", "warn", "OneDrive 동기화 폴더 안에 작업 폴더/모델 캐시가 있음 - 밖으로 옮기세요")
    else:
        add("onedrive", "pass", "OneDrive 폴더 아님")
    plen = len(str(workdir.resolve()))
    add("path_length", "warn" if plen > 100 else "pass", f"작업 경로 길이 {plen}자 (Windows 260자 제한 여유)")
    kenv = cfg.root / "provider_envs" / "kokoro" / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    add("kokoro_env", "pass" if kenv.is_file() else "info",
        "설치됨" if kenv.is_file() else "미설치 (Windows: scripts/setup_kokoro.ps1)")
    status = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": status, "checks": checks}


def format_doctor(rep: dict) -> str:
    tag = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}
    rows = [f"{tag[c['status']]:<5} {c['name']:<22} {c['detail']}" for c in rep["checks"]]
    rows.append(f"종합: {tag[rep['status']]}")
    return "\n".join(rows)
