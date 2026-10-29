from __future__ import annotations

import os
import sys

from ..config import Config
from ..platform.ffmpeg import FFmpeg
from .tts.base import ProviderError, TTSProvider


def create_tts_provider(name: str, cfg: Config, ffmpeg: FFmpeg) -> TTSProvider:
    spec = (cfg.defaults.get("providers") or {}).get(name)
    if spec is None:
        known = ", ".join((cfg.defaults.get("providers") or {}))
        raise ProviderError(f"알 수 없는 TTS 공급자: {name} (등록된 공급자: {known})")
    if name == "flite":
        from .tts.flite import FliteProvider
        return FliteProvider(ffmpeg)
    if spec.get("kind") == "subprocess":
        from .tts.subprocess_provider import SubprocessTTSProvider
        env_dir = cfg.root / spec["env_dir"]
        worker = env_dir / "worker.py"
        if not worker.is_file():
            raise ProviderError(f"워커가 없습니다: {worker}")
        venv_py = env_dir / (".venv/Scripts/python.exe" if sys.platform.startswith("win") else ".venv/bin/python")
        if not venv_py.is_file():
            raise ProviderError(
                f"{name} 환경이 설치되지 않았습니다 ({venv_py}). "
                f"Windows: scripts/setup_{name}.ps1 실행, 그 외: `uv sync --project {env_dir} --python 3.12`"
            )
        # 가중치·보이스는 HuggingFace 캐시로 내려받는다. 문서 3.3의 모델 캐시 위치로 모은다.
        hf_home = cfg.model_cache() / "hf"
        hf_home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HF_HOME": str(hf_home), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        return SubprocessTTSProvider([venv_py, worker], cwd=env_dir, env=env)
    raise ProviderError(f"공급자 {name}: 지원하지 않는 kind={spec.get('kind')}")
