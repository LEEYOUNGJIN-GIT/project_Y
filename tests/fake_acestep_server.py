"""ACE-Step 1.5 REST 서버(docs/en/API.md)의 응답 형식을 따라 만든 가짜 서버. 생성 스크립트의 클라이언트 흐름 시험용."""
import json
import math
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from urllib.parse import parse_qs, urlparse


def make_wav(seconds: float, rate: int = 24000) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<hh", *(int(6000 * math.sin(2 * math.pi * 220 * i / rate)),) * 2)
                               for i in range(int(seconds * rate))))
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    tasks: dict = {}
    requests: list = []
    polls: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/v1/models":
            return self._send(200, {"data": {"models": [{"name": "acestep-v15-turbo", "is_default": True}],
                                             "default_model": "acestep-v15-turbo"}, "code": 200})
        if u.path == "/v1/audio":
            task = parse_qs(u.query)["path"][0]
            return self._send(200, make_wav(self.tasks[task]["dur"]), "audio/wav")
        self._send(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        if self.path == "/release_task":
            Handler.requests.append(body)
            tid = f"task-{len(Handler.requests)}"
            if body["prompt"].startswith("FAIL"):
                Handler.tasks[tid] = {"fail": True, "dur": 0}
            else:
                Handler.tasks[tid] = {"fail": False, "dur": float(body["audio_duration"]) * (0.3 if "SHORT" in body["prompt"] else 1.0),
                                      "seed": body.get("seed", 4242), "prompt": body["prompt"]}
            return self._send(200, {"data": {"task_id": tid, "status": "queued", "queue_position": 1}, "code": 200})
        if self.path == "/query_result":
            out = []
            for tid in body["task_id_list"]:
                Handler.polls[tid] = Handler.polls.get(tid, 0) + 1
                t = Handler.tasks[tid]
                if t["fail"]:
                    out.append({"task_id": tid, "status": 2, "result": "[]"})
                elif Handler.polls[tid] < 2:                         # 첫 조회는 진행 중
                    out.append({"task_id": tid, "status": 0, "result": "[]"})
                else:
                    res = [{"file": f"/v1/audio?path={tid}", "status": 1, "prompt": t["prompt"], "seed_value": str(t["seed"]),
                            "metas": {"bpm": 78, "duration": t["dur"]}, "dit_model": "acestep-v15-turbo", "lm_model": ""}]
                    out.append({"task_id": tid, "status": 1, "result": json.dumps(res)})
            return self._send(200, {"data": out, "code": 200})
        self._send(404, {})


def start():
    Handler.tasks, Handler.requests, Handler.polls = {}, [], {}
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"
