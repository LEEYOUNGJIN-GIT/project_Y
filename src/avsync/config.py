"""설정 로딩. configs/ 의 YAML을 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


class ConfigError(RuntimeError):
    pass


def repo_root() -> Path:
    env = os.environ.get("AVSYNC_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    env = os.environ.get("AVSYNC_CONFIG_DIR")
    return Path(env) if env else repo_root() / "configs"


def load_yaml(path: Path) -> dict:
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"파일이 없습니다: {path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML 문법 오류 {path}: {e}") from e
    return data if data is not None else {}


@dataclass(frozen=True)
class Config:
    defaults: dict
    gates: dict
    tones: dict
    root: Path

    def profile(self, name: str) -> dict:
        return load_yaml(config_dir() / "profiles" / f"{name}.yaml")

    def tone(self, name: str) -> dict:
        if name not in self.tones:
            raise ConfigError(f"톤 프리셋이 없습니다: {name} (후보: {', '.join(self.tones)})")
        return self.tones[name]

    def model_cache(self) -> Path:
        configured = self.defaults.get("model_cache")
        if configured:
            return Path(configured)
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA")
            if base:
                return Path(base) / "avsync" / "models"
        return Path.home() / ".cache" / "avsync" / "models"


def load_config() -> Config:
    d = config_dir()
    return Config(
        defaults=load_yaml(d / "defaults.yaml"),
        gates=load_yaml(d / "quality_gates.yaml"),
        tones=load_yaml(d / "tone_presets.yaml"),
        root=repo_root(),
    )
