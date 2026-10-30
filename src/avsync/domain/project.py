"""project.yaml 로딩과 검증 (4.2)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError, load_yaml
from .keys import PROJECT, check_tree

ORIGINAL_POLICIES = ("remove", "keep", "duck")
REFRAMES = ("none", "blur_pad", "crop_center", "pad_solid")
BGM_SOURCES = ("none", "imported")  # generated는 후순위


@dataclass
class BgmSpec:
    source: str
    path: Path | None
    license_note: str
    level_db: float | None
    duck_db: float | None


@dataclass
class Project:
    root: Path
    project_id: str
    video: Path | None
    script: Path
    guide: Path | None
    tone: str
    original: str
    duck_under_narration_db: float | None
    bgm: BgmSpec
    profile: str
    reframe: str
    captions: str
    caption_style: dict
    loudness_lufs: float | None
    true_peak_dbfs: float | None
    raw: dict
    warnings: list[str] = None  # type: ignore[assignment]

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def manifests(self) -> Path:
        return self.root / "manifests"

    def ensure_dirs(self) -> None:
        for p in (self.cache, self.outputs, self.manifests):
            p.mkdir(parents=True, exist_ok=True)


def load_project(path: Path) -> Project:
    path = Path(path)
    data = load_yaml(path)
    root = path.parent
    problems: list[str] = []

    if data.get("schema_version") != 3:
        problems.append("schema_version은 3이어야 합니다")
    pid = str(data.get("project_id", "")).strip()
    if not pid:
        problems.append("project_id가 필요합니다")

    inp = data.get("input") or {}
    video = root / inp["video"] if inp.get("video") else None
    script = root / inp.get("script", "script.yaml")
    guide = root / inp["guide"] if inp.get("guide") else None

    audio = data.get("audio") or {}
    original = audio.get("original", "duck")
    if original not in ORIGINAL_POLICIES:
        problems.append(f"audio.original은 {ORIGINAL_POLICIES} 중 하나여야 합니다: {original!r}")

    bg = audio.get("bgm") or {}
    source = bg.get("source", "none")
    if source not in BGM_SOURCES:
        problems.append(f"audio.bgm.source는 {BGM_SOURCES} 중 하나여야 합니다 (generated는 후순위): {source!r}")
    bgm_path = root / bg["path"] if bg.get("path") else None
    if source == "imported" and bgm_path is None:
        problems.append("audio.bgm.source=imported 이면 audio.bgm.path가 필요합니다")

    out = data.get("output") or {}
    reframe = out.get("reframe", "blur_pad")
    if reframe not in REFRAMES:
        problems.append(f"output.reframe은 {REFRAMES} 중 하나여야 합니다: {reframe!r}")
    captions = out.get("captions", "sidecar_srt")
    if captions not in ("none", "sidecar_srt", "burn_in"):
        problems.append(f"output.captions는 none|sidecar_srt|burn_in 중 하나여야 합니다: {captions!r}")

    if problems:
        raise ConfigError(f"{path}: " + "; ".join(problems))

    return Project(
        root=root, project_id=pid, video=video, script=script, guide=guide,
        tone=data.get("tone", "yoga_flow_calm"),
        original=original,
        duck_under_narration_db=audio.get("duck_under_narration_db"),
        bgm=BgmSpec(
            source=source, path=bgm_path, license_note=str(bg.get("license_note", "")),
            level_db=bg.get("level_db"), duck_db=bg.get("duck_db"),
        ),
        profile=out.get("profile", "reels_9x16"), reframe=reframe, captions=captions,
        caption_style=out.get("caption_style") or {},
        loudness_lufs=out.get("loudness_lufs"), true_peak_dbfs=out.get("true_peak_dbfs"),
        raw=data, warnings=check_tree(data, PROJECT),
    )
