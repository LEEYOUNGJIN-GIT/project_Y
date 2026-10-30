import glob
from pathlib import Path

import pytest

from avsync.captions.ass import Caption, CaptionStyle, FontError, build_ass, prepare_ass_dir, read_font_family

FONTS = [p for pat in ("/usr/share/fonts/**/DejaVuSans.ttf", "/usr/share/fonts/**/*.ttf", "C:/Windows/Fonts/arial.ttf")
         for p in glob.glob(pat, recursive=True)]
needs_font = pytest.mark.skipif(not FONTS, reason="시스템에 TTF 폰트가 없음")


@needs_font
def test_font_family_is_read_from_name_table():
    fam = read_font_family(Path(FONTS[0]))
    assert fam and fam.isprintable()
    if FONTS[0].endswith("DejaVuSans.ttf"):
        assert fam == "DejaVu Sans"


def test_not_a_font_is_rejected(tmp_path):
    p = tmp_path / "x.ttf"
    p.write_bytes(b"not a font at all" * 10)
    with pytest.raises(FontError):
        read_font_family(p)


def style(tmp_path):
    return CaptionStyle(font_file=Path(FONTS[0]) if FONTS else tmp_path / "none.ttf", font_license="test license")


def test_karaoke_tags_sum_to_caption_length(tmp_path):
    words = [{"text": "Inhale", "start_us": 1_000_000, "end_us": 1_400_000},
             {"text": "up", "start_us": 1_600_000, "end_us": 1_900_000}]
    ass = build_ass([Caption(1_000_000, 2_000_000, "Inhale up", words)], style(tmp_path), "DejaVu Sans")
    line = [l for l in ass.splitlines() if l.startswith("Dialogue")][0]
    assert "Dialogue: 0,0:00:01.00,0:00:02.00,Default" in line
    # 단어 사이 틈(0.2s)은 앞 단어에 포함: 60cs + 40cs = 100cs(= 캡션 길이 1.0s)
    assert "{\\kf60}Inhale {\\kf40}up" in line


def test_plain_sentence_when_no_words_and_escaping(tmp_path):
    ass = build_ass([Caption(0, 1_000_000, "Say {hi}\\now")], style(tmp_path), "X")
    assert "Say (hi)\\\\now" in ass and "\\kf" not in ass


def test_zero_or_negative_captions_dropped(tmp_path):
    ass = build_ass([Caption(-2_000_000, -1_000_000, "gone"), Caption(0, 0, "empty")], style(tmp_path), "X")
    assert "Dialogue" not in ass


@needs_font
def test_prepare_requires_license_and_font(tmp_path):
    st = style(tmp_path)
    prepare_ass_dir(tmp_path / "ok", [Caption(0, 1_000_000, "a")], st)
    assert (tmp_path / "ok" / "captions.ass").is_file() and (tmp_path / "ok" / "fonts" / Path(FONTS[0]).name).is_file()
    st.font_license = " "
    with pytest.raises(FontError, match="라이선스"):
        prepare_ass_dir(tmp_path / "x", [], st)
    with pytest.raises(FontError, match="폰트 파일이 없습니다"):
        prepare_ass_dir(tmp_path / "y", [], CaptionStyle(font_file=tmp_path / "nope.ttf", font_license="l"))
