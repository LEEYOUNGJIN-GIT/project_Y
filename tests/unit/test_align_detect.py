import random

from avsync.guide.align import BLOCK_US, detect_tone


def synth(n=600, tone_at=200, tone_blocks=20, noise=1e-6, seed=1):
    rnd = random.Random(seed)
    band = [noise * rnd.random() for _ in range(n)]
    total = [b + noise * 5 * rnd.random() for b in band]
    for i in range(tone_at, tone_at + tone_blocks):
        band[i] += 1e-2
        total[i] += 1.05e-2                       # 거의 모든 에너지가 톤 대역
    return band, total


def test_finds_tone_onset_within_one_block():
    band, total = synth(tone_at=200)
    hits = detect_tone(band, total, 100)
    assert hits and abs(hits[0].time_us - 200 * BLOCK_US) <= BLOCK_US
    assert hits[0].tonality > 0.9


def test_broadband_noise_burst_is_rejected():
    band, total = synth(tone_at=200)
    for i in range(400, 420):                      # 같은 크기의 광대역 소음: 톤 대역 비율이 낮다
        band[i] += 1e-4
        total[i] += 1e-2
    hits = detect_tone(band, total, 100)
    assert [h.time_us for h in hits][:1] == [200 * BLOCK_US] and all(h.time_us != 400 * BLOCK_US for h in hits)


def test_no_tone_returns_empty():
    rnd = random.Random(3)
    band = [1e-6 * rnd.random() for _ in range(500)]
    assert detect_tone(band, [b * 5 for b in band], 100) == []
    assert detect_tone([0.0] * 5, [0.0] * 5, 100) == []                      # 너무 짧은 입력
