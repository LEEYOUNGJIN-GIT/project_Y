"""CI 전용: 후보 모델의 GitHub 저장소 README(설치·OS·GPU/CPU·사용법)를 로그로 남긴다. github.com은 개발 환경에서 열리지 않을 수 있다."""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOS = ["inclusionAI/Ming-3.0-tts", "smirki/lure-tts", "QwenLM/Qwen3-TTS"]
KEY = re.compile(r"windows|cpu|gpu|cuda|vram|mps|apple|install|requirements|python|pip |conda|uv |llama|onnx|gguf|speed|rtf|instruct|tag|acting|emotion|pitch|prompt", re.I)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    for repo in REPOS:
        d = Path(tempfile.mkdtemp(prefix="repo_"))
        r = subprocess.run(["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(d / "r")], capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            print(f"=== REPO {repo}: clone 실패 {r.stderr[-200:]} ===", flush=True)
            continue
        root = d / "r"
        readme = next((p for p in sorted(root.glob("README*")) if p.is_file()), None)
        text = readme.read_text(encoding="utf-8", errors="replace") if readme else ""
        lines = [l for l in text.splitlines() if l.strip() and not re.match(r"\s*(<img|!\[|<p align|<div|</div)", l)]
        key = [l for l in lines if KEY.search(l)]
        files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)[:60]
        print(f"=== REPO {repo} (파일 {len(files)}+개) ===\n[최상위/주요 파일]\n" + "\n".join(files[:40])
              + f"\n[README 앞부분]\n" + "\n".join(lines[:70])[:5000] + f"\n[README 핵심 줄]\n" + "\n".join(key[:60])[:5000]
              + f"\n=== END {repo} ===", flush=True)
        for name in ("requirements.txt", "pyproject.toml"):
            p = root / name
            if p.is_file():
                print(f"--- {repo}/{name} ---\n{p.read_text(encoding='utf-8', errors='replace')[:2500]}\n--- END ---", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
