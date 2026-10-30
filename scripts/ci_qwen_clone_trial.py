"""CI 전용: 줄마다 목소리가 달라지는 문제(VoiceDesign은 호출마다 설명에서 목소리를 새로 만든다)를 복제 방식으로 막을 수 있는지 시험한다.

A) 줄마다 VoiceDesign으로 합성   B) VoiceDesign으로 기준 음성 하나를 만들고 Base 모델로 복제해 모든 줄을 합성.
객관 지표는 줄별 피치 중앙값의 편차(간이 추정)뿐이다. 같은 사람처럼 들리는지는 사람이 들어야 한다.
"""
from __future__ import annotations

import gc
import json
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "ci_artifacts" / "qwen_clone_trial"
DESIGN = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
BASE = os.environ.get("QWEN_BASE", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
DESC = "A calm, warm female yoga instructor in her thirties. Soft, slightly breathy voice, slow and unhurried, gentle and soothing."
REF_TEXT = "Soften your shoulders. Let the breath move slowly."
LINES = ["Inhale, arms up.", "Exhale, fold forward.", "Soften your shoulders and let the jaw relax.", "Stay here for five slow breaths."]


def log(m: str) -> None:
    print(m, flush=True)


def median_f0(wav: np.ndarray, sr: int) -> float | None:
    """간이 피치 추정: 40 ms 프레임 자기상관 (70-400 Hz), 에너지가 큰 프레임의 중앙값. 정밀한 측정이 아니다."""
    x = wav.astype(np.float64)
    n, hop = int(0.04 * sr), int(0.01 * sr)
    if len(x) < n * 3:
        return None
    frames = [x[i:i + n] for i in range(0, len(x) - n, hop)]
    en = np.array([np.sqrt(np.mean(f * f)) for f in frames])
    gate = en.max() * 0.25
    lo, hi = int(sr / 400), int(sr / 70)
    f0s = []
    for f, e in zip(frames, en):
        if e < gate:
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, mode="full")[n - 1:]
        if ac[0] <= 0:
            continue
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] / ac[0] > 0.5:
            f0s.append(sr / k)
    return float(np.median(f0s)) if len(f0s) >= 5 else None


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    import psutil
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel
    rows = {"A_design_each": [], "B_clone_one_reference": []}

    def run(label, fn, name):
        t = time.time()
        try:
            wavs, sr = fn()
        except Exception:  # noqa: BLE001
            log(f"[{label}/{name}] 실패:\n{traceback.format_exc()[-1800:]}")
            return None
        wall = time.time() - t
        w = wavs[0]
        sf.write(str(OUT / f"{label}_{name}.wav"), w, sr)
        f0 = median_f0(w, sr)
        rows[label].append({"line": name, "audio_s": round(len(w) / sr, 2), "wall_s": round(wall, 1), "f0_hz": round(f0, 1) if f0 else None})
        log(f"[{label}/{name}] 오디오 {len(w) / sr:.2f}s / 소요 {wall:.1f}s / 피치 중앙값 {f0 and round(f0, 1)} Hz")
        return w, sr

    t0 = time.time()
    design = Qwen3TTSModel.from_pretrained(DESIGN, device_map="cpu", dtype=torch.bfloat16)
    log(f"VoiceDesign 로드 {time.time() - t0:.1f}s")
    ref = run("A_design_each", lambda: design.generate_voice_design(text=REF_TEXT, instruct=DESC, language="English"), "ref")
    for i, line in enumerate(LINES):
        run("A_design_each", lambda line=line: design.generate_voice_design(text=line, instruct=DESC, language="English"), f"l{i}")
    del design
    gc.collect()
    if ref is None:
        return 1
    ref_wav, ref_sr = ref
    t0 = time.time()
    base = Qwen3TTSModel.from_pretrained(BASE, device_map="cpu", dtype=torch.bfloat16)
    log(f"Base 로드 {time.time() - t0:.1f}s ({BASE}), 메모리 {psutil.Process(os.getpid()).memory_info().rss / 1e9:.1f} GB")
    prompt = base.create_voice_clone_prompt(ref_audio=(ref_wav, ref_sr), ref_text=REF_TEXT)
    for i, line in enumerate(LINES):
        run("B_clone_one_reference", lambda line=line: base.generate_voice_clone(text=line, language="English", voice_clone_prompt=prompt), f"l{i}")

    def spread(label):
        v = [r["f0_hz"] for r in rows[label] if r["f0_hz"] and r["line"] != "ref"]
        return (round(float(np.std(v)), 1), round(float(np.mean(v)), 1)) if len(v) >= 2 else None
    summ = {"rows": rows, "f0_std_mean_A": spread("A_design_each"), "f0_std_mean_B": spread("B_clone_one_reference")}
    (OUT / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"### 줄별 피치 중앙값 (표준편차, 평균 Hz): A 줄마다 설계 {summ['f0_std_mean_A']}, B 한 기준 음성 복제 {summ['f0_std_mean_B']} (간이 지표)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
