from array import array

import pytest

from avsync.audio.envelope import build_envelope, merge_windows
from avsync.audio.wavutil import Pcm, read_wav, trim_silence, write_wav
from avsync.domain.script import Line, ScriptError
from avsync.guide.schedule import build_plan, load_guide

EXAMPLE = """mode: audio_first
count_in: {text: "Ready. Three, two, one.", count_in_s: 6.0, start_tone: true}
breath: {count_s: 4.5}
steps:
  - {line: l01, counts: 1}
  - {line: l02, counts: 1}
  - {line: l06, counts: 11, hold: "Shat 1 + 5 breaths", hold_mode: first_only}
  - {line: l09, counts: 1}
tail_s: 3.0
"""


def lines(*ids):
    return {i: Line(id=i, text=f"text {i}") for i in ids}


def guide(tmp_path, body):
    p = tmp_path / "guide.yaml"
    p.write_text(body, encoding="utf-8")
    return load_guide(p)


def test_schedule_arithmetic(tmp_path):
    plan = build_plan(guide(tmp_path, EXAMPLE), lines("l01", "l02", "l06", "l09"), lead_in_ms=300)
    starts = [s.start_us for s in plan.steps]
    assert starts == [6_000_000, 10_500_000, 15_000_000, 15_000_000 + 11 * 4_500_000]
    assert plan.steps[0].speak_at_us == 6_300_000                       # 호흡 시작 + lead_in
    assert plan.total_us == 6_000_000 + 14 * 4_500_000 + 3_000_000
    assert plan.tone_at_us == 6_000_000
    ci = plan.items[0]
    assert ci["id"] == "count_in" and ci["until_us"] == 6_000_000       # 카운트인은 첫 호흡 전에 끝나야 한다
    assert all(isinstance(i["at_us"], int) for i in plan.items)


def test_sample_case_total_is_94_5(tmp_path):
    """문서 12.5: 6 + 19카운트 x 4.5 + 3 = 94.5초."""
    ids = [f"l0{i}" for i in range(1, 10)]
    steps = "\n".join(f"  - {{line: {i}, counts: {11 if i == 'l06' else 1}}}" for i in ids)
    spec = guide(tmp_path, f"mode: audio_first\ncount_in: {{text: 'Ready', count_in_s: 6.0}}\nbreath: {{count_s: 4.5}}\nsteps:\n{steps}\ntail_s: 3.0\n")
    assert build_plan(spec, lines(*ids), lead_in_ms=300).total_us == 94_500_000


def test_every_breath_cues(tmp_path):
    body = EXAMPLE.replace('hold_mode: first_only', 'hold_mode: every_breath, hold_cues: [Inhale, Exhale]')
    plan = build_plan(guide(tmp_path, body), lines("l01", "l02", "l06", "l09"), lead_in_ms=300)
    cues = [i for i in plan.items if i["kind"] == "hold_cue"]
    assert len(cues) == 10 and [c["text"] for c in cues[:3]] == ["Inhale", "Exhale", "Inhale"]
    assert cues[0]["at_us"] == 15_000_000 + 4_500_000 + 300_000
    assert [i["at_us"] for i in plan.items] == sorted(i["at_us"] for i in plan.items)


def test_every_breath_requires_cues(tmp_path):
    with pytest.raises(ScriptError, match="hold_cues"):
        guide(tmp_path, EXAMPLE.replace('hold_mode: first_only', 'hold_mode: every_breath'))


def test_guide_errors(tmp_path):
    with pytest.raises(ScriptError, match="audio_first"):
        guide(tmp_path, EXAMPLE.replace("audio_first", "video_first"))
    with pytest.raises(ScriptError, match="count_s"):
        guide(tmp_path, EXAMPLE.replace("count_s: 4.5", "count_s: 0"))
    with pytest.raises(ScriptError, match="script.yaml에 없습니다"):
        build_plan(guide(tmp_path, EXAMPLE), lines("l01"), lead_in_ms=300)


def test_merge_windows():
    assert merge_windows([(5, 9), (1, 3), (2, 4), (9, 10)]) == [(1, 4), (5, 10)]


def test_envelope_levels_and_ramps(tmp_path):
    rate = 48000
    p = tmp_path / "env.wav"
    build_envelope(p, duration_us=3_000_000, rate=rate, base_gain=1.0, window_gain=0.5,
                   windows=[(1_000_000, 2_000_000)], attack_us=100_000, release_us=200_000)
    e = read_wav(p)
    assert e.frames == 3 * rate and e.channels == 2
    at = lambda sec: e.data[int(sec * rate) * 2] / 32767          # noqa: E731
    assert at(0.5) == pytest.approx(1.0, abs=1e-3)
    assert at(0.95) == pytest.approx(0.5 + 0.5 * 0.5, abs=0.02)   # 어택 중간쯤
    assert at(1.0 + 1e-3) == pytest.approx(0.5, abs=1e-3)          # 구간 시작에는 이미 감쇠 완료
    assert at(1.5) == pytest.approx(0.5, abs=1e-3)
    assert at(2.1) == pytest.approx(0.75, abs=0.03)                # 릴리스 중간
    assert at(2.5) == pytest.approx(1.0, abs=1e-3)


def test_trim_silence_keeps_margins(tmp_path):
    rate = 1000
    sig = array("h", [0] * 300 + [5000] * 200 + [0] * 500)
    out = trim_silence(Pcm(rate, 1, sig), keep_head_ms=20, keep_tail_ms=60)
    assert out.frames == 20 + 200 + 60
    assert trim_silence(Pcm(rate, 1, array("h", [0] * 100))).frames == 100      # 전부 무음이면 그대로


def test_wav_roundtrip(tmp_path):
    pcm = Pcm(24000, 1, array("h", [0, 1000, -1000, 32767, -32768]))
    p = tmp_path / "x.wav"
    write_wav(p, pcm)
    assert read_wav(p).data == pcm.data
