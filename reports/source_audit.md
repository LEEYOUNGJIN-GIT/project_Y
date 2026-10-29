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

## 확인하지 못한 것 (구현·판단에 쓰지 않음)

| 항목 | 이유 | 다음 행동 |
|---|---|---|
| Kokoro **가중치** 라이선스와 모델 카드 | HuggingFace 차단 | 사용자 PC에서 모델 카드 원문 확인 후 이 표 갱신 |
| Kokoro Windows 설치 성공 여부(espeak 계열 의존성 포함), 속도, 메모리, 콜드 스타트, 음색 목록, 영어 요가 용어 발음 품질 | 실행 불가 | `scripts/setup_kokoro.ps1` -> `avsync benchmark tts --provider kokoro --listen` |
| Kokoro `speed` 파라미터의 길이 영향(길이가 속도에 반비례하는지) | 실행 불가 | 벤치마크 후 5.1 재합성 루프가 3회 안에 수렴하는지 확인 |
| `huggingface.co`에서 Kokoro 가중치 파일의 정확한 이름·크기 | 차단 | 위와 동일 |
| Gemini TTS 무료 등급·한도·약관, Lyria 가격, `google-gemini/gemini-api-cli` | MVP 범위 밖(Phase 3). 이번 세션에서 조회하지 않음 | Phase 3 승인 시 공식 문서·저장소를 직접 읽고 기록 |
| Kaggle/Colab 무료 GPU 한도, ACE-Step 1.5 T4 호환, Stable Audio 3 | Phase 3 | 위와 동일 |
| Reels 최대 길이, 앱 음악 라이브러리 이용 범위 | 조회하지 않음 | 샘플(94.5초) 길이 결정 전 사용자가 확인 |

## 자산 기록 (10장 간소화)

| 대상 | 기록 |
|---|---|
| 시험용 BGM | 개발 시험에서 FFmpeg `sine`으로 합성한 사인파 화음. 저작권 없음. 저장소에 포함하지 않음 |
| 시험용 TTS | FFmpeg `libflite`(flite). 파이프라인 시험 전용(`production_ok=false`)이며 제작·품질 평가에 쓰지 않음 |
| FFmpeg 빌드 | 개발 컨테이너: Ubuntu 패키지 6.1.1, `--enable-gpl`. 개인 사용 |
