from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import FakeProvider  # noqa: E402

from avsync.config import load_config  # noqa: E402
from avsync.platform.ffmpeg import FFmpeg, ToolError, find_tools  # noqa: E402


@pytest.fixture(scope="session")
def ff():
    try:
        return FFmpeg(find_tools(load_config()))
    except ToolError:
        pytest.skip("FFmpeg 없음")


@pytest.fixture(scope="session")
def video916(tmp_path_factory, ff):
    """9:16 CFR H.264 30fps 12초, 120Hz 환경음 포함."""
    p = tmp_path_factory.mktemp("fx") / "v916.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=12", "-f", "lavfi", "-i", "sine=f=120:d=12",
            "-af", "volume=0.2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30",
            "-c:a", "aac", "-b:a", "96k", str(p)])
    return p


@pytest.fixture(scope="session")
def video169(tmp_path_factory, ff):
    p = tmp_path_factory.mktemp("fx") / "v169.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=6", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(p)])
    return p


@pytest.fixture(scope="session")
def video_rot(tmp_path_factory, ff):
    """가로로 인코딩되고 회전 메타(90도)만 붙은 세로 영상 - 휴대폰 영상의 흔한 형태."""
    d = tmp_path_factory.mktemp("fx")
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=12", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(d / "base.mp4")])
    ff.run(["-display_rotation", "90", "-i", str(d / "base.mp4"), "-c", "copy", str(d / "rot.mp4")])
    return d / "rot.mp4"


@pytest.fixture(scope="session")
def video_vfr(tmp_path_factory, ff):
    """프레임을 불규칙하게 빼서 만든 VFR 영상."""
    p = tmp_path_factory.mktemp("fx") / "vfr.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=8", "-vf", "select='not(mod(n,7))+not(mod(n,3))'",
            "-fps_mode", "vfr", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p)])
    return p


@pytest.fixture(scope="session")
def bgm_wav(tmp_path_factory, ff):
    p = tmp_path_factory.mktemp("fx") / "bgm.wav"
    ff.run(["-f", "lavfi", "-i", "sine=f=330:d=10", "-f", "lavfi", "-i", "sine=f=440:d=10",
            "-filter_complex", "[0][1]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo,volume=0.4",
            "-c:a", "pcm_s16le", str(p)])
    return p


@pytest.fixture
def fake(monkeypatch):
    prov = FakeProvider()
    monkeypatch.setattr("avsync.pipeline.create_tts_provider", lambda name, cfg, ff: prov)
    return prov
