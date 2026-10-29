import pytest

from avsync.domain.script import ScriptError, count_words, load_script


def write(tmp_path, body):
    p = tmp_path / "script.yaml"
    p.write_text(body, encoding="utf-8")
    return p


GOOD = """approved: false
voice: {provider: kokoro, voice_id: null}
pronunciations: {Ekam: "EH-kum"}
no_narration: [["00:20.0", "00:24.0"]]
lines:
  - {id: l01, at: "00:00.8", text: "Start standing.", mood: calm}
  - {id: l02, at: "00:04.5", until: "00:09.0", text: "Inhale, lift the arms.", emphasis: [Inhale]}
"""


def test_valid(tmp_path):
    s = load_script(write(tmp_path, GOOD), require_at=True)
    assert [l.id for l in s.lines] == ["l01", "l02"]
    assert s.lines[0].at_us == 800_000 and s.lines[1].until_us == 9_000_000
    assert s.no_narration == [(20_000_000, 24_000_000)]
    assert s.voice.base_speed is None            # 미지정이면 톤 프리셋 값을 쓰도록 None


def test_errors_have_line_numbers(tmp_path):
    body = """approved: false
lines:
  - {id: l01, at: "00:05.0", text: "A."}
  - {id: l01, at: "00:06.0", text: "dup"}
  - {id: l03, at: "zzz", text: "bad time"}
  - {id: l04, at: "00:07.0", until: "00:06.0", text: "until before at"}
  - {id: l05, at: "00:03.0", text: "out of order"}
  - {id: l06, at: "00:08.0", text: ""}
"""
    with pytest.raises(ScriptError) as e:
        load_script(write(tmp_path, body), require_at=True)
    msg = str(e.value)
    assert ":4:" in msg and "중복" in msg
    assert ":5:" in msg and "시간 형식" in msg
    assert ":6:" in msg and "until" in msg
    assert ":7:" in msg and "시간순" in msg
    assert ":8:" in msg and "text가 비어" in msg


def test_at_required_only_for_video_first(tmp_path):
    body = "lines:\n  - {id: a, text: hi}\n"
    with pytest.raises(ScriptError, match="at이 필요"):
        load_script(write(tmp_path, body), require_at=True)
    assert load_script(write(tmp_path, body), require_at=False).lines[0].at_us is None


def test_narration_inside_no_narration_window(tmp_path):
    body = 'no_narration: [["00:10.0","00:12.0"]]\nlines:\n  - {id: a, at: "00:11.0", text: hi}\n'
    with pytest.raises(ScriptError, match="no_narration"):
        load_script(write(tmp_path, body), require_at=True)


def test_yaml_syntax_error_has_line(tmp_path):
    with pytest.raises(ScriptError, match=r"script.yaml:\d+"):
        load_script(write(tmp_path, "lines: [\n  - {id: a\n"), require_at=False)


def test_missing_file(tmp_path):
    with pytest.raises(ScriptError, match="파일이 없습니다"):
        load_script(tmp_path / "nope.yaml", require_at=False)


def test_count_words():
    assert count_words("Inhale, arms up.") == 3
    assert count_words("Don't stop; hip-width") == 3
