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


def kokoro_python() -> Path:
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / "provider_envs" / "kokoro" / ".venv" / sub


MODEL_INFO_CODE = r"""
import json
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_files('hexgrad/Kokoro-82M')
voices = sorted(f.split('/')[-1][:-3] for f in files if f.startswith('voices/') and f.endswith('.pt'))
mi = api.model_info('hexgrad/Kokoro-82M')
card = mi.card_data.to_dict() if getattr(mi, 'card_data', None) else {}
print(json.dumps({'voices': voices, 'license': card.get('license'), 'sha': mi.sha, 'tags': mi.tags,
                  'other_files': [f for f in files if not f.startswith('voices/')]}))
"""


def model_info() -> dict | None:
    """가중치 라이선스와 음색 목록을 HuggingFace에서 직접 읽는다 (개발 환경에서는 접근 불가였던 사실 확인)."""
    import subprocess
    r = subprocess.run([str(kokoro_python()), "-c", MODEL_INFO_CODE], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        summary("### Kokoro 모델 정보 조회 실패\n```\n" + r.stderr[-600:] + "\n```")
        return None
    info = json.loads(r.stdout.strip().splitlines()[-1])
    (OUT / "kokoro_model_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    english = [v for v in info["voices"] if v[:2] in ("af", "am", "bf", "bm")]
    summary(f"### Kokoro-82M 모델 정보 (HuggingFace 직접 조회)\n- 가중치 라이선스(model card): **{info['license']}**\n"
            f"- 리비전: {info['sha']}\n- 영어 음색 {len(english)}개: {', '.join(english)}\n- 그 외 파일: {', '.join(info['other_files'])}")
    return info


VOICE_SENTENCES = ["Shat. Exhale, downward dog. Stay for five breaths.", "Slow down. This is not a race."]
PREFERRED = ["af_heart", "af_bella", "af_nicole", "af_sarah", "am_adam", "am_michael", "bf_emma", "bm_george"]


def voice_samples(info: dict) -> None:
    """후보 음색별로 같은 두 문장을 합성해 사용자가 직접 비교해 고를 수 있게 한다 (5.6: 후보 3개 청취)."""
    from avsync.config import load_config
    from avsync.platform.ffmpeg import FFmpeg, find_tools
    from avsync.providers.registry import create_tts_provider
    from avsync.providers.tts.base import TTSRequest
    cfg = load_config()
    prov = create_tts_provider("kokoro", cfg, FFmpeg(find_tools(cfg)))
    out = OUT / "voices"
    out.mkdir(exist_ok=True)
    avail = set(info["voices"])
    chosen = [v for v in PREFERRED if v in avail] or sorted(v for v in avail if v[:2] in ("af", "am"))[:6]
    rows = ["### 후보 음색별 합성 길이(초) - 같은 두 문장, 속도 0.95"]
    try:
        for v in chosen:
            durs = []
            for i, text in enumerate(VOICE_SENTENCES):
                res = prov.synthesize(TTSRequest(text, v, 0.95, "en-US", out / f"{v}_{i + 1}.wav"))
                durs.append(f"{res.duration_us / 1e6:.2f}")
            rows.append(f"- {v}: {' / '.join(durs)}")
    finally:
        prov.close()
    summary("\n".join(rows))


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
    info = model_info()
    if info:
        try:
            voice_samples(info)
        except Exception as e:  # noqa: BLE001 - 음색 비교는 부가 산출물이라 실패해도 가이드 합성은 계속
            summary(f"### 음색 샘플 생성 실패: {type(e).__name__}: {e}")

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
