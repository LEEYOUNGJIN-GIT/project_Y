"""CI 전용: 요가 큐잉에 맞는 목소리를 찾기 위한 청취용 변형 모음 (Kokoro, Windows 러너).

현재 설정(af_heart, 속도 0.95)이 "딱딱하고 기계적"이라는 평가에 대해, Kokoro 안에서 바꿀 수 있는 손잡이만 하나씩 바꿔 본다:
속도, 음색(단일/혼합), 문장 부호 조정, 후처리(EQ/잔향/피치). 이 스크립트는 소리를 만들 뿐 좋고 나쁨을 판정하지 않는다 -
판정은 사람이 듣고 한다. 모든 변형은 같은 문장 3개를 같은 순서로 읽고, 비교가 공정하도록 같은 음량으로 맞춘다.
"""
from __future__ import annotations

import array
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from avsync.audio.wavutil import Pcm, read_wav, write_wav
from avsync.config import load_config
from avsync.platform.ffmpeg import FFmpeg, find_tools
from avsync.providers.registry import create_tts_provider
from avsync.providers.tts.base import TTSRequest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ci_artifacts" / "voice_lab"
GAP_S = 0.9

SENTENCES = [
    "Inhale, arms up. Exhale, fold forward.",
    "Soften your shoulders. Let the breath move slowly.",
    "Stay here for five breaths. There is nothing to achieve.",
]

BASE_BLEND = "af_heart,af_nicole"
WARM = "highpass=f=70,bass=g=3:f=200,treble=g=-4:f=6000"
ROOM = "aecho=0.9:0.55:40|85:0.22|0.12"


def ellipsis(text: str) -> str:
    return text.replace(". ", "... ").replace(", ", ",  ")


def pitch_filter(rate: int, semitones: float) -> str:
    f = 2 ** (semitones / 12)
    return f"asetrate={rate * f:.2f},aresample={rate},atempo={1 / f:.6f}"


# (라벨, 음색, 속도, 문장 변환, 후처리 종류, 설명)
VARIANTS = [
    ("baseline_af_heart_0.95", "af_heart", 0.95, None, None, "현재 설정 (이전에 들으신 것과 같은 조건)"),
    ("speed_af_heart_0.85", "af_heart", 0.85, None, None, "속도만 0.85로"),
    ("speed_af_heart_0.78", "af_heart", 0.78, None, None, "속도만 0.78로 (더 느리게; 부자연스러운지 확인)"),
    ("voice_af_nicole_0.85", "af_nicole", 0.85, None, None, "다른 음색"),
    ("voice_af_sky_0.85", "af_sky", 0.85, None, None, "다른 음색"),
    ("voice_af_bella_0.85", "af_bella", 0.85, None, None, "다른 음색"),
    ("voice_bf_emma_0.85", "bf_emma", 0.85, None, None, "영국식 여성 음색"),
    ("voice_bf_isabella_0.85", "bf_isabella", 0.85, None, None, "영국식 여성 음색"),
    ("voice_am_michael_0.85", "am_michael", 0.85, None, None, "남성 음색"),
    ("blend_heart+nicole_0.85", "af_heart,af_nicole", 0.85, None, None, "음색 혼합(스타일 벡터 평균)"),
    ("blend_nicole+sky_0.85", "af_nicole,af_sky", 0.85, None, None, "음색 혼합"),
    ("blend_heart+emma_0.85", "af_heart,bf_emma", 0.85, None, None, "음색 혼합(미국+영국)"),
    ("text_ellipsis_heart_0.85", "af_heart", 0.85, ellipsis, None, "문장 부호로 끊기(마침표 -> 말줄임표, 쉼표 뒤 공백)"),
    ("fx_warm", BASE_BLEND, 0.85, None, "warm", "혼합 음색 + 따뜻한 EQ(저음 +3 dB, 고음 -4 dB)"),
    ("fx_warm_room", BASE_BLEND, 0.85, None, "room", "따뜻한 EQ + 아주 작은 방 울림"),
    ("fx_warm_pitch-1", BASE_BLEND, 0.85, None, "pitch-1", "따뜻한 EQ + 피치 1반음 낮춤"),
    ("fx_warm_pitch-2", BASE_BLEND, 0.85, None, "pitch-2", "따뜻한 EQ + 피치 2반음 낮춤"),
]


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def join(parts: list[Path], out: Path) -> float:
    pcms = [read_wav(p) for p in parts]
    rate = pcms[0].rate
    gap = Pcm(rate, 1, array.array("h", bytes(2 * int(rate * GAP_S))))
    data = pcms[0].data[:0]
    for i, p in enumerate(pcms):
        data = data + p.data + (gap.data if i < len(pcms) - 1 else gap.data[:0])
    write_wav(out, Pcm(rate, 1, data))
    return len(data) / rate


