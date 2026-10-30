"""audio-first: 가이드 소리 생성과 내보내기 (3.5, 4.4)."""
from __future__ import annotations

import dataclasses
import math
import struct
import wave
from pathlib import Path

from .. import pipeline as P
from ..audio.mix import BusItem
from ..domain.hashing import sha256_file, sha256_json
from ..domain.manifest import now_iso, read_json, write_json
from ..domain.script import Line, load_script
from ..domain.timebase import fmt_time, us_to_samples
from .schedule import GuidePlan, GuideSpec, build_plan, load_guide

TONE_RATE = 48000
# 소리를 바꾸는 코드 변경(무음 트리밍, 믹스 공식 등)이 있을 때 올린다. 올리면 가이드 해시가 바뀌어 재촬영 경고가 나온다.
SOUND_ENGINE_VERSION = 3


def write_tone(path: Path, hz: float, ms: int, fade_ms: int = 5) -> int:
    n = us_to_samples(ms * 1000, TONE_RATE)
    f = us_to_samples(fade_ms * 1000, TONE_RATE)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / f if f else 1.0, (n - 1 - i) / f if f else 1.0)
        frames += struct.pack("<h", int(0.35 * 32767 * env * math.sin(2 * math.pi * hz * i / TONE_RATE)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TONE_RATE)
        w.writeframes(bytes(frames))
    return n * 1_000_000 // TONE_RATE


def compute_guide_hash(ctx: P.Context, script_path: Path, guide_path: Path, provider_name: str) -> str:
    """재촬영 필요 여부 판단용 해시. 소리를 바꾸는 입력은 모두 포함한다."""
    s = load_script(script_path, require_at=False)
    bgm = ctx.project.bgm
    return sha256_json({
        "engine": SOUND_ENGINE_VERSION,
        "script": [(l.id, l.text, l.mood, l.pause_after_ms) for l in s.lines],
        "voice": [s.voice.provider, s.voice.voice_id, s.voice.base_speed],
        "pron": s.pronunciations,
        "guide": sha256_file(guide_path),
        "provider": provider_name,
        "tone": ctx.tone,
        "gates": {"tts": ctx.gates["tts"], "guide": ctx.gates["guide"], "mix": ctx.gates["mix"]},
        "bgm": {"sha": sha256_file(bgm.path) if bgm.path and bgm.path.is_file() else None,
                "level": bgm.level_db, "duck": bgm.duck_db, "source": bgm.source},
        "target": [ctx.project.loudness_lufs, ctx.project.true_peak_dbfs],
    })


def schedule_table(plan: GuidePlan, fit: dict | None) -> str:
    fit_by = {r["id"]: r for r in fit["items"]} if fit else {}
    rows = ["단계        호흡시작   내레이션   길이    허용     카운트"]
    for it in plan.items:
        r = fit_by.get(it["id"])
        step = next((s for s in plan.steps if s.line == it["id"]), None)
        start = fmt_time(step.start_us) if step else "-"
        dur = f"{r['duration_us'] / 1e6:5.2f}s" if r else "   -  "
        allowed = f"{(r['allowed_end_us'] - r['at_us']) / 1e6:5.2f}s" if r else "   -  "
        rows.append(f"{it['id']:<10}  {start}   {fmt_time(it['at_us'])}   {dur}  {allowed}  "
                    f"{step.counts if step else '-'}")
    rows.append(f"총 길이 {fmt_time(plan.total_us)} ({plan.total_us / 1e6:.1f}초) = 카운트인 "
                f"{plan.count_in_us / 1e6:.1f}s + 카운트 {sum(s.counts for s in plan.steps)}개 x {plan.count_us / 1e6:.2f}s "
                f"+ 여유 {plan.tail_us / 1e6:.1f}s")
    return "\n".join(rows)


def resolve_beats(ctx: P.Context, spec: GuideSpec) -> tuple[GuideSpec, dict | None]:
    """음악 우선: count_beats x 60/BPM 으로 호흡 길이를 정한다. from_bgm이면 프로젝트 BGM을 분석한다."""
    from ..audio.beats import analyze_file
    if spec.count_beats is None:
        return spec, None
    info = None
    bgm = ctx.project.bgm
    need_analysis = spec.bpm == "from_bgm" or spec.beat_align
    if need_analysis:
        if bgm.source != "imported" or bgm.path is None or not bgm.path.is_file():
            raise P.ConfigError("breath.bpm: from_bgm / beat_align에는 audio.bgm.source: imported 와 존재하는 BGM 파일이 필요합니다")
        from ..audio.beats import BeatInfo
        cache = ctx.path("cache", f"beats_{sha256_file(bgm.path)[:12]}.json")
        if not cache.is_file():
            write_json(cache, analyze_file(ctx.ff, bgm.path, ctx.path("cache", "beats_work")).as_dict())
        # 첫 실행과 캐시를 읽는 이후 실행이 같은 값을 쓰도록 항상 캐시(반올림된 값)에서 읽는다
        d = read_json(cache)
        info = BeatInfo(d["bpm"], d["period_us"], d["phase_us"], d["confidence"], d["beats_us"], d["downbeat_offset"], d["notes"])
    if spec.bpm == "from_bgm":
        if not info.has_beat:
            raise P.ConfigError(f"BGM에서 안정적인 박자를 찾지 못했습니다 ({'; '.join(info.notes)}). "
                                f"breath.bpm에 숫자를 직접 적거나 count_s를 쓰세요")
        bpm = info.bpm
    else:
        bpm = float(spec.bpm)
    count_us = round(spec.count_beats * 60_000_000 / bpm)
    d = {"bpm_used": bpm, "bpm_source": spec.bpm if spec.bpm == "from_bgm" else "explicit",
         "count_beats": spec.count_beats, "count_us": count_us, "analysis": info.as_dict() if info else None, "warnings": []}
    if info is not None and info.has_beat and spec.bpm != "from_bgm" and abs(info.bpm - bpm) / bpm > 0.01:
        d["warnings"].append(f"명시한 BPM {bpm:g}이 BGM 분석값 {info.bpm:.1f}과 1% 넘게 다름 - 박 정렬 의미가 없을 수 있음")
    return dataclasses.replace(spec, count_us=count_us), d


def build_guide(ctx: P.Context, *, provider_name: str | None = None, dry_run: bool = False) -> dict:
    proj = ctx.project
    if proj.guide is None:
        raise P.ConfigError("project.yaml의 input.guide가 필요합니다 (audio-first)")
    script = load_script(proj.script, require_at=False)
    spec: GuideSpec = load_guide(proj.guide)
    spec, beat_res = resolve_beats(ctx, spec)
    gg = ctx.gates["guide"]
    lead_ms = spec.lead_in_ms if spec.lead_in_ms is not None else gg["lead_in_ms"]
    plan = build_plan(spec, script.by_id(), lead_in_ms=lead_ms)
    name = provider_name or script.voice.provider or ctx.cfg.defaults["tts"]["default_provider"]

    unused = [l.id for l in script.lines if l.id not in {s.line for s in plan.steps}]
    notes = [f"script.yaml의 {unused}는 guide.yaml steps에 없어 사용되지 않음"] if unused else []

    all_lines = dict(script.by_id())
    for it in plan.items:
        if it["id"] not in all_lines:
            all_lines[it["id"]] = Line(id=it["id"], text=it["text"], mood=it["mood"])
    script2 = dataclasses.replace(script, lines=list(all_lines.values()))

    timeline = {"schema": 1, "mode": "audio_first", "duration_us": plan.total_us, "fps": None, "video": None,
                "items": [{k: v for k, v in it.items() if k != "kind"} for it in plan.items],
                "no_narration": [], "script_sha256": sha256_file(proj.script)}
    result: dict = {"plan": dataclasses.asdict(plan), "notes": notes, "total_us": plan.total_us, "beats": beat_res}
    if beat_res:
        notes += beat_res["warnings"]
    if dry_run:
        result["table"] = schedule_table(plan, None)
        return result

    write_json(ctx.path("man", "timeline.json"), timeline)
    fit = P.run_tts(ctx, timeline, script2, provider_name=name)
    result["fit"] = fit
    result["table"] = schedule_table(plan, fit)
    if P.fit_has_errors(fit):
        result["status"] = "fit_error"
        return result

    ghash = compute_guide_hash(ctx, proj.script, proj.guide, name)
    h8 = ghash[:8]
    approved = script.approved
    stem = f"guide_{h8}" + ("" if approved else ".preview")
    outdir = ctx.path("out", "guide")
    outdir.mkdir(parents=True, exist_ok=True)
    work = ctx.path("cache", "mix_guide")

    cue_items = []
    if plan.tone_at_us is not None:
        tone_wav = work / "tone.wav"
        dur = write_tone(tone_wav, gg["tone_hz"], gg["tone_ms"])
        cue_items.append(BusItem(tone_wav, plan.tone_at_us, dur, "tone"))
    wav = outdir / f"{stem}.wav"
    head_trim = 0
    if spec.beat_align and beat_res and beat_res["analysis"] and beat_res["analysis"]["bpm"]:
        a = beat_res["analysis"]
        head_trim = (a["phase_us"] - plan.count_in_us) % a["period_us"]           # 첫 호흡 시각에 BGM의 박이 오도록
        notes.append(f"박 정렬: BGM 앞 {head_trim / 1e6:.3f}초를 잘라 첫 호흡({fmt_time(plan.count_in_us)})을 박에 맞춤 "
                     f"(분석 {a['bpm']:.1f} BPM, 신뢰도 {a['confidence']:.2f})")
    elif spec.beat_align:
        notes.append("박 정렬 요청이 있으나 BGM에서 박자를 찾지 못해 정렬하지 않음")
    mix = P.run_mix(ctx, timeline, fit, work=work, out_wav=wav, original_media=None, original_policy="remove",
                    original_has_audio=False, cue_items=cue_items, bgm_head_trim_us=head_trim)
    m4a = outdir / f"{stem}.m4a"
    ctx.ff.run(["-i", str(wav), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(m4a)], context="m4a 내보내기")

    captions = []
    for r in fit["items"]:
        if r["id"] == "count_in":
            continue
        for seg in r["audio"]["segments"]:
            captions.append({"start_us": r["at_us"] + seg["start_us"], "end_us": r["at_us"] + seg["end_us"],
                             "text": seg["original"],
                             "words": [{"text": w["text"], "start_us": r["at_us"] + w["start_us"],
                                        "end_us": r["at_us"] + w["end_us"]} for w in seg.get("words", [])]})
    gjson = {
        "schema": 1, "created": now_iso(), "hash": ghash, "hash8": h8, "approved": approved,
        "project_id": proj.project_id, "wav": str(wav), "m4a": str(m4a), "wav_sha256": sha256_file(wav),
        "total_us": plan.total_us, "count_in_us": plan.count_in_us, "count_us": plan.count_us,
        "tone_at_us": plan.tone_at_us, "tone_hz": gg["tone_hz"], "tone_ms": gg["tone_ms"], "lead_in_us": plan.lead_in_us,
        "steps": [dataclasses.asdict(s) for s in plan.steps],
        "narration_windows": mix["narration_windows"], "captions": captions,
        "loudness_target": mix["target"], "provider": fit["provider"]["name"], "beats": beat_res,
    }
    gpath = outdir / f"{stem}.json"
    write_json(gpath, gjson)

    # 이미 내보낸 다른 가이드가 있으면 재촬영 경고 (3.5)
    hist_path = ctx.path("man", "guide_history.json")
    hist = read_json(hist_path) if hist_path.is_file() else {"exports": []}
    others = sorted({e["hash8"] for e in hist["exports"] if e["hash8"] != h8})
    if others:
        notes.append(f"이전에 내보낸 가이드({', '.join(others)})와 내용이 다릅니다. 이미 그 가이드로 촬영했다면 "
                     f"새 가이드에 맞춰 영상 재촬영이 필요합니다.")
    if not any(e["hash8"] == h8 and e["approved"] == approved for e in hist["exports"]):
        hist["exports"].append({"hash8": h8, "approved": approved, "created": now_iso(), "wav": str(wav)})
        write_json(hist_path, hist)
    if not approved:
        notes.append("script.yaml이 approved: false라서 .preview 파일로만 내보냄 (촬영용은 승인 후 다시 실행)")

    P.write_manifest(ctx, timeline, fit, mix, None, None)
    result.update({"status": "ok", "hash8": h8, "wav": str(wav), "m4a": str(m4a), "json": str(gpath),
                   "approved": approved, "notes": notes, "mix": {k: v for k, v in mix.items() if k != "stems"}})
    return result
