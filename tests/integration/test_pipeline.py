"""FFmpeg가 필요한 통합 테스트. TTS는 가짜 공급자, 영상·BGM은 합성 파일을 쓴다."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from avsync.cli import main
from avsync.domain.manifest import read_json
from avsync.audio.loudness import measure_rms_db
from avsync.platform.process import run

pytestmark = pytest.mark.integration


def md5_video(ff, path: Path) -> str:
    r = run([ff.tools.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
             "-c", "copy", "-f", "md5", "-"], check=True)
    return r.stdout.strip()


def write_project(root: Path, *, video=None, guide=False, script: str, bgm: Path | None = None, extra_audio="",
                  guide_yaml: str | None = None, output: str | None = None):
    root.mkdir(parents=True, exist_ok=True)
    inp = "  guide: guide.yaml\n" if guide else f"  video: {video}\n"
    bg = (f"  bgm: {{source: imported, path: '{bgm}', license_note: 'test tone', duck_db: 6.0}}\n" if bgm
          else "  bgm: {source: none}\n")
    (root / "project.yaml").write_text(
        f"schema_version: 3\nproject_id: t1\ninput:\n{inp}  script: script.yaml\ntone: yoga_flow_calm\n"
        f"audio:\n  original: duck\n{extra_audio}{bg}"
        f"output: {output or '{profile: reels_9x16, reframe: blur_pad, captions: sidecar_srt}'}\n",
        encoding="utf-8")
    (root / "script.yaml").write_text(script, encoding="utf-8")
    if guide_yaml:
        (root / "guide.yaml").write_text(guide_yaml, encoding="utf-8")
    return root / "project.yaml"


VF_SCRIPT = """approved: {appr}
voice: {{provider: fake, voice_id: v1}}
no_narration: [["00:08.0", "00:10.0"]]
lines:
  - {{id: a, at: "00:01.0", text: "Start standing here.", mood: calm}}
  - {{id: b, at: "00:04.0", text: "Inhale, lift the arms.", mood: warm}}
"""

AF_SCRIPT = """approved: true
voice: {provider: fake, voice_id: v1}
lines:
  - {id: l01, text: "Ekam. Inhale up.", mood: calm}
  - {id: l02, text: "Dve. Exhale fold.", mood: calm}
  - {id: l03, text: "Stay here.", mood: calm}
"""
AF_GUIDE = """mode: audio_first
count_in: {text: "Ready. Three.", count_in_s: 3.0, start_tone: true}
breath: {count_s: 3.0}
steps:
  - {line: l01, counts: 1}
  - {line: l02, counts: 1}
  - {line: l03, counts: 2}
