# avsync

촬영한 영상에 영어 TTS 내레이션과 BGM을 입혀 9:16 MP4로 만드는 개인용 CLI. 구현 기준은 지시서 v3.2다.
상태·검증 내용은 [`reports/`](reports/), 해석과 지시서에서 벗어난 부분은 [`docs/open_questions.md`](docs/open_questions.md)에 있다.

**현재 상태:** MVP와 Phase 3-5(Gemini TTS, 원격 GPU BGM, `align`, burn-in 자막, 단어 타임스탬프, 드리프트 보정, 음악 우선)를
구현했다. GitHub Actions에서 Ubuntu/Windows 테스트와 **Windows에서 실제 Kokoro 실행**을 검증한다.
검증하지 못한 것(사용자 PC 실측, Gemini/Kaggle 실제 호출, 청취 평가)은 보고서에 따로 적었다.

## 설치

요구: Python 3.11+, [uv](https://docs.astral.sh/uv/), FFmpeg 4.4+ (`libx264`, `aac`; burn-in은 libass의 `ass` 필터).

```bash
uv sync
uv run avsync doctor
```

Windows는 `configs/defaults.yaml`의 `tools.ffmpeg`, `tools.ffprobe`에 절대 경로를 적고 `allow_path_fallback: false`로 둔다
(또는 환경변수 `AVSYNC_FFMPEG`, `AVSYNC_FFPROBE`). 작업 폴더와 모델 캐시는 OneDrive 밖에 둔다.

Kokoro(실제 TTS)는 별도 환경(Python 3.12)이다. Windows: `scripts\setup_kokoro.ps1`, 그 외: `uv sync --project provider_envs/kokoro --python 3.12`.
첫 합성 때 가중치와 음색 파일을 HuggingFace에서 내려받고, 영어 G2P용 spaCy 모델도 자동 설치된다(네트워크 필요).

## 제작 방식

| | audio-first (새 촬영 기본) | video-first (이미 찍은 영상) |
|---|---|---|
| 흐름 | `guide` -> 가이드 소리를 이어폰으로 들으며 촬영 -> `merge` | `build` (= `plan` -> `tts` -> `mix` -> `render` -> `qa`) |
| 시각 | 호흡 격자(`guide.yaml`)가 계산 | `script.yaml`의 `at`을 직접 지정 |

### audio-first

```bash
uv run avsync init my_reel --example surya_a          # workspaces/my_reel 생성
cd workspaces/my_reel                                 # project.yaml에서 bgm 지정, script.yaml 문구 확정
uv run avsync check-script                            # 문법, 글자 수 예산, 발음, 설정 오타 점검
uv run avsync guide                                   # 시간표, 총 길이, 가이드 WAV/M4A 내보내기
```

`script.yaml`을 `approved: true`로 바꾼 뒤 `guide`를 다시 실행해야 촬영용 가이드가 나온다(그 전에는 `.preview` 파일).
`outputs/guide/guide_<해시8>.m4a`를 휴대폰에서 **이어폰으로** 들으며 촬영한다. 시작 톤(880 Hz)에서 첫 호흡이 시작된다.

```bash
# 오프셋을 직접 지정 (프레임 표기 97f 가능)
uv run avsync merge --video raw.mp4 --guide outputs/guide/guide_ab12cd34.wav --offset 00:03.240 \
    --first-mark 00:09.25 --last-mark 01:30.30 --mark l06=00:50.1 --shot-hash ab12cd34

# 톤을 스피커로 짧게 녹음했다면 자동 정렬 (먼저 후보만 보려면 `avsync align`)
uv run avsync merge --video raw.mp4 --guide outputs/guide/guide_ab12cd34.wav --auto-align

# 기기 간 클록 드리프트 보정 (첫/마지막 마크로 비율 계산, 중간 --mark로 독립 검증)
uv run avsync merge ... --first-mark 00:09.25 --last-mark 01:30.30 --stretch --mark l06=00:50.1
```

- `--offset`: raw 영상 기준, 가이드 0초(카운트인 시작)가 놓이는 시각. `--tone-at`: 톤 위치를 안다면 그 시각.
- `--first-mark/--last-mark/--mark 단계=시각`: 카운트 동작이 영상에서 시작하는 시각. 어긋남이 ±0.15초를 넘으면 exit 2.
- `--stretch`: 마크로 구한 비율이 [0.98, 1.02]를 벗어나면 마크 오류로 보고 거부한다.
- `--shot-hash`: 촬영에 쓴 가이드의 해시. 합성하려는 가이드와 다르면 "영상 재촬영 필요" 오류.
- 스피커로 가이드를 틀어 촬영했다면 `--original remove`. 영상 소리의 시작 시각이 영상과 어긋난 파일도 보정한다.

### 음악 우선 (박자에 맞춘 호흡 격자)

```yaml
# guide.yaml
breath: {count_beats: 4, bpm: from_bgm, beat_align: true}     # 또는 bpm: 72
```

BGM에서 템포를 분석해 카운트 1개를 4박으로 정하고, 첫 호흡이 박에 오도록 BGM 앞부분을 자른다.
박자가 없는 앰비언트 BGM(이 프로젝트의 기본 프리셋)은 `from_bgm`이 "박자 없음"으로 거부하니 `count_s`를 쓴다.
`avsync beats 곡.wav`로 먼저 확인할 수 있다.

### video-first

```bash
uv run avsync build            # 미승인이면 자동으로 미리듣기 렌더. fit 오류가 있으면 거기서 멈춘다
# 단계별로: plan, tts --all|--line ID, fit-report, mix, render [--preview], qa
```

## 자막

`output.captions`: `none` | `sidecar_srt`(기본) | `burn_in`. burn-in은 폰트 **파일과 라이선스를 직접 지정**해야 한다.

```yaml
output:
  captions: burn_in
  caption_style: {font_file: assets/MyFont.ttf, font_license: "SIL OFL 1.1", font_size: 64, margin_v: 420}
```

공급자가 단어 시각을 주면(Kokoro) 단어 단위 카라오케 하이라이트가 되고, 아니면 문장 단위로 표시한다.
`margin_v`(아래 여백)는 플랫폼 UI에 가려지지 않는 안전 영역을 확인하지 못해 정한 시작값이다.

## TTS 공급자

| 공급자 | 상태 |
|---|---|
| `kokoro` | 로컬 CPU. **Windows 러너에서 실제 합성·벤치마크·단어 시각까지 검증.** 사용자 PC 실측은 아직 |
| `gemini` | 공식 `gemini-api` CLI를 감쌈. 가짜 CLI로만 검증(실제 호출 미검증). 사용 전 `avsync gemini confirm-free --yes` |
| `flite` | FFmpeg 내장. 파이프라인 시험 전용(제작 금지) |

```bash
uv run avsync benchmark tts --provider kokoro --voice af_heart --listen        # RTF/메모리/콜드 스타트 + 청취 문장 25개
uv run avsync benchmark tts --provider kokoro,gemini --listen                   # 같은 6문장 비교 (Phase 0)
```

## BGM

```bash
uv run avsync bgm import music.wav --note "출처·라이선스" --vocals no
uv run avsync bgm check music.wav                       # 보컬 휴리스틱, 음량, 피크
# 원격 GPU(Kaggle)로 일괄 생성: 먼저 곡 2-3개(10-15초)로 시험 실행
uv run avsync bgm remote-plan examples/bgm_prompts.yaml
uv run avsync bgm remote-run  examples/bgm_prompts.yaml --owner <Kaggle 사용자명>
uv run avsync bgm remote-status
uv run avsync bgm remote-fetch                          # 검증을 통과한 곡만 library/bgm/에 등록
```

Kaggle 토큰은 `kaggle` CLI의 표준 위치에 두며 이 도구는 읽지 않는다. 생성 스크립트는 Kaggle에서 한 번도 실행된 적이 없다.

## 종료 코드

`0` 성공 / `1` 오류(입력·환경) / `2` 내용 검사 실패(fit 초과·겹침, QA fail, 드리프트) / `130` 사용자 중단.
모든 명령은 `--json`, `--dry-run`, `--project`를 받는다.

## 알아둘 동작

- 영상은 9:16·h264·yuv420p·CFR이면 **스트림 복사**(화질 손실 없음). 그 외·burn-in·정확한 트림은 재인코딩하고 사유를 보고한다.
- 대본 문구는 도구가 바꾸지 않는다. 길이가 넘으면 `fit-report`가 보고하고 단축 후보만 제안한다.
- `emphasis`(강조 단어 앞뒤 무음)와 `cue_speed`(카운트 단어만 있는 문장의 속도)는 톤 프리셋 값으로 동작한다.
- 목표 -14 LUFS에 못 미치면 QA가 경고한다. 리미터가 줄일 수 있는 양은 `quality_gates.yaml`의 `mix.limiter_budget_db`.
- `approved: false`이면 최종 렌더·촬영용 가이드를 만들지 않는다(미리듣기만).
- 설정 파일의 알 수 없는 키(오타)는 경고한다.

## 개발

```bash
uv run pytest                      # 전체 (FFmpeg 필요, 약 4분)
uv run pytest -m "not integration" # 단위 테스트만 (FFmpeg 불필요)
```

CI(`.github/workflows/ci.yml`)는 Ubuntu/Windows에서 전체 테스트를 돌리고, Windows에서 Kokoro 스모크(`scripts/ci_kokoro_smoke.py`)를
실행해 벤치마크, 후보 음색별 샘플, 샘플 가이드를 아티팩트로 올린다 (러너 성능은 사용자 PC와 다르다).

구조: `src/avsync/{domain,providers,guide,audio,video,tts,captions,remote,qa,platform}`, 설정 `configs/`, 공급자 환경 `provider_envs/`.
