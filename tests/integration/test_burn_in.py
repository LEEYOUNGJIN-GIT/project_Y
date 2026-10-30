"""burn-in 자막이 실제로 그려지는지 픽셀로 확인한다 (단색 배경 위 밝은 글자)."""
from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest
from test_pipeline import AF_GUIDE, AF_SCRIPT, VF_SCRIPT, cli, guide_files, write_project

from avsync.domain.manifest import read_json
from avsync.platform.process import run

pytestmark = pytest.mark.integration

FONTS = [p for pat in ("/usr/share/fonts/**/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/**/*.ttf")
         for p in glob.glob(pat, recursive=True)]


@pytest.fixture(scope="module")
def static_video(tmp_path_factory, ff):
    if not FONTS or not ff.has_filter("ass"):
        pytest.skip("폰트 또는 libass(ass 필터)가 없음")
    p = tmp_path_factory.mktemp("bi") / "blue.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=0x101060:s=360x640:r=30:d=12", "-f", "lavfi", "-i", "sine=f=120:d=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p)])
    return p


def ymax(ff, video: Path, t: float) -> float:
    """t초 프레임의 하단 영역(y 1300-1700) 최대 밝기(Y)."""
    r = run([ff.tools.ffmpeg, "-hide_banner", "-nostdin", "-ss", str(t), "-i", str(video), "-frames:v", "1",
             "-vf", "crop=iw:400:0:1300,signalstats,metadata=print:file=-", "-f", "null", "-"])
    m = re.findall(r"lavfi\.signalstats\.YMAX=(\d+)", r.stdout + r.stderr)
    assert m, r.stderr[-300:]
    return float(m[-1])


def style_yaml():
    return ("{profile: reels_9x16, reframe: blur_pad, captions: burn_in, "
            f"caption_style: {{font_file: '{FONTS[0]}', font_license: 'test-only system font', font_size: 90}}}}")


def test_video_first_burn_in_draws_text_only_while_spoken(tmp_path, ff, fake, static_video):
    proj = write_project(tmp_path / "자막 프로젝트", video=static_video, script=VF_SCRIPT.format(appr="true"),
                         output=style_yaml())
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0 and cli("render", *p) == 0
    out = proj.parent / "outputs" / "t1.mp4"
    man = read_json(proj.parent / "manifests" / "manifest.json")["render"]
    assert man["render_mode"] == "reencode" and any("burn-in" in r for r in man["reencode_reasons"])
    # 항목 a: 1.0초에 시작하는 "Start standing here."(약 1.2초 길이). 2.5초에는 자막이 없다
    assert ymax(ff, out, 1.5) > 180
    assert ymax(ff, out, 3.0) < 100
    assert out.with_suffix(".srt").is_file()                       # burn-in이어도 사이드카 SRT는 함께


def test_burn_in_requires_font_license(tmp_path, ff, fake, static_video):
    out = style_yaml().replace("font_license: 'test-only system font', ", "")
    proj = write_project(tmp_path / "p", video=static_video, script=VF_SCRIPT.format(appr="true"), output=out)
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0
    assert cli("render", *p) == 1                                  # 라이선스 없는 폰트는 거부 (10장)


def test_audio_first_merge_burn_in(tmp_path, ff, fake, static_video, bgm_wav):
    proj = write_project(tmp_path / "af", guide=True, script=AF_SCRIPT, bgm=bgm_wav, guide_yaml=AF_GUIDE,
                         output=style_yaml())
    assert cli("guide", "--project", proj) == 0
    wav, gj = guide_files(proj)
    out = tmp_path / "burn.mp4"
    assert cli("merge", "--project", proj, "--video", static_video, "--guide", wav, "--offset", "0.5", "--original", "remove",
               "--out", out, "--burn-in") in (0, 2)
    # l01 = offset 0.5 + 3.0 + 0.3 = 3.8초부터: "Ekam."(3.8-4.2) 틈(450ms) "Inhale up."(4.6-5.3)
    assert ymax(ff, out, 4.9) > 180 and ymax(ff, out, 4.4) < 100 and ymax(ff, out, 1.0) < 100   # 4.4초는 문장 사이 틈
    assert read_json(out.with_suffix(".qa.json"))["render"]["render_mode"] == "reencode"
