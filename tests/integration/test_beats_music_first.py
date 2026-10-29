import json
import random

import pytest
from clicks import RATE, click_track, mix, tones, write
from test_pipeline import AF_SCRIPT, cli, guide_files, write_project

from avsync.audio.beats import analyze_file
from avsync.guide.schedule import load_guide
from avsync.domain.script import ScriptError

pytestmark = pytest.mark.integration


def analyze(ff, tmp_path, name, sig):
    return analyze_file(ff, write(tmp_path / f"{name}.wav", sig), tmp_path / f"w_{name}")


@pytest.mark.parametrize("bpm,phase", [(90, 0.37), (72, 0.12), (120, 0.2)])
def test_tempo_and_phase_of_click_tracks(ff, tmp_path, bpm, phase):
    info = analyze(ff, tmp_path, f"c{bpm}", click_track(bpm, phase=phase))
    assert info.has_beat and info.bpm == pytest.approx(bpm, abs=0.3)
    assert abs(info.phase_us - phase * 1e6) < 15_000                      # 첫 박 위치 오차 15ms 이내
    assert info.downbeat_offset == 0                                     # 저음 킥이 4박마다 있는 구성


def test_noisy_and_soft_beats_over_a_pad_are_found(ff, tmp_path):
    assert analyze(ff, tmp_path, "n", click_track(65, noise=0.05)).bpm == pytest.approx(65, abs=0.5)
    bed = analyze(ff, tmp_path, "bed", mix(tones([220, 330]), click_track(90, accent=False, amp_scale=0.25)))
    assert bed.bpm == pytest.approx(90, abs=0.5)                          # 지속음 패드 위의 약한 박


@pytest.mark.parametrize("name,sig", [("pad2", tones([220, 330])), ("pad3", tones([196, 247, 294])),
                                      ("lfo", tones([220, 330], lfo=0.6)),
                                      ("noise", [int(3000 * random.Random(5).uniform(-1, 1)) for _ in range(30 * RATE)])])
def test_beatless_ambient_is_rejected(ff, tmp_path, name, sig):
    """이 계정의 기본 BGM(비트리스 앰비언트)이 박자로 오판되면 안 된다 - 시험 중 거짓 양성을 고쳤다."""
    info = analyze(ff, tmp_path, name, sig)
    assert not info.has_beat and info.notes


def test_tempo_octave_ambiguity_is_reported(ff, tmp_path):
    info = analyze(ff, tmp_path, "c140", click_track(140, phase=0.05))
    assert info.bpm in (pytest.approx(70, abs=0.5), pytest.approx(140, abs=0.5))
    assert any("옥타브" in n for n in info.notes)


def test_known_limitation_detuned_pad_beating_is_taken_for_a_pulse(ff, tmp_path):
    """한계를 테스트로 고정: 3Hz로 맥놀이하는 디튠 패드는 규칙적 진폭 펄스라 박으로 오인한다."""
    assert analyze(ff, tmp_path, "detune", tones([220, 223])).has_beat


# ---------------------------------------------------------------- 음악 우선 가이드

def project_for(tmp_path, ff, fake, bgm_samples, breath):
    bgm = write(tmp_path / "bgm_click.wav", bgm_samples)
    guide = f"""mode: audio_first
count_in: {{text: "Ready. Three.", count_in_s: 3.0, start_tone: true}}
breath: {breath}
steps:
  - {{line: l01, counts: 1}}
  - {{line: l02, counts: 1}}
  - {{line: l03, counts: 2}}
tail_s: 1.0
"""
    return write_project(tmp_path / "mf", guide=True, script=AF_SCRIPT, bgm=bgm, guide_yaml=guide)


