# 출처 감사 (source audit)

지시서 0장: 모델명·버전·성능·라이선스를 추정하지 않고 원문으로 확인한 것과 못 한 것을 구분해 기록한다.
확인일: 2026-10-29. 확인 환경: 개발 컨테이너(Linux). 외부 호스트는 프록시를 통해 PyPI만 열려 있었고
`huggingface.co`, `github.com`은 403/연결 실패였다.

## 확인한 것

| 항목 | 확인 방법 | 결과 |
|---|---|---|
| `kokoro` 패키지 버전·파이썬 요구 | PyPI JSON 메타데이터 | 최신 0.9.4, `requires_python <3.13,>=3.10` -> 3.13 환경에서는 설치 불가, 별도 환경(3.12) 필요 |
| `kokoro` 의존성 | 같은 메타데이터 | `huggingface-hub, loguru, misaki[en]>=0.9.4, numpy, torch, transformers` |
| `kokoro` **코드** 라이선스 | PyPI 메타데이터의 license 필드 전문 | **Apache License 2.0.** v2 문서의 "코드 MIT" 기술과 **다르다.** GitHub 저장소의 LICENSE 파일은 접근 불가라 대조 못 함. 둘 다 허용적 라이선스지만 사실로 굳히기 전에 원문 재확인 필요 |
| `misaki` 라이선스·버전 | PyPI 메타데이터 | 0.9.4, Apache 2.0 표기. `[en]` extra: espeakng-loader, num2words, phonemizer-fork, spacy 등 |
| `KPipeline` 호출 형태 | wheel 소스(`kokoro/pipeline.py`) 직접 읽음 (실행 안 함) | `KPipeline(lang_code, repo_id=None, ...)`, `__call__(text, voice, speed, split_pattern=r'\n+')`가 `Result`를 yield, `Result`는 `(graphemes, phonemes, audio)`로 풀림. 보이스는 `hf_hub_download(repo_id, 'voices/{voice}.pt')`로 **지연 다운로드** |
| 발음 표기 문법 | wheel 소스(`misaki/en.py`) | `[단어](/음소/)` 링크 문법을 `LINK_REGEX`로 처리. 괄호 안이 `/.../`이면 음소 지정으로 쓰임 |
| FFmpeg 필터·인코더 | `ffmpeg -filters/-encoders` (6.1.1, GPL 빌드, libass/libflite/libx264 포함) | `doctor`가 필수 필터를 점검. 필요한 필터 전부 존재 |

## 추가로 확인한 것 (Phase 3-5 구현 세션, 2026-10-29)

개발 컨테이너에서는 `raw.githubusercontent.com`, `api.github.com`, PyPI만 열려 있었다(`ai.google.dev`, `kaggle.com`, `huggingface.co`, `github.com`은 차단/해석 불가).

