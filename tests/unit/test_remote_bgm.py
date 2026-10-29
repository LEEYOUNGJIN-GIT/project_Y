import json
import math
import random
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest
from fake_acestep_server import Handler, start

from avsync.audio.vocals import FPS, score_envelope
from avsync.cli import main
from avsync.config import load_config
from avsync.domain.manifest import read_json
from avsync.remote.kaggle import KaggleCli, RemoteDirs, RemoteError, parse_status, remote_fetch, remote_run, remote_status, slugify
from avsync.remote.kaggle_script import render_script
from avsync.remote.plan import PlanError, load_plan

FAKE_KAGGLE = Path(__file__).parent.parent / "fake_kaggle_cli.py"


def write_plan(tmp_path, body: str) -> Path:
    p = tmp_path / "prompts.yaml"
    p.write_text(body, encoding="utf-8")
    return p


GOOD = """tracks:
  - {id: calm_pad_01, duration_s: 12, prompt: "calm ambient instrumental, warm pad, no vocals", bpm_hint: [70, 85], seeds: [11, 12]}
  - {id: soft_02, duration_s: 20, prompt: "soft felt piano, beatless, instrumental", seeds: [5]}
avoid: [artist_names]
"""


# ---------------------------------------------------------------- 계획 검증

def test_plan_valid(tmp_path):
    plan = load_plan(write_plan(tmp_path, GOOD))
    assert plan.jobs == 3 and plan.total_audio_s == 12 * 2 + 20 and plan.tracks[0].bpm_hint == (70, 85)
    assert plan.hash() == load_plan(write_plan(tmp_path, GOOD)).hash() and len(plan.hash()) == 12


@pytest.mark.parametrize("prompt,needle", [
    ("ambient pad in the style of Brian Eno", "모사"),
    ("music by Hans Zimmer, no vocals", "모사"),
    ("pad, contact me@example.com, no vocals", "개인 정보"),
    ("download from https://x.y/z instrumental", "개인 정보"),
    ("ambient pad with soft singing", "보컬 관련"),
])
def test_plan_rejects_imitation_private_and_vocal_prompts(tmp_path, prompt, needle):
    with pytest.raises(PlanError, match=needle):
        load_plan(write_plan(tmp_path, f'tracks:\n  - {{id: a, duration_s: 15, prompt: "{prompt}"}}\n'))


def test_plan_other_errors(tmp_path):
    with pytest.raises(PlanError) as e:
        load_plan(write_plan(tmp_path, """tracks:
  - {id: Bad Id, duration_s: 15, prompt: "pad, instrumental"}
  - {id: a, duration_s: 5, prompt: "pad, instrumental"}
  - {id: a, duration_s: 700, prompt: "pad, instrumental", vocals: allowed}
  - {id: b, duration_s: 15, prompt: "pad, instrumental", bpm_hint: [90, 60]}
"""))
    msg = str(e.value)
    assert "소문자" in msg and "10-600초" in msg and "중복" in msg and "vocals는 forbidden" in msg and "bpm_hint" in msg


# ---------------------------------------------------------------- 상태 파싱 / slug

@pytest.mark.parametrize("text,state", [
    ('me/k has status "KernelWorkerStatus.COMPLETE"', "complete"), ('me/k has status "running"', "running"),
    ('me/k has status "KernelWorkerStatus.ERROR"\nFailure message: "OOM"', "error"),
    ('me/k has status "queued"', "queued"), ('me/k has status "CANCEL_ACKNOWLEDGED"', "cancelled"), ("???", "unknown")])
def test_parse_status(text, state):
    assert parse_status(text)[0] == state
    if state == "error":
        assert parse_status(text)[1] == "OOM"


def test_slug_matches_title():
    assert slugify("avsync bgm 1a2b3c4d") == "avsync-bgm-1a2b3c4d"


# ---------------------------------------------------------------- 보컬 휴리스틱 (합성 신호 기준; 실제 음악으로는 미검증)

def _speech(seed, syl=(0.08, 0.22), gap=(0.03, 0.15), n=30 * FPS):
    rnd, env = random.Random(seed), []
    while len(env) < n:
        if rnd.random() < 0.12:
            env += [0.002] * int(rnd.uniform(0.3, 0.8) * FPS)
        d = int(rnd.uniform(*syl) * FPS)
        env += [rnd.uniform(0.05, 0.15) * math.sin(math.pi * k / max(1, d)) ** 0.7 + 0.002 for k in range(d)]
        env += [0.002] * int(rnd.uniform(*gap) * FPS)
    return env[:n]


