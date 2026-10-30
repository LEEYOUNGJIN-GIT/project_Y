"""공식 gemini-api CLI의 README 계약(명령/플래그/종료 코드/--out 규약)만 흉내 내는 시험용 스크립트.

종료 코드: 0 성공, 1 런타임/API 오류, 2 사용법 오류, 3 인증 오류. 미디어 명령은 쓴 파일 경로만 stdout에 출력한다.
FAKE_MODE: ok | auth | usage | rate | rate_once | fail.  FAKE_LOG: 호출 기록(JSON Lines) 경로.
"""
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path

KEY = os.environ.get("GEMINI_API_KEY", "")


def main(argv) -> int:
    if argv[:1] == ["version"]:
        print("gemini-api fake 0.0.1")
        return 0
    if argv[:1] != ["tts"]:
        print("usage: gemini-api tts", file=sys.stderr)
        return 2
    opts, i = {}, 1
    while i < len(argv):
        if argv[i] in ("-f", "--file", "--voice", "--out", "-m"):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    text = Path(opts["-f"]).read_text(encoding="utf-8") if "-f" in opts else ""
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text, "voice": opts.get("--voice"), "model": opts.get("-m")}, ensure_ascii=False) + "\n")
    mode = os.environ.get("FAKE_MODE", "ok")
    counter = Path(os.environ.get("FAKE_COUNTER", os.devnull))
    if mode == "auth":
        print(f"API key not valid: {KEY}", file=sys.stderr)          # 키가 오류 문구에 섞여 나오는 경우를 흉내
        return 3
    if mode == "usage":
        print("invalid flag", file=sys.stderr)
        return 2
    if mode == "rate" or (mode == "rate_once" and not counter.exists()):
        if mode == "rate_once":
            counter.write_text("1")
        print(f"429 RESOURCE_EXHAUSTED quota exceeded (key={KEY})", file=sys.stderr)
        return 1
    if mode == "hang" or (mode == "hang_once" and not counter.exists()):
        if mode == "hang_once":
            counter.write_text("1")
        time.sleep(30)                                            # 네트워크가 멈춘 상황 (공급자의 timeout_s가 먼저 끊는다)
        return 1
    if mode == "http503":
        print("503 Service Unavailable", file=sys.stderr)
        return 1
    if mode == "voice_unavailable":
        print("voice 'Foo' is unavailable for this model", file=sys.stderr)
        return 1
    if mode == "fail":
        print("500 internal error", file=sys.stderr)
        return 1
    rate = int(os.environ.get("FAKE_RATE", "24000"))
    n = int(len(text) * 0.05 * rate)
    out = Path(opts["--out"]) / "speech.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * k / rate))) for k in range(n)))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
