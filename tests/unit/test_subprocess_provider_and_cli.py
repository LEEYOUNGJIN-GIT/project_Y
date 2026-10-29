import json
import sys
from pathlib import Path

import pytest
from helpers import FakeProvider

from avsync.cli import main
from avsync.providers.tts.base import ProviderError, TTSRequest
from avsync.providers.tts.subprocess_provider import SubprocessTTSProvider

WORKER = Path(__file__).parent.parent / "fake_worker.py"


@pytest.fixture
def prov():
    p = SubprocessTTSProvider([sys.executable, str(WORKER)])
    yield p
    p.close()


def req(tmp_path, text="Hello there", voice="v", speed=1.0, name="a"):
    return TTSRequest(text, voice, speed, "en-US", tmp_path / f"{name}.wav")


def test_protocol_roundtrip(prov, tmp_path):
    caps = prov.capabilities()
    assert caps.name == "fakeworker" and caps.speed and prov.cold_start_s is not None
    r = prov.synthesize(req(tmp_path))
    assert r.duration_us == pytest.approx(11 * 0.05 * 1e6, abs=100) and r.meta["voice"] == "v"
    # 속도가 빠르면 짧아진다 - 공급자 인터페이스의 speed 의미
    assert prov.synthesize(req(tmp_path, speed=1.25, name="b")).duration_us < r.duration_us


def test_worker_error_becomes_provider_error_and_survives(prov, tmp_path):
    with pytest.raises(ProviderError, match="voice_id"):
        prov.synthesize(req(tmp_path, voice=None))
    assert prov.synthesize(req(tmp_path, name="ok")).duration_us > 0            # 오류 뒤에도 같은 워커로 계속 동작


def test_worker_crash_does_not_hang_and_restarts(prov, tmp_path):
    prov.capabilities()
    with pytest.raises(ProviderError, match="종료"):
        prov.synthesize(req(tmp_path, text="__CRASH__"))
    assert prov.synthesize(req(tmp_path, name="again")).duration_us > 0          # 다음 호출에서 워커 재기동


def test_close_is_idempotent(prov):
    prov.capabilities()
    prov.close()
    prov.close()


# ------------------------------------------------------------------ CLI 스모크 (FFmpeg 불필요한 것)

def test_init_blank_and_example(tmp_path, capsys):
    assert main(["init", "blank1", "--base", str(tmp_path)]) == 0
    assert (tmp_path / "blank1" / "project.yaml").is_file()
    assert main(["init", "ex1", "--base", str(tmp_path), "--example", "surya_a"]) == 0
    assert "project_id: ex1" in (tmp_path / "ex1" / "project.yaml").read_text(encoding="utf-8")
    assert main(["init", "blank1", "--base", str(tmp_path)]) == 1               # 이미 있으면 거부
    assert main(["init", "bad name!", "--base", str(tmp_path)]) == 1


def test_init_dry_run_writes_nothing(tmp_path):
    assert main(["init", "x", "--base", str(tmp_path), "--dry-run"]) == 0
    assert not (tmp_path / "x").exists()


