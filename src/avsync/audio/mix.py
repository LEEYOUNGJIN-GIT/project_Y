"""믹스 단계 (5.4). 단계마다 중간 파일을 남겨 재현·검증할 수 있게 한다.

우선순위: 내레이션 > 원본 소리 > BGM.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from ..domain.timebase import US, us_to_samples
from ..platform.ffmpeg import FFmpeg
from ..platform.process import ProcessFailed
from .envelope import build_envelope
from .loudness import master_normalize, measure_ebur128

RATE = 48000
_FMT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


@dataclass
class BusItem:
    wav: Path
    at_us: int
    duration_us: int
    id: str = ""


def _run_script(ff: FFmpeg, inputs: list[Path], script: str, out_label: str, out: Path, work: Path,
                codec: str = "pcm_f32le", context: str = "믹스") -> None:
    """긴 filter_complex는 인자 길이 제한(특히 Windows)을 피하려고 스크립트 파일로 전달한다."""
    work.mkdir(parents=True, exist_ok=True)
    sp = work / f"{out.stem}.filter.txt"
    sp.write_text(script, encoding="utf-8")
    args: list[str] = []
    for p in inputs:
        args += ["-i", str(p)]
    tail = ["-map", f"[{out_label}]", "-ar", str(RATE), "-c:a", codec, str(out)]
    # FFmpeg 6.x는 -filter_complex_script, 7.x 이후는 -/filter_complex (구 옵션은 폐지 예정). 둘 다 시도한다.
    try:
        ff.run([*args, "-filter_complex_script", str(sp), *tail], context=context)
    except ProcessFailed as e:
        if "Unrecognized option" not in e.result.stderr:
            raise
        ff.run([*args, "-/filter_complex", str(sp), *tail], context=context)


def render_bus(ff: FFmpeg, items: list[BusItem], duration_us: int, out: Path, work: Path, *,
               high_pass_hz: float | None = None, comp_ratio: float | None = None, fade_ms: int = 15) -> None:
    """항목 WAV들을 정수 샘플 위치에 배치한 스테레오 48 kHz 버스를 만든다. 리샘플링은 여기서 한 번만 한다."""
    total = us_to_samples(duration_us, RATE)
    lines = []
    fade = fade_ms / 1000
    for i, it in enumerate(items):
        d = it.duration_us / US
        f = min(fade, d / 2)
        delay = us_to_samples(it.at_us, RATE)
        lines.append(
            f"[{i}:a]{_FMT},afade=t=in:d={f:.4f},afade=t=out:st={max(0, d - f):.6f}:d={f:.4f},"
            f"adelay=delays={delay}S:all=1[n{i}]"
        )
    labels = "".join(f"[n{i}]" for i in range(len(items)))
    chain = [f"{labels}amix=inputs={len(items)}:normalize=0:duration=longest:dropout_transition=0"]
    if high_pass_hz:
        chain.append(f"highpass=f={high_pass_hz}")
    if comp_ratio and comp_ratio > 1:
        chain.append(f"acompressor=threshold=0.1:ratio={comp_ratio}:attack=20:release=250")
    chain.append(f"apad=whole_len={total}")
    chain.append(f"atrim=end_sample={total}")
    lines.append(",".join(chain) + "[bus]")
    _run_script(ff, [i.wav for i in items], ";\n".join(lines), "bus", out, work, context="내레이션 버스")


def prepare_bgm(ff: FFmpeg, src: Path, src_duration_us: int, duration_us: int, out: Path, work: Path, *,
                fade_in_s: float, fade_out_s: float, crossfade_s: float = 2.0,
                midrange_cut_db: float = 0.0) -> dict:
    """BGM을 영상 길이에 맞춘다: 짧으면 크로스페이드 루프, 길면 자름, 끝은 페이드아웃. 속도는 바꾸지 않는다."""
    xf = crossfade_s
    L = src_duration_us / US
    D = duration_us / US
    info = {"loops": 1, "crossfade_s": 0.0}
    if L >= D:
        n = 1
        chain = f"[0:a]{_FMT}"
        inputs = [src]
    else:
        if L <= 2 * xf:
            raise ValueError(f"BGM이 너무 짧아 크로스페이드 루프를 만들 수 없습니다 ({L:.1f}s <= {2 * xf:.1f}s)")
        n = math.ceil((D - xf) / (L - xf))
        inputs = [src] * n
        fmts = ";".join(f"[{i}:a]{_FMT}[b{i}]" for i in range(n))
        prev = "b0"
        xf_parts = []
        for i in range(1, n):
            xf_parts.append(f"[{prev}][b{i}]acrossfade=d={xf}:c1=tri:c2=tri[x{i}]")
            prev = f"x{i}"
        chain = fmts + ";" + ";".join(xf_parts) + (";" if xf_parts else "") + f"[{prev}]anull"
        info = {"loops": n, "crossfade_s": xf}
    tail = [f"atrim=end={D:.6f}", "asetpts=PTS-STARTPTS"]
    if midrange_cut_db:
        tail.append(f"equalizer=f=2000:t=h:w=3000:g={midrange_cut_db}")
    if fade_in_s > 0:
        tail.append(f"afade=t=in:d={fade_in_s}")
    if fade_out_s > 0:
        tail.append(f"afade=t=out:st={max(0.0, D - fade_out_s):.6f}:d={fade_out_s}")
    total = us_to_samples(duration_us, RATE)
    tail += [f"apad=whole_len={total}", f"atrim=end_sample={total}"]
    script = chain + "," + ",".join(tail) + "[bgm]"
    _run_script(ff, inputs, script, "bgm", out, work, context="BGM 준비")
    return info


def apply_gain(ff: FFmpeg, src: Path, out: Path, work: Path, *, gain_db: float) -> None:
    _run_script(ff, [src], f"[0:a]volume={gain_db}dB[o]", "o", out, work, context="이득 적용")


def apply_envelope(ff: FFmpeg, src: Path, env: Path, out: Path, work: Path) -> None:
    _run_script(ff, [src, env], "[0:a][1:a]amultiply[o]", "o", out, work, context="덕킹 적용")


def extract_original(ff: FFmpeg, media: Path, start_us: int, duration_us: int, out: Path, work: Path, *,
                     stream_offset_us: int = 0) -> None:
    """원본 영상의 소리를 [start, start+duration] (영상 시간) 구간으로 뽑아 48 kHz 스테레오로 맞춘다.

    stream_offset_us: 디코드한 오디오 0초가 영상 시간의 몇 us인지(오디오 start_time - 영상 start_time).
    """
    total = us_to_samples(duration_us, RATE)
    s = start_us / US
    if stream_offset_us > 0:
        pre = f"adelay=delays={us_to_samples(stream_offset_us, RATE)}S:all=1,"
    elif stream_offset_us < 0:
        pre = f"atrim=start_sample={us_to_samples(-stream_offset_us, RATE)},asetpts=PTS-STARTPTS,"
    else:
        pre = ""
    script = (f"[0:a]{_FMT},{pre}atrim=start={s:.6f},asetpts=PTS-STARTPTS,"
              f"apad=whole_len={total},atrim=end_sample={total}[o]")
    _run_script(ff, [media], script, "o", out, work, context="원본 소리 추출")


def sum_stems(ff: FFmpeg, stems: list[Path], out: Path, work: Path) -> None:
    labels = "".join(f"[{i}:a]" for i in range(len(stems)))
    script = f"{labels}amix=inputs={len(stems)}:normalize=0:duration=longest:dropout_transition=0[o]"
    _run_script(ff, stems, script, "o", out, work, context="스템 합산")


def stretch_audio(ff: FFmpeg, src: Path, tempo: float, out: Path, work: Path) -> None:
    """피치를 유지한 채 길이를 1/tempo배로 만든다 (atempo, 범위 0.5-2.0). 미세 드리프트 보정용."""
    if not 0.5 <= tempo <= 2.0:
        raise ValueError(f"atempo 범위(0.5-2.0) 밖: {tempo}")
    _run_script(ff, [src], f"[0:a]atempo={tempo:.8f}[o]", "o", out, work, context="드리프트 보정(atempo)")


def align_audio(ff: FFmpeg, src: Path, shift_us: int, duration_us: int, out: Path, work: Path) -> None:
    """src의 0초가 결과의 shift_us 위치에 오도록 놓는다. 음수면 앞부분을 잘라 낸다(검은 화면/무음 삽입 없음)."""
    total = us_to_samples(duration_us, RATE)
    if shift_us >= 0:
        pre = f"adelay=delays={us_to_samples(shift_us, RATE)}S:all=1"
    else:
        pre = f"atrim=start_sample={us_to_samples(-shift_us, RATE)},asetpts=PTS-STARTPTS"
    script = f"[0:a]{_FMT},{pre},apad=whole_len={total},atrim=end_sample={total}[o]"
    _run_script(ff, [src], script, "o", out, work, context="오디오 정렬")


@dataclass
class MasterResult:
    path: Path
    loudness: dict
    narration_lufs: float | None
    bgm_lufs: float | None
    bgm_gain_db: float | None


def normalize_master(ff: FFmpeg, premix: Path, out: Path, *, lufs: float, true_peak: float, budget_db: float,
                     codec: str = "pcm_s16le") -> dict:
    return master_normalize(ff, premix, out, target_i=lufs, target_tp=true_peak, limiter_budget_db=budget_db,
                            codec=codec)


def compute_bgm_gain(ff: FFmpeg, narration_bus: Path, bgm_fit: Path, level_rel_db: float) -> tuple[float, float | None, float | None]:
    """BGM 통합음량이 (내레이션 통합음량 + level_rel_db)가 되도록 하는 이득(dB)."""
    n = measure_ebur128(ff, narration_bus).integrated
    b = measure_ebur128(ff, bgm_fit).integrated
    if n is None or b is None:
        raise RuntimeError("내레이션 또는 BGM이 무음이라 상대 음량을 계산할 수 없습니다")
    return (n + level_rel_db) - b, n, b


def make_duck_envelope(path: Path, duration_us: int, windows: list[tuple[int, int]], *, depth_db: float,
                       attack_ms: int, release_ms: int) -> list[tuple[int, int]]:
    g = 10 ** (-abs(depth_db) / 20)
    return build_envelope(path, duration_us=duration_us, rate=RATE, base_gain=1.0, window_gain=g,
                          windows=windows, attack_us=attack_ms * 1000, release_us=release_ms * 1000)


def make_keep_only_envelope(path: Path, duration_us: int, keep_windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """원본 소리를 remove로 둘 때, no_narration 구간에서만 소리를 살린다 (짧은 램프로 클릭 방지)."""
    return build_envelope(path, duration_us=duration_us, rate=RATE, base_gain=0.0, window_gain=1.0,
                          windows=keep_windows, attack_us=20_000, release_us=20_000)