tail_s: 1.0
"""


def cli(*args) -> int:
    return main([str(a) for a in args])


# ------------------------------------------------------------------ video-first

def test_video_first_end_to_end_with_stream_copy(tmp_path, ff, fake, video916, bgm_wav, capsys):
    proj = write_project(tmp_path / "프로젝트 공백", video=video916, script=VF_SCRIPT.format(appr="true"), bgm=bgm_wav)
    p = ["--project", str(proj)]
    assert cli("plan", *p) == 0
    assert cli("tts", "--all", *p) == 0
    assert cli("mix", *p) == 0
    assert cli("render", *p) == 0
    out = proj.parent / "outputs" / "t1.mp4"
    assert out.is_file() and out.with_suffix(".srt").is_file()
    assert md5_video(ff, out) == md5_video(ff, video916)                  # 영상 스트림 비트 단위 보존
    rc = cli("qa", *p)
    rep = read_json(proj.parent / "outputs" / "qa_report.json")
    by = {c["name"]: c for c in rep["checks"]}
    assert by["av_duration"]["status"] == "pass"
    assert by["duck_depth"]["status"] == "pass" and by["duck_depth"]["value"] == pytest.approx(6.0, abs=0.3)
    assert by["fit_overlap"]["status"] == "pass"
    assert rc in (0, 2) and by["true_peak"]["status"] == "pass" and by["clipping"]["status"] == "pass"
    man = read_json(proj.parent / "manifests" / "manifest.json")
    seg = man["items"][0]["segments"][0]
    assert seg["original"] and seg["spoken"] and seg["normalized"]         # 원문·정규화문·발음문 보존


def test_unapproved_script_blocks_final_render(tmp_path, ff, fake, video916, capsys):
    proj = write_project(tmp_path / "p", video=video916, script=VF_SCRIPT.format(appr="false"))
    p = ["--project", str(proj)]
    assert cli("plan", *p) == 0 and cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0
    assert cli("render", *p) == 1                                          # 최종 렌더 거부
    assert not (proj.parent / "outputs" / "t1.mp4").exists()
    assert cli("render", "--preview", *p) == 0
    assert (proj.parent / "outputs" / "t1.preview.mp4").is_file()


def test_fit_overlap_detected_and_blocks_mix(tmp_path, ff, fake, video916, capsys):
    script = VF_SCRIPT.format(appr="true").replace('at: "00:04.0"', 'at: "00:01.5"')
    proj = write_project(tmp_path / "p", video=video916, script=script)
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 2
    rep = read_json(proj.parent / "manifests" / "fit_report.json")
    assert rep["summary"]["overlap"] >= 1
    assert cli("mix", *p) == 1


def test_no_narration_window_keeps_original_when_removed(tmp_path, ff, fake, video916):
    proj = write_project(tmp_path / "p", video=video916, script=VF_SCRIPT.format(appr="true"))
    (proj).write_text(proj.read_text().replace("original: duck", "original: remove"), encoding="utf-8")
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0
    master = proj.parent / "outputs" / "t1_mix.wav"
    inside = measure_rms_db(ff, master, 8_500_000, 1_000_000)             # no_narration 8~10초 구간
    outside = measure_rms_db(ff, master, 6_000_000, 1_500_000)            # 내레이션 없는 다른 구간
    assert inside is not None and inside > -40                              # 원본 환경음(120Hz)이 살아 있음
    assert outside is None or outside < -60                                 # remove라서 나머지는 무음


def test_reframe_169_to_9x16_reencodes(tmp_path, ff, fake, video169):
    proj = write_project(tmp_path / "p", video=video169, script=VF_SCRIPT.format(appr="true").replace('"00:08.0", "00:10.0"', '"00:04.5", "00:05.5"').replace('at: "00:04.0"', 'at: "00:02.5"'))
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0 and cli("render", *p) == 0
    out = proj.parent / "outputs" / "t1.mp4"
    r = run([ff.tools.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(out)], check=True)
    assert r.stdout.strip() == "1080,1920"
    assert read_json(proj.parent / "manifests" / "manifest.json")["render"]["render_mode"] == "reencode"


def test_rotated_phone_video_is_9x16_and_copied_with_rotation(tmp_path, ff, fake, video_rot):
    from avsync.config import load_config
    from avsync.platform.ffmpeg import find_tools
    from avsync.video.probe import probe_video
    info = probe_video(find_tools(load_config()), video_rot)
    assert (info.width, info.height, info.rotation) == (640, 360, 90)
    assert (info.display_width, info.display_height) == (360, 640)           # 회전 반영해 세로로 판정
    proj = write_project(tmp_path / "p", video=video_rot, script=VF_SCRIPT.format(appr="true"))
    p = ["--project", str(proj)]
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0 and cli("render", *p) == 0
    out = proj.parent / "outputs" / "t1.mp4"
    assert read_json(proj.parent / "manifests" / "manifest.json")["render"]["render_mode"] == "copy"
    side = run([ff.tools.ffprobe, "-v", "error", "-select_streams", "v", "-show_entries", "stream_side_data=rotation",
                "-of", "csv=p=0", str(out)], check=True).stdout
    assert "90" in side                                                       # 회전 메타데이터 유지


def test_vfr_detected_and_converted_to_cfr(tmp_path, ff, fake, video_vfr):
    proj = write_project(tmp_path / "p", video=video_vfr, script=VF_SCRIPT.format(appr="true").replace('"00:08.0", "00:10.0"', '"00:06.0", "00:07.0"'))
    p = ["--project", str(proj)]
    assert cli("plan", *p) == 0
    tl = read_json(proj.parent / "manifests" / "timeline.json")
    assert tl["video"]["is_vfr"] is True
    assert cli("tts", "--all", *p) == 0 and cli("mix", *p) == 0 and cli("render", *p) == 0
    man = read_json(proj.parent / "manifests" / "manifest.json")
    assert man["render"]["render_mode"] == "reencode" and any("VFR" in r for r in man["render"]["reencode_reasons"])


# ------------------------------------------------------------------ audio-first

@pytest.fixture
def af_project(tmp_path, ff, fake, bgm_wav):
    return write_project(tmp_path / "촬영 프로젝트", guide=True, script=AF_SCRIPT, bgm=bgm_wav, guide_yaml=AF_GUIDE)


def guide_files(proj: Path):
    d = proj.parent / "outputs" / "guide"
    wav = next(p for p in d.glob("guide_*.wav") if ".preview" not in p.name)
    return wav, json.loads(wav.with_suffix(".json").read_text(encoding="utf-8"))


def test_guide_exports_and_reports_total(af_project, ff, capsys):
    assert cli("guide", "--project", af_project) == 0
    wav, gj = guide_files(af_project)
    assert gj["total_us"] == 3_000_000 + 4 * 3_000_000 + 1_000_000 == 16_000_000
    assert wav.with_suffix(".m4a").is_file() and gj["hash8"] in wav.name
    dur = run([ff.tools.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(wav)],
              check=True).stdout.strip()
    assert float(dur) == pytest.approx(16.0, abs=1e-6)
    out = capsys.readouterr().out
    assert "총 길이 00:16.000" in out


def test_guide_item_too_long_is_error_and_no_export(af_project, ff, capsys):
    """한 호흡(3초)에 못 들어가는 긴 문구: 속도 상한을 써도 넘으면 exit 2, 가이드 파일은 만들지 않는다."""
    s = af_project.parent / "script.yaml"
    s.write_text(s.read_text().replace("Ekam. Inhale up.", "Ekam. " + "Inhale up now please. " * 8), encoding="utf-8")
    assert cli("guide", "--project", af_project) == 2
    out = capsys.readouterr().out
    assert "exceeded" in out and "제안" in out
    assert not list((af_project.parent / "outputs").glob("guide/guide_*.wav"))


def test_merge_places_audio_at_offset_and_preserves_video(af_project, ff, video916, tmp_path, capsys):
    assert cli("guide", "--project", af_project) == 0
    wav, gj = guide_files(af_project)
    out = tmp_path / "final out.mp4"
    rc = cli("merge", "--video", video916, "--guide", wav, "--offset", "00:00.500", "--first-mark", "00:03.55",
             "--last-mark", "00:09.45", "--out", out, "--original", "remove")
    assert rc == 0
    rep = read_json(out.with_suffix(".qa.json"))
    by = {c["name"]: c for c in rep["checks"]}
    assert by["av_duration"]["status"] == "pass"
    assert by["drift_first"]["value"] == pytest.approx(0.05, abs=1e-6)       # 3.55 - (0.5 + 3.0)
    assert by["drift_last"]["status"] == "pass"
    assert md5_video(ff, out) == md5_video(ff, video916)
    # 시작 톤은 raw 시간 0.5 + 3.0 = 3.5초에 있어야 한다 (880Hz 대역 에너지)
    def band(start, dur):
        r = run([ff.tools.ffmpeg, "-hide_banner", "-nostdin", "-ss", str(start), "-t", str(dur), "-i", str(out),
                 "-af", "bandpass=f=880:width_type=h:w=60,astats", "-f", "null", "-"])
        m = re.findall(r"RMS level dB: (-?[\d.]+|-inf)", r.stderr)
        return float(m[-1]) if m and "inf" not in m[-1] else -120.0
    assert band(3.52, 0.06) > band(3.1, 0.2) + 25
    # SRT 첫 자막 = offset + 첫 호흡(3.0) + lead_in(0.3) 근처
    first = re.search(r"00:00:(\d\d),(\d\d\d) -->", out.with_suffix(".srt").read_text(encoding="utf-8"))
    assert int(first.group(1)) * 1000 + int(first.group(2)) == 3800


def test_drift_beyond_tolerance_fails(af_project, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    rc = cli("merge", "--video", video916, "--guide", wav, "--offset", "0.5", "--last-mark", "00:09.90",
             "--out", tmp_path / "o.mp4", "--original", "remove")
    assert rc == 2
    assert read_json((tmp_path / "o.qa.json"))["status"] == "fail"


def test_negative_offset_trims_guide_front(af_project, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    out = tmp_path / "neg.mp4"
    assert cli("merge", "--video", video916, "--guide", wav, "--offset", "-00:01.0", "--out", out, "--original", "remove") == 0
    rep = read_json(out.with_suffix(".qa.json"))
    assert any("앞부분을 잘라냄" in n for n in rep["notes"])
    assert {c["name"]: c for c in rep["checks"]}["av_duration"]["status"] == "pass"


def test_trim_reencodes_and_keeps_durations_in_sync(af_project, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    out = tmp_path / "trim.mp4"
    assert cli("merge", "--video", video916, "--guide", wav, "--offset", "00:02.0", "--trim-start", "00:01.0",
               "--trim-end", "00:11.0", "--out", out, "--original", "remove") == 0
    rep = read_json(out.with_suffix(".qa.json"))
    assert rep["render"]["render_mode"] == "reencode"
    assert {c["name"]: c for c in rep["checks"]}["av_duration"]["status"] == "pass"
    dur = float(run([ff_probe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)], check=True).stdout)
    assert dur == pytest.approx(10.0, abs=0.07)


def ff_probe():
    from avsync.config import load_config
    from avsync.platform.ffmpeg import find_tools
    return find_tools(load_config()).ffprobe


def test_reshoot_warning_and_shot_hash(af_project, video916, tmp_path, capsys):
    cli("guide", "--project", af_project)
    wav1, gj1 = guide_files(af_project)
    s = af_project.parent / "script.yaml"
    s.write_text(s.read_text().replace("Dve. Exhale fold.", "Dve. Exhale and fold."), encoding="utf-8")
    capsys.readouterr()
    assert cli("guide", "--project", af_project) == 0
    assert "재촬영이 필요" in capsys.readouterr().out                        # 새 가이드가 이전 내보내기와 다름
    wavs = sorted((af_project.parent / "outputs" / "guide").glob("guide_*.wav"))
    assert len(wavs) == 2
    new = next(w for w in wavs if gj1["hash8"] not in w.name)
    # 첫 가이드로 촬영했다고 선언(--shot-hash)하고 새 가이드를 합성하면 거부
    assert cli("merge", "--video", video916, "--guide", new, "--offset", "0", "--shot-hash", gj1["hash8"],
               "--out", tmp_path / "x.mp4", "--original", "remove") == 1


def test_unapproved_guide_is_preview_only(tmp_path, ff, fake, bgm_wav, video916):
    proj = write_project(tmp_path / "p", guide=True, script=AF_SCRIPT.replace("approved: true", "approved: false"),
                         bgm=bgm_wav, guide_yaml=AF_GUIDE)
    assert cli("guide", "--project", proj) == 0
    previews = list((proj.parent / "outputs" / "guide").glob("guide_*.preview.wav"))
    assert len(previews) == 1
    assert cli("merge", "--video", video916, "--guide", previews[0], "--offset", "0", "--out", tmp_path / "a.mp4",
               "--original", "remove") == 1
    assert cli("merge", "--video", video916, "--guide", previews[0], "--offset", "0", "--out", tmp_path / "a.mp4",
               "--original", "remove", "--preview") == 0


def test_tampered_guide_wav_rejected(af_project, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    data = bytearray(wav.read_bytes())
    data[-100] ^= 0xFF
    wav.write_bytes(bytes(data))
    assert cli("merge", "--video", video916, "--guide", wav, "--offset", "0", "--out", tmp_path / "t.mp4",
               "--original", "remove") == 1


def test_original_policies_via_stems(af_project, ff, video916, tmp_path):
    """최종 mp4는 정책마다 정규화 수준이 달라 절대 레벨을 비교할 수 없으므로 중간 스템으로 검증한다."""
    cli("guide", "--project", af_project)
    wav, gj = guide_files(af_project)
    stems = {}
    for pol in ("remove", "duck", "keep"):
        out = tmp_path / f"{pol}.mp4"
        assert cli("merge", "--video", video916, "--guide", wav, "--offset", "0", "--out", out, "--original", pol) in (0, 2)
        stems[pol] = out.parent / f".avsync_merge_{out.stem}"
        assert read_json(out.with_suffix(".qa.json"))["original_policy_applied"] == (None if pol == "remove" else pol)
    assert not (stems["remove"] / "original_raw.wav").exists()                 # remove는 원본 소리를 아예 쓰지 않음
    win = gj["narration_windows"][1]                                            # 두 번째 항목 구간 (첫 구간은 카운트인)
    a, b = win[0] + 200_000, win[1] - 200_000
    raw = measure_rms_db(ff, stems["duck"] / "original_raw.wav", a, b - a)
    ducked = measure_rms_db(ff, stems["duck"] / "original_ducked.wav", a, b - a)
    assert raw - ducked == pytest.approx(15.0, abs=0.5)                         # duck_under_narration_db = -15
    quiet_a = gj["narration_windows"][-1][1] + 1_500_000                        # 내레이션이 없는 뒤쪽 구간에서는 감쇠 없음
    r2 = measure_rms_db(ff, stems["duck"] / "original_raw.wav", quiet_a, 1_000_000)
    d2 = measure_rms_db(ff, stems["duck"] / "original_ducked.wav", quiet_a, 1_000_000)
    assert r2 == pytest.approx(d2, abs=0.2)


# ------------------------------------------------------------------ align / stretch

def make_raw_with_guide(ff, wav: Path, out: Path, delay_s: float, tempo: float = 1.0, seconds: int = 24):
    """가이드 소리가 delay_s 뒤에 (옵션: tempo배 속도로) 스피커로 녹음된 것처럼 잡음과 섞은 촬영본."""
    ff.run(["-f", "lavfi", "-i", f"testsrc2=s=360x640:r=30:d={seconds}", "-i", str(wav),
            "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:a=0.01",
            "-filter_complex", f"[1:a]atempo={tempo},adelay={int(delay_s * 1000)}:all=1,aformat=channel_layouts=stereo[g];"
                               f"[2:a]aformat=channel_layouts=stereo[n];[g][n]amix=inputs=2:normalize=0,atrim=0:{seconds}[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(out)])


def test_align_recovers_offset_from_recorded_tone(af_project, ff, tmp_path, capsys):
    assert cli("guide", "--project", af_project) == 0
    wav, gj = guide_files(af_project)
    raw = tmp_path / "raw_tone.mp4"
    make_raw_with_guide(ff, wav, raw, delay_s=2.5)
    capsys.readouterr()
    assert cli("align", "--video", raw, "--guide", wav, "--json") == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert abs(hit["offset_us"] - 2_500_000) <= 20_000                                   # +-20ms
    out = tmp_path / "auto.mp4"
    assert cli("merge", "--video", raw, "--guide", wav, "--auto-align", "--out", out, "--original", "remove") in (0, 2)
    assert abs(read_json(out.with_suffix(".qa.json"))["offset_us"] - 2_500_000) <= 20_000


def test_align_reports_failure_when_no_tone(af_project, ff, video916, capsys):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    capsys.readouterr()
    assert cli("align", "--video", video916, "--guide", wav) == 2                         # 120Hz 환경음뿐: 톤 없음
    assert cli("merge", "--video", video916, "--guide", wav, "--auto-align") == 1


def test_stretch_corrects_clock_drift_and_independent_mark_verifies(af_project, ff, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, gj = guide_files(af_project)
    # 촬영 기기 클록이 1.8% 빠르게 가는 상황: 영상 시각 = 0.5 + 1.018 * 가이드 시각
    t = lambda g: f"00:{0.5 + 1.018 * g:06.3f}"      # noqa: E731
    base = ["--video", video916, "--guide", wav, "--offset", "0.5", "--original", "remove",
            "--first-mark", t(3.0), "--last-mark", t(9.0), "--mark", f"l02={t(6.0)}"]
    # 보정 없이: 마지막 마크가 162ms 어긋나 허용치(150ms)를 넘는다
    assert cli("merge", *base, "--out", tmp_path / "nostretch.mp4") == 2
    rep0 = read_json((tmp_path / "nostretch.qa.json"))
    assert {c["name"]: c for c in rep0["checks"]}["drift_last"]["status"] == "fail"
    # 보정: 첫/마지막 마크로 비율을 구해 가이드를 늘이면, 마크에 쓰지 않은 중간 카운트(l02)도 일치해야 한다
    assert cli("merge", *base, "--stretch", "--out", tmp_path / "s.mp4") == 0
    rep = read_json(tmp_path / "s.qa.json")
    by = {c["name"]: c for c in rep["checks"]}
    assert rep["stretch"]["scale"] == pytest.approx(1.018, abs=2e-5)
    assert by["drift_l02"]["status"] == "pass" and abs(by["drift_l02"]["value"]) < 0.005            # 5ms 이내
    d = float(run([ff.tools.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                   str(tmp_path / ".avsync_merge_s" / "guide_stretched.wav")], check=True).stdout)
    assert d == pytest.approx(16.0 * 1.018, abs=0.03)                                            # 길이가 비율만큼 늘어남 (atempo 처리 단위 오차 ~10ms)


def test_stretch_rejects_implausible_marks(af_project, video916, tmp_path):
    cli("guide", "--project", af_project)
    wav, _ = guide_files(af_project)
    rc = cli("merge", "--video", video916, "--guide", wav, "--offset", "0.5", "--original", "remove",
             "--first-mark", "00:03.5", "--last-mark", "00:20.0", "--stretch", "--out", tmp_path / "x.mp4")
    assert rc == 1                                                                                # 비율이 범위 밖: 보정하지 않고 거부
    assert cli("merge", "--video", video916, "--guide", wav, "--offset", "0.5", "--original", "remove",
               "--stretch", "--out", tmp_path / "y.mp4") == 1                                       # 마크 없이 stretch 불가


def test_audio_start_offset_is_respected_for_original_audio(tmp_path, ff):
    """오디오 스트림이 영상보다 0.5초 늦게 시작하는 파일: 원본 소리가 영상 시간 기준으로 놓여야 한다."""
    from avsync.audio import mix as M
    from avsync.config import load_config
    from avsync.platform.ffmpeg import find_tools
    from avsync.video.probe import probe_video
    vid = tmp_path / "late_audio.mp4"
    ff.run(["-f", "lavfi", "-i", "testsrc2=s=64x64:r=30:d=3", "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=f=440:d=2",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac", str(vid)])
    info = probe_video(find_tools(load_config()), vid)
    assert abs(info.audio_offset_us - 500_000) < 60_000
    out = tmp_path / "orig.wav"
    M.extract_original(ff, vid, 0, 3_000_000, out, tmp_path / "w", stream_offset_us=info.audio_offset_us)
    early = measure_rms_db(ff, out, 100_000, 300_000)        # 영상 시간 0.1-0.4초: 아직 소리 없음
    later = measure_rms_db(ff, out, 800_000, 400_000)        # 0.8-1.2초: 소리 있음
    assert (early is None or early < -60) and later is not None and later > -30
