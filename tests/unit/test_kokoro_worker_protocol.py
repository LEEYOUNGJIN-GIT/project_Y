"""실제 Kokoro 워커의 프로토콜 계층을 모델·네트워크 없이 시험한다 (kokoro는 지연 import라 hello에는 필요 없다)."""
import json
import subprocess
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parents[2] / "provider_envs" / "kokoro" / "worker.py"


def run_py(code: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_claimed_channel_isolates_library_and_child_process_output():
    """fd 1로 나가는 출력(파이썬 print, 하위 프로세스)은 stderr로 가고, 프로토콜 채널에는 우리가 쓴 것만 남는다."""
    code = f"""
import runpy, subprocess, sys
ns = runpy.run_path(r'{WORKER}', run_name='not_main')
proto = ns['_claim_protocol_channel']()
print('library noise')                                                        # 파이썬 print
subprocess.run([sys.executable, '-c', 'print("child noise")'])               # 하위 프로세스 (uv pip 등)
proto.write('{{"ok": true}}\\n'); proto.flush()
"""
    p = run_py(code)
    assert p.stdout.splitlines() == ['{"ok": true}'], p.stdout + p.stderr
    assert "library noise" in p.stderr and "child noise" in p.stderr


def test_hello_reports_word_timestamps_and_phoneme_markup():
    p = run_py(f"import runpy; runpy.run_path(r'{WORKER}', run_name='__main__')",
               json.dumps({"op": "hello"}) + "\n" + json.dumps({"op": "bye"}) + "\n")
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, p.stdout + p.stderr
    caps = json.loads(lines[0])["capabilities"]
    assert caps["name"] == "kokoro" and caps["word_timestamps"] is True and caps["pronunciation_mode"] == "phoneme_markup"


def test_synthesis_errors_come_back_as_json_and_worker_keeps_running():
    """kokoro가 없는 환경에서도(여기서는 미설치) 오류는 JSON으로 돌아오고, 이어지는 요청이 처리된다."""
    msg = {"op": "synthesize", "text": "Hi", "voice": "v", "speed": 1.0, "lang": "en-US", "out": "x.wav"}
    p = run_py(f"import runpy; runpy.run_path(r'{WORKER}', run_name='__main__')",
               "\n".join(json.dumps(m) for m in (msg, {"op": "hello"}, {"op": "bye"})) + "\n")
    replies = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    assert replies[0]["ok"] is False and replies[0]["error"]
    assert replies[1]["ok"] is True                         # 오류 뒤에도 워커가 살아 있다


def test_stats_op_reports_plausible_peak_memory():
    """Windows CI 회귀: 런처 PID로 재면 3.9MB가 나왔다. 워커 자체 보고는 파이썬 인터프리터 수준(수십 MB 이상)이어야 한다."""
    p = run_py(f"import runpy; runpy.run_path(r'{WORKER}', run_name='__main__')",
               json.dumps({"op": "stats"}) + "\n" + json.dumps({"op": "bye"}) + "\n")
    reply = json.loads([l for l in p.stdout.splitlines() if l.strip()][0])
    assert reply["ok"] is True and reply["peak_rss_mb"] is not None and reply["peak_rss_mb"] > 8