def test_check_script_audio_first_example(tmp_path, capsys):
    main(["init", "ex", "--base", str(tmp_path), "--example", "surya_a"])
    capsys.readouterr()
    assert main(["check-script", "--project", str(tmp_path / "ex" / "project.yaml"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    msgs = " ".join(f["message"] for f in out["findings"])
    assert out["lines"] == 9 and "max_cue_words" in msgs                           # l06/l09가 6단어 초과


def test_missing_project_is_clean_error(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["plan"]) == 1
    assert "project.yaml" in capsys.readouterr().err


def test_benchmark_with_fake_provider(tmp_path, monkeypatch, capsys):
    from avsync.benchmark import run_benchmark
    from avsync.config import load_config
    from avsync.platform.ffmpeg import FFmpeg, find_tools
    monkeypatch.setattr("avsync.benchmark.create_tts_provider", lambda n, c, f: FakeProvider())
    cfg = load_config()
    rep = run_benchmark(cfg, FFmpeg(find_tools(cfg)), "fake", voice="v", targets_s=[5, 10],
                        sentences_file=cfg.root / "benchmarks" / "sentences.txt", listen_dir=tmp_path / "L",
                        acceptance={"tts_30s_max_minutes": None}, report_dir=tmp_path)
    assert [r["target_s"] for r in rep["runs"]] == [5, 10] and all(r["audio_s"] >= r["target_s"] for r in rep["runs"])
    assert len(list((tmp_path / "L").glob("*.wav"))) >= 20                         # 청취 평가 문장 20개 이상 (3.4)
    assert any("미입력" in v for v in rep["verdict"])                               # 허용 시간이 null이면 판정 보류
    assert (tmp_path / "benchmark_fake.json").is_file()


def test_worker_with_non_ascii_text_and_errors_roundtrips(tmp_path):
    """Windows 회귀: 자식 stdout 기본 인코딩이 cp1252여도 한글 오류 메시지·IPA 문자가 깨지거나 워커가 죽지 않아야 한다."""
    p = SubprocessTTSProvider([sys.executable, str(WORKER)])
    try:
        with pytest.raises(ProviderError, match="voice_id"):                    # 한글 포함 오류 응답
            p.synthesize(req(tmp_path, voice=None))
        r = p.synthesize(req(tmp_path, text="[Ekam](/ˈɛkʌm/). Inhale", name="ipa"))   # IPA 문자 입력
        assert r.duration_us > 0
    finally:
        p.close()


def test_doctor_filter_detection_uses_help_not_listing(ff):
    assert ff.has_filter("adelay") and ff.has_filter("amix")
    assert not ff.has_filter("definitely_not_a_filter")


def test_stdout_noise_from_libraries_is_ignored(prov, tmp_path, capfd):
    """Windows CI 회귀: 라이브러리가 stdout에 쓴 진행 메시지가 JSON 프로토콜을 깨면 안 된다."""
    r = prov.synthesize(req(tmp_path, text="__NOISE__"))
    assert r.duration_us > 0
    assert "Downloading en-core-web-sm" in capfd.readouterr().err               # 소음은 버리지 않고 stderr로 흘려보냄


def test_brace_lines_that_are_not_replies_do_not_desynchronise_the_protocol(prov, tmp_path, capfd):
    """`{`로 시작하는 잡음(딕셔너리 repr 등)을 응답으로 오인해 JSONDecodeError가 나거나 응답이 한 칸씩 밀리던 결함 (코드 리뷰)."""
    r = prov.synthesize(req(tmp_path, text="__BRACE__"))
    assert r.duration_us > 0
    err = capfd.readouterr().err
    assert "dict repr from a library" in err and "{not json at all" in err
    again = prov.synthesize(req(tmp_path, text="after the noise"))             # 다음 요청이 자기 응답을 받는다
    assert again.duration_us > 0 and again.meta.get("voice")


def test_cli_reports_missing_kaggle_cleanly(tmp_path, monkeypatch, capsys):
    """kaggle CLI가 없을 때 파이썬 트레이스백 대신 한국어 오류와 종료 코드 1 (코드 리뷰)."""
    from avsync.cli import main
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    monkeypatch.chdir(tmp_path)
    assert main(["bgm", "remote-status"]) == 1
    assert "kaggle" in capsys.readouterr().err


def test_benchmark_voice_is_per_provider_when_several_providers(capsys):
    """`--provider a,b --voice x`가 voice를 조용히 버려 첫 공급자에서 'voice_id 없음'으로 실패하던 동작 (코드 리뷰)."""
    from avsync.cli import _parse_voices, main
    assert _parse_voices(None, ["kokoro", "gemini"]) == {}
    assert _parse_voices("af_heart", ["kokoro"]) == {"kokoro": "af_heart"}
    assert _parse_voices("kokoro=af_heart,gemini=Kore", ["kokoro", "gemini"]) == {"kokoro": "af_heart", "gemini": "Kore"}
    assert main(["benchmark", "tts", "--provider", "kokoro,gemini", "--voice", "af_heart", "--dry-run"]) == 1
    assert "공급자별" in capsys.readouterr().err
    assert main(["benchmark", "tts", "--provider", "kokoro", "--voice", "kokoro=af_heart,x=y", "--dry-run"]) == 1