def test_vocal_heuristic_separates_syllabic_from_pads():
    n = 30 * FPS
    pad = [0.1 * (1 + 0.4 * math.sin(2 * math.pi * 0.3 * i / FPS)) for i in range(n)]
    assert score_envelope(pad).score < 0.1
    assert score_envelope([0.0] * n).windows == 0
    for seed in (1, 2, 3):
        assert score_envelope(_speech(seed)).score > 0.6
    gate4 = [0.1 if (i * 4 / FPS) % 1 < 0.5 else 0.01 for i in range(n)]
    assert score_envelope(gate4).score < 0.35                 # 규칙적 게이트는 '리듬'으로 감점


def test_vocal_heuristic_known_limitations_are_documented_by_tests():
    slow = score_envelope(_speech(4, (0.15, 0.35), (0.05, 0.25)))
    assert slow.score < 0.6                                   # 한계: 아주 느린 음절의 음성은 놓친다
    pluck3 = [0.1 * math.exp(-6 * ((i / FPS * 3) % 1) / 3) + 0.005 for i in range(30 * FPS)]
    assert 0.35 <= score_envelope(pluck3).score < 0.6         # 한계: 3Hz 플럭은 '불확실' 구간으로 들어온다


# ---------------------------------------------------------------- 생성 스크립트가 문서대로 서버와 통신하는가

def test_generated_script_talks_to_acestep_rest_flow(tmp_path, monkeypatch):
    plan = load_plan(write_plan(tmp_path, """tracks:
  - {id: ok_a, duration_s: 10, prompt: "calm pad, instrumental", bpm_hint: [70, 80], seeds: [11, -1]}
  - {id: bad_b, duration_s: 10, prompt: "FAIL pad, instrumental", seeds: [1]}
"""))
    script = tmp_path / "run.py"
    script.write_text(render_script(plan.as_dict(), plan.hash(), "abc123"), encoding="utf-8")
    srv, url = start()
    try:
        out = tmp_path / "tracks"
        env = {"AVSYNC_SKIP_INSTALL": "1", "AVSYNC_ACE_URL": url, "AVSYNC_OUT": str(out), "AVSYNC_WORK": str(tmp_path),
               "PATH": __import__("os").environ["PATH"], "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
        p = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=120)
        assert p.returncode == 0, p.stdout + p.stderr
    finally:
        srv.shutdown()
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    by = {(r["id"], r["seed"]): r for r in results}
    assert by[("ok_a", 11)]["file"] == "ok_a_s11.wav" and by[("ok_a", -1)]["file"] == "ok_a_s-1.wav"
    assert by[("bad_b", 1)]["file"] is None and "생성 실패" in by[("bad_b", 1)]["error"]      # 한 곡 실패해도 나머지 계속
    assert (out / "ok_a_s11.wav").read_bytes()[:4] == b"RIFF"
    meta = json.loads((out / "ok_a_s11.json").read_text(encoding="utf-8"))
    assert meta["dit_model"] == "acestep-v15-turbo" and meta["repo_revision"] == "skipped"
    first = Handler.requests[0]                              # 요청 본문이 문서의 파라미터 이름을 쓴다
    assert first["lyrics"] == "[Instrumental]" and first["audio_format"] == "wav" and first["audio_duration"] == 10
    assert first["thinking"] is False and first["use_random_seed"] is False and first["seed"] == 11 and first["bpm"] == 75
    assert Handler.requests[1]["use_random_seed"] is True and "seed" not in Handler.requests[1]


# ---------------------------------------------------------------- Kaggle CLI 래퍼 + 검증/등록

