"""사용자 PC에서 직접 실행하는 Gemini TTS 스타일 지시문 비교 키트.

왜 사용자가 실행하나: 이 개발 환경은 ai.google.dev에 접근할 수 없고, API 키 입력·저장과 `gemini confirm-free`는
도구가 대신하지 않기로 한 승인 범위 밖이다. 이 스크립트는 키를 읽지 않으며(gemini-api CLI가 관리), 무료 확인 파일도 만들지 않는다.

준비(사용자가 직접):
  1. gemini-api CLI 설치 후 인증 (`gemini-api whoami`로 확인)
  2. 결제가 연결되지 않은 프로젝트의 키인지 확인하고 `avsync gemini confirm-free --yes`
  3. `gemini-api tts --help`로 음성 이름 확인

실행 예:
  uv run python scripts/gemini_style_lab.py --voices <음성1>,<음성2>

호출 수 = 음성 수 x 스타일 수 x 문장 수. 한도를 아끼려고 기본 상한(--max-calls 16)을 넘으면 호출 전에 중단한다.
결과는 outputs/gemini_style_lab/ 에 번호가 붙은 WAV와 README.md로 남는다. 어느 쪽이 요가 큐잉에 맞는지는 들어서 정한다.
"""
from __future__ import annotations

import argparse
import array
import sys
import time
from pathlib import Path

from avsync.audio.wavutil import Pcm, read_wav, write_wav
from avsync.config import load_config
from avsync.platform.ffmpeg import FFmpeg, find_tools
from avsync.providers.tts.base import ProviderError, RateLimitError, TTSRequest

SENTENCES = [
    "Inhale, arms up. Exhale, fold forward.",
    "Soften your shoulders. Let the breath move slowly.",
]

# (이름, 지시문). 지시문은 문장 앞에 그대로 붙는다. 모델이 지시문을 소리 내어 읽을 수도 있어 길이로 점검한다.
STYLES = [
    ("none", ""),
    ("calm", "Say in a calm, soft, slow voice: "),
    ("instructor", "You are a gentle yoga instructor guiding a class in a quiet studio. "
                   "Speak slowly and softly, with warmth and unhurried pauses. Say: "),
    ("whisper", "Say in a soft, breathy, almost whispered voice, slowly, like guiding a meditation: "),
]
READ_ALOUD_RATIO = 1.6          # 기준(none)보다 이만큼 이상 길면 지시문을 읽었을 가능성으로 표시 (휴리스틱)
GAP_S = 0.9


def join(parts: list[Path], out: Path) -> None:
    pcms = [read_wav(p) for p in parts]
    rate = pcms[0].rate
    gap = array.array("h", bytes(2 * int(rate * GAP_S)))
    data = array.array("h")
    for i, p in enumerate(pcms):
        data += p.data
        if i < len(pcms) - 1:
            data += gap
    write_wav(out, Pcm(rate, 1, data))


def run_lab(prov, voices: list[str], styles: list[tuple[str, str]], sentences: list[str], out: Path, *,
            max_calls: int, pause_s: float = 0.0) -> int:
    """공급자 객체를 받아 변형을 합성한다. 반환: 0 정상, 2 한도/오류로 일부만 완료."""
    calls = len(voices) * len(styles) * len(sentences)
    if calls > max_calls:
        raise SystemExit(f"호출 {calls}회가 상한 {max_calls}회를 넘습니다. 음성·스타일을 줄이거나 --max-calls를 올리세요 (무료 한도 보호)")
    out.mkdir(parents=True, exist_ok=True)
    rows = ["| # | 파일 | 길이(s) | 지시문을 읽었을 가능성 | 지시문 |", "|---|---|---|---|---|"]
    status = 0
    n = 0
    baseline: dict[tuple[str, int], float] = {}
    try:
        for voice in voices:
            for sname, prefix in styles:
                prov.style_prefix = prefix
                parts, flag = [], ""
                for i, text in enumerate(sentences):
                    p = out / f"_{voice}_{sname}_{i}.wav"
                    res = prov.synthesize(TTSRequest(text, voice, 1.0, "en-US", p))
                    parts.append(p)
                    sec = res.duration_us / 1e6
                    if sname == "none":
                        baseline[(voice, i)] = sec
                    elif (voice, i) in baseline and sec > baseline[(voice, i)] * READ_ALOUD_RATIO:
                        flag = "예 (확인 필요)"
                    if pause_s:
                        time.sleep(pause_s)
                n += 1
                final = out / f"{n:02d}_{voice}_{sname}.wav"
                join(parts, final)
                for p in parts:
                    p.unlink(missing_ok=True)
                dur = read_wav(final).duration_us / 1e6
                rows.append(f"| {n:02d} | {final.name} | {dur:.1f} | {flag or '-'} | {prefix.strip() or '(없음)'} |")
    except RateLimitError as e:
        rows.append(f"\n**중단: 한도/일시 차단.** {e}")
        status = 2
    except ProviderError as e:
        rows.append(f"\n**중단: 공급자 오류.** {e}")
        status = 2
    finally:
        for p in out.glob("_*.wav"):
            p.unlink(missing_ok=True)
    (out / "README.md").write_text(
        "# Gemini TTS 스타일 지시문 비교\n\n같은 문장을 음성별·지시문별로 읽었습니다. 번호 순서대로 들어 보세요.\n\n"
        + "\n".join(f"- {s}" for s in sentences) + "\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    return status


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Gemini TTS 스타일 지시문 비교 (사용자 PC에서 실행)")
    ap.add_argument("--voices", required=True, help="쉼표로 구분한 음성 이름 (`gemini-api tts --help`로 확인)")
    ap.add_argument("--out", default="outputs/gemini_style_lab")
    ap.add_argument("--max-calls", type=int, default=16)
    ap.add_argument("--pause-s", type=float, default=1.0, help="호출 사이 대기(초). 한도 보호")
    args = ap.parse_args()
    from avsync.providers.registry import create_tts_provider
    cfg = load_config()
    prov = create_tts_provider("gemini", cfg, FFmpeg(find_tools(cfg)))
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    try:
        return run_lab(prov, voices, STYLES, SENTENCES, Path(args.out), max_calls=args.max_calls, pause_s=args.pause_s)
    finally:
        prov.close()


if __name__ == "__main__":
    sys.exit(main())