| 항목 | 확인 방법 | 결과 |
|---|---|---|
| `google-gemini/gemini-api-cli` | README 원문(raw.githubusercontent) | 공식 org, README 배지 Apache-2.0, Go 단일 바이너리. `gemini-api tts`(`-f/--file`, `--stdin`, `--voice`, `--multi-speaker`, `--out`; README 표기 기본 모델 `gemini-3.8-flash-tts`), `gemini-api music`(`lyria-3.5`). 종료 코드 0 성공/1 런타임·API/2 사용법/3 인증. 미디어 명령은 쓴 파일 경로만 stdout에 출력. `--dry-run`은 자격 증명을 가린 요청을 출력. 인증: `GEMINI_API_KEY` 환경변수 또는 OS 키체인. **저장소 라이선스 파일·최종 갱신일·릴리스는 api.github.com 403으로 못 읽음** |
| Gemini API가 새 "Interactions API"(`response_format`, `previous_interaction_id`)를 쓰는 듯함 | 같은 README | 지시서와 내 기억의 `generateContent` TTS 형태가 지금도 맞는지 **확인하지 못했다.** 그래서 REST 대신 CLI를 감쌌다 |
| ACE-Step 1.5 | README, docs/en/INFERENCE.md, API.md, INSTALL.md 원문 | 라이선스 **MIT**. Python 3.11-3.12. "CUDA GPU 권장, MPS/ROCm/Intel XPU/**CPU**도 지원"(지시서 5.3의 "CPU 명시 없음"과 다름). 4GB VRAM 미만 구동, 10초-10분 길이. REST 서버 `uv run acestep-api`(기본 127.0.0.1:8001), `POST /release_task`, `POST /query_result`, `GET /v1/audio?path=`, `GET /v1/models`; 파라미터 `prompt`, `lyrics`("[Instrumental]"), `audio_duration`(10-600), `bpm`(30-300), `seed`, `use_random_seed`, `batch_size`, `inference_steps`(turbo 8), `audio_format`(wav 등), `thinking`. 모델은 첫 실행 때 자동 다운로드. **T4 호환·속도·Kaggle 설치는 미확인** |
| `kaggle` CLI 2.2.5 | PyPI wheel 소스 | `kernels push -p DIR [--accelerator A]`, `kernels status REF`(출력 `<ref> has status "<상태>"` + 선택적 `Failure message`), `kernels output REF -p DIR`. 메타데이터: `id`, `title`(5자 이상), `code_file`, `language`, `kernel_type`(script/notebook), `is_private`, `enable_gpu`, `enable_internet`. 인증: `KAGGLE_API_TOKEN` 또는 `~/.kaggle/access_token`. **무료 GPU 한도·휴대폰 인증 요건은 문서를 못 읽어 미확인** |
| Kokoro 단어 타임스탬프 | kokoro 0.9.4 소스 | 영어 경로(`lang_code` a/b)에서 `join_timestamps`가 토큰의 `start_ts/end_ts`(초)를 모델 예측 길이로 채운다. **강제 정렬이 아니라 예측값** (지시서 11장 "단어 타임스탬프 미확인"의 답) |
| Kokoro 실행 | GitHub Actions Windows 러너(2026-10-29) | Python 3.12 환경 설치, HF에서 가중치·음색 다운로드, misaki가 spaCy 모델(`en-core-web-sm` 3.8.0)을 자동 설치, 음색 `af_heart`로 합성 성공. 모델 정보(라이선스·음색 목록)는 같은 잡에서 HF API로 조회 - 아래 표 |

## 확인하지 못한 것 (구현·판단에 쓰지 않음)

| 항목 | 이유 | 다음 행동 |
|---|---|---|
| Kokoro **가중치** 라이선스와 모델 카드 | HuggingFace 차단 | 사용자 PC에서 모델 카드 원문 확인 후 이 표 갱신 |
| Kokoro Windows 설치 성공 여부(espeak 계열 의존성 포함), 속도, 메모리, 콜드 스타트, 음색 목록, 영어 요가 용어 발음 품질 | 실행 불가 | `scripts/setup_kokoro.ps1` -> `avsync benchmark tts --provider kokoro --listen` |
| Kokoro `speed` 파라미터의 길이 영향(길이가 속도에 반비례하는지) | 실행 불가 | 벤치마크 후 5.1 재합성 루프가 3회 안에 수렴하는지 확인 |
| `huggingface.co`에서 Kokoro 가중치 파일의 정확한 이름·크기 | 차단 | 위와 동일 |
| Gemini TTS 무료 등급·한도·약관, 음성 이름 목록, 출력 형식, Lyria 가격·무료 여부 | `ai.google.dev` 접근 불가 | 사용자가 도메인을 허용하거나, 사용자 PC에서 `gemini-api tts --help`, `gemini-api models`로 확인 |
| Kaggle 무료 GPU 한도·인증 요건, ACE-Step 1.5의 T4(bf16 미지원) 호환·속도, Colab, Stable Audio 3 | `kaggle.com` 접근 불가, 실행 환경 없음 | 사용자가 시험 실행(곡 2-3개)으로 확인 |
| Reels 최대 길이, 앱 음악 라이브러리 이용 범위 | 조회하지 않음 | 샘플(94.5초) 길이 결정 전 사용자가 확인 |

## 자산 기록 (10장 간소화)

| 대상 | 기록 |
|---|---|
| 시험용 BGM | 개발 시험에서 FFmpeg `sine`으로 합성한 사인파 화음. 저작권 없음. 저장소에 포함하지 않음 |
| 시험용 TTS | FFmpeg `libflite`(flite). 파이프라인 시험 전용(`production_ok=false`)이며 제작·품질 평가에 쓰지 않음 |
| FFmpeg 빌드 | 개발 컨테이너: Ubuntu 패키지 6.1.1, `--enable-gpl`. 개인 사용 |
