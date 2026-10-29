# CLAUDE.md

영어 TTS와 BGM을 입히는 개인용 영상 도구(avsync). 구현 기준은 사용자가 준 지시서 v3.2 — 핵심 원칙만 옮긴다.

## 반드시 지킬 것
- 모델명·버전·성능·라이선스를 추정하지 않는다. 확인한 것은 `reports/source_audit.md`에, 못 한 것은 "미확인"으로 적는다.
- 대본 문구를 자동으로 바꾸지 않는다. 보고하고 제안만 한다.
- `approved: false`이면 최종 렌더·촬영용 가이드를 만들지 않는다.
- 원본 영상은 수정하지 않는다. 촬영 영상·사용자 음성을 외부로 보내지 않는다.
- 시간은 정수 us/샘플. 부동소수점 누적 금지. 외부 프로세스는 인자 배열(`shell=True` 금지), 경로는 `pathlib`.
- MVP 이후 기능(Gemini TTS, 원격 GPU BGM, `align`, burn-in 자막, 음악 우선)은 **사용자 승인 후**에만 구현한다.
- 소리를 바꾸는 변경을 하면 `guide/build.py`의 `SOUND_ENGINE_VERSION`을 올린다(재촬영 경고용 해시).

## 작업 방법
- 테스트: `uv run pytest`. 통합 테스트는 FFmpeg와 합성 영상·가짜 TTS(`tests/helpers.py`)를 쓰며 모델이 필요 없다.
- "PASS"를 믿지 말고 `ffprobe`/`silencedetect`/`astats` 등 별도 도구로 결과를 확인한다.
- 지시서와 다르게 구현한 곳과 해석은 `docs/open_questions.md`에 번호를 붙여 기록한다.
- 이 개발 환경은 Linux이고 모델 호스트가 막혀 있다. Windows 경로와 Kokoro 실행은 검증되지 않았다.
