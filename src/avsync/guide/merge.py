"""audio-first: 따라 찍은 영상과 가이드 소리를 오프셋으로 합친다 (4.4)."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from .. import pipeline as P
from ..audio import mix as M
from ..captions.srt import build_srt
from ..domain.hashing import sha256_file
from ..domain.manifest import now_iso, read_json, write_json
from ..domain.timebase import fmt_time, parse_time
from ..qa import checks as Q
from ..video.probe import probe_video
from ..video.render import mux


def merge_guide(cfg, ff, *, video: Path, guide_wav: Path, out: Path, offset_us: int | None,
                tone_at_us: int | None, first_mark_us: int | None, last_mark_us: int | None,
                original: str, trim_start_us: int, trim_end_us: int | None, accurate_trim: bool,
                shot_hash8: str | None, preview_ok: bool, reframe: str, profile_name: str,
                work: Path, duck_under_db: float) -> dict:
    gj_path = guide_wav.with_suffix(".json")
    if not gj_path.is_file():
        raise P.ConfigError(f"가이드 메타 JSON이 없습니다: {gj_path} (guide 명령이 같이 내보낸 파일이 필요합니다)")
    gj = read_json(gj_path)
    notes: list[str] = []
    gates = cfg.gates
    if sha256_file(guide_wav) != gj["wav_sha256"]:
        raise P.ConfigError("가이드 WAV가 메타 JSON의 해시와 다릅니다 (파일이 바뀌었거나 다른 가이드입니다)")
    if not gj["approved"] and not preview_ok:
        raise P.ConfigError("미승인(approved: false) 가이드입니다. 승인 후 다시 내보내거나 --preview를 지정하세요.")
    if shot_hash8 and shot_hash8 != gj["hash8"]:
        raise P.ConfigError(
            f"영상 재촬영 필요: 촬영에 쓴 가이드({shot_hash8})와 합성하려는 가이드({gj['hash8']})의 내용이 다릅니다. "
            f"새 가이드로 다시 촬영하거나, 촬영한 가이드 파일로 합성하세요.")

    info = probe_video(ff.tools, video)
    if info.is_vfr:
        notes.append(f"VFR 의심({info.vfr_detail}) - CFR로 재인코딩하며 프레임 단위 오프셋은 CFR 변환 후 기준임. "
                     f"오프셋 정확도(1프레임)는 실측으로 확인할 것")
    if offset_us is None:
        if tone_at_us is None:
            raise P.ConfigError("--offset 또는 --tone-at 중 하나가 필요합니다")
        if gj["tone_at_us"] is None:
            raise P.ConfigError("이 가이드에는 시작 톤이 없어 --tone-at을 쓸 수 없습니다. --offset을 쓰세요")
        offset_us = tone_at_us - gj["tone_at_us"]

    end_us = trim_end_us if trim_end_us is not None else info.duration_us
    if end_us > info.duration_us:
        raise P.ConfigError(f"trim-end({fmt_time(end_us)})가 영상 길이({fmt_time(info.duration_us)})를 넘습니다")
    if trim_start_us >= end_us:
        raise P.ConfigError("trim-start가 trim-end보다 앞이어야 합니다")
    D = end_us - trim_start_us
    shift = offset_us - trim_start_us
    work.mkdir(parents=True, exist_ok=True)

    guide_end = shift + gj["total_us"]
    if guide_end < D - 1_000_000:
        notes.append(f"영상이 가이드보다 {(D - guide_end) / 1e6:.1f}초 길다 (끝부분은 무음/BGM 없음)")
    if guide_end > D:
        last_win_end = max(b for _, b in gj["narration_windows"]) + shift
        notes.append(f"가이드 끝 {(guide_end - D) / 1e6:.1f}초가 영상 밖이라 잘림"
                     + (" - 마지막 내레이션이 잘림!" if last_win_end > D else ""))
    if shift < 0:
        notes.append(f"오프셋이 영상 시작보다 앞({fmt_time(shift)}): 가이드 앞부분을 잘라냄 (검은 화면 삽입 없음)")

    aligned = work / "guide_aligned.wav"
    M.align_audio(ff, guide_wav, shift, D, aligned, work)
    stems = [aligned]
    used_orig = None
    if original != "remove" and info.has_audio:
        raw = work / "original_raw.wav"
        M.extract_original(ff, video, trim_start_us, D, raw, work)
        if original == "keep":
            stems.append(raw)
        else:
            wins = [(a + shift, b + shift) for a, b in gj["narration_windows"]]
            env = work / "orig_duck_env.wav"
            M.make_duck_envelope(env, D, wins, depth_db=abs(duck_under_db), attack_ms=50, release_ms=300)
            od = work / "original_ducked.wav"
            M.apply_envelope(ff, raw, env, od, work)
            stems.append(od)
        used_orig = original
    elif original != "remove" and not info.has_audio:
        notes.append("영상에 오디오 트랙이 없어 원본 소리 정책을 무시함")

    if len(stems) == 1:
        master = aligned          # 가이드 소리를 그대로 재사용 (재합성·재정규화 없음)
    else:
        pre = work / "premix.wav"
        M.sum_stems(ff, stems, pre, work)
        master = work / "master.wav"
        t = gj["loudness_target"]
        M.normalize_master(ff, pre, master, lufs=t["lufs"], true_peak=t["true_peak_dbfs"],
                           budget_db=gates["mix"]["limiter_budget_db"])

    profile = cfg.profile(profile_name)
    r = mux(ff, info, master, out, profile, reframe, trim_start_us=trim_start_us,
            trim_end_us=end_us if (trim_start_us > 0 or trim_end_us is not None) else None,
            accurate_trim=accurate_trim)
    srt_path = out.with_suffix(".srt")
    caps = [{"at_us": c["start_us"], "audio": {"segments": [{"start_us": 0, "end_us": c["end_us"] - c["start_us"],
                                                              "original": c["text"]}]}} for c in gj["captions"]]
    srt_path.write_text(build_srt(caps, shift_us=shift), encoding="utf-8")

    # ---- QA
    g = gates
    checks = [Q.check_av_duration(ff, out, info.frame_us, g["video"]["duration_tolerance_frames"])]
    t = gj["loudness_target"]
    checks += Q.check_loudness(ff, out, t["lufs"], g["mix"]["loudness_tolerance_lu"], t["true_peak_dbfs"],
                               g["mix"]["true_peak_tolerance_db"])
    checks.append(Q.check_clipping(ff, out, g["mix"]["clip_peak_dbfs"]))
    tol = int(g["guide"]["drift_tolerance_s"] * 1_000_000)
    drift: dict = {"tolerance_us": tol}
    for label, mark, step in (("first", first_mark_us, gj["steps"][0]), ("last", last_mark_us, gj["steps"][-1])):
        if mark is None:
            continue
        expected = offset_us + step["start_us"]
        d = mark - expected
        drift[label] = {"mark_us": mark, "expected_us": expected, "drift_us": d}
        checks.append(Q.Check(f"drift_{label}", "pass" if abs(d) <= tol else "fail",
                              f"{label} 표시 {fmt_time(mark)} / 기대 {fmt_time(expected)} / 어긋남 {d / 1000:+.0f}ms "
                              f"(허용 ±{tol / 1000:.0f}ms; + 는 영상 동작이 가이드보다 늦음)", d / 1e6))
    if last_mark_us is None:
        notes.append("--last-mark가 없어 마지막 카운트의 어긋남(드리프트)을 측정하지 못함")
    status = Q.summarize(checks)
    report = {"schema": 1, "created": now_iso(), "status": status, "checks": [c.as_dict() for c in checks],
              "notes": notes, "offset_us": offset_us, "shift_us": shift, "guide_hash8": gj["hash8"],
              "render": r, "output": str(out), "output_sha256": sha256_file(out), "drift": drift,
              "original_policy_applied": used_orig, "preview": not gj["approved"]}
    write_json(out.with_suffix(".qa.json"), report)
    out.with_suffix(".qa.md").write_text(
        Q.render_markdown(out.name, checks, Q.listening_checklist(not gj["approved"]), notes), encoding="utf-8")
    return report


def parse_marks(values: dict, fps: Fraction | None) -> dict:
    return {k: (parse_time(v, fps=fps) if v is not None else None) for k, v in values.items()}
