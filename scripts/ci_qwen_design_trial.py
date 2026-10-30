"""CI 전용: Qwen3-TTS-12Hz-1.7B-VoiceDesign을 Windows CPU에서 시험한다.

배경: 0.6B CustomVoice는 `instruct`를 코드에서 무시한다 (qwen-tts 0.1.1: `if tts_model_size in "0b6": instruct = None`).
지시문 제어는 1.7B 체크포인트에서만 의미가 있다. VoiceDesign은 "목소리를 설명하는 문장"으로 음색과 말투를 정한다.
메모리가 빠듯해 bfloat16으로 올린다. 측정: 로드 시간, RTF, 메모리, 실패 여부. 소리의 좋고 나쁨은 판정하지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "ci_artifacts" / "qwen_design_trial"
MODEL = os.environ.get("QWEN_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
SENTENCES = ["Inhale, arms up. Exhale, fold forward.", "Soften your shoulders. Let the breath move slowly."]
DESIGNS = [
    ("calm_female", "A calm, warm female yoga instructor in her thirties. Soft, slightly breathy voice, slow and unhurried, gentle and soothing."),
    ("whisper_female", "A gentle, low, almost whispered female voice guiding a meditation. Very slow, with long soft pauses between phrases."),
    ("calm_male", "A warm, relaxed male yoga teacher with a deep, soft voice. Slow, reassuring and unhurried."),
]
BUDGET_S = 28 * 60


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    import psutil
    import soundfile as sf
    import torch
    from huggingface_hub import HfApi
    from qwen_tts import Qwen3TTSModel
    try:
        info = HfApi().model_info(MODEL)
        lic = [t for t in info.tags if t.startswith("license:")]
        log(f"모델 확인: {MODEL} {lic} 파일 {len(info.siblings or [])}개")
    except Exception as e:  # noqa: BLE001
        log(f"모델 조회 실패: {type(e).__name__}: {str(e)[:200]}")
        return 1
    log(f"torch {torch.__version__}, threads {torch.get_num_threads()}, 메모리 {psutil.virtual_memory().total / 1e9:.1f} GB")
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(MODEL, device_map="cpu", dtype=torch.bfloat16)
    load_s = time.time() - t0
    log(f"모델 로드 {load_s:.1f}s, 메모리 {psutil.Process(os.getpid()).memory_info().rss / 1e9:.1f} GB")
    rows, total_audio, total_wall, t_all = [], 0.0, 0.0, time.time()
    for name, desc in DESIGNS:
        for i, text in enumerate(SENTENCES):
            if time.time() - t_all > BUDGET_S:
                log("시간 상한 도달, 중단")
                break
            t1 = time.time()
            try:
                wavs, sr = model.generate_voice_design(text=text, instruct=desc, language="English")
            except Exception:  # noqa: BLE001
                log(f"[{name}/{i}] 실패:\n{traceback.format_exc()[-1800:]}")
                rows.append({"design": name, "i": i, "error": True})
                continue
            wall = time.time() - t1
            wav = wavs[0]
            audio_s = len(wav) / sr
            sf.write(str(OUT / f"{name}_{i}.wav"), wav, sr)
            total_audio += audio_s
            total_wall += wall
            rows.append({"design": name, "i": i, "audio_s": round(audio_s, 2), "wall_s": round(wall, 1), "rtf": round(wall / audio_s, 2), "sr": sr})
            log(f"[{name}/{i}] 오디오 {audio_s:.2f}s / 소요 {wall:.1f}s / RTF {wall / audio_s:.2f}")
    rss = psutil.Process(os.getpid()).memory_info().rss / 1e6
    summ = {"model": MODEL, "load_s": round(load_s, 1), "total_audio_s": round(total_audio, 1), "total_wall_s": round(total_wall, 1),
            "rtf_overall": round(total_wall / total_audio, 2) if total_audio else None, "rss_mb": round(rss), "rows": rows}
    (OUT / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"### Qwen3-TTS 1.7B VoiceDesign CPU 시험 요약: 로드 {load_s:.1f}s, RTF(전체) {summ['rtf_overall']}, 메모리 {summ['rss_mb']} MB, 성공 {sum(1 for r in rows if 'audio_s' in r)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
