"""`avsync init`: 작업 폴더 생성."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .config import ConfigError, repo_root

_BLANK = {
    "project.yaml": """schema_version: 3
project_id: {pid}
input:
  video: assets/input.mp4     # audio-first 이면 이 줄 대신 guide: guide.yaml
  script: script.yaml
tone: yoga_flow_calm
audio:
  original: duck
  bgm:
    source: none
output:
  profile: reels_9x16
  reframe: blur_pad
  captions: sidecar_srt
""",
    "script.yaml": """approved: false
voice: {{provider: null, voice_id: null, base_speed: null}}
pronunciations: {{}}
lines:
  - {{id: l01, at: "00:00.8", text: "Start standing, feet hip-width apart.", mood: calm}}
""",
    "brief.md": "# brief\n- 주제:\n- 길이:\n- 핵심(최대 3개):\n- 하지 말 것:\n- 동작 시각(영상 기준):\n- 톤: yoga_flow_calm\n- 참고 자료:\n",
}


def init_workspace(pid: str, base: Path, example: str | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", pid):
        raise ConfigError("project_id는 영문, 숫자, _, - 만 쓸 수 있습니다")
    dest = base / pid
    if dest.exists():
        raise ConfigError(f"이미 있습니다: {dest}")
    (dest / "assets").mkdir(parents=True)
    if example:
        src = repo_root() / "examples" / example
        if not src.is_dir():
            raise ConfigError(f"예제가 없습니다: {example}")
        for f in src.iterdir():
            shutil.copyfile(f, dest / f.name)
        text = (dest / "project.yaml").read_text(encoding="utf-8")
        (dest / "project.yaml").write_text(re.sub(r"project_id: .*", f"project_id: {pid}", text, count=1),
                                           encoding="utf-8")
    else:
        for name, body in _BLANK.items():
            (dest / name).write_text(body.format(pid=pid), encoding="utf-8")
    return dest
