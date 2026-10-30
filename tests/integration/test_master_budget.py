"""목표 음량에 필요한 리미터 예산을 보고하고, 예산이 모자라면 정직하게 '미달'로 보고하는지."""
from __future__ import annotations

import pytest

from avsync.audio.loudness import master_normalize

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def flat(tmp_path_factory, ff):
    """피크가 낮은 일정한 사인파 - 리미터 없이 목표에 닿는다."""
    p = tmp_path_factory.mktemp("mb") / "flat.wav"
    ff.run(["-f", "lavfi", "-i", "sine=f=300:d=10:sample_rate=48000", "-af", "volume=0.03,aformat=channel_layouts=mono",
            "-c:a", "pcm_s16le", str(p)])
    return p


@pytest.fixture(scope="module")
def peaky(tmp_path_factory, ff):
    """작은 사인파 + 초당 한 번 20 ms의 큰 버스트 (말소리처럼 피크가 평균보다 훨씬 큼)."""
    p = tmp_path_factory.mktemp("mb") / "peaky.wav"
    ff.run(["-f", "lavfi", "-i", "sine=f=300:d=10:sample_rate=48000", "-f", "lavfi", "-i",
            "sine=f=500:d=10:sample_rate=48000", "-filter_complex",
            "[0]volume=0.03[a];[1]volume='if(lt(mod(t,1),0.02),0.5,0)':eval=frame[b];"
            "[a][b]amix=inputs=2:normalize=0,aformat=channel_layouts=mono",
            "-c:a", "pcm_s16le", str(p)])
    return p


def test_no_budget_needed_when_headroom_suffices(ff, flat, tmp_path):
    r = master_normalize(ff, flat, tmp_path / "a.wav", target_i=-14, target_tp=-1, limiter_budget_db=0.0)
    assert r["needed_budget_db"] == 0.0 and r["reached_target"] is True


def test_needed_budget_matches_definition_and_shortfall_is_reported(ff, peaky, tmp_path):
    r = master_normalize(ff, peaky, tmp_path / "b.wav", target_i=-14, target_tp=-1, limiter_budget_db=0.5)
    expect = max(0.0, (-14 - r["input_i"]) - (-1 - r["input_tp"]))
    assert r["needed_budget_db"] == pytest.approx(expect, abs=0.06)
    assert r["needed_budget_db"] > 0.5                           # 이 신호는 예산 0.5 dB로는 모자란다
    assert r["reached_target"] is False                          # 모자라면 맞춘 척하지 않는다
    assert r["output_i"] < -14 - 0.5
