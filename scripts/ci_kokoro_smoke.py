"""CI 전용: Kokoro 벤치마크와 샘플 가이드 합성을 실제로 돌리고 산출물을 ci_artifacts/ 에 모은다.

사용자가 내려받아 직접 들어 볼 수 있도록 청취용 WAV와 가이드 WAV를 함께 남긴다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from avsync.cli import main

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ci_artifacts"
VOICE = os.environ.get("KOKORO_VOICE", "af_heart")     # kokoro 소스의 docstring 예시에 나오는 음색


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main_() -> int:
    OUT.mkdir(exist_ok=True)
    rc = main(["benchmark", "tts", "--provider", "kokoro", "--voice", VOICE, "--targets", "15,30", "--listen"])
    bench = ROOT / "reports" / "benchmark_kokoro.json"
    if bench.is_file():
        shutil.copy(bench, OUT / bench.name)
        b = json.loads(bench.read_text(encoding="utf-8"))
        summary(f"### Kokoro 벤치마크 (러너 기준, 사용자 PC 아님)\n- 콜드 스타트 {b['cold_start_s']}s\n"
                + "\n".join(f"- {r['target_s']}초 분량: 소요 {r['wall_s']}s, RTF {r['rtf']}" for r in b["runs"])
                + f"\n- 최대 메모리 {b['peak_memory_mb']} MB ({b['peak_memory_method']})")
    listen = ROOT / "reports" / "listening" / "kokoro"
    if listen.is_dir():
        shutil.copytree(listen, OUT / "listening", dirs_exist_ok=True)
    if rc != 0:
        return rc

    work = Path(tempfile.mkdtemp(prefix="avsync_ci_"))
    main(["init", "surya_a", "--base", str(work), "--example", "surya_a"])
    proj = work / "surya_a"
    s = (proj / "script.yaml").read_text(encoding="utf-8")
    s = s.replace("approved: false", "approved: true").replace(
        "voice: {provider: null, voice_id: null, base_speed: 0.95}",
        f"voice: {{provider: kokoro, voice_id: {VOICE}, base_speed: 0.95}}")
    (proj / "script.yaml").write_text(s, encoding="utf-8")
    rc = main(["guide", "--project", str(proj / "project.yaml")])
    if (proj / "outputs").is_dir():
        shutil.copytree(proj / "outputs" / "guide", OUT / "guide", dirs_exist_ok=True)
        fit = proj / "manifests" / "fit_report.json"
        if fit.is_file():
            shutil.copy(fit, OUT / "fit_report.json")
            rep = json.loads(fit.read_text(encoding="utf-8"))
            lines = ["### Kokoro 단어 타임스탬프 진단 (공급자 보고: word_timestamps=%s)" % rep["provider"]["word_timestamps"]]
            for r in rep["items"][:4]:
                for seg in r["audio"]["segments"]:
                    ws = " ".join(f"{w['text']}[{w['start_us'] / 1e6:.2f}-{w['end_us'] / 1e6:.2f}]" for w in seg["words"])
                    lines.append(f"- {r['id']} \"{seg['original']}\" ({seg['start_us'] / 1e6:.2f}-{seg['end_us'] / 1e6:.2f}s) words: {ws or '(없음)'}")
            lines.append("- 발음 사전 적용 문장: " + "; ".join(
                f"{seg['original']} -> {seg['spoken']}" for r in rep["items"][:3] for seg in r["audio"]["segments"] if seg["spoken"] != seg["normalized"]))
            summary("\n".join(lines))
    summary(f"### 샘플 가이드 합성 종료 코드: {rc} (0=성공, 2=길이 초과/겹침 보고)")
    return 0 if rc in (0, 2) else rc


if __name__ == "__main__":
    sys.exit(main_())
