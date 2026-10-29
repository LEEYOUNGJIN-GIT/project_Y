# Phase 0 + MVP 보고서

작성: 2026-10-29. 대상: 지시서 v3.2 (13장 1-5번). **MVP에서 멈추고 보고한다. 승인 전에는 Phase 3 이후를 구현하지 않았다.**

## 1. 한 줄 결론

FFmpeg만으로 되는 부분(오프셋 합성, 믹스, 덕킹, 정규화, 스트림 복사, 가이드 시간표, QA)은 구현하고 검증했다.
**Kokoro는 이 환경에서 실행하지 못했다.** 따라서 "기본 TTS 공급자 확정"(3.4)과 "사용자 PC 실측 보고"(12.3)는 아직 미충족이며,
사용자 PC에서 한 번 실행해야 닫힌다. 청취가 필요한 수용 기준도 사람이 들어야 한다.

## 2. 실행 환경의 한계 (먼저 읽을 것)

| 구분 | 개발 환경(이 보고서의 모든 실측) | 사용자 환경(지시서의 대상) |
|---|---|---|
| OS | Linux 컨테이너, 4코어, 15 GB | Windows 10/11, CPU 전용 |
| 네트워크 | PyPI만 열림. HuggingFace·GitHub 차단 | 일반 인터넷 |
| TTS | 시험용 flite(`production_ok=false`), 결정적 가짜 공급자(테스트) | Kokoro (미검증) |

따라서 아래 수치 중 **RTF·메모리·콜드 스타트는 사용자 PC 판단 근거가 아니다.** Windows 전용 코드 경로
(`taskkill` 트리 종료, `BELOW_NORMAL`, `GlobalMemoryStatusEx`, `PeakWorkingSetSize`, `.ps1`)는 작성만 했고 실행하지 못했다.

## 3. MVP 수용 기준 (12.3) 점검

| 기준 | 상태 | 근거 |
|---|---|---|
| 30초 샘플 영상+대본+BGM -> 9:16 MP4 | 충족(조건부) | 12초 합성 영상 자동 테스트, 110초 영상 수동 실행. 실제 TTS가 아닌 flite/가짜 공급자 |
| 오디오·영상 길이 차이 1프레임 이내 | 충족 | 모든 경로에서 0.00프레임 (`av_duration` 검사, 테스트 다수) |
| 초과·겹침 문장이 fit-report에서 검출 | 충족 | `test_fit_overlap_detected_and_blocks_mix`, 단위 테스트, 수동 실행(exit 2) |
| 한 문장 재합성해도 다른 문장 해시 유지 | 충족 | `test_cache_hits_and_isolation`; 수동 `tts --line a03`에서 다른 항목 합성 호출 0회 |
| 스트림 복사 경로에서 원본 화질 유지 | 충족 | 출력/원본 영상 스트림 MD5 동일 (테스트 + 수동). 회전 메타데이터 영상도 유지 |
| 내레이션 구간에서 BGM이 낮아짐 | 충족(측정) | 감쇠 중앙값 6.00 dB (목표 6.0). 가이드·video-first 모두 |
| **직접 청취로 내레이션이 묻히지 않음** | **미충족(청취 필요)** | AI는 들을 수 없다. `qa_report.md`에 체크리스트로 남김 |
| 통합 음량·true peak·clipping 보고 | 충족 | `loudness`, `true_peak`, `clipping` 검사 |
| 원본 소리 정책(remove/keep/duck, no_narration) | 충족 | duck은 내레이션 구간에서 -15.0 dB, 구간 밖 0 dB. remove+no_narration은 해당 구간만 소리 유지 (테스트) |
| 네트워크 없이 전체 경로 동작 | 부분 충족 | flite/가짜 공급자로는 외부 호출 없음. **Kokoro는 첫 실행 때 가중치·음색을 HuggingFace에서 받는다**(사용자 데이터 전송은 아님) |
| **사용자 PC 실측 보고서(RTF, 메모리, 콜드 스타트)와 라이선스 기록** | **미충족** | 벤치마크 하니스는 구현·검증 완료. 실측은 사용자 PC 필요. 라이선스는 `source_audit.md`(코드 라이선스 일부 확인, 가중치 미확인) |
| audio-first: 시간표·총 길이 보고, WAV/M4A 내보내기 | 충족 | `test_guide_exports_and_reports_total` |
| merge 오프셋 적용, 마지막 카운트 어긋남 측정 | 충족 | `--first-mark/--last-mark`로 측정, ±0.15초 초과 시 exit 2 (`test_drift_beyond_tolerance_fails`). 마크는 사용자가 영상에서 찍어야 함 |
| 가이드 해시 변경 후 merge 시 재촬영 경고 | 충족 | `test_reshoot_warning_and_shot_hash` |
| 이어폰/스피커 유출 경로 모두 합성 | 충족 | `--original duck|keep|remove` 테스트 |
| 12.5: 카운트 발음 청취 가능, BGM이 내레이션을 가리지 않음 | **미충족(청취 필요)** | Kokoro 실행 + 사람의 청취 |

