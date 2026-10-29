import pytest
from helpers import FakeProvider

from avsync.domain.script import Line
from avsync.tts.normalize import is_cue_sentence, split_emphasis
from avsync.tts.synth import Synthesizer


def test_split_emphasis():
    assert split_emphasis("Inhale, lift the arms.", ["Inhale"]) == [("Inhale,", True), ("lift the arms.", False)]
    assert split_emphasis("Lift the arms, then Exhale slowly.", ["Exhale"]) == [
        ("Lift the arms, then", False), ("Exhale", True), ("slowly.", False)]
    assert split_emphasis("Inhale, lift. Exhale, fold.", ["Inhale", "Exhale"]) == [
        ("Inhale,", True), ("lift. Exhale,", False)][:1] + [("lift.", False), ("Exhale,", True), ("fold.", False)]
    assert split_emphasis("Lift the arms.", ["Exhale"]) == [("Lift the arms.", False)]          # 못 찾으면 분할 안 함
    assert split_emphasis("Inhale in", ["in"]) == [("Inhale", False), ("in", True)]              # 단어 경계


def test_is_cue_sentence():
    cues = {"ekam", "dve"}
    assert is_cue_sentence("Ekam.", cues) and is_cue_sentence("Ekam, Dve", cues)
    assert not is_cue_sentence("Ekam. Inhale", cues) and not is_cue_sentence("...", cues)


def make(tmp_path, fake, **kw):
    return Synthesizer(fake, tmp_path / "c", voice_id="v", lang="en-US", pronunciations=kw.pop("pron", {}),
                       sentence_pause_ms=450, **kw)


def test_emphasis_inserts_exact_pause(tmp_path):
    fake = FakeProvider()
    syn = make(tmp_path, fake, emphasis_pause_ms=150)
    la = syn.synth_line(Line(id="a", text="Inhale, lift the arms.", emphasis=["Inhale"]), 1.0)
    assert fake.texts == ["Inhale,", "lift the arms."]                 # 조각 합성
    assert len(la.segments) == 1 and la.segments[0].fragments == 2        # 자막은 문장 단위 유지
    plain = make(tmp_path / "p", FakeProvider()).synth_line(Line(id="a", text="Inhale, lift the arms."), 1.0)
    # 설정한 무음 150ms에 조각마다 붙는 트림 여유(앞 20 + 뒤 60 = 80ms)가 더해지고,
    # 조각으로 나누면 문장 중간 공백 한 글자(0.06s)가 빠진다: 150 + 80 - 60 = 170ms
    assert la.duration_us - plain.duration_us == pytest.approx(170_000, abs=1_000)
    gap = la.segments[0]
    assert gap.end_us - gap.start_us == la.duration_us
    assert la.segments[0].original == "Inhale, lift the arms."


def test_emphasis_off_when_disabled_and_cache_separate(tmp_path):
    fake = FakeProvider()
    line = Line(id="a", text="Inhale, lift the arms.", emphasis=["Inhale"])
    off = make(tmp_path, fake, emphasis_pause_ms=None).synth_line(line, 1.0)
    on = make(tmp_path, fake, emphasis_pause_ms=150).synth_line(line, 1.0)
    assert off.cache_key != on.cache_key and off.segments[0].fragments == 1


def test_cue_sentence_uses_cue_speed_and_ignores_refit(tmp_path):
    fake = FakeProvider()
    syn = make(tmp_path, fake, pron={"Ekam": "EH-kum"}, cue_speed=1.0)
    la = syn.synth_line(Line(id="a", text="Ekam. Inhale, arms up."), 0.95)
    assert fake.speeds == [1.0, 0.95] and [s.is_cue for s in la.segments] == [True, False]
    fake.speeds.clear()
    syn.synth_line(Line(id="a", text="Ekam. Inhale, arms up."), 1.04)       # 재합성 속도는 본문에만 적용
    assert fake.speeds == [1.0, 1.04]


def test_cue_speed_capped_by_max_speed(tmp_path):
    fake = FakeProvider()
    syn = make(tmp_path, fake, pron={"Ekam": "x"}, cue_speed=1.5, max_speed=1.06)
    syn.synth_line(Line(id="a", text="Ekam."), 0.95)
    assert fake.speeds == [1.06]
