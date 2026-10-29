"""Kaggle 커널에서 실행될 스크립트(run.py)를 만든다.

ACE-Step 1.5의 문서화된 REST 서버(`acestep-api`, 기본 127.0.0.1:8001)를 띄우고 /release_task -> /query_result ->
/v1/audio 로 트랙을 만든다 (docs/en/API.md 기준). 이 스크립트는 Kaggle에서 한 번도 실행되지 않았다. REST 호출 흐름은
로컬 가짜 서버로 검증했지만 설치(uv sync)·모델 다운로드·T4 호환/속도는 첫 시험 실행(10-15초 곡 2-3개)으로 확인해야 한다.
"""
from __future__ import annotations

import json

TEMPLATE = r'''# avsync가 생성한 Kaggle 커널 스크립트. 계획 해시: __PLAN_HASH__
import json, os, pathlib, subprocess, sys, time, traceback, urllib.parse, urllib.request

PLAN = json.loads(__PLAN_JSON__)
BASE = os.environ.get("AVSYNC_ACE_URL", "http://127.0.0.1:8001")
OUT = pathlib.Path(os.environ.get("AVSYNC_OUT", "/kaggle/working/tracks"))
WORK = pathlib.Path(os.environ.get("AVSYNC_WORK", "/kaggle/working"))
REPO_URL = "https://github.com/ACE-Step/ACE-Step-1.5.git"
REVISION = __REVISION__
OUT.mkdir(parents=True, exist_ok=True)
LOG = open(OUT / "log.txt", "a", encoding="utf-8")


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOG.write(msg + "\n")
    LOG.flush()


def sh(cmd, **kw):
    log("+", " ".join(cmd) if isinstance(cmd, list) else cmd)
    subprocess.run(cmd, check=True, **kw)


def http(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def install_and_start():
    if os.environ.get("AVSYNC_SKIP_INSTALL"):
        return None, "skipped"
    repo = WORK / "ACE-Step-1.5"
    if not repo.exists():
        sh(["git", "clone", "--depth", "50", REPO_URL, str(repo)])
    if REVISION:
        sh(["git", "-C", str(repo), "checkout", REVISION])
    rev = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    sh([sys.executable, "-m", "pip", "install", "-q", "uv"])
    sh([sys.executable, "-m", "uv", "sync", "--python", "3.12"], cwd=repo)
    proc = subprocess.Popen([sys.executable, "-m", "uv", "run", "acestep-api"], cwd=repo, stdout=LOG, stderr=subprocess.STDOUT)
    return proc, rev


def wait_ready(proc, timeout_s=1800):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"acestep-api가 종료됨 (코드 {proc.returncode})")
        try:
            return json.loads(http("GET", "/v1/models", timeout=10))
        except Exception:
            time.sleep(10)
    raise TimeoutError("acestep-api가 준비되지 않음")


def generate(track, seed):
    body = {"prompt": track["prompt"], "lyrics": "[Instrumental]", "thinking": False, "audio_format": "wav",
            "audio_duration": track["duration_s"], "inference_steps": 8, "batch_size": 1,
            "use_random_seed": seed < 0}
    if seed >= 0:
        body["seed"] = seed
    if track.get("bpm_hint"):
        body["bpm"] = (track["bpm_hint"][0] + track["bpm_hint"][1]) // 2
    sub = json.loads(http("POST", "/release_task", body))
    task_id = sub["data"]["task_id"]
    t0 = time.time()
    while time.time() - t0 < 1800:
        res = json.loads(http("POST", "/query_result", {"task_id_list": [task_id]}))
        item = res["data"][0]
        if item["status"] == 1:
            return json.loads(item["result"])[0], task_id
        if item["status"] == 2:
            raise RuntimeError(f"생성 실패: {item.get('result')}")
        time.sleep(5)
    raise TimeoutError("생성 시간 초과")


def main():
    results = []
    proc = None
    try:
        proc, rev = install_and_start()
        models = wait_ready(proc)
        log("models:", json.dumps(models)[:300])
        for track in PLAN["tracks"]:
            for seed in track["seeds"]:
                name = f"{track['id']}_s{seed}"
                rec = {"id": track["id"], "seed": seed, "file": None, "error": None}
                try:
                    t0 = time.time()
                    info, task_id = generate(track, seed)
                    url = info["file"]
                    wav = http("GET", url if url.startswith("/") else "/" + url, timeout=300)
                    (OUT / f"{name}.wav").write_bytes(wav)
                    meta = {"task_id": task_id, "prompt": track["prompt"], "requested_duration_s": track["duration_s"],
                            "seed_value": info.get("seed_value"), "dit_model": info.get("dit_model"),
                            "lm_model": info.get("lm_model"), "metas": info.get("metas"), "repo_revision": rev,
                            "elapsed_s": round(time.time() - t0, 1)}
                    (OUT / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    rec["file"] = f"{name}.wav"
                    log("OK", name, meta["elapsed_s"], "s")
                except Exception as e:  # 한 곡이 실패해도 나머지는 계속한다 (부분 결과 보존)
                    rec["error"] = f"{type(e).__name__}: {e}"
                    log("FAIL", name, rec["error"])
                    log(traceback.format_exc())
                results.append(rec)
                (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def render_script(plan_dict: dict, plan_hash: str, revision: str | None = None) -> str:
    return (TEMPLATE.replace("__PLAN_HASH__", plan_hash)
            .replace("__PLAN_JSON__", repr(json.dumps(plan_dict, ensure_ascii=False)))
            .replace("__REVISION__", repr(revision)))
