# CLAUDE.md

영어 TTS와 BGM을 입히는 개인용 영상 도구(avsync). 구현 기준은 사용자가 준 지시서 v3.2 — 핵심 원칙만 옮긴다.

## 반드시 지킬 것
- 모델명·버전·성능·라이선스를 추정하지 않는다. 확인한 것은 `reports/source_audit.md`에, 못 한 것은 "미확인"으로 적는다.
- 대본 문구를 자동으로 바꾸지 않는다. 보고하고 제안만 한다.
- `approved: false`이면 최종 렌더·촬영용 가이드를 만들지 않는다.
- 원본 영상은 수정하지 않는다. 촬영 영상·사용자 음성을 외부로 보내지 않는다.
- 시간은 정수 us/샘플. 부동소수점 누적 금지. 외부 프로세스는 인자 배열(`shell=True` 금지), 경로는 `pathlib`.
- 사용자가 Phase 3-5(Gemini TTS, 원격 GPU BGM, `align`, burn-in 자막, 단어 시각, 음악 우선)의 구현을 승인했다. 그러나 **유료 사용(Lyria), API 키/토큰의 입력·저장, `gemini confirm-free` 대리 실행, Kaggle 실제 푸시는 승인 범위 밖**이다(docs/open_questions.md D절).
- 소리를 바꾸는 변경을 하면 `guide/build.py`의 `SOUND_ENGINE_VERSION`을 올린다(재촬영 경고용 해시).

## 작업 방법
- 테스트: `uv run pytest`. 통합 테스트는 FFmpeg와 합성 영상·가짜 TTS(`tests/helpers.py`)를 쓰며 모델이 필요 없다.
- "PASS"를 믿지 말고 `ffprobe`/`silencedetect`/`astats` 등 별도 도구로 결과를 확인한다.
- 지시서와 다르게 구현한 곳과 해석은 `docs/open_questions.md`에 번호를 붙여 기록한다.
- 로컬 개발 환경은 Linux이고 모델 호스트·`ai.google.dev`·`kaggle.com`이 막혀 있다. Windows와 Kokoro는 로컬이 아니라 CI에서 검증한다. 사용자 PC 실측과 청취는 아직 없다.
- CI(.github/workflows/ci.yml)가 Windows와 Kokoro를 실제로 돌린다. Windows/Kokoro 관련 변경은 푸시 후 Actions 결과(`mcp__github__actions_*`)로 확인한다. 로컬 Linux 통과는 Windows 통과를 뜻하지 않는다(이미 6건의 Windows 전용 결함이 CI에서 나왔다).
- 워커/자식 프로세스 입출력은 UTF-8을 강제하고, 프로토콜 채널은 stdout에서 분리한다. 서드파티 출력이 섞인다.
- 외부 서비스(Gemini, Kaggle)는 가짜 CLI/서버(`tests/fake_*.py`)로만 검증된다. 실제 호출 결과로 오해하게 쓰지 않는다.
