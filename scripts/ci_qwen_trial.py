"""CI 전용: Qwen3-TTS-12Hz-0.6B-CustomVoice를 Windows CPU에서 시험한다 (카드: `instruct`로 어조/리듬/감정 지시).

카드의 예시는 CUDA + flash_attention_2 기준이라 CPU(float32)로 바꿔 돌린다. 이 시험이 알려주는 것: 설치·실행 가능 여부,
CPU 속도(RTF), 메모리, 그리고 사용자가 들어볼 샘플. 좋은 소리인지는 판정하지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "ci_artifacts" / "qwen_trial"
MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
SENTENCES = ["Inhale, arms up. Exhale, fold forward.", "Soften your shoulders. Let the breath move slowly."]
SPEAKERS = ["Serena", "Ryan", "Aiden"]          # 카드: Serena=Warm, gentle young female (중국어 모국어), Ryan/Aiden=영어 남성
INSTRUCTS = [
    ("none", None),
    ("calm", "Speak in a very calm, soft, slow, soothing tone, like a gentle yoga instructor guiding a class."),
    ("whisper", "Speak in a soft, breathy, almost whispered voice, slowly, with long pauses, like guiding a meditation."),
]
BUDGET_S = 20 * 60                              # 전체 합성 시간 상한


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    import torch
    from qwen_tts import Qwen3TTSModel
    log(f"torch {torch.__version__}, threads {torch.get_num_threads()}")
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(MODEL, device_map="cpu", dtype=torch.float32)
    load_s = time.time() - t0
    log(f"모델 로드 {load_s:.1f}s")
    rows, total_audio, total_wall = [], 0.0, 0.0
    t_all = time.time()
    for sp in SPEAKERS:
        for iname, instruct in INSTRUCTS:
            for i, text in enumerate(SENTENCES):
                if time.time() - t_all > BUDGET_S:
                    log("시간 상한 도달, 중단")
                    break
                kw = {"text": text, "language": "English", "speaker": sp}
                if instruct:
                    kw["instruct"] = instruct
                t1 = time.time()
                try:
                    wavs, sr = model.generate_custom_voice(**kw)
                except Exception:  # noqa: BLE001
                    log(f"[{sp}/{iname}/{i}] 실패:\n{traceback.format_exc()[-1500:]}")
                    rows.append({"speaker": sp, "instruct": iname, "i": i, "error": True})
                    continue
                wall = time.time() - t1
                wav = wavs[0]
                audio_s = len(wav) / sr
                path = OUT / f"{sp}_{iname}_{i}.wav"
                sf.write(str(path), wav, sr)
                total_audio += audio_s
                total_wall += wall
                rows.append({"speaker": sp, "instruct": iname, "i": i, "audio_s": round(audio_s, 2), "wall_s": round(wall, 1),
                             "rtf": round(wall / audio_s, 2), "sr": sr})
                log(f"[{sp}/{iname}/{i}] 오디오 {audio_s:.2f}s / 소요 {wall:.1f}s / RTF {wall / audio_s:.2f}")
    try:
        import psutil
        rss = psutil.Process(os.getpid()).memory_info().rss / 1e6
    except Exception:  # noqa: BLE001
        rss = None
    summ = {"model": MODEL, "load_s": round(load_s, 1), "total_audio_s": round(total_audio, 1), "total_wall_s": round(total_wall, 1),
            "rtf_overall": round(total_wall / total_audio, 2) if total_audio else None, "rss_mb": round(rss) if rss else None, "rows": rows}
    (OUT / "summary.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"### Qwen3-TTS 0.6B CPU 시험 요약: 로드 {load_s:.1f}s, RTF(전체) {summ['rtf_overall']}, 메모리 {summ['rss_mb']} MB, 성공 {sum(1 for r in rows if 'audio_s' in r)}/{len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
