# Phase 3-5 구현 보고서

작성: 2026-10-29. 근거: 로컬 테스트 152개 + GitHub Actions(Ubuntu, Windows). **승인 범위와 제외 범위는 `docs/open_questions.md` D절.**

## 1. 요약

| 영역 | 상태 | 검증 수준 |
|---|---|---|
| `emphasis: pause_only`, `cue_speed` | 구현 | 단위 테스트(결정적 가짜 TTS). 억양 자연스러움은 청취 필요 |
| `align`(자동 오프셋), `merge --auto-align` | 구현 | 합성 촬영본(톤을 2.5초 지연·잡음에 섞음)에서 20ms 이내 복구. 실제 스피커 녹음 영상은 미검증 |
| `merge --stretch`, `--mark` | 구현 | 1.8% 드리프트 시뮬레이션: 보정 후 독립 중간 마크 5ms 이내 |
| burn-in 자막, 단어 카라오케 | 구현 | **픽셀 검사**(자막 있는 시각엔 글자, 문장 사이·앞 구간엔 없음), Windows 포함 |
| 단어 타임스탬프 | 구현 | **Windows 러너에서 실제 Kokoro 출력 확인** (모델 예측값, 강제 정렬 아님) |
| Gemini TTS | 구현 | **가짜 CLI로만 검증.** 실제 호출·음성 이름·무료 한도 미검증 |
| 원격 GPU BGM(Kaggle) | 구현 | 가짜 kaggle CLI + ACE-Step 문서대로 만든 가짜 REST 서버로 생성 스크립트를 실행. **Kaggle 실제 실행 미검증** |
| 보컬 감지 | 구현(휴리스틱) | 합성 신호로만 보정. 한계를 테스트로 고정 |
| 음악 우선(`beats`, `count_beats`) | 구현 | 합성 클릭 트랙. 완성 가이드 재분석으로 박 정렬 30ms 이내 확인 |
| `build`, 설정 오타 경고, doctor 확장 | 구현 | 통합 테스트 |
| CI | Ubuntu/Windows 전체 테스트 + Windows Kokoro 스모크 | |
| Lyria(유료 음악), 강제 정렬 도구 평가 | **미구현** | 승인 범위 밖 / 후보 미검증 |

## 2. CI에서 처음으로 확인된 사실 (개발 환경에서는 불가능했던 것)

GitHub Actions Windows 러너(Windows Server 2025, FFmpeg 9.0.2, Python 3.12). **사용자 PC가 아니므로 성능 수치는 참고용이다.**

| 항목 | 결과 |
|---|---|
| Kokoro 설치·실행 | 성공. `uv sync --python 3.12` 약 10초(캐시), 가중치·음색은 첫 합성 때 HF에서 다운로드, misaki가 spaCy 모델을 자동 설치 |
| 가중치 라이선스 | **Apache-2.0** (model card), 리비전 `f3ff3571...` |
| 영어 음색 | 28개 (목록은 `source_audit.md`) |
| 콜드 스타트 | 26.8초 (프로세스 기동 + 모델 로드 + 첫 합성) |
| RTF | 0.62-0.65 (15초 분량 11.0초, 30초 분량 20.7초) - 실시간보다 빠름 |
| 최대 메모리 | 1390 MB (워커 자체 보고) |
| 샘플 가이드 (af_heart, 속도 0.95) | 총 94.5초, 10개 항목 모두 `ok`, 가장 긴 항목 2.23초(허용 4.25초) |
| 음색별 길이 차이 | 같은 두 문장: af_heart 3.90/2.58초, af_bella 4.33/2.98, **af_nicole 5.92/4.05**, af_sarah 4.25/2.95, am_adam 3.95/2.65, am_michael 4.33/2.92, bf_emma 4.00/2.83, bm_george 4.53/3.10 - 음색이 길이 예산에 영향을 준다 |
| 단어 시각 예 | `Inhale[1.05-1.50] arms[1.57-1.87] up[1.87-2.23]` (문장 시작 기준) |
| 발음 사전 적용 | `Ekam.` -> `ehkum.`, `Dve.` -> `dveh.` (철자 치환). **이 발음이 맞게 들리는지는 사람이 들어야 한다** |

**청취용 산출물:** 러너가 후보 음색별 샘플, 청취 문장 25개, 샘플 가이드(WAV/M4A)를 아티팩트로 올렸다.
GitHub 로그인 후 <https://github.com/LEEYOUNGJIN-GIT/project_Y/actions/runs/40854524076/artifacts/12889445186> 에서 내려받을 수 있다
(보관 기간은 GitHub 기본 설정을 따른다).