@pytest.fixture
def kaggle(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_KAGGLE_LOG", str(tmp_path / "push.json"))
    return KaggleCli([sys.executable, str(FAKE_KAGGLE)])


def _tone_wav(path: Path, seconds: float, hz=330, amp=0.3, am=None, rate=24000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            e = am(i / rate) if am else 1.0
            frames += struct.pack("<h", int(32767 * amp * e * math.sin(2 * math.pi * hz * i / rate)))
        w.writeframes(bytes(frames))


def test_run_status_fetch_register_and_discard(tmp_path, kaggle, ff, monkeypatch):
    plan = load_plan(write_plan(tmp_path, """tracks:
  - {id: good, duration_s: 12, prompt: "calm pad, instrumental", seeds: [1]}
  - {id: short, duration_s: 12, prompt: "calm pad, instrumental", seeds: [2]}
  - {id: voice, duration_s: 12, prompt: "calm pad, instrumental", seeds: [3]}
  - {id: broken, duration_s: 12, prompt: "calm pad, instrumental", seeds: [4]}
"""))
    dirs = RemoteDirs(tmp_path / "remote")
    dry = remote_run(plan, dirs, None, owner="me", accelerator=None, revision=None, dry_run=True)
    assert dry["dry_run"] and dry["kernel"]["id"] == f"me/avsync-bgm-{plan.hash()[:8]}" and not dirs.state.exists()
    res = remote_run(plan, dirs, kaggle, owner="me", accelerator="NvidiaTeslaT4", revision=None, dry_run=False)
    logged = json.loads((tmp_path / "push.json").read_text())
    assert logged["argv"][-2:] == ["--accelerator", "NvidiaTeslaT4"] and logged["meta"]["is_private"] is True
    assert read_json(dirs.state)["status"] == "queued" and res["ref"] == logged["meta"]["id"]
    assert (dirs.kernel / "run.py").is_file() and (dirs.kernel / "kernel-metadata.json").is_file()

    monkeypatch.setenv("FAKE_KAGGLE_STATUS", "KernelWorkerStatus.RUNNING")
    assert remote_status(dirs, kaggle)["status"] == "running"
    monkeypatch.setenv("FAKE_KAGGLE_STATUS", "KernelWorkerStatus.COMPLETE")
    assert remote_status(dirs, kaggle)["status"] == "complete"

    src = tmp_path / "kernel_out"
    src.mkdir()
    _tone_wav(src / "good_s1.wav", 12)
    _tone_wav(src / "short_s2.wav", 3)
    syl = random.Random(7)
    t_on = [0.0]
    segs = []
    while t_on[0] < 12:
        d = syl.uniform(0.08, 0.2); segs.append((t_on[0], t_on[0] + d)); t_on[0] += d + syl.uniform(0.03, 0.12)
    _tone_wav(src / "voice_s3.wav", 12, hz=900, am=lambda t: 1.0 if any(a <= t < b for a, b in segs) else 0.02)
    for n in ("good_s1", "short_s2", "voice_s3"):
        (src / f"{n}.json").write_text(json.dumps({"dit_model": "acestep-v15-turbo", "repo_revision": "rev1"}), encoding="utf-8")
    (src / "results.json").write_text(json.dumps([
        {"id": "good", "seed": 1, "file": "good_s1.wav", "error": None}, {"id": "short", "seed": 2, "file": "short_s2.wav", "error": None},
        {"id": "voice", "seed": 3, "file": "voice_s3.wav", "error": None},
        {"id": "broken", "seed": 4, "file": None, "error": "RuntimeError: 생성 실패"}]), encoding="utf-8")
    monkeypatch.setenv("FAKE_KAGGLE_TRACKS", str(src))

    lib = tmp_path / "library"
    rep = remote_fetch(dirs, kaggle, ff, load_config().gates, library=lib)
    assert [a["name"] for a in rep["accepted"]] == ["good_s1"]
    disc = {d["name"]: d["reasons"] for d in rep["discarded"]}
    assert "short_s2" in disc and "요청 길이" in disc["short_s2"][0]
    assert "voice_s3" in disc and "보컬" in disc["voice_s3"][0]
    assert rep["failed"][0]["name"] == "broken_s4" and rep["complete"] is True
    meta = next(lib.glob("good_s1_*.json")).read_text(encoding="utf-8")
    assert '"generator": "ace-step-1.5"' in meta and "보장되지 않음" in meta and '"vocals": "no"' in meta
    assert not list(lib.glob("short_s2_*")) and not list(lib.glob("voice_s3_*"))        # 폐기된 곡은 등록하지 않음


def test_fetch_without_results_gives_actionable_error(tmp_path, kaggle, ff, monkeypatch):
    plan = load_plan(write_plan(tmp_path, GOOD))
    dirs = RemoteDirs(tmp_path / "remote")
    remote_run(plan, dirs, kaggle, owner="me", accelerator=None, revision=None, dry_run=False)
    empty = tmp_path / "empty"; empty.mkdir()
    monkeypatch.setenv("FAKE_KAGGLE_TRACKS", str(empty))
    with pytest.raises(RemoteError, match="results.json이 없습니다"):
        remote_fetch(dirs, kaggle, ff, load_config().gates, library=tmp_path / "lib")


def test_cli_remote_plan_and_dry_run(tmp_path, capsys):
    p = write_plan(tmp_path, GOOD)
    assert main(["bgm", "remote-plan", str(p)]) == 0
    assert "작업 3개" in capsys.readouterr().out
    assert main(["bgm", "remote-run", str(p), "--dry-run"]) == 1              # owner 없음
    assert main(["bgm", "remote-run", str(p), "--owner", "me", "--dry-run"]) == 0
    assert main(["bgm", "remote-plan", str(write_plan(tmp_path, 'tracks:\n  - {id: a, duration_s: 15, prompt: "pad by Aphex Twin"}\n'))]) == 1
