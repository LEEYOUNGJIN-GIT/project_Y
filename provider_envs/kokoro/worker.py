"""Kokoro 워커 (JSON Lines). provider_envs/kokoro 의 별도 uv 환경에서 실행한다.

검증 상태: kokoro 0.9.4 wheel의 pipeline.py 소스와 대조해 호출 시그니처(KPipeline(lang_code, repo_id),
pipeline(text, voice=, speed=) -> Result가 (graphemes, phonemes, audio)로 풀림)를 확인했다.
그러나 개발 환경에서 HuggingFace가 막혀 가중치를 받을 수 없어 **실행해 보지는 못했다.**
사용자 PC에서 `scripts/setup_kokoro.ps1` 후 `avsync benchmark tts --provider kokoro`로 검증한다.
"""
from __future__ import annotations

import json
import sys

SR = 24000


def main() -> int:
    # Windows 기본 입출력 인코딩은 시스템 코드페이지다. 한글 메시지·IPA 발음 표기를 위해 UTF-8로 고정한다.
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    pipeline = None
    version = "unknown"
    for raw in sys.stdin:
        msg = json.loads(raw)
        op = msg.get("op")
        try:
            if op == "hello":
                import importlib.metadata as md
                try:
                    version = md.version("kokoro")
                except md.PackageNotFoundError:
                    pass
                reply = {"ok": True, "capabilities": {
                    "name": "kokoro", "version": version, "languages": ["en-US"], "speed": True,
                    "word_timestamps": False, "emotion": False, "sample_rate": SR, "cpu": True,
                    "pronunciation_mode": "phoneme_markup", "production_ok": True,
                }}
            elif op == "synthesize":
                if pipeline is None:
                    from kokoro import KPipeline  # 지연 import: 콜드 스타트 측정에 포함
                    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
                import numpy as np
                import soundfile as sf
                voice = msg.get("voice")
                if not voice:
                    raise ValueError("voice_id가 지정되지 않았습니다. 청취 후 script.yaml의 voice.voice_id를 정하세요.")
                chunks = []
                for _, _, a in pipeline(msg["text"], voice=voice, speed=float(msg["speed"])):
                    if a is None:
                        continue
                    chunks.append(a.detach().cpu().numpy() if hasattr(a, "detach") else np.asarray(a))
                if not chunks:
                    raise ValueError("합성 결과가 비었습니다")
                sf.write(msg["out"], np.concatenate(chunks), SR, subtype="PCM_16")
                reply = {"ok": True, "sample_rate": SR, "meta": {"voice": voice}}
            elif op == "bye":
                return 0
            else:
                reply = {"ok": False, "error": f"알 수 없는 op: {op}"}
        except Exception as e:  # noqa: BLE001 - 워커 경계에서 모든 오류를 JSON으로 돌려준다
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