## 3. CI가 잡아낸 Windows/Kokoro 결함 (모두 수정, 회귀 테스트 추가)

로컬(Linux) 테스트는 전부 통과하던 상태였다.

| # | 결함 | 영향 |
|---|---|---|
| 1 | 자식 프로세스 stdout 기본 인코딩이 cp1252 | 워커가 한글 오류 메시지·IPA 문자에서 죽음. **Windows에서 발음 사전의 IPA 표기가 조용히 깨질 수 있었음** |
| 2 | `ffmpeg -filters` 출력 형식이 FFmpeg 9에서 달라짐 | `doctor`가 모든 필터를 없다고 보고 -> `-h filter=NAME`으로 확인 |
| 3 | misaki가 첫 합성 때 spaCy 모델 설치 출력을 stdout에 씀 | JSON 프로토콜 파손 -> 워커가 프로토콜 채널을 따로 확보하고 부모는 JSON이 아닌 줄을 무시 |
| 4 | `uv pip install`이 상속된 `VIRTUAL_ENV`(메인 환경)에 설치 | 워커 환경에 `VIRTUAL_ENV` 지정 |
| 5 | 워커 사망 직후 다음 호출이 죽은 파이프에 씀(자식 미회수 경쟁 조건) | 사망 감지 즉시 회수·핸들 폐기 |
| 6 | venv의 `python.exe`는 런처라 PID로 잰 메모리가 3.9MB | 워커가 자기 최대 메모리를 보고 (실제 1390MB) |
| 7 | 생성된 Kaggle 스크립트가 한글 `print`에서 cp1252로 죽음 | 스크립트 시작 시 UTF-8 고정 |

## 4. 구현 중 발견·수정한 설계 결함 (로컬)

- 오디오 스트림 `start_time`이 영상과 다른 파일에서 `align`이 엉뚱한 톤을 고름 -> 스트림 시작 시각 차이 반영(원본 소리 추출에도 적용)
- 비트 분석: 지속음 패드를 85 BPM(신뢰도 0.98)으로 오판 -> 3대역 onset과 절대 강도 게이트로 수정. 지속음 패드 위의 약한 박은 대역 분리 후 검출
- 보컬 휴리스틱 임계값은 추측이 아니라 합성 신호에서 측정한 분포로 정함

## 5. 아직 검증되지 않은 것 (정직한 목록)

1. **사용자 PC의 성능**(콜드 스타트·RTF·메모리). 러너 수치로 갈음하지 말 것. `configs/acceptance_wait.yaml`이 비어 있어 통과/실패 판정도 보류 상태
2. **발음·음색·억양** - 산스크리트 카운트(Ekam, Dve ...), 강조 조각 합성의 억양, 속도 0.95의 자연스러움. 청취 필요
3. **Gemini 실제 호출** - `gemini-api` CLI 설치와 키, 음성 이름, 출력 형식, 무료 한도. `ai.google.dev`가 막혀 공식 문서를 읽지 못함
4. **Kaggle 실제 실행** - ACE-Step 설치(uv sync, Python 3.12), T4 호환(bf16), 속도, 무료 한도, 인증. 생성 스크립트는 한 번도 Kaggle에서 돌지 않았다
5. **실제 음악으로 비트/보컬 분석** - 합성 신호로만 보정했다
6. **스피커 녹음 영상으로 `align`** - 합성 믹스로만 검증
7. **burn-in 안전 영역** - `margin_v` 420px는 추측
8. 강제 정렬 도구 평가(후보 미검증)

## 6. 권장 순서

1. CI 아티팩트의 후보 음색 샘플을 들어 음색 3개를 고르고, 카운트 단어 발음을 평가한다(필요하면 `pronunciations`에 `/음소/` 표기 시험).
2. `script.yaml`에 `voice: {provider: kokoro, voice_id: <선택>}` 지정, `approved: true` 후 `avsync guide`로 촬영용 가이드 생성.
3. 사용자 PC에서 `scripts\setup_kokoro.ps1` -> `avsync benchmark tts --provider kokoro --voice <음색> --acceptance configs/acceptance_wait.yaml`.
4. (선택) Gemini: `gemini-api` 설치, 결제 미연결 키, `avsync gemini confirm-free --yes`, `avsync benchmark tts --provider kokoro,gemini --listen`로 같은 6문장 비교.
5. (선택) Kaggle: `bgm remote-plan` -> 곡 2-3개(10-15초)로 시험 `remote-run` -> `remote-status` -> `remote-fetch`.
