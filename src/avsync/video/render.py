"""영상 mux와 리프레임 (7장). 가능하면 영상 스트림을 복사하고, 필요한 경우에만 재인코딩한다."""
from __future__ import annotations

import dataclasses
from pathlib import Path

from ..domain.timebase import US
from ..platform.ffmpeg import FFmpeg
from .probe import VideoInfo

ASPECT = 9 / 16


def is_9x16(info: VideoInfo) -> bool:
    return abs(info.display_width / info.display_height - ASPECT) < 0.005


def plan_video(info: VideoInfo, reframe: str, *, trim: bool, accurate_trim: bool = True) -> tuple[str, list[str]]:
    """('copy'|'reencode', 사유 목록)"""
    reasons: list[str] = []
    if info.codec != "h264":
        reasons.append(f"코덱이 h264가 아님({info.codec})")
    if info.pix_fmt != "yuv420p":
        reasons.append(f"pix_fmt이 yuv420p가 아님({info.pix_fmt})")
    if not is_9x16(info) and reframe != "none":
        reasons.append(f"9:16이 아님({info.display_width}x{info.display_height}), reframe={reframe}")
    if info.is_vfr:
        reasons.append(f"가변 프레임률(VFR) 의심: {info.vfr_detail}")
    if trim and accurate_trim:
        reasons.append("정확한 트림은 재인코딩이 필요")
    return ("reencode" if reasons else "copy"), reasons


def _reframe_filter(mode: str, w: int, h: int) -> str:
    if mode == "blur_pad":
        return (f"split=2[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"boxblur=luma_radius=40:luma_power=2[bg];[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p")
    if mode == "crop_center":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p"
    if mode == "pad_solid":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p")
    return "format=yuv420p"


def mux(ff: FFmpeg, info: VideoInfo, audio: Path, out: Path, profile: dict, reframe: str, *,
        trim_start_us: int = 0, trim_end_us: int | None = None, accurate_trim: bool = True,
        audio_codec_args: list[str] | None = None, burn_dir: Path | None = None) -> dict:
    """영상(info.path)과 오디오(audio)를 합친다. audio는 트림 후 타임라인 0초부터 시작한다고 본다."""
    trimmed = trim_start_us > 0 or trim_end_us is not None
    mode, reasons = plan_video(info, reframe, trim=trimmed, accurate_trim=accurate_trim)
    if burn_dir is not None:
        mode = "reencode"
        reasons.append("burn-in 자막은 재인코딩이 필요")
    out.parent.mkdir(parents=True, exist_ok=True)
    if burn_dir is not None:      # 필터 인자에는 상대 이름만 쓰므로 cwd를 바꾼다. 그 전에 모든 경로를 절대 경로로 고정
        out, audio = Path(out).resolve(), Path(audio).resolve()
        info = dataclasses.replace(info, path=str(Path(info.path).resolve()))

    pre: list[str] = []
    if trimmed:
        if trim_start_us > 0:
            pre += ["-ss", f"{trim_start_us / US:.6f}"]
        if trim_end_us is not None:
            pre += ["-t", f"{(trim_end_us - trim_start_us) / US:.6f}"]
    args = [*pre, "-i", str(info.path), "-i", str(audio)]

    if mode == "copy":
        args += ["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
    else:
        w, h = profile["width"], profile["height"]
        vf = _reframe_filter(reframe, w, h) if reframe != "none" else "format=yuv420p"
        graph = f"[0:v:0]{vf}[v]"
        if burn_dir is not None:
            graph = f"[0:v:0]{vf}[v0];[v0]ass=captions.ass:fontsdir=fonts[v]"
        args += ["-filter_complex", graph, "-map", "[v]", "-map", "1:a:0"]
        args += ["-c:v", profile["video_codec"], "-crf", str(profile["crf"]), "-preset", profile["preset"],
                 "-pix_fmt", profile["pix_fmt"]]
        if info.is_vfr:
            args += ["-fps_mode", "cfr", "-r", str(profile.get("default_fps", 30))]
    args += audio_codec_args or ["-c:a", profile["audio_codec"], "-b:a", profile["audio_bitrate"],
                                 "-ar", str(profile["audio_rate"]), "-ac", str(profile["audio_channels"])]
    args += ["-movflags", "+faststart", str(out)]
    ff.run(args, context="mux", cwd=burn_dir)
    return {"render_mode": mode, "reencode_reasons": reasons}
