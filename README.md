# avsync

촬영한 영상에 영어 TTS 내레이션과 BGM을 입혀 9:16 MP4로 만드는 개인용 CLI. 구현 기준은 지시서 v3.2이며,
현재 상태와 검증 내용은 [`reports/phase0_report.md`](reports/phase0_report.md), 해석·이탈 사항은
[`docs/open_questions.md`](docs/open_questions.md)에 있다.

**현재 상태: MVP 구현 완료, 승인 대기.** Kokoro는 사용자 PC에서 아직 실행되지 않았다(개발 환경에서 모델 호스트 차단).

## 설치

요구: Python 3.11+, [uv](https://docs.astral.sh/uv/), FFmpeg 4.4+ (`libx264`, `aac`, `amix normalize` 옵션 필요).

```bash
uv sync
uv run avsync doctor
```

Windows는 `configs/defaults.yaml`의 `tools.ffmpeg`, `tools.ffprobe`에 절대 경로를 적고 `allow_path_fallback: false`로 둔다
(또는 환경변수 `AVSYNC_FFMPEG`, `AVSYNC_FFPROBE`). 작업 폴더와 모델 캐시는 OneDrive 밖에 둔다.

Kokoro(실제 TTS)는 별도 환경이다: Windows는 `scripts\setup_kokoro.ps1`, 그 외는 `uv sync --project provider_envs/kokoro --python 3.12`.

## 두 가지 제작 방식

| | audio-first (새 촬영 기본) | video-first (이미 찍은 영상) |
|---|---|---|
| 흐름 | `guide` -> 가이드 소리를 이어폰으로 들으며 촬영 -> `merge` | `plan` -> `tts` -> `mix` -> `render` -> `qa` |
| 시각 | 호흡 격자(`guide.yaml`)가 계산 | `script.yaml`의 `at`을 직접 지정 |

### audio-first

```bash
uv run avsync init my_reel --example surya_a          # workspaces/my_reel 생성 (예제: 수리야 나마스카라 A)
cd workspaces/my_reel                                 # project.yaml에서 bgm 지정, script.yaml 문구 확정
uv run avsync check-script
uv run avsync guide                                   # 시간표, 총 길이, 가이드 WAV/M4A 내보내기
```

`script.yaml`을 `approved: true`로 바꾼 뒤 `guide`를 다시 실행해야 촬영용(`.preview`가 아닌) 가이드가 나온다.
`outputs/guide/guide_<해시8>.m4a`를 휴대폰에서 **이어폰으로** 들으며 촬영한다. 시작 톤(880 Hz)에서 첫 호흡이 시작된다.

```bash
# 촬영본에서 시작 톤(또는 첫 동작 시작 프레임)을 찾아 오프셋 입력. 프레임 표기(97f)도 가능
uv run avsync merge --video raw.mp4 --guide outputs/guide/guide_ab12cd34.wav \
    --offset 00:03.240 --first-mark 00:09.25 --last-mark 01:30.30 --shot-hash ab12cd34
```

- `--offset`: raw 영상 기준, 가이드 0초(카운트인 시작)가 놓이는 시각. 톤 위치를 안다면 `--tone-at`.
- `--first-mark/--last-mark`: 첫/마지막 카운트 동작이 영상에서 시작하는 시각. 어긋남이 ±0.15초를 넘으면 exit 2.
- `--shot-hash`: 촬영에 쓴 가이드의 해시. 합성하려는 가이드와 다르면 "영상 재촬영 필요" 오류.
- 스피커로 가이드를 틀어 촬영했다면 `--original remove`.

### video-first

```bash
uv run avsync plan && uv run avsync tts --all && uv run avsync fit-report
uv run avsync mix && uv run avsync render --preview    # approved: true 이후에는 --preview 없이
uv run avsync qa
```

## 종료 코드

`0` 성공 / `1` 오류(입력·환경) / `2` 내용 검사 실패(fit 초과·겹침, QA fail, 드리프트) / `130` 사용자 중단.
모든 명령은 `--json`, `--dry-run`, `--project`를 받는다.

## 알아둘 동작

- 영상은 9:16·h264·yuv420p·CFR이면 **스트림 복사**(화질 손실 없음). 그 외에는 재인코딩하고 사유를 보고한다.
- 대본 문구는 도구가 바꾸지 않는다. 길이가 넘으면 `fit-report`가 보고하고 단축 후보만 제안한다.
- 목표 -14 LUFS에 못 미치면 QA가 경고한다. 리미터가 줄일 수 있는 양은 `quality_gates.yaml`의 `mix.limiter_budget_db`.
- `approved: false`이면 최종 렌더·촬영용 가이드를 만들지 않는다(미리듣기만).

## 개발

```bash
uv run pytest                      # 전체 (FFmpeg 필요, 약 2분)
uv run pytest -m "not integration" # 단위 테스트만 (FFmpeg 불필요)
```

구조: `src/avsync/{domain,providers,guide,audio,video,tts,captions,qa,platform}`, 설정 `configs/`, 공급자 환경 `provider_envs/`.
