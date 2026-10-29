import dataclasses

import pytest
from helpers import FakeProvider

from avsync.domain.script import Line
from avsync.tts.fit import FitInput, allowed_end, finalize_flags, fit_lines
from avsync.tts.normalize import apply_pronunciations, split_sentences
from avsync.tts.synth import Synthesizer

GATES = dict(max_speed=1.06, speed_step=0.01, max_iterations=3, gap_min_ms=250)


def make_synth(tmp_path, provider, pron=None):
    return Synthesizer(provider, tmp_path / "tts", voice_id="v", lang="en-US", pronunciations=pron or {},
                       sentence_pause_ms=450)


def inp(text, at, nxt=None, until=None, end=60_000_000, speed=0.95, pause=0, lid="a"):
    return FitInput(Line(id=lid, text=text, pause_after_ms=pause), at, until, nxt, end, speed)


def test_split_sentences():
    assert split_sentences("Ekam. Inhale, arms up.") == ["Ekam.", "Inhale, arms up."]
    assert split_sentences("  One sentence only  ") == ["One sentence only"]


def test_pronunciation_modes():
    r = apply_pronunciations("Ekam. Then ekam again", {"Ekam": "EH-kum"}, "respell")
    assert r.spoken == "ehkum. Then ehkum again" and r.original == "Ekam. Then ekam again"
    ipa = apply_pronunciations("Inhale, Ekam.", {"Ekam": "/ˈɛkʌm/"}, "phoneme_markup")
    assert ipa.spoken == "Inhale, [Ekam](/ˈɛkʌm/)."
    skipped = apply_pronunciations("Ekam.", {"Ekam": "/ˈɛkʌm/"}, "respell")
    assert skipped.spoken == "Ekam." and skipped.warnings
    # 단어 경계: 'Shat'이 'Shatter' 안에서 치환되면 안 된다
    assert apply_pronunciations("Shatter. Shat.", {"Shat": "shut"}, "respell").spoken == "Shatter. shut."


def test_allowed_end_uses_gap_and_pause():
    fi = inp("x", 1_000_000, nxt=5_000_000)
    assert allowed_end(fi, 250) == 4_750_000
    assert allowed_end(inp("x", 1_000_000, nxt=5_000_000, pause=1000), 250) == 4_000_000     # pause_after가 더 큼
    assert allowed_end(inp("x", 1_000_000, nxt=5_000_000, until=3_000_000), 250) == 3_000_000
    assert allowed_end(inp("x", 1_000_000, end=2_000_000), 250) == 2_000_000


def test_speed_refit_fixes_slightly_long_line(tmp_path):
    fake = FakeProvider()
    syn = make_synth(tmp_path, fake)
    text = "Hello there friend"                              # 18자 -> 1.08s@1.0
    base = syn.synth_line(Line(id="a", text=text), 0.95).duration_us
    allowed = base - 60_000                                    # 기본 속도로는 60ms 초과
    out = fit_lines([inp(text, 0, until=allowed)], syn.synth_line, speed_supported=True, **{
        "max_speed": 1.06, "speed_step": 0.01, "max_iterations": 3, "gap_min_ms": 250})[0]
    assert out.status == "speed_adjusted" and out.flags == []
    assert 0.95 < out.speed <= 1.06
    assert out.end_us <= allowed


def test_speed_cap_and_exceeded_with_suggestions_but_text_untouched(tmp_path):
    fake = FakeProvider()
    syn = make_synth(tmp_path, fake)
    line = Line(id="a", text="This sentence is much too long. And it has a second sentence too. And a third one.")
    original = dataclasses.replace(line)
    out = fit_lines([FitInput(line, 0, 2_000_000, None, 60_000_000, 0.95)], syn.synth_line, speed_supported=True,
                    max_speed=1.06, speed_step=0.01, max_iterations=3, gap_min_ms=250)[0]
    assert out.status == "exceeded" and "exceeded" in out.flags
    assert out.speed == pytest.approx(1.06)                    # 상한을 넘기지 않는다
    assert out.suggestions and any("단어" in s for s in out.suggestions)
    assert line == original                                    # 문구를 자동으로 바꾸지 않는다
    assert max(fake.speeds) <= 1.06 + 1e-9


def test_no_speed_support_means_no_refit(tmp_path):
    fake = FakeProvider(speed_support=False)
    syn = make_synth(tmp_path, fake)
    out = fit_lines([inp("Hello there friend, how are you today", 0, until=500_000)], syn.synth_line,
                    speed_supported=False, **GATES)[0]
    assert out.status == "exceeded" and any("지원하지 않아" in n for n in out.notes)
    assert out.speed == 0.95


def test_overlap_and_past_end(tmp_path):
    syn = make_synth(tmp_path, FakeProvider())
    items = fit_lines([inp("A fairly long first sentence here.", 0, nxt=500_000, lid="a"),
                       inp("Second one.", 500_000, end=700_000, lid="b")],
                      syn.synth_line, speed_supported=True, **GATES)
    recs = [i.as_dict() for i in items]
    finalize_flags(recs, 700_000)
    assert "overlap" in recs[0]["flags"]
    assert "past_end" in recs[1]["flags"]


def test_cache_hits_and_isolation(tmp_path):
    fake = FakeProvider()
    syn = make_synth(tmp_path, fake)
    a, b = Line(id="a", text="First line here."), Line(id="b", text="Second line here.")
    a1, b1 = syn.synth_line(a, 0.95), syn.synth_line(b, 0.95)
    calls = syn.calls
    a2 = syn.synth_line(a, 0.95)
    assert a2.from_cache and syn.calls == calls and a2.wav_sha256 == a1.wav_sha256
    # b만 바꾸면 a의 캐시 키·파일 해시는 그대로
    b2 = syn.synth_line(Line(id="b", text="Second line CHANGED."), 0.95)
    assert not b2.from_cache and b2.cache_key != b1.cache_key
    assert syn.synth_line(a, 0.95).wav_sha256 == a1.wav_sha256
    # 속도가 다르면 다른 캐시
    assert syn.synth_line(a, 0.96).cache_key != a1.cache_key


def test_sentence_pause_and_segments(tmp_path):
    syn = make_synth(tmp_path, FakeProvider())
    la = syn.synth_line(Line(id="a", text="Ekam. Inhale, arms up."), 1.0)
    s0, s1 = la.segments
    assert s1.start_us - s0.end_us == 450_000                  # 문장 사이 무음
    assert la.duration_us == s1.end_us
    assert [s.original for s in la.segments] == ["Ekam.", "Inhale, arms up."]
