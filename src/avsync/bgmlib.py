"""BGM 라이브러리 등록 (5.2의 1단계, 10장 자산 기록)."""
from __future__ import annotations

import shutil
from pathlib import Path

from .audio.loudness import measure_ebur128, measure_peak_db
from .config import ConfigError, repo_root
from .domain.hashing import sha256_file
from .domain.manifest import now_iso, write_json
from .platform.ffmpeg import FFmpeg
from .video.probe import probe_audio_duration_us


def import_bgm(ff: FFmpeg, src: Path, note: str, vocals: str, *, library: Path | None = None,
               extra: dict | None = None) -> dict:
    if not note.strip():
        raise ConfigError("--note(출처·라이선스)가 필요합니다. 라이선스를 모르는 음원은 등록하지 않습니다.")
    if vocals not in ("yes", "no", "unknown"):
        raise ConfigError("--vocals는 yes|no|unknown 중 하나여야 합니다")
    src = Path(src)
    if not src.is_file():
        raise ConfigError(f"파일이 없습니다: {src}")
    dur = probe_audio_duration_us(ff.tools, src)       # 오디오가 아니면 여기서 실패한다
    sha = sha256_file(src)
    lib = library or (repo_root() / "library" / "bgm")
    lib.mkdir(parents=True, exist_ok=True)
    dst = lib / f"{src.stem}_{sha[:8]}{src.suffix.lower()}"
    if not dst.exists():
        shutil.copyfile(src, dst)
    ld = measure_ebur128(ff, dst)
    peak = measure_peak_db(ff, dst)
    warnings = []
    if vocals != "no":
        warnings.append("보컬 유무 미확인/있음: 내레이션과 충돌하므로 들어서 확인하세요 (`avsync bgm check`의 휴리스틱은 참고용이며 실제 보컬 음악으로 검증되지 않음)")
    if peak is not None and peak > -0.1:
        warnings.append(f"원본 BGM 피크 {peak:.1f} dBFS - clipping 가능성")
    meta = {"file": dst.name, "sha256": sha, "source_file": str(src), "duration_us": dur,
            "integrated_lufs": ld.integrated, "peak_dbfs": round(peak, 2) if peak is not None else None, "license_note": note, "vocals": vocals,
            "imported": now_iso(), "warnings": warnings, **(extra or {})}
    write_json(dst.with_suffix(".json"), meta)
    return {**meta, "path": str(dst)}
