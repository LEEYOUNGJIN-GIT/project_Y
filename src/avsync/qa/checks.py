"""QA 검사 (12.3). 측정할 수 있는 것만 판정하고, 청취가 필요한 항목은 체크리스트로 남긴다."""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from pathlib import Path

from ..audio.loudness import measure_ebur128, measure_peak_db, measure_rms_db
from ..domain.timebase import US, fmt_time
from ..platform.ffmpeg import FFmpeg
from ..video.probe import stream_durations_us


@dataclass
class Check:
    name: str
    status: str      # pass | warn | fail
    detail: str
    value: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def check_av_duration(ff: FFmpeg, media: Path, frame_us: int, tol_frames: int) -> Check:
    d = stream_durations_us(ff.tools, media)
    v, a = d["video"], d["audio"]
    if v is None or a is None:
        return Check("av_duration", "fail", f"영상/오디오 스트림 길이를 읽지 못함: {d}")
    diff = abs(v - a)
    frames = diff / frame_us if frame_us else float("inf")
    ok = frames <= tol_frames
    return Check("av_duration", "pass" if ok else "fail",
                 f"영상 {fmt_time(v)} / 오디오 {fmt_time(a)} / 차이 {diff / 1000:.1f}ms = {frames:.2f}프레임 (허용 {tol_frames})",
                 round(frames, 3))


def check_loudness(ff: FFmpeg, media: Path, target: float, tol: float, tp_target: float, tp_tol: float) -> list[Check]:
    ld = measure_ebur128(ff, media)
    out = []
    if ld.integrated is None:
        return [Check("loudness", "fail", "통합 음량을 측정하지 못함(무음?)")]
    ok = abs(ld.integrated - target) <= tol
    out.append(Check("loudness", "pass" if ok else "warn",
                     f"통합 {ld.integrated:.1f} LUFS (목표 {target} ±{tol}), LRA {ld.lra}", ld.integrated))
    if ld.true_peak is not None:
        okp = ld.true_peak <= tp_target + tp_tol
        out.append(Check("true_peak", "pass" if okp else "warn",
                         f"true peak {ld.true_peak:.1f} dBTP (목표 {tp_target} 이하, 허용 +{tp_tol}; AAC 인코딩 후 측정)",
                         ld.true_peak))
    return out


def check_clipping(ff: FFmpeg, media: Path, clip_db: float) -> Check:
    pk = measure_peak_db(ff, media)
    if pk is None:
        return Check("clipping", "warn", "샘플 피크를 측정하지 못함")
    return Check("clipping", "pass" if pk < clip_db else "fail",
                 f"샘플 피크 {pk:.2f} dBFS (기준 {clip_db} 미만)", pk)


def check_fit(fit: dict) -> list[Check]:
    s = fit["summary"]
    checks = []
    for key, label in (("exceeded", "허용 길이 초과"), ("overlap", "다음 항목과 겹침"), ("past_end", "타임라인 끝 초과")):
        ids = [r["id"] for r in fit["items"] if key in r["flags"]]
        checks.append(Check(f"fit_{key}", "fail" if ids else "pass",
                            f"{label}: {', '.join(ids) if ids else '없음'}", float(s[key])))
    return checks


def check_duck_depth(ff: FFmpeg, mix_rep: dict, tol_db: float) -> Check:
    bgm = mix_rep.get("bgm")
    stems = mix_rep.get("stems", {})
    if not bgm or "bgm_scaled" not in stems or "bgm_ducked" not in stems:
        return Check("duck_depth", "pass", "BGM 없음 - 검사 생략")
    want = float(bgm["duck_db"])
    depths = []
    for a, b in mix_rep["narration_windows"]:
        s, e = a + 150_000, b - 150_000          # 램프 구간 제외
        if e - s < 400_000:
            continue
        r_scaled = measure_rms_db(ff, Path(stems["bgm_scaled"]), s, e - s)
        r_duck = measure_rms_db(ff, Path(stems["bgm_ducked"]), s, e - s)
        if r_scaled is None or r_duck is None:
            continue
        depths.append(r_scaled - r_duck)
    if not depths:
        return Check("duck_depth", "warn", "측정 가능한 내레이션 구간(0.7초 이상)이 없음")
    med = statistics.median(depths)
    ok = abs(med - want) <= tol_db
    return Check("duck_depth", "pass" if ok else "fail",
                 f"내레이션 구간 BGM 감쇠 중앙값 {med:.2f} dB (목표 {want} ±{tol_db}, 구간 {len(depths)}개)", round(med, 2))


def listening_checklist(preview: bool) -> list[str]:
    items = [
        "내레이션이 BGM에 묻히지 않는가 (노트북 스피커와 이어폰 모두)",
        "카운트 단어(Ekam, Dve ...) 발음을 알아들을 수 있는가",
        "각 문장이 해당 동작 시작에 맞춰 들리는가",
        "원본 소리(duck/keep)에 말소리·음악이 섞여 있다면 내레이션과 충돌하지 않는가 / 음악이면 권리 문제는 없는가",
        "BGM 루프 이음매나 끝 페이드가 거슬리지 않는가",
    ]
    if preview:
        items.insert(0, "이 결과는 미리듣기(approved: false)다. 문구 승인 전에는 게시하지 않는다")
    return items


def summarize(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


def render_markdown(title: str, checks: list[Check], checklist: list[str], extra: list[str] | None = None) -> str:
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"# QA 보고서: {title}", "", f"종합: **{mark[summarize(checks)]}**", "",
             "| 검사 | 결과 | 상세 |", "|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.name} | {mark[c.status]} | {c.detail} |")
    if extra:
        lines += ["", "## 참고", *[f"- {x}" for x in extra]]
    lines += ["", "## 직접 청취 체크리스트 (자동 판정 불가)", *[f"- [ ] {x}" for x in checklist], ""]
    return "\n".join(lines)