테스트: `uv run pytest` -> **62개 통과**(단위 45 + 통합 17, 약 2분). 통합 테스트는 합성 영상·BGM과 가짜 TTS를 쓴다.

## 4. 독립 검증 (내 코드가 낸 PASS를 믿지 않고 외부 도구로 확인)

| 확인 | 도구 | 결과 |
|---|---|---|
| 샘플 가이드 총 길이 | `ffprobe` | 94.500초 = 지시서 12.5의 손계산(94.5초)과 일치 |
| 시작 톤 위치 | `silencedetect` | 가이드에서 6.001초(기대 6.000). merge 후 raw 시간 9.24초 직후에만 880 Hz 에너지(-5.8 dB; 앞뒤 -39 dB 이하) |
| 내레이션 시작 시각 | `silencedetect` | 무음 트리밍 후 6.321 / 10.820 / 15.320 ... (기대 + 약 20 ms = 의도한 앞 여유) |
| 자막 시작 | SRT | `Ekam.` 9.540초 = offset 3.240 + 6.300 |
| 영상 스트림 보존 | `ffmpeg -c copy -f md5` | 원본과 동일 |
| 덕킹 깊이 | `astats` | 6.00 dB |

## 5. 시험 중 발견해 고친 결함

| # | 결함 | 발견 경위 | 조치 |
|---|---|---|---|
| 1 | 공급자가 앞에 넣는 무음(flite 0.2초) 때문에 큐가 동작보다 늦게 들림 | `silencedetect` 독립 검증 | 합성 직후 앞뒤 무음 트리밍 (해석 R4) |
| 2 | 문장 길이를 µs로 반올림해 누적해서 전체 길이와 1 µs 불일치 | 단위 테스트 | 샘플 단위 누적 |
| 3 | 소리를 바꾸는 코드 변경에도 가이드 해시가 같아 재촬영 경고를 놓침 | 수동 실행 중 | 해시에 `SOUND_ENGINE_VERSION` 포함 (R9) |
| 4 | `loudnorm`이 동적 모드로 떨어져 덕킹·균형을 마스터에서 바꿈 | 믹스 보고서의 `normalization_type: dynamic` | 선형 이득 + 리미터 예산 (R3) |
| 5 | `--offset -00:01.0`을 argparse가 옵션으로 오인 | 통합 테스트 | 음수 시각 전처리 |
| 6 | 단축 제안이 2문장일 때 중복 | 수동 실행 | 중복 제거 |

## 6. 설계 이탈 (지시서와 다르게 한 곳)

덕킹 방식(사이드체인 대신 이득 곡선), 정규화 방식(`loudnorm` 대신 선형+리미터), 무음 트리밍 추가. 사유와 대가는
`docs/open_questions.md`의 R2, R3, R4. 지시서가 사이드체인·loudnorm을 명시했으므로 **되돌리기를 원하면 알려 달라.**

## 7. 사용자 PC에서 해야 할 일 (순서대로)

```powershell
git clone <저장소>; cd project_Y
uv sync
uv run avsync doctor                       # FFmpeg 경로·필터 점검, reports\ffmpeg_build.txt 생성
powershell -ExecutionPolicy Bypass -File scripts\setup_kokoro.ps1
uv run avsync benchmark tts --provider kokoro --voice <음색> --listen   # RTF/메모리/콜드 스타트 + 청취 문장 25개
```

- `--voice`에 쓸 음색 이름은 Kokoro 문서에서 확인해야 한다(이 환경에서는 목록을 읽지 못함 [재확인]).
- 청취용 WAV는 `reports\listening\kokoro\`에 생긴다. 발음(산스크리트 포함)·속도·음색을 평가하고 `voice_id`를 정한다.
- `configs\acceptance_wait.yaml`을 채우면 벤치마크가 통과/실패를 판정한다.
- 설치가 실패하면 오류 메시지를 알려 달라. 이 스크립트와 `provider_envs/kokoro/worker.py`는 실행해 보지 못했다.

## 8. 승인이 필요한 다음 단계 (지금은 구현하지 않음)

지시서 순서: Gemini TTS 공급자(5.7) -> 원격 GPU BGM(6장) -> 자동 오프셋 `align` -> burn-in 자막 -> 음악 우선.
Kokoro 청취 결과가 나온 뒤 5.7의 Phase 0 비교(동일 6문장)를 하는 순서가 합리적이다.
