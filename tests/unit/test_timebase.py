from fractions import Fraction

import pytest

from avsync.domain.timebase import (TimeError, fmt_srt, fmt_time, parse_time, samples_to_us, us_to_samples)


@pytest.mark.parametrize("text,us", [
    ("00:04.5", 4_500_000), ("00:00.8", 800_000), ("1:02.250", 62_250_000), ("12.5", 12_500_000),
    ("01:00:00.001", 3_600_001_000), ("0:00", 0), ("-00:01.0", -1_000_000), ("00:03.240", 3_240_000),
])
def test_parse(text, us):
    assert parse_time(text) == us


def test_numbers_are_seconds():
    assert parse_time(12) == 12_000_000
    assert parse_time(0.3) == 300_000


def test_no_float_drift():
    # 0.1 + 0.2 같은 부동소수점 오차 없이 문자열에서 정수 us로
    assert parse_time("00:00.3") == 300_000
    assert parse_time("00:00.1") + parse_time("00:00.2") == parse_time("00:00.3")


def test_frames_need_fps():
    with pytest.raises(TimeError):
        parse_time("97f")
    assert parse_time("30f", fps=Fraction(30)) == 1_000_000
    assert parse_time("1f", fps=Fraction(30000, 1001)) == 33_367          # 29.97fps 한 프레임 = 33.3667ms


@pytest.mark.parametrize("bad", ["abc", "1:2:3:4", "00:61x", "", "1.2.3", True])
def test_bad(bad):
    with pytest.raises(TimeError):
        parse_time(bad)


def test_samples_roundtrip_is_exact_on_grid():
    for n in (0, 1, 47, 48000, 480001, 4_536_000):
        assert us_to_samples(samples_to_us(n, 48000), 48000) == n


def test_samples_rounding_is_integer_and_symmetric():
    assert us_to_samples(1_000_000, 48000) == 48000
    assert us_to_samples(-1_000_000, 48000) == -48000
    assert us_to_samples(10, 48000) == 0          # 0.48 샘플 -> 0
    assert us_to_samples(11, 48000) == 1          # 0.528 샘플 -> 1


def test_fmt():
    assert fmt_time(62_250_000) == "01:02.250"
    assert fmt_time(-1_500_000) == "-00:01.500"
    assert fmt_srt(3_723_456_000) == "01:02:03,456"
