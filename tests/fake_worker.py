"""SubprocessTTSProvider 프로토콜 시험용 워커 (JSON Lines). 실제 Kokoro 워커와 같은 메시지 형식을 쓴다."""
import json
import math
import sys
import wave

RATE = 24000


def main() -> int:
    for raw in sys.stdin:
        msg = json.loads(raw)
        op = msg.get("op")
        if op == "hello":
            reply = {"ok": True, "capabilities": {
                "name": "fakeworker", "version": "0", "languages": ["en-US"], "speed": True,
                "word_timestamps": False, "emotion": False, "sample_rate": RATE, "cpu": True,
                "pronunciation_mode": "respell", "production_ok": True}}
        elif op == "synthesize":
            if msg["text"] == "__CRASH__":
                sys.exit(3)
            if msg["text"] == "__NOISE__":
                print("Downloading en-core-web-sm ...")          # 라이브러리가 stdout에 쓰는 상황
                sys.stdout.flush()
            if msg["text"] == "__BRACE__":
                print("{'warn': 'dict repr from a library'}")      # {로 시작하지만 프로토콜 응답이 아닌 줄
                print("{not json at all")
                sys.stdout.flush()
            if not msg.get("voice"):
                reply = {"ok": False, "error": "voice_id가 지정되지 않았습니다"}
            else:
                n = int(len(msg["text"]) * 0.05 / msg["speed"] * RATE)
                with wave.open(msg["out"], "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(RATE)
                    w.writeframes(b"".join(int(8000 * math.sin(2 * math.pi * 200 * i / RATE)).to_bytes(2, "little", signed=True)
                                           for i in range(n)))
                reply = {"ok": True, "sample_rate": RATE, "meta": {"voice": msg["voice"]}}
        elif op == "bye":
            return 0
        else:
            reply = {"ok": False, "error": f"알 수 없는 op: {op}"}
        sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