def fx_chain(kind: str | None, rate: int) -> str:
    if kind is None:
        chain = []
    elif kind == "warm":
        chain = [WARM]
    elif kind == "room":
        chain = [WARM, ROOM]
    elif kind.startswith("pitch"):
        chain = [pitch_filter(rate, float(kind[5:])), WARM]
    else:
        raise ValueError(kind)
    chain.append("loudnorm=I=-20:TP=-2:LRA=11")          # 비교가 공정하도록 음량 통일 (감상용, 마스터링 아님)
    return ",".join(chain)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    tools = find_tools(cfg)
    ff = FFmpeg(tools)
    provider = os.environ.get("LAB_PROVIDER", "kokoro")                       # 로컬 시험용 (CI에서는 kokoro)
    prov = create_tts_provider(provider, cfg, ff)
    forced = os.environ.get("LAB_VOICE_OVERRIDE")                               # 로컬 시험용: 모든 변형에 같은 음성 사용
    work = Path(tempfile.mkdtemp(prefix="avsync_lab_"))
    cache: dict[tuple, list[Path]] = {}
    rows = ["| # | 파일 | 길이(s) | 설명 |", "|---|---|---|---|"]
    meta = []
    try:
        for n, (label, voice, speed, transform, fx, desc) in enumerate(VARIANTS):
            voice = forced or voice
            key = (voice, speed, transform.__name__ if transform else None)
            if key not in cache:
                try:
                    wavs = []
                    for i, text in enumerate(SENTENCES):
                        t = transform(text) if transform else text
                        p = work / f"{len(cache)}_{i}.wav"
                        prov.synthesize(TTSRequest(t, voice, speed, "en-US", p))
                        wavs.append(p)
                except Exception as e:  # noqa: BLE001 - 없는 음색 등: 이 변형만 건너뛰고 계속
                    rows.append(f"| {n:02d} | {label} | - | 건너뜀: {type(e).__name__}: {str(e)[:100]} |")
                    continue
                cache[key] = wavs
            raw = work / f"raw_{n}.wav"
            join(cache[key], raw)
            rate = read_wav(raw).rate
            out = OUT / f"{n:02d}_{label}.wav"
            r = subprocess.run([str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                                "-af", fx_chain(fx, rate), "-ar", "24000", "-c:a", "pcm_s16le", str(out)],
                               capture_output=True, text=True, encoding="utf-8")
            if r.returncode != 0:
                rows.append(f"| {n:02d} | {label} | - | 후처리 실패: {r.stderr[-150:]} |")
                continue
            dur = read_wav(out).duration_us / 1e6
            rows.append(f"| {n:02d} | {out.name} | {dur:.1f} | {desc} |")
            meta.append({"file": out.name, "voice": voice, "speed": speed, "fx": fx, "text_transform": bool(transform),
                         "duration_s": round(dur, 2), "description": desc})
    finally:
        prov.close()
    (OUT / "README.md").write_text(
        "# 요가 큐잉 목소리 비교 (Kokoro)\n\n모든 파일은 같은 3개 문장을 읽고 같은 음량(-20 LUFS)으로 맞췄습니다. 번호 순서대로 들어 보세요.\n\n"
        + "\n".join(f"- {s}" for s in SENTENCES) + "\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    (OUT / "variants.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    summary("### 목소리 비교 변형 (청취용)\n" + "\n".join(rows))
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