def test_count_beats_with_bpm_from_bgm_aligns_music_to_breaths(tmp_path, ff, fake):
    proj = project_for(tmp_path, ff, fake, click_track(90, seconds=30, phase=0.37),
                       "{count_beats: 4, bpm: from_bgm, beat_align: true}")
    assert cli("guide", "--project", proj) == 0
    wav, gj = guide_files(proj)
    count_us = 4 * 60_000_000 / 90                                         # 2.6667초
    # from_bgm은 분석한 실제 템포를 따른다(90.0009 BPM 등). 몇십 us 차이는 정상이다
    assert gj["count_us"] == pytest.approx(count_us, abs=5_000) and gj["beats"]["bpm_source"] == "from_bgm"
    assert [s["start_us"] for s in gj["steps"]] == [pytest.approx(3_000_000 + k * count_us, abs=15_000) for k in (0, 1, 2)]
    # 독립 검증: 완성된 가이드 WAV를 다시 분석해 BGM의 박이 각 호흡 시작점에 놓이는지 본다
    info = analyze_file(ff, wav, tmp_path / "w_guide")
    assert info.has_beat and info.bpm == pytest.approx(90, abs=0.5)
    T = info.period_us
    for st in gj["steps"]:
        err = (st["start_us"] - info.phase_us + T // 2) % T - T // 2        # 가장 가까운 박과의 시간차
        assert abs(err) < 30_000, (st["line"], err)                         # 30ms 이내


def test_without_beat_align_music_is_not_shifted(tmp_path, ff, fake):
    proj = project_for(tmp_path, ff, fake, click_track(90, seconds=30, phase=0.02), "{count_beats: 4, bpm: 90}")
    assert cli("guide", "--project", proj) == 0
    wav, gj = guide_files(proj)
    info = analyze_file(ff, wav, tmp_path / "w_guide")
    # 첫 호흡(3.0초)은 박 주기(0.6667초)의 4.5배 지점이라, 위상 0.02초의 BGM 박과는 거의 반 박(약 0.31초) 어긋난다
    T = info.period_us
    err = (3_000_000 - info.phase_us + T // 2) % T - T // 2
    assert abs(err) > 200_000


def test_from_bgm_with_beatless_bgm_fails_with_guidance(tmp_path, ff, fake, capsys):
    proj = project_for(tmp_path, ff, fake, tones([220, 330]), "{count_beats: 4, bpm: from_bgm}")
    capsys.readouterr()
    assert cli("guide", "--project", proj) == 1
    err = capsys.readouterr().err
    assert "안정적인 박자를 찾지 못했습니다" in err and "count_s" in err


def test_explicit_bpm_disagreeing_with_bgm_warns(tmp_path, ff, fake, capsys):
    proj = project_for(tmp_path, ff, fake, click_track(90, seconds=30), "{count_beats: 4, bpm: 100, beat_align: true}")
    capsys.readouterr()
    assert cli("guide", "--project", proj) == 0
    assert "1% 넘게 다름" in capsys.readouterr().out


def test_cli_beats_command(tmp_path, ff, capsys):
    p = write(tmp_path / "c.wav", click_track(100, seconds=20, phase=0.1))
    assert cli("beats", p, "--json") == 0
    assert json.loads(capsys.readouterr().out)["bpm"] == pytest.approx(100, abs=0.5)
    q = write(tmp_path / "pad.wav", tones([220, 330], seconds=20))
    assert cli("beats", q) == 0
    assert "박자 없음" in capsys.readouterr().out


def test_guide_yaml_breath_validation(tmp_path):
    def load(breath):
        f = tmp_path / "g.yaml"
        f.write_text(f"mode: audio_first\ncount_in: {{count_in_s: 3}}\nbreath: {breath}\nsteps:\n  - {{line: a, counts: 1}}\n", encoding="utf-8")
        return load_guide(f)
    assert load("{count_beats: 4, bpm: 72}").count_beats == 4
    for bad, needle in (("{count_s: 3, count_beats: 4, bpm: 72}", "함께 쓸 수"), ("{count_beats: 4}", "breath.bpm이 필요"),
                        ("{count_beats: 0, bpm: 72}", "1 이상"), ("{count_beats: 4, bpm: 500}", "breath.bpm이 필요"),
                        ("{count_s: 3, bpm: 72}", "count_beats와 함께")):
        with pytest.raises(ScriptError, match=needle):
            load(bad)
