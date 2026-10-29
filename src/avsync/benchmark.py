"""TTS 벤치마크 (3.4): RTF, 최대 메모리, 콜드 스타트, 청취용 문장 출력."""
from __future__ import annotations

import os
import platform
import shutil
import tempfile
import time
from pathlib import Path

from .audio.wavutil import read_wav
from .config import Config
from .domain.manifest import now_iso, write_json
from .platform.ffmpeg import FFmpeg
from .providers.registry import create_tts_provider
from .providers.tts.base import TTSRequest
from .tts.normalize import apply_pronunciations, split_sentences


def _peak_mb(provider) -> tuple[float | None, str]:
    """측정할 수 있는 만큼만 보고한다. 측정 방식을 함께 돌려준다."""
    proc = getattr(provider, "_proc", None)
    if proc is not None:
        pid = proc.pid
        if os.name != "nt":
            try:
                for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                    if line.startswith("VmHWM:"):
                        return int(line.split()[1]) / 1024, "워커 프로세스 VmHWM"
            except OSError:
                pass
        else:
            try:
                import ctypes
                from ctypes import wintypes

                class PMC(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
                k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
                h = k32.OpenProcess(0x1000 | 0x0400, False, pid)   # QUERY_LIMITED | QUERY_INFORMATION
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
                k32.CloseHandle(h)
                if ok:
                    return pmc.PeakWorkingSetSize / 1024 ** 2, "워커 프로세스 PeakWorkingSetSize"
            except Exception:  # noqa: BLE001
                pass
        return None, "측정 실패"
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024, "자식 프로세스 최대 RSS(ffmpeg 등 포함)"
    except ImportError:
        return None, "이 플랫폼에서 측정 수단 없음"


def run_benchmark(cfg: Config, ff: FFmpeg, provider_name: str, *, voice: str | None, targets_s: list[int],
                  sentences_file: Path, listen_dir: Path | None, pron: dict[str, str] | None = None,
                  acceptance: dict | None = None, report_dir: Path) -> dict:
    sentences = [s.strip() for s in sentences_file.read_text(encoding="utf-8").splitlines() if s.strip()]
    provider = create_tts_provider(provider_name, cfg, ff)
    tmp = Path(tempfile.mkdtemp(prefix="avsync_bench_"))
    try:
        caps = provider.capabilities()
        voice = voice or ((cfg.defaults.get("providers") or {}).get(provider_name) or {}).get("default_voice")
        pron = pron or {}

        def synth(text: str, name: str):
            sp = apply_pronunciations(text, pron, caps.pronunciation_mode).spoken
            t0 = time.perf_counter()
            res = provider.synthesize(TTSRequest(sp, voice, 1.0, "en-US", tmp / f"{name}.wav"))
            return time.perf_counter() - t0, res

        cold_t0 = time.perf_counter()
        first_s, first_res = synth(split_sentences(sentences[0])[0], "cold")
        cold_total = time.perf_counter() - cold_t0           # 프로세스 기동 + 모델 로드 + 첫 합성
        runs = []
        for target in targets_s:
            wall = 0.0
            audio_us = 0
            n = 0
            i = 0
            while audio_us < target * 1_000_000:
                for sent in split_sentences(sentences[i % len(sentences)]):
                    dt, res = synth(sent, f"b{target}_{n}")
                    wall += dt
                    audio_us += res.duration_us
                    n += 1
                i += 1
            runs.append({"target_s": target, "audio_s": round(audio_us / 1e6, 2), "wall_s": round(wall, 2),
                         "rtf": round(wall / (audio_us / 1e6), 3), "synth_calls": n})
        peak, how = _peak_mb(provider)

        listening = []
        if listen_dir:
            listen_dir.mkdir(parents=True, exist_ok=True)
            for k, sent in enumerate(sentences, 1):
                parts = []
                for j, ss in enumerate(split_sentences(sent)):
                    _, r = synth(ss, f"L{k}_{j}")
                    parts.append(r.wav_path)
                dst = listen_dir / f"{k:02d}.wav"
                if len(parts) == 1:
                    shutil.copyfile(parts[0], dst)
                else:
                    from .audio.wavutil import concat, silence, write_wav
                    pcms = []
                    for p in parts:
                        pcm = read_wav(p)
                        if pcms:
                            pcms.append(silence(pcm.rate, pcm.channels, 450_000))
                        pcms.append(pcm)
                    write_wav(dst, concat(pcms))
                listening.append({"n": k, "text": sent, "file": dst.name})
            (listen_dir / "index.md").write_text(
                f"# 청취 평가 문장 ({provider_name}, voice={voice})\n\n"
                "평가 항목: 발음, 숫자·약어, 요가·산스크리트 용어, 속도, 음색\n\n"
                + "\n".join(f"{x['n']:02d}. `{x['file']}` - {x['text']}" for x in listening) + "\n",
                encoding="utf-8")
        rep = {
            "created": now_iso(), "provider": caps.as_dict(), "voice": voice,
            "environment": {"platform": platform.platform(), "python": platform.python_version(),
                            "cpu_count": os.cpu_count(),
                            "note": "이 수치는 측정을 실행한 기기의 값이다. 사용자 PC에서 실행한 결과만 판단 근거로 쓴다."},
            "cold_start_s": round(cold_total, 2), "first_request_s": round(first_s, 2),
            "provider_reported_cold_start_s": getattr(provider, "cold_start_s", None),
            "runs": runs, "peak_memory_mb": round(peak, 1) if peak else None, "peak_memory_method": how,
            "listening": listening, "listening_sentences": len(sentences),
            "acceptance": acceptance, "verdict": _verdict(runs, cold_total, acceptance, caps.production_ok),
        }
        write_json(report_dir / f"benchmark_{provider_name}.json", rep)
        return rep
    finally:
        provider.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _verdict(runs, cold, acc, production_ok) -> list[str]:
    out = []
    if not production_ok:
        out.append("이 공급자는 파이프라인 시험 전용이라 기본 공급자 후보가 아님")
    if not acc or acc.get("tts_30s_max_minutes") is None:
        out.append("허용 대기 시간(acceptance_wait)이 미입력이라 통과/실패를 판정하지 않음")
        return out
    r30 = next((r for r in runs if r["target_s"] == 30), None)
    if r30:
        minutes = r30["wall_s"] / 60
        ok = minutes <= acc["tts_30s_max_minutes"]
        out.append(f"30초 분량 합성 {minutes:.2f}분 vs 허용 {acc['tts_30s_max_minutes']}분: {'통과' if ok else '초과'}")
    return out
