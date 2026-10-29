"""ffprobe로 입력 영상 정보를 읽는다 (7장)."""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from ..domain.timebase import US, parse_fps
from ..platform.ffmpeg import Tools
from ..platform.process import run


@dataclass
class VideoInfo:
    path: str
    duration_us: int
    width: int
    height: int
    rotation: int
    display_width: int
    display_height: int
    fps_num: int
    fps_den: int
    codec: str
    pix_fmt: str
    is_vfr: bool
    vfr_detail: str
    has_audio: bool
    audio_codec: str | None
    audio_rate: int | None
    audio_channels: int | None
    audio_duration_us: int | None
    nb_frames: int | None

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den or 1)

    @property
    def frame_us(self) -> int:
        return round(US / self.fps) if self.fps else 0

    def as_dict(self) -> dict:
        return asdict(self)


def _probe_json(tools: Tools, path: Path, extra: list[str]) -> dict:
    res = run([tools.ffprobe, "-v", "error", "-print_format", "json", *extra, str(path)], check=True,
              context=f"ffprobe 실패: {path}")
    return json.loads(res.stdout)


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag is not None else 0


def _dur_us(v) -> int | None:
    if v in (None, "N/A"):
        return None
    return round(float(v) * US)


def detect_vfr(tools: Tools, path: Path) -> tuple[bool, str]:
    """패킷 PTS 간격으로 판정한다. 간격의 중앙값에서 1% 넘게 벗어난 간격이 1% 넘으면 VFR로 본다.

    임계값은 이 도구의 시작값이며 사용자 기기 영상으로 보정해야 한다 (문서 11장: 미확인).
    """
    res = run([tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
               "packet=pts_time", "-of", "csv=p=0", str(path)], check=True)
    pts = sorted(float(x) for x in res.stdout.split() if x and x != "N/A")
    if len(pts) < 3:
        return False, "패킷이 너무 적어 판정 불가"
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    med = statistics.median(deltas)
    if med <= 0:
        return False, "간격 중앙값이 0"
    off = sum(1 for d in deltas if abs(d - med) > 0.01 * med)
    frac = off / len(deltas)
    return frac > 0.01, f"간격 {len(deltas)}개 중 {off}개가 중앙값({med * 1000:.3f}ms)에서 1% 이상 벗어남"


def probe_video(tools: Tools, path: Path) -> VideoInfo:
    path = Path(path)
    data = _probe_json(tools, path, ["-show_format", "-show_streams"])
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise ValueError(f"영상 스트림이 없습니다: {path}")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    w, h = int(v["width"]), int(v["height"])
    rot = _rotation(v)
    dw, dh = (h, w) if rot in (90, 270) else (w, h)
    fps = parse_fps(v.get("r_frame_rate") or v.get("avg_frame_rate") or "30/1")
    dur = _dur_us(v.get("duration")) or _dur_us(data.get("format", {}).get("duration"))
    if dur is None:
        raise ValueError(f"영상 길이를 알 수 없습니다: {path}")
    vfr, detail = detect_vfr(tools, path)
    nb = v.get("nb_frames")
    return VideoInfo(
        path=str(path), duration_us=dur, width=w, height=h, rotation=rot,
        display_width=dw, display_height=dh, fps_num=fps.numerator, fps_den=fps.denominator,
        codec=v.get("codec_name", ""), pix_fmt=v.get("pix_fmt", ""), is_vfr=vfr, vfr_detail=detail,
        has_audio=a is not None, audio_codec=a.get("codec_name") if a else None,
        audio_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        audio_channels=int(a["channels"]) if a and a.get("channels") else None,
        audio_duration_us=_dur_us(a.get("duration")) if a else None,
        nb_frames=int(nb) if nb and nb != "N/A" else None,
    )


def probe_audio_duration_us(tools: Tools, path: Path) -> int:
    data = _probe_json(tools, path, ["-show_format"])
    return _dur_us(data["format"]["duration"]) or 0


def stream_durations_us(tools: Tools, path: Path) -> dict[str, int | None]:
    """스트림별 길이. 영상/오디오 길이 일치(1프레임 이내) 검사에 쓴다."""
    data = _probe_json(tools, path, ["-show_entries", "stream=codec_type,duration:format=duration"])
    out: dict[str, int | None] = {"video": None, "audio": None, "format": _dur_us(data.get("format", {}).get("duration"))}
    for s in data.get("streams", []):
        if s.get("codec_type") in out and out[s["codec_type"]] is None:
            out[s["codec_type"]] = _dur_us(s.get("duration"))
    return out
