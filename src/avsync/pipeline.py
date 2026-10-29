"""단계별 오케스트레이션: 타임라인 -> TTS/fit -> 믹스 -> 렌더 -> manifest. CLI는 이 함수들을 호출만 한다."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .audio import mix as M
from .audio.mix import BusItem
from .captions.srt import write_srt
from .config import Config, ConfigError, load_config
from .domain.hashing import sha256_file, sha256_json
from .domain.manifest import now_iso, read_json, write_json
from .domain.project import Project, load_project
from .domain.script import Line, Script, load_script
from .domain.timebase import fmt_time
from .platform.ffmpeg import FFmpeg, find_tools
from .providers.registry import create_tts_provider
from .providers.tts.base import ProviderError
from .tts.fit import FitInput, fit_lines, finalize_flags
from .tts.synth import Synthesizer
from .qa import checks as Q
from .video.probe import VideoInfo, probe_audio_duration_us, probe_video
from .video.render import mux


@dataclass
class Context:
    cfg: Config
    ff: FFmpeg
    project: Project
    tone: dict
    gates: dict

    @property
    def tts_cfg(self) -> dict:
        return self.tone.get("tts", {})

    def path(self, kind: str, name: str) -> Path:
        base = {"cache": self.project.cache, "out": self.project.outputs, "man": self.project.manifests}[kind]
        base.mkdir(parents=True, exist_ok=True)
        return base / name


def make_context(project_path: Path) -> Context:
    cfg = load_config()
    project = load_project(project_path)
    project.ensure_dirs()
    return Context(cfg, FFmpeg(find_tools(cfg)), project, cfg.tone(project.tone), cfg.gates)


# ---------------------------------------------------------------- 타임라인

def video_info(ctx: Context) -> VideoInfo:
    if ctx.project.video is None or not ctx.project.video.is_file():
        raise ConfigError(f"입력 영상이 없습니다: {ctx.project.video}")
    return probe_video(ctx.ff.tools, ctx.project.video)


def build_video_timeline(ctx: Context, write: bool = True) -> tuple[dict, Script]:
    info = video_info(ctx)
    script = load_script(ctx.project.script, require_at=True, fps=info.fps)
    problems = []
    for ln in script.lines:
        if ln.at_us >= info.duration_us:
            problems.append(f"{ln.id}: at({fmt_time(ln.at_us)})이 영상 길이({fmt_time(info.duration_us)}) 밖입니다")
        if ln.until_us is not None and ln.until_us > info.duration_us:
            problems.append(f"{ln.id}: until({fmt_time(ln.until_us)})이 영상 길이 밖입니다")
    if problems:
        raise ConfigError("\n".join(problems))
    tl = {
        "schema": 1, "mode": "video_first", "duration_us": info.duration_us,
        "fps": f"{info.fps_num}/{info.fps_den}", "video": info.as_dict(),
        "items": [{"id": l.id, "text": l.text, "at_us": l.at_us, "until_us": l.until_us, "mood": l.mood,
                   "pause_after_ms": l.pause_after_ms} for l in script.lines],
        "no_narration": [list(w) for w in script.no_narration],
        "script_sha256": sha256_file(ctx.project.script),
    }
    if write:
        write_json(ctx.path("man", "timeline.json"), tl)
    return tl, script


# ---------------------------------------------------------------- TTS + fit

def effective_speed(line: Line, script: Script, tone_tts: dict, warnings: list[str]) -> float:
    base = script.voice.base_speed if script.voice.base_speed is not None else float(tone_tts.get("speed", 1.0))
    moods = tone_tts.get("moods") or {}
    if line.mood not in moods:
        warnings.append(f"{line.id}: 알 수 없는 mood '{line.mood}' -> 배율 1.0 적용 (프리셋 mood: {', '.join(moods) or '없음'})")
    return round(base * float(moods.get(line.mood, 1.0)), 4)


def resolve_voice(ctx: Context, script: Script, provider_name: str) -> str | None:
    if script.voice.voice_id:
        return script.voice.voice_id
    return ((ctx.cfg.defaults.get("providers") or {}).get(provider_name) or {}).get("default_voice")


def run_tts(ctx: Context, timeline: dict, script: Script, *, provider_name: str | None = None,
            only_ids: list[str] | None = None) -> dict:
    name = provider_name or script.voice.provider or ctx.cfg.defaults["tts"]["default_provider"]
    provider = create_tts_provider(name, ctx.cfg, ctx.ff)
    try:
        caps = provider.capabilities()
        voice = resolve_voice(ctx, script, name)
        if voice is None and name != "flite":
            raise ProviderError(
                f"voice_id가 정해지지 않았습니다. 후보 음색을 청취한 뒤 script.yaml의 voice.voice_id를 지정하세요 ({name}).")
        g = ctx.gates["tts"]
        pause = int(ctx.tts_cfg.get("pause_between_sentences_ms", g["sentence_pause_ms"]))
        emph = ctx.tts_cfg.get("emphasis")
        synth = Synthesizer(provider, ctx.path("cache", "tts"), voice_id=voice, lang="en-US",
                            pronunciations=script.pronunciations, sentence_pause_ms=pause,
                            emphasis_pause_ms=int(ctx.tts_cfg.get("emphasis_pause_ms", 150)) if emph == "pause_only" else None,
                            cue_speed=ctx.tts_cfg.get("cue_speed"), cue_words=script.cue_words, max_speed=g["max_speed"])
        warnings: list[str] = []
        by_id = script.by_id()
        items = timeline["items"]
        inputs = []
        for i, it in enumerate(items):
            if only_ids and it["id"] not in only_ids:
                continue
            nxt = items[i + 1]["at_us"] if i + 1 < len(items) else None
            inputs.append(FitInput(by_id[it["id"]], it["at_us"], it["until_us"], nxt, timeline["duration_us"],
                                   effective_speed(by_id[it["id"]], script, ctx.tts_cfg, warnings)))
        if only_ids:
            missing = set(only_ids) - {fi.line.id for fi in inputs}
            if missing:
                raise ConfigError(f"알 수 없는 항목 id: {sorted(missing)}")
        t0 = time.perf_counter()
        fitted = fit_lines(inputs, synth.synth_line, speed_supported=caps.speed, max_speed=g["max_speed"],
                           speed_step=g["speed_step"], max_iterations=g["max_refit_iterations"],
                           gap_min_ms=g["gap_min_ms"])
        elapsed = time.perf_counter() - t0
        records = [f.as_dict() for f in fitted]
        fit_path = ctx.path("man", "fit_report.json")
        if only_ids and fit_path.is_file():
            old = {r["id"]: r for r in read_json(fit_path)["items"]}
            old.update({r["id"]: r for r in records})
            order = [it["id"] for it in items]
            records = [old[i] for i in order if i in old]
        finalize_flags(records, timeline["duration_us"])
        if caps.emotion is False and any(l.mood != "calm" for l in script.lines):
            warnings.append("공급자가 감정 제어를 지원하지 않아 mood는 속도 배율에만 반영됨")
        if any(l.emphasis for l in script.lines) and ctx.tts_cfg.get("emphasis") != "pause_only":
            warnings.append("톤 프리셋의 emphasis가 pause_only가 아니라 emphasis 지정이 무시됨")
        for r in records:
            warnings += r["audio"].get("warnings", [])
        report = {
            "schema": 1, "created": now_iso(), "provider": caps.as_dict(), "voice_id": voice,
            "tone": ctx.project.tone, "approved": script.approved,
            "synth_calls": synth.calls, "synth_seconds": round(synth.synth_seconds, 3),
            "fit_seconds": round(elapsed, 3), "warnings": sorted(set(warnings)),
            "summary": {
                "items": len(records),
                "ok": sum(1 for r in records if r["status"] == "ok"),
                "speed_adjusted": sum(1 for r in records if r["status"] == "speed_adjusted"),
                "exceeded": sum(1 for r in records if "exceeded" in r["flags"]),
                "overlap": sum(1 for r in records if "overlap" in r["flags"]),
                "past_end": sum(1 for r in records if "past_end" in r["flags"]),
            },
            "items": records,
        }
        write_json(fit_path, report)
        return report
    finally:
        provider.close()


def format_fit_table(report: dict) -> str:
    rows = ["id    시작       종료       허용종료   속도   상태            비고"]
    for r in report["items"]:
        flag = ",".join(r["flags"]) or r["status"]
        note = "; ".join(r["notes"])
        rows.append(f"{r['id']:<5} {fmt_time(r['at_us'])}  {fmt_time(r['end_us'])}  {fmt_time(r['allowed_end_us'])}  "
                    f"{r['speed']:<5.2f}  {flag:<14}  {note}")
        for s in r["suggestions"]:
            rows.append(f"      제안(문구는 자동 변경하지 않음): {s}")
    s = report["summary"]
    rows.append(f"요약: 항목 {s['items']} / ok {s['ok']} / 속도조정 {s['speed_adjusted']} / 초과 {s['exceeded']} / "
                f"겹침 {s['overlap']} / 끝초과 {s['past_end']}")
    return "\n".join(rows)


def fit_has_errors(report: dict) -> bool:
    s = report["summary"]
    return bool(s["exceeded"] or s["overlap"] or s["past_end"])


# ---------------------------------------------------------------- 믹스

def run_mix(ctx: Context, timeline: dict, fit: dict, *, work: Path, out_wav: Path,
            original_media: Path | None, original_policy: str, original_has_audio: bool,
            cue_items: list[BusItem] | None = None) -> dict:
    ff, tone, g = ctx.ff, ctx.tone, ctx.gates["mix"]
    D = timeline["duration_us"]
    work.mkdir(parents=True, exist_ok=True)
    rep: dict = {"warnings": []}

    eq = tone.get("narration_eq", {})
    items = [BusItem(Path(r["audio"]["wav_path"]), r["at_us"], r["duration_us"], r["id"]) for r in fit["items"]]
    narr = work / "narration.wav"
    M.render_bus(ff, items, D, narr, work, high_pass_hz=eq.get("high_pass_hz"),
                 comp_ratio=eq.get("compression_ratio"), fade_ms=g["fade_ms"])
    stems = [narr]
    windows = [(r["at_us"], r["at_us"] + r["duration_us"]) for r in fit["items"]]
    rep["narration_windows"] = [list(w) for w in windows]

    bgm_cfg = tone.get("bgm", {})
    proj_bgm = ctx.project.bgm
    if proj_bgm.source == "imported":
        bpath = proj_bgm.path
        if bpath is None or not bpath.is_file():
            raise ConfigError(f"BGM 파일이 없습니다: {bpath}")
        fit_bgm = work / "bgm_fit.wav"
        info = M.prepare_bgm(ff, bpath, probe_audio_duration_us(ctx.ff.tools, bpath), D, fit_bgm, work,
                             fade_in_s=float(bgm_cfg.get("fade_in_s", 1.0)),
                             fade_out_s=float(bgm_cfg.get("fade_out_s", 2.0)),
                             midrange_cut_db=float(bgm_cfg.get("midrange_1_4khz_cut_db", 0.0)))
        level = proj_bgm.level_db if proj_bgm.level_db is not None else bgm_cfg.get("level_vs_narration_db", -14)
        gain, n_lufs, b_lufs = M.compute_bgm_gain(ff, narr, fit_bgm, float(level))
        scaled = work / "bgm_scaled.wav"
        M.apply_gain(ff, fit_bgm, scaled, work, gain_db=gain)
        duck_db = proj_bgm.duck_db if proj_bgm.duck_db is not None else float(bgm_cfg.get("duck_db", 6.0))
        env = work / "bgm_duck_env.wav"
        M.make_duck_envelope(env, D, windows, depth_db=duck_db,
                             attack_ms=int(bgm_cfg.get("duck_attack_ms", 80)),
                             release_ms=int(bgm_cfg.get("duck_release_ms", 600)))
        ducked = work / "bgm_ducked.wav"
        M.apply_envelope(ff, scaled, env, ducked, work)
        stems.append(ducked)
        rep["bgm"] = {"path": str(bpath), "sha256": sha256_file(bpath), **info, "level_rel_db": level,
                      "gain_db": round(gain, 2), "narration_lufs": n_lufs, "bgm_lufs": b_lufs,
                      "duck_db": duck_db, "license_note": proj_bgm.license_note}
        if info["loops"] > 1:
            rep["warnings"].append(f"BGM이 짧아 {info['loops']}회 크로스페이드 루프함")
        if not proj_bgm.license_note:
            rep["warnings"].append("BGM license_note가 비어 있음 (출처·라이선스를 기록하세요)")

    if original_media is not None and original_has_audio:
        raw = work / "original_raw.wav"
        vid = timeline.get("video") or {}
        M.extract_original(ff, original_media, 0, D, raw, work,
                           stream_offset_us=vid.get("audio_start_us", 0) - vid.get("video_start_us", 0))
        no_narr = [tuple(w) for w in timeline.get("no_narration", [])]
        policy = original_policy
        if policy == "keep":
            stems.append(raw)
        elif policy == "duck":
            depth = ctx.project.duck_under_narration_db
            depth = abs(depth if depth is not None else tone.get("duck_under_narration_db", -15))
            env = work / "orig_duck_env.wav"
            M.make_duck_envelope(env, D, windows, depth_db=depth, attack_ms=50, release_ms=300)
            od = work / "original_ducked.wav"
            M.apply_envelope(ff, raw, env, od, work)
            stems.append(od)
        elif policy == "remove" and no_narr:
            env = work / "orig_keep_env.wav"
            M.make_keep_only_envelope(env, D, no_narr)
            ok = work / "original_kept.wav"
            M.apply_envelope(ff, raw, env, ok, work)
            stems.append(ok)
        rep["original"] = {"policy": policy, "stems_used": len(stems)}
    elif original_policy != "remove" and original_media is not None:
        rep["warnings"].append("원본에 오디오 트랙이 없어 원본 소리 정책을 무시함")

    if cue_items:
        cue = work / "cue.wav"
        M.render_bus(ff, cue_items, D, cue, work, fade_ms=5)
        stems.append(cue)

    pre = work / "premix.wav"
    M.sum_stems(ff, stems, pre, work)
    lufs = ctx.project.loudness_lufs if ctx.project.loudness_lufs is not None else g["loudness_lufs"]
    tp = ctx.project.true_peak_dbfs if ctx.project.true_peak_dbfs is not None else g["true_peak_dbfs"]
    ln = M.normalize_master(ff, pre, out_wav, lufs=lufs, true_peak=tp, budget_db=g["limiter_budget_db"])
    if not ln["reached_target"]:
        rep["warnings"].append(f"목표 {lufs} LUFS에 못 미침({ln['output_i']:.1f} LUFS): 피크 제한 예산 {g['limiter_budget_db']} dB 소진. 내레이션 피크를 낮추거나 예산을 늘리세요")
    rep["loudnorm"] = ln
    rep["target"] = {"lufs": lufs, "true_peak_dbfs": tp}
    rep["master"] = str(out_wav)
    rep["master_sha256"] = sha256_file(out_wav)
    rep["stems"] = {p.stem: str(p) for p in work.glob("*.wav")}
    write_json(ctx.path("man", "mix_report.json"), rep)
    return rep


# ---------------------------------------------------------------- 렌더 + manifest

def caption_style(ctx: Context):
    from .captions.ass import CaptionStyle
    cs = {**(ctx.cfg.defaults.get("captions") or {}), **ctx.project.caption_style}
    ff_ = cs.get("font_file")
    prof = ctx.cfg.profile(ctx.project.profile)
    return CaptionStyle(font_file=(ctx.project.root / ff_) if ff_ else None, font_license=str(cs.get("font_license") or ""),
                        font_size=int(cs.get("font_size", 64)), margin_v=int(cs.get("margin_v", 420)),
                        margin_h=int(cs.get("margin_h", 90)), width=prof["width"], height=prof["height"])


def captions_from_fit(fit: dict) -> list:
    from .captions.ass import Caption
    out = []
    for r in fit["items"]:
        for seg in r["audio"]["segments"]:
            a = r["at_us"]
            out.append(Caption(a + seg["start_us"], a + seg["end_us"], seg["original"],
                               [{"text": w["text"], "start_us": a + w["start_us"], "end_us": a + w["end_us"]}
                                for w in seg.get("words", [])]))
    return sorted(out, key=lambda c: c.start_us)


def run_render(ctx: Context, timeline: dict, mix_rep: dict, fit: dict, *, preview: bool) -> dict:
    info = video_info(ctx)
    profile = ctx.cfg.profile(ctx.project.profile)
    suffix = ".preview.mp4" if preview else ".mp4"
    out = ctx.path("out", f"{ctx.project.project_id}{suffix}")
    burn = None
    if ctx.project.captions == "burn_in":
        from .captions.ass import prepare_ass_dir
        if not ctx.ff.has_filter("ass"):
            raise ConfigError("이 FFmpeg 빌드에는 ass 필터(libass)가 없어 burn-in 자막을 쓸 수 없습니다. output.captions: sidecar_srt를 쓰세요")
        burn = prepare_ass_dir(ctx.path("cache", "burn"), captions_from_fit(fit), caption_style(ctx))
    r = mux(ctx.ff, info, Path(mix_rep["master"]), out, profile, ctx.project.reframe, burn_dir=burn)
    res = {"output": str(out), "sha256": sha256_file(out), **r, "preview": preview}
    if ctx.project.captions in ("sidecar_srt", "burn_in"):
        srt = out.with_suffix(".srt")
        write_srt(srt, fit["items"])
        res["srt"] = str(srt)
    return res


def write_manifest(ctx: Context, timeline: dict, fit: dict, mix_rep: dict, render: dict | None, qa: dict | None) -> Path:
    items = []
    for r in fit["items"]:
        items.append({"id": r["id"], "at_us": r["at_us"], "speed": r["speed"], "status": r["status"],
                      "wav_sha256": r["audio"]["wav_sha256"], "cache_key": r["audio"]["cache_key"],
                      "segments": r["audio"]["segments"]})
    man = {
        "schema": 1, "created": now_iso(), "project_id": ctx.project.project_id,
        "ffmpeg": ctx.ff.version().splitlines()[0],
        "config_sha256": sha256_json({"gates": ctx.gates, "tone": ctx.tone}),
        "inputs": {"video": sha256_file(ctx.project.video) if ctx.project.video else None,
                   "script": sha256_file(ctx.project.script)},
        "provider": fit["provider"], "voice_id": fit["voice_id"], "tone": ctx.project.tone,
        "approved": fit["approved"], "timeline": timeline, "items": items,
        "mix": {k: v for k, v in mix_rep.items() if k != "stems"}, "render": render, "qa": qa,
    }
    path = ctx.path("man", "manifest.json")
    write_json(path, man)
    return path


# ---------------------------------------------------------------- QA

def run_qa(ctx: Context, media: Path, timeline: dict, fit: dict, mix_rep: dict, *, preview: bool) -> dict:
    g = ctx.gates
    num, den = (int(x) for x in timeline["fps"].split("/"))
    frame_us = round(1_000_000 * den / num)
    target = mix_rep["target"]
    checks = [Q.check_av_duration(ctx.ff, media, frame_us, g["video"]["duration_tolerance_frames"])]
    checks += Q.check_loudness(ctx.ff, media, target["lufs"], g["mix"]["loudness_tolerance_lu"],
                               target["true_peak_dbfs"], g["mix"]["true_peak_tolerance_db"])
    checks.append(Q.check_clipping(ctx.ff, media, g["mix"]["clip_peak_dbfs"]))
    checks += Q.check_fit(fit)
    checks.append(Q.check_duck_depth(ctx.ff, mix_rep, g["mix"]["duck_depth_tolerance_db"]))
    if preview:
        checks.append(Q.Check("approval", "warn", "script.yaml이 approved: false - 미리듣기 결과물"))
    notes = list(mix_rep.get("warnings", [])) + list(fit.get("warnings", []))
    vid = timeline.get("video") or {}
    if vid.get("has_audio"):
        notes.append("원본 영상에 소리가 있음: 말소리·음악 포함 여부는 자동 판별하지 않으므로 직접 확인 (10장)")
    rep = {"schema": 1, "created": now_iso(), "status": Q.summarize(checks), "media": str(media),
           "checks": [c.as_dict() for c in checks], "notes": notes}
    write_json(ctx.path("out", "qa_report.json"), rep)
    ctx.path("out", "qa_report.md").write_text(
        Q.render_markdown(ctx.project.project_id, checks, Q.listening_checklist(preview), notes), encoding="utf-8")
    return rep
