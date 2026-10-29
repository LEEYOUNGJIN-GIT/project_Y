import json
import sys
from pathlib import Path

import pytest

from avsync.cli import main
from avsync.providers.tts.base import ProviderError, RateLimitError, TTSRequest
from avsync.providers.tts.gemini_cli import GeminiCliTTSProvider, redact

FAKE = Path(__file__).parent.parent / "fake_gemini_cli.py"
SECRET = "AIzaSyFAKEKEYFAKEKEYFAKEKEY123456"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_COUNTER", str(tmp_path / "counter"))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return monkeypatch


def make(ff, tmp_path, *, ack=True, **kw):
    ackp = tmp_path / "ack.json"
    if ack:
        ackp.write_text("{}")
    return GeminiCliTTSProvider(ff, command=[sys.executable, str(FAKE)], ack_path=ackp, backoff_s=0.01, **kw)


def req(tmp_path, text="Inhale, arms up.", voice="Kore", name="a"):
    return TTSRequest(text, voice, 1.0, "en-US", tmp_path / f"{name}.wav")


def test_success_sends_only_the_text_and_converts_to_wav(env, ff, tmp_path):
    p = make(ff, tmp_path, model="m-1", style_prefix="Say calmly: ")
    r = p.synthesize(req(tmp_path))
    calls = [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
    assert calls == [{"text": "Say calmly: Inhale, arms up.", "voice": "Kore", "model": "m-1"}]    # 문장+스타일 외에 보내지 않음
    assert r.duration_us == pytest.approx(len("Say calmly: Inhale, arms up.") * 0.05 * 1e6, rel=0.02)
    caps = p.capabilities()
    assert caps.name == "gemini-cli" and not caps.speed and caps.network and "m-1" in caps.version and "fake 0.0.1" in caps.version


def test_output_sample_rate_is_not_assumed(env, ff, tmp_path):
    env.setenv("FAKE_RATE", "16000")
    r = make(ff, tmp_path).synthesize(req(tmp_path))
    assert r.sample_rate == 16000


def test_requires_free_ack_and_voice(env, ff, tmp_path):
    with pytest.raises(ProviderError, match="무료 등급 확인"):
        make(ff, tmp_path, ack=False).synthesize(req(tmp_path))
    assert make(ff, tmp_path, ack=False, free_only=False).synthesize(req(tmp_path)).duration_us > 0
    with pytest.raises(ProviderError, match="voice_id"):
        make(ff, tmp_path).synthesize(req(tmp_path, voice=None))
    assert not (tmp_path / "calls.jsonl").exists() or "무료" not in (tmp_path / "calls.jsonl").read_text()


def test_auth_error_not_retried_and_key_redacted(env, ff, tmp_path):
    env.setenv("FAKE_MODE", "auth")
    p = make(ff, tmp_path)
    with pytest.raises(ProviderError) as e:
        p.synthesize(req(tmp_path))
    assert "인증 실패" in str(e.value) and SECRET not in str(e.value) and "[REDACTED]" in str(e.value)
    assert p.attempts == 1


def test_usage_error_not_retried(env, ff, tmp_path):
    env.setenv("FAKE_MODE", "usage")
    p = make(ff, tmp_path)
    with pytest.raises(ProviderError, match="종료 코드 2"):
        p.synthesize(req(tmp_path))
    assert p.attempts == 1


def test_rate_limit_retries_are_bounded_then_fail_closed(env, ff, tmp_path):
    env.setenv("FAKE_MODE", "rate")
    p = make(ff, tmp_path, max_retries=2)
    with pytest.raises(RateLimitError) as e:
        p.synthesize(req(tmp_path))
    assert p.attempts == 3 and SECRET not in str(e.value) and "캐시" in str(e.value) and "--provider" in str(e.value)


def test_transient_rate_limit_recovers(env, ff, tmp_path):
    env.setenv("FAKE_MODE", "rate_once")
    p = make(ff, tmp_path, max_retries=2)
    assert p.synthesize(req(tmp_path)).duration_us > 0 and p.attempts == 2


def test_non_rate_failure_is_plain_provider_error_without_retry(env, ff, tmp_path):
    env.setenv("FAKE_MODE", "fail")
    p = make(ff, tmp_path)
    with pytest.raises(ProviderError, match="종료 코드 1") as e:
        p.synthesize(req(tmp_path))
    assert not isinstance(e.value, RateLimitError) and p.attempts == 1


def test_redact():
    assert SECRET not in redact(f"bad key {SECRET} here") and "api_key=abc123" not in redact("x api_key=abc123 y")


def test_confirm_free_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AVSYNC_CONFIG_DIR", str(_cfg_with_cache(tmp_path)))
    assert main(["gemini", "confirm-free"]) == 1                     # --yes 없이는 안내만 하고 기록하지 않음
    assert not (tmp_path / "cache" / "gemini_free_ack.json").exists()
    assert main(["gemini", "confirm-free", "--yes"]) == 0
    assert (tmp_path / "cache" / "gemini_free_ack.json").is_file()


def _cfg_with_cache(tmp_path):
    import shutil
    from avsync.config import config_dir
    d = tmp_path / "cfg"
    shutil.copytree(config_dir(), d)
    y = (d / "defaults.yaml").read_text(encoding="utf-8").replace("model_cache: null", f"model_cache: '{(tmp_path / 'cache').as_posix()}'")
    (d / "defaults.yaml").write_text(y, encoding="utf-8")
    return d
