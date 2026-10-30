"""사용자 PC용 Gemini 스타일 비교 키트(scripts/gemini_style_lab.py)의 동작을 가짜 CLI로 시험한다. 실제 Gemini 호출은 아니다."""
import importlib.util
import sys
from pathlib import Path

import pytest

from avsync.platform.ffmpeg import FFmpeg, find_tools
from avsync.config import load_config
from avsync.providers.tts.gemini_cli import GeminiCliTTSProvider

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests" / "fake_gemini_cli.py"


@pytest.fixture(scope="module")
def lab():
    spec = importlib.util.spec_from_file_location("gemini_style_lab", ROOT / "scripts" / "gemini_style_lab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def prov(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    ack = tmp_path / "ack.json"
    ack.write_text("{}")
    try:
        ff = FFmpeg(find_tools(load_config()))
    except Exception:
        pytest.skip("FFmpeg 없음")
    return GeminiCliTTSProvider(ff, command=[sys.executable, str(FAKE)], ack_path=ack, backoff_s=0.01)


def test_runs_all_variants_and_flags_instruction_read_aloud(lab, prov, tmp_path):
    out = tmp_path / "lab"
    rc = lab.run_lab(prov, ["V1"], lab.STYLES, lab.SENTENCES, out, max_calls=16)
    assert rc == 0
    wavs = sorted(p.name for p in out.glob("*.wav"))
    assert wavs == [f"0{i + 1}_V1_{s}.wav" for i, (s, _) in enumerate(lab.STYLES)]          # 임시 파일은 정리됨
    readme = (out / "README.md").read_text(encoding="utf-8")
    none_row = next(l for l in readme.splitlines() if "_none.wav" in l)
    assert "예 (확인 필요)" not in none_row
    # 가짜 CLI는 길이가 글자 수에 비례하므로 지시문이 붙은 변형은 '읽었을 가능성'으로 표시된다
    assert all("예 (확인 필요)" in l for l in readme.splitlines() if "_calm.wav" in l or "_instructor.wav" in l)


def test_refuses_before_any_call_when_over_cap(lab, prov, tmp_path):
    with pytest.raises(SystemExit, match="상한"):
        lab.run_lab(prov, ["A", "B", "C"], lab.STYLES, lab.SENTENCES, tmp_path / "x", max_calls=16)       # 3x4x2 = 24
    assert not (tmp_path / "calls.jsonl").exists()                                                          # 호출 0회


def test_rate_limit_stops_gracefully_and_keeps_partial_results(lab, prov, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "rate")
    out = tmp_path / "rl"
    assert lab.run_lab(prov, ["V1"], lab.STYLES, lab.SENTENCES, out, max_calls=16) == 2
    assert "한도" in (out / "README.md").read_text(encoding="utf-8")
    assert not list(out.glob("_*.wav"))
