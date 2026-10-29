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
    p = SubprocessTTSProvider([sys.executable, "-I", str(WORKER)])
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
