"""Kokoro 워커 (JSON Lines). provider_envs/kokoro 의 별도 uv 환경에서 실행한다.

검증 상태: kokoro 0.9.4 wheel의 pipeline.py 소스와 대조해 호출 시그니처(KPipeline(lang_code, repo_id),
pipeline(text, voice=, speed=) -> Result가 (graphemes, phonemes, audio)로 풀림)를 확인했다.
그러나 개발 환경에서 HuggingFace가 막혀 가중치를 받을 수 없어 **실행해 보지는 못했다.**
사용자 PC에서 `scripts/setup_kokoro.ps1` 후 `avsync benchmark tts --provider kokoro`로 검증한다.
"""
from __future__ import annotations

import json
import os
import sys

SR = 24000


def _peak_rss_mb():
    """이 프로세스의 최대 상주 메모리(MB). Windows의 venv python.exe는 실제 인터프리터를 자식으로 띄우는 런처라
    부모가 PID로 재면 런처(수 MB)를 재게 된다. 그래서 워커가 자기 값을 직접 보고한다."""
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return v / (1024 ** 2 if sys.platform == "darwin" else 1024)
    except ImportError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        k32, ps = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        return pmc.PeakWorkingSetSize / 1024 ** 2 if ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb) else None
    except Exception:  # noqa: BLE001
        return None


def _claim_protocol_channel():
    """프로토콜 전용 채널을 복제해 확보하고, 이후 fd 1로 나가는 모든 출력을 stderr로 돌린다.

    CI(Windows)에서 misaki가 첫 합성 때 spaCy 모델을 자동 설치하며 stdout에 출력해 JSON 프로토콜이 깨졌다.
    라이브러리 print뿐 아니라 하위 프로세스(uv pip 등)의 출력도 fd 수준에서 막아야 한다.
    """
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1, newline="\n")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto


def main() -> int:
    # Windows 기본 입출력 인코딩은 시스템 코드페이지다. 한글 메시지·IPA 발음 표기를 위해 UTF-8로 고정한다.
    sys.stdin.reconfigure(encoding="utf-8")
    out = _claim_protocol_channel()
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
                    "word_timestamps": True, "emotion": False, "sample_rate": SR, "cpu": True,
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
                words = []
                offset = 0.0                     # 청크가 이어 붙는 위치(초). 토큰 시각은 청크 기준 상대값이다
                for res in pipeline(msg["text"], voice=voice, speed=float(msg["speed"])):
                    a = res.audio
                    if a is None:
                        continue
                    arr = a.detach().cpu().numpy() if hasattr(a, "detach") else np.asarray(a)
                    # kokoro 0.9.4 English path: join_timestamps()가 모델 예측 길이로 start_ts/end_ts(초)를 채운다.
                    # 강제 정렬이 아니라 예측값이므로 어긋날 수 있다.
                    for t in (getattr(res, "tokens", None) or []):
                        st, en = getattr(t, "start_ts", None), getattr(t, "end_ts", None)
                        if st is not None and en is not None and any(ch.isalnum() for ch in t.text):
                            words.append({"text": t.text, "start": offset + float(st), "end": offset + float(en)})
                    chunks.append(arr)
                    offset += len(arr) / SR
                if not chunks:
                    raise ValueError("합성 결과가 비었습니다")
                sf.write(msg["out"], np.concatenate(chunks), SR, subtype="PCM_16")
                reply = {"ok": True, "sample_rate": SR, "meta": {"voice": voice, "words": words}}
            elif op == "stats":
                reply = {"ok": True, "peak_rss_mb": _peak_rss_mb()}
            elif op == "bye":
                return 0
            else:
                reply = {"ok": False, "error": f"알 수 없는 op: {op}"}
        except Exception as e:  # noqa: BLE001 - 워커 경계에서 모든 오류를 JSON으로 돌려준다
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        out.write(json.dumps(reply, ensure_ascii=False) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
