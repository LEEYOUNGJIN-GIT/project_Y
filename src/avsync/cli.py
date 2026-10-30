"""avsync CLI. 종료 코드: 0 성공 / 1 오류 / 2 내용 검사 실패(fit·QA) / 130 사용자 중단."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from . import pipeline as P
from .config import ConfigError, load_config
from .remote.kaggle import RemoteError
from .domain.manifest import read_json
from .domain.script import ScriptError, load_script
from .domain.timebase import TimeError, fmt_time, parse_time
from .platform.ffmpeg import FFmpeg, ToolError, find_tools
from .platform.process import ProcessFailed
from .providers.tts.base import ProviderError

EXIT_OK, EXIT_ERROR, EXIT_CONTENT, EXIT_INTERRUPT = 0, 1, 2, 130


def _emit(args, result: dict, text: str | None = None) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    elif text:
        print(text)


def _project_path(args) -> Path:
    p = Path(args.project) if args.project else Path("project.yaml")
    if not p.is_file():
        raise ConfigError(f"project.yaml을 찾을 수 없습니다: {p} (--project로 지정하거나 작업 폴더에서 실행)")
    return p


def _ctx(args) -> P.Context:
    return P.make_context(_project_path(args))


def _is_audio_first(ctx: P.Context) -> bool:
    return ctx.project.guide is not None and ctx.project.video is None


# ------------------------------------------------------------------ 명령

def cmd_doctor(args) -> int:
    from .doctor import format_doctor, run_doctor
    rep = run_doctor(write_reports=not args.dry_run)
    _emit(args, rep, format_doctor(rep))
    return EXIT_ERROR if rep["status"] == "fail" else EXIT_OK


def cmd_init(args) -> int:
    from .scaffold import init_workspace
    base = Path(args.base)
    if args.dry_run:
        _emit(args, {"would_create": str(base / args.project_id)}, f"[dry-run] 생성 예정: {base / args.project_id}")
        return EXIT_OK
    dest = init_workspace(args.project_id, base, args.example)
    _emit(args, {"created": str(dest)}, f"생성됨: {dest}\n다음: brief.md와 script.yaml을 채운 뒤 `avsync check-script --project {dest / 'project.yaml'}`")
    return EXIT_OK


def cmd_inspect_video(args) -> int:
    from .video.probe import probe_video
    cfg = load_config()
    info = probe_video(find_tools(cfg), Path(args.video))
    d = info.as_dict()
    text = "\n".join([
        f"파일: {info.path}", f"길이: {fmt_time(info.duration_us)}  fps: {info.fps_num}/{info.fps_den} (프레임 {info.frame_us}us)",
        f"해상도: {info.width}x{info.height} 회전 {info.rotation} -> 표시 {info.display_width}x{info.display_height}",
        f"코덱: {info.codec} {info.pix_fmt}  VFR: {'예' if info.is_vfr else '아니오'} ({info.vfr_detail})",
        f"오디오: {'있음 ' + str(info.audio_codec) + ' ' + str(info.audio_rate) + 'Hz' if info.has_audio else '없음'}",
    ])
    _emit(args, d, text)
    return EXIT_OK


def cmd_check_script(args) -> int:
    from .checkscript import check_script
    from .guide.schedule import load_guide
    from .domain.script import load_script
    ctx = _ctx(args)
    af = _is_audio_first(ctx)
    fps = None
    if not af:
        fps = P.video_info(ctx).fps
    script = load_script(ctx.project.script, require_at=not af, fps=fps)
    budgets: dict[str, int | None] = {}
    pre_findings: list[dict] = []
    if af:
        from .guide.build import resolve_beats
        spec = load_guide(ctx.project.guide)
        lead = (spec.lead_in_ms if spec.lead_in_ms is not None else ctx.gates["guide"]["lead_in_ms"]) * 1000
        if spec.count_beats is not None:               # 음악 우선: count_us는 BPM으로 먼저 계산해야 한다 (그대로 두면 0)
            try:
                spec, _ = resolve_beats(ctx, spec)
            except ConfigError as e:
                pre_findings.append({"level": "warn", "id": None, "message": f"호흡 길이를 계산하지 못해 글자 수 예산 검사를 건너뜀: {e}"})
        for st in spec.steps:
            budgets[st.line] = st.counts * spec.count_us - lead if spec.count_us > 0 else None
    else:
        info = P.video_info(ctx)
        L = script.lines
        for i, ln in enumerate(L):
            end = ln.until_us or (L[i + 1].at_us if i + 1 < len(L) else info.duration_us)
            budgets[ln.id] = end - ln.at_us
    findings = check_script(script, ctx.tts_cfg, ctx.cfg.defaults["tts"]["words_per_second"],
                            budgets_us=budgets, audio_first=af)
    findings = pre_findings + findings
    findings += [{"level": "warn", "id": None, "message": w} for w in script.warnings]
    if af:
        findings += [{"level": "warn", "id": None, "message": f"guide.yaml {w}"} for w in load_guide(ctx.project.guide).warnings]
    lines = [f"{f['level'].upper():<5} {f['id'] or '-':<6} {f['message']}" for f in findings]
    lines.append(f"항목 {len(script.lines)}개, 경고 {sum(1 for f in findings if f['level'] == 'warn')}개")
    _emit(args, {"findings": findings, "lines": len(script.lines)}, "\n".join(lines))
    return EXIT_OK


def cmd_plan(args) -> int:
    ctx = _ctx(args)
    if _is_audio_first(ctx):
        raise ConfigError("audio-first 프로젝트입니다. `avsync guide`를 쓰세요 (plan은 video-first용).")
    tl, _ = P.build_video_timeline(ctx, write=not args.dry_run)
    rows = [f"영상 {fmt_time(tl['duration_us'])}, {len(tl['items'])}개 항목"]
    for it in tl["items"]:
        rows.append(f"  {it['id']:<5} {fmt_time(it['at_us'])}" + (f" ~ {fmt_time(it['until_us'])}" if it["until_us"] else "") + f"  {it['text']}")
    if tl["video"]["is_vfr"]:
        rows.append(f"주의: VFR 의심 - {tl['video']['vfr_detail']}")
    _emit(args, tl, "\n".join(rows))
    return EXIT_OK


def cmd_tts(args) -> int:
    ctx = _ctx(args)
    if _is_audio_first(ctx):
        raise ConfigError("audio-first 프로젝트는 `avsync guide`가 합성까지 수행합니다.")
    if args.dry_run:
        _emit(args, {"would_run": "tts"}, f"[dry-run] 공급자 {args.provider or '(script/기본)'}로 "
                                          f"{'전체' if not args.line else args.line} 합성 + fit")
        return EXIT_OK
    tl, script = P.build_video_timeline(ctx)
    rep = P.run_tts(ctx, tl, script, provider_name=args.provider, only_ids=args.line or None)
    _emit(args, rep, P.format_fit_table(rep) + f"\n합성 호출 {rep['synth_calls']}회 (캐시 적중 항목은 제외)")
    return EXIT_CONTENT if P.fit_has_errors(rep) else EXIT_OK


def _load_fit(ctx: P.Context) -> dict:
    p = ctx.path("man", "fit_report.json")
    if not p.is_file():
        raise ConfigError("fit_report.json이 없습니다. 먼저 `avsync tts --all`을 실행하세요.")
    return read_json(p)


def cmd_fit_report(args) -> int:
    ctx = _ctx(args)
    rep = _load_fit(ctx)
    _emit(args, rep, P.format_fit_table(rep) + ("\n경고:\n  " + "\n  ".join(rep["warnings"]) if rep["warnings"] else ""))
    return EXIT_CONTENT if P.fit_has_errors(rep) else EXIT_OK


def cmd_preview(args) -> int:
    ctx = _ctx(args)
    tl, script = P.build_video_timeline(ctx)
    rep = P.run_tts(ctx, tl, script, provider_name=args.provider, only_ids=[args.line])
    r = next(x for x in rep["items"] if x["id"] == args.line)
    _emit(args, r, f"{args.line}: {r['audio']['wav_path']}\n길이 {r['duration_us'] / 1e6:.2f}s, 속도 {r['speed']}, 상태 {r['status']} {','.join(r['flags'])}")
    return EXIT_OK


def cmd_mix(args) -> int:
    ctx = _ctx(args)
    if _is_audio_first(ctx):
        raise ConfigError("audio-first 프로젝트의 믹스는 `avsync guide`가 수행합니다.")
    if args.dry_run:
        _emit(args, {"would_run": "mix"}, "[dry-run] 내레이션 버스 -> BGM(루프·이득·덕킹) -> 원본 소리 -> 합산 -> 2-pass 정규화")
        return EXIT_OK
    tl, _ = P.build_video_timeline(ctx, write=False)
    fit = _load_fit(ctx)
    if P.fit_has_errors(fit) and not args.allow_errors:
        raise ConfigError("fit 오류(초과/겹침/끝초과)가 있습니다. `avsync fit-report`로 확인하고 고치거나 --allow-errors로 진행하세요.")
    info = tl["video"]
    rep = P.run_mix(ctx, tl, fit, work=ctx.path("cache", "mix"), out_wav=ctx.path("out", f"{ctx.project.project_id}_mix.wav"),
                    original_media=ctx.project.video, original_policy=ctx.project.original,
                    original_has_audio=info["has_audio"])
    ld = rep["loudnorm"]
    _emit(args, rep, f"믹스 완료: {rep['master']}\n출력 음량 {ld['output_i']:.1f} LUFS / TP {ld['output_tp']:.1f} dBTP / 이득 {ld['gain_db']:+.1f} dB"
          f"{' (리미터 사용)' if ld['limited'] else ''}"
          + ("".join(f"\n경고: {w}" for w in rep["warnings"])))
    return EXIT_OK


def cmd_render(args) -> int:
    ctx = _ctx(args)
    fit = _load_fit(ctx)
    if not fit["approved"] and not args.preview:
        raise ConfigError("script.yaml이 approved: false입니다. 최종 렌더는 승인 후에만 가능합니다. 미리듣기는 --preview.")
    if args.dry_run:
        _emit(args, {"would_run": "render"}, "[dry-run] 영상 mux (스트림 복사 가능하면 복사) + SRT")
        return EXIT_OK
    tl, _ = P.build_video_timeline(ctx, write=False)
    mix = read_json(ctx.path("man", "mix_report.json"))
    res = P.run_render(ctx, tl, mix, fit, preview=args.preview or not fit["approved"])
    P.write_manifest(ctx, tl, fit, mix, res, None)
    _emit(args, res, f"출력: {res['output']}\n방식: {res['render_mode']}" + (f" ({'; '.join(res['reencode_reasons'])})" if res["reencode_reasons"] else " (영상 스트림 복사)"))
    return EXIT_OK


def cmd_qa(args) -> int:
    ctx = _ctx(args)
    tl, _ = P.build_video_timeline(ctx, write=False)
    fit, mix = _load_fit(ctx), read_json(ctx.path("man", "mix_report.json"))
    final = ctx.path("out", f"{ctx.project.project_id}.mp4")
    preview = not final.is_file()
    media = ctx.path("out", f"{ctx.project.project_id}.preview.mp4") if preview else final
    if not media.is_file():
        raise ConfigError("렌더 결과가 없습니다. 먼저 `avsync render`를 실행하세요.")
    rep = P.run_qa(ctx, media, tl, fit, mix, preview=preview or not fit["approved"])
    man = ctx.path("man", "manifest.json")
    if man.is_file():
        m = read_json(man)
        m["qa"] = {"status": rep["status"], "checks": rep["checks"]}
        from .domain.manifest import write_json
        write_json(man, m)
    rows = [f"{c['status'].upper():<5} {c['name']:<16} {c['detail']}" for c in rep["checks"]]
    rows.append(f"종합: {rep['status'].upper()}  (보고서: {ctx.path('out', 'qa_report.md')})")
    _emit(args, rep, "\n".join(rows))
    return EXIT_CONTENT if rep["status"] == "fail" else EXIT_OK


def cmd_build(args) -> int:
    """video-first 일괄 실행: plan -> tts -> (fit 오류 시 중단) -> mix -> render -> qa."""
    import contextlib
    import io
    import types
    ctx = _ctx(args)
    if _is_audio_first(ctx):
        raise ConfigError("audio-first 프로젝트입니다. `avsync guide` 후 촬영하고 `avsync merge`를 쓰세요.")
    approved = load_script(ctx.project.script, require_at=True, fps=P.video_info(ctx).fps).approved
    preview = args.preview or not approved
    if preview and not args.json:
        print("참고: script.yaml이 approved: false라서 미리듣기 렌더(.preview.mp4)로 진행합니다" if not approved else "미리듣기 렌더로 진행합니다")
    steps = [("plan", cmd_plan, {}), ("tts", cmd_tts, {"all": True, "line": None}), ("mix", cmd_mix, {"allow_errors": False}),
             ("render", cmd_render, {"preview": preview}), ("qa", cmd_qa, {})]
    results = {}
    rc = EXIT_OK
    for name, fn, extra in steps:
        ns = types.SimpleNamespace(**{**vars(args), **extra, "json": args.json, "dry_run": False})
        if args.json:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                step_rc = fn(ns)
            try:
                results[name] = {"rc": step_rc, "result": json.loads(buf.getvalue() or "null")}
            except json.JSONDecodeError:
                results[name] = {"rc": step_rc, "output": buf.getvalue()}
        else:
            print(f"\n== {name} ==")
            step_rc = fn(ns)
            results[name] = {"rc": step_rc}
        if step_rc != EXIT_OK and name != "qa":
            rc = step_rc
            break
        if name == "qa":
            rc = step_rc
    if args.json:
        print(json.dumps({"steps": results, "rc": rc}, ensure_ascii=False, indent=2, default=str))
    return rc


def cmd_guide(args) -> int:
    from .guide.build import build_guide
    ctx = _ctx(args)
    res = build_guide(ctx, provider_name=args.provider, dry_run=args.dry_run)
    out = [res["table"]]
    if "fit" in res:
        out.append(P.format_fit_table(res["fit"]))
    out += [f"참고: {n}" for n in res.get("notes", [])]
    if res.get("status") == "ok":
        out.append(f"가이드 해시 {res['hash8']}\n  WAV: {res['wav']}\n  M4A: {res['m4a']}\n  JSON: {res['json']}")
    out_text = "\n".join(out)
    safe = {k: v for k, v in res.items() if k != "fit"} if args.json else res
    _emit(args, safe, out_text)
    return EXIT_CONTENT if res.get("status") == "fit_error" else EXIT_OK


def cmd_merge(args) -> int:
    from .guide.merge import merge_guide
    from .video.probe import probe_video
    cfg = load_config()
    tools = find_tools(cfg)
    ff = FFmpeg(tools)
    video, guide = Path(args.video), Path(args.guide)
    info = probe_video(tools, video)
    fps = info.fps
    off = parse_time(args.offset, fps=fps) if args.offset else None
    tone = parse_time(args.tone_at, fps=fps) if args.tone_at else None
    if args.auto_align:
        if off is not None or tone is not None:
            raise ConfigError("--auto-align은 --offset/--tone-at과 함께 쓸 수 없습니다")
        from .guide.align import find_tone
        gjson = read_json(guide.with_suffix(".json"))
        if gjson.get("tone_at_us") is None:
            raise ConfigError("이 가이드에는 시작 톤이 없어 --auto-align을 쓸 수 없습니다")
        hits = find_tone(ff, video, Path(tempfile.mkdtemp(prefix="avsync_align_")), freq_hz=gjson.get("tone_hz", 880.0),
                         tone_ms=gjson.get("tone_ms", 100), stream_offset_us=info.audio_offset_us)
        if not hits:
            raise ConfigError("영상 소리에서 시작 톤을 찾지 못했습니다. 스피커로 톤을 녹음했는지 확인하거나 --offset을 직접 지정하세요")
        print(f"자동 정렬: 톤 {fmt_time(hits[0].time_us)} (점수 {hits[0].score:.2f}, 후보 {len(hits)}개) -> "
              f"오프셋 {fmt_time(hits[0].time_us - gjson['tone_at_us'])}", file=sys.stderr)
        tone = hits[0].time_us
    if (off is None) == (tone is None):
        raise ConfigError("--offset, --tone-at, --auto-align 중 정확히 하나를 지정하세요")
    marks = {}
    for m in args.mark or []:
        name, _, t = m.partition("=")
        if not t:
            raise ConfigError(f"--mark는 단계=시각 형식이어야 합니다: {m!r} (예: l06=01:00.2)")
        marks[name] = parse_time(t, fps=fps)
    project = None
    proj_ctx = None
    if args.project:
        proj_ctx = P.make_context(Path(args.project))          # 한 번만 만든다 (설정 로드·도구 탐색·오타 경고가 두 번 나오지 않게)
        project = proj_ctx.project
    out = Path(args.out) if args.out else video.with_name(video.stem + ".final.mp4")
    policy = args.original or (project.original if project else "duck")
    duck_under = (project.duck_under_narration_db if project and project.duck_under_narration_db is not None else
                  cfg.tone(project.tone if project else "yoga_flow_calm").get("duck_under_narration_db", -15))
    burn_style = None
    if args.burn_in or (project and project.captions == "burn_in"):
        if project is None:
            raise ConfigError("--burn-in에는 폰트 설정이 있는 --project가 필요합니다 (output.caption_style)")
        burn_style = P.caption_style(proj_ctx)
    if args.dry_run:
        _emit(args, {"would_run": "merge"}, f"[dry-run] {video.name} + {guide.name}, 오프셋 {args.offset or args.tone_at}, 원본 소리 {policy} -> {out}")
        return EXIT_OK
    rep = merge_guide(
        cfg, ff, video=video, guide_wav=guide, out=out, offset_us=off, tone_at_us=tone,
        first_mark_us=parse_time(args.first_mark, fps=fps) if args.first_mark else None,
        last_mark_us=parse_time(args.last_mark, fps=fps) if args.last_mark else None,
        marks=marks, stretch=args.stretch,
        original=policy, trim_start_us=parse_time(args.trim_start, fps=fps) if args.trim_start else 0,
        trim_end_us=parse_time(args.trim_end, fps=fps) if args.trim_end else None,
        accurate_trim=not args.keyframe_trim, shot_hash8=args.shot_hash, preview_ok=args.preview,
        reframe=args.reframe or (project.reframe if project else "blur_pad"),
        profile_name=project.profile if project else "reels_9x16",
        work=(project.cache / "merge") if project else out.parent / f".avsync_merge_{out.stem}",
        duck_under_db=duck_under, burn_style=burn_style)
    rows = [f"{c['status'].upper():<5} {c['name']:<16} {c['detail']}" for c in rep["checks"]]
    rows += [f"참고: {n}" for n in rep["notes"]]
    rows.append(f"종합: {rep['status'].upper()}  출력: {rep['output']}  (영상 {rep['render']['render_mode']})")
    _emit(args, rep, "\n".join(rows))
    return EXIT_CONTENT if rep["status"] == "fail" else EXIT_OK


def cmd_align(args) -> int:
    from .guide.align import find_tone
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    gj = read_json(Path(args.guide).with_suffix(".json"))
    if gj.get("tone_at_us") is None:
        raise ConfigError("이 가이드에는 시작 톤이 없습니다")
    from .video.probe import probe_video
    info = probe_video(ff.tools, Path(args.video))
    hits = find_tone(ff, Path(args.video), Path(tempfile.mkdtemp(prefix="avsync_align_")),
                     freq_hz=gj.get("tone_hz", 880.0), tone_ms=gj.get("tone_ms", 100),
                     stream_offset_us=info.audio_offset_us)
    res = {"hits": [dict(h.as_dict(), offset_us=h.time_us - gj["tone_at_us"]) for h in hits]}
    if not hits:
        _emit(args, res, "시작 톤을 찾지 못했습니다. 이어폰만 쓰면 톤이 녹음되지 않습니다. 스피커로 시작 톤을 짧게 틀거나 --offset을 직접 찾으세요.")
        return EXIT_CONTENT
    rows = [f"후보 {i + 1}: 톤 {fmt_time(h.time_us)} 점수 {h.score:.2f} 톤성 {h.tonality:.2f} 대비 {h.contrast_db:.0f}dB -> "
            f"--offset {fmt_time(h.time_us - gj['tone_at_us'])}" for i, h in enumerate(hits)]
    rows.append("후보가 여러 개면 영상에서 실제 톤 위치를 눈으로 확인하세요 (자동 정렬은 가장 높은 점수를 씁니다).")
    _emit(args, res, "\n".join(rows))
    return EXIT_OK


def _parse_voices(spec: str | None, names: list[str]) -> dict[str, str]:
    """`--voice`: 공급자가 하나면 음성 이름, 여러 개면 `kokoro=af_heart,gemini=Kore` (음성 이름은 공급자마다 다르다)."""
    if not spec:
        return {}
    if "=" in spec:
        out = {}
        for part in spec.split(","):
            k, sep, v = part.partition("=")
            if not sep or not k.strip() or not v.strip():
                raise ConfigError(f"--voice 형식 오류: {part!r} (예: kokoro=af_heart,gemini=Kore)")
            out[k.strip()] = v.strip()
        extra = sorted(set(out) - set(names))
        if extra:
            raise ConfigError(f"--voice에 --provider에 없는 공급자가 있습니다: {extra}")
        return out
    if len(names) != 1:
        raise ConfigError("공급자가 여럿이면 --voice를 공급자별로 지정하세요 (예: --voice kokoro=af_heart,gemini=Kore). "
                          "음성 이름은 공급자마다 다릅니다")
    return {names[0]: spec}


def cmd_benchmark(args) -> int:
    from .benchmark import run_benchmark
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    root = cfg.root
    acc = None
    if args.acceptance:
        from .config import load_yaml
        acc = (load_yaml(Path(args.acceptance)) or {}).get("acceptance_wait")
    names = [n.strip() for n in args.provider.split(",") if n.strip()]
    voices = _parse_voices(args.voice, names)
    if args.dry_run:
        _emit(args, {"would_run": "benchmark"}, f"[dry-run] {names}: 목표 {args.targets or '(공급자 기본)'}초 분량 합성 측정")
        return EXIT_OK
    rows: list[str] = []
    reps = {}
    for name in names:
        net = name == "gemini"          # 네트워크 공급자는 호출이 한도를 소모하므로 기본 측정량을 줄인다
        targets = args.targets or ("15" if net else "15,30,60")
        limit = args.max_sentences or (6 if net else None)      # 5.7 Phase 0: 동일 대본 6문장 비교
        listen = (root / "reports" / "listening" / name) if args.listen else None
        rep = run_benchmark(cfg, ff, name, voice=voices.get(name),
                            targets_s=[int(x) for x in targets.split(",")],
                            sentences_file=root / "benchmarks" / "sentences.txt", listen_dir=listen, acceptance=acc,
                            report_dir=root / "reports", listen_limit=limit)
        reps[name] = rep
        rows += [f"공급자 {name} (voice={rep['voice']}) - 측정 기기: {rep['environment']['platform']}",
                 f"콜드 스타트(기동+모델 로드+첫 합성): {rep['cold_start_s']}s"]
        rows += [f"  {r['target_s']}초 분량: 오디오 {r['audio_s']}s / 소요 {r['wall_s']}s / RTF {r['rtf']}" for r in rep["runs"]]
        rows.append(f"최대 메모리: {rep['peak_memory_mb']} MB ({rep['peak_memory_method']})")
        rows += [f"판정: {v}" for v in rep["verdict"]]
        if listen:
            rows.append(f"청취 파일 {len(rep['listening'])}개: {listen}")
    _emit(args, reps if len(names) > 1 else reps[names[0]], "\n".join(rows))
    return EXIT_OK


def cmd_beats(args) -> int:
    from .audio.beats import analyze_file
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    info = analyze_file(ff, Path(args.file), Path(tempfile.mkdtemp(prefix="avsync_beats_")))
    if not info.has_beat:
        _emit(args, info.as_dict(), "박자 없음: " + "; ".join(info.notes) + "\n(앰비언트/드론이면 정상입니다. guide.yaml은 breath.count_s를 쓰세요)")
        return EXIT_OK
    rows = [f"BPM {info.bpm:.2f} (신뢰도 {info.confidence:.2f}), 주기 {info.period_us / 1000:.1f}ms, 첫 박 {fmt_time(info.phase_us)}",
            "다운비트(4박 주기 추정): " + (f"{info.downbeat_offset}번째 박" if info.downbeat_offset is not None else "불확실")]
    rows += [f"참고: {n}" for n in info.notes]
    rows.append("주의: 합성 클릭 트랙으로만 검증한 신호처리 휴리스틱입니다. 실제 곡은 귀로 확인하세요. 템포 오차 e는 곡 길이에 비례한 박 위치 어긋남으로 누적됩니다.")
    _emit(args, info.as_dict(), "\n".join(rows))
    return EXIT_OK


def cmd_gemini(args) -> int:
    from .domain.manifest import now_iso, write_json
    from .providers.tts.gemini_cli import find_cli
    cfg = load_config()
    ack = cfg.model_cache() / "gemini_free_ack.json"
    if args.gcmd == "status":
        try:
            cmd = find_cli(((cfg.defaults.get("providers") or {}).get("gemini") or {}).get("cli_path"))
            cli_txt = f"gemini-api: {cmd[0]}"
        except Exception as e:  # noqa: BLE001
            cli_txt = f"gemini-api: 없음 ({e})"
        _emit(args, {"ack": ack.is_file()}, f"{cli_txt}\n무료 키 확인: {'완료 ' + read_json(ack)['acknowledged_at'] if ack.is_file() else '없음 (avsync gemini confirm-free --yes)'}\n"
              "키는 이 도구가 읽지 않습니다. `gemini-api whoami`로 CLI의 인증 상태를 확인하세요.")
        return EXIT_OK
    if not args.yes:
        print("Gemini 무료 등급 사용 확인 (지시서 5.7)\n"
              "- 사용할 API 키는 결제(Billing)가 연결되지 않은 프로젝트에서 발급한 것이어야 합니다.\n"
              "- 무료 등급의 입력은 제품 개선에 쓰일 수 있습니다. 전송되는 것은 script.yaml의 대본 문장뿐입니다.\n"
              "- 한도를 넘거나 결제 연결 프로젝트의 키를 쓰면 과금될 수 있으며, 이 도구는 결제 상태를 알 수 없습니다.\n"
              "위 내용을 확인했다면 `avsync gemini confirm-free --yes`를 실행하세요.", file=sys.stderr)
        return EXIT_ERROR
    write_json(ack, {"acknowledged_at": now_iso(), "statement": "결제 미연결 프로젝트의 키를 사용함을 사용자가 확인함"})
    log = cfg.root / "reports" / "gemini_ack.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{now_iso()} 결제 미연결 프로젝트의 키 사용 확인 (source_audit 기록용)\n")
    _emit(args, {"ack": True}, f"확인 기록: {ack}")
    return EXIT_OK


def _remote_dirs(cfg):
    from .remote.kaggle import RemoteDirs
    return RemoteDirs(cfg.root / "library" / "bgm" / "_remote")


def cmd_bgm_remote(args) -> int:
    from .remote import kaggle as K
    from .remote.plan import load_plan
    cfg = load_config()
    dirs = _remote_dirs(cfg)
    rc = (cfg.defaults.get("remote") or {}).get("kaggle") or {}
    cmd = args.bgcmd
    if cmd == "remote-plan":
        plan = load_plan(Path(args.prompts))
        rows = [f"{t.id:<16} {t.duration_s:>5.0f}s  시드 {len(t.seeds)}개  {t.prompt[:70]}" for t in plan.tracks]
        rows.append(f"작업 {plan.jobs}개, 생성 오디오 합계 {plan.total_audio_s:.0f}초, 계획 해시 {plan.hash()}")
        state = read_json(dirs.state) if dirs.state.is_file() else {}
        if (plan.jobs > 3 or any(t.duration_s > 30 for t in plan.tracks)) and not state.get("fetched"):
            rows.append("권장: 먼저 곡 2-3개(10-15초)로 시험 실행해 소요 시간과 한도 소모를 기록한 뒤 일괄 작업 크기를 정하세요 (6.3). "
                        "이 스크립트는 Kaggle에서 한 번도 실행된 적이 없습니다.")
        _emit(args, {"jobs": plan.jobs, "hash": plan.hash(), "total_audio_s": plan.total_audio_s}, "\n".join(rows))
        return EXIT_OK
    if cmd == "remote-run":
        plan = load_plan(Path(args.prompts))
        owner = args.owner or rc.get("owner")
        if not owner:
            raise ConfigError("Kaggle 사용자명이 필요합니다: --owner 또는 configs/defaults.yaml의 remote.kaggle.owner")
        cli = None if args.dry_run else K.KaggleCli(K.find_kaggle(rc.get("cli_path")))
        res = K.remote_run(plan, dirs, cli, owner=owner, accelerator=args.accelerator or rc.get("accelerator"),
                           revision=args.revision, dry_run=args.dry_run)
        _emit(args, res, (f"[dry-run] 커널 폴더 {res['kernel_dir']}\n  {' '.join(res['command'])}\n  메타: {json.dumps(res['kernel'], ensure_ascii=False)}"
                          if res["dry_run"] else f"푸시 완료: {res['ref']}\n다음: avsync bgm remote-status"))
        return EXIT_OK
    cli = K.KaggleCli(K.find_kaggle(rc.get("cli_path")))
    ff = FFmpeg(find_tools(cfg))
    if cmd == "remote-status":
        res = K.remote_status(dirs, cli)
        _emit(args, res, f"{res['ref']}: {res['status']}" + (f"\n실패 메시지: {res['failure']}" if res["failure"] else ""))
        return EXIT_CONTENT if res["status"] in ("error", "cancelled") else EXIT_OK
    if cmd == "remote-fetch":
        res = K.remote_fetch(dirs, cli, ff, cfg.gates)
        rows = [f"등록 {len(res['accepted'])}곡 / 폐기 {len(res['discarded'])}곡 / 생성 실패 {len(res['failed'])}곡 (결과 {res['results']}개)"]
        rows += [f"  등록: {a['name']} -> {Path(a['path']).name}" + (f" [경고: {'; '.join(a['warnings'])}]" if a["warnings"] else "") for a in res["accepted"]]
        rows += [f"  폐기: {d['name']}: {'; '.join(d['reasons'])}" for d in res["discarded"]]
        rows += [f"  실패: {f['name']}: {f['error']}" for f in res["failed"]]
        if not res["complete"]:
            rows.append("일부만 완료된 결과입니다. 남은 곡은 계획에서 제외하고 다시 remote-run 하세요.")
        _emit(args, res, "\n".join(rows))
        return EXIT_OK
    raise ConfigError(f"알 수 없는 명령: {cmd}")


def cmd_bgm_check(args) -> int:
    from .audio.loudness import measure_ebur128, measure_peak_db
    from .audio.vocals import vocal_score
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    f = Path(args.file)
    voc = vocal_score(ff, f, Path(tempfile.mkdtemp(prefix="avsync_vocal_")))
    ld, pk = measure_ebur128(ff, f), measure_peak_db(ff, f)
    g = cfg.gates["bgm"]
    verdict = ("보컬 가능성 높음(폐기 후보)" if voc.score >= g["vocal_discard_score"] else
               "불확실(청취 필요)" if voc.score >= g["vocal_unknown_score"] else "보컬 가능성 낮음")
    res = {"vocal": voc.as_dict(), "integrated_lufs": ld.integrated, "peak_dbfs": pk, "verdict": verdict}
    _emit(args, res, f"보컬 휴리스틱 {voc.score:.2f} -> {verdict}\n통합 {ld.integrated} LUFS, 피크 {pk} dBFS\n"
          "주의: 이 점수는 ML 판정이 아닌 휴리스틱이며 규칙적인 3-8Hz 리듬 악기를 오탐할 수 있습니다. 최종 판단은 청취입니다.")
    return EXIT_OK


def cmd_bgm_import(args) -> int:
    from .bgmlib import import_bgm
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    if args.dry_run:
        _emit(args, {"would_run": "bgm import"}, f"[dry-run] {args.file} 등록 (note={args.note!r})")
        return EXIT_OK
    meta = import_bgm(ff, Path(args.file), args.note, args.vocals)
    _emit(args, meta, f"등록: {meta['path']}\n길이 {fmt_time(meta['duration_us'])}, 통합 {meta['integrated_lufs']} LUFS, 피크 {meta['peak_dbfs']} dBFS"
          + "".join(f"\n경고: {w}" for w in meta["warnings"]))
    return EXIT_OK


# ------------------------------------------------------------------ 파서

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", help="project.yaml 경로 (기본: ./project.yaml)")
    common.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    common.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 할 일만 보여 줌")
    common.add_argument("--resume", action="store_true", help="이어서 실행 (합성·믹스 캐시는 항상 재사용되므로 기본 동작과 같음)")

    ap = argparse.ArgumentParser(prog="avsync", description="영상에 영어 TTS 내레이션과 BGM을 입히는 개인용 도구")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, **kw)
        sp.set_defaults(func=fn)
        return sp

    add("doctor", cmd_doctor, "환경 점검 (FFmpeg 필터, 경로, 메모리 ...)")
    sp = add("init", cmd_init, "작업 폴더 생성")
    sp.add_argument("project_id")
    sp.add_argument("--base", default="workspaces")
    sp.add_argument("--example", help="examples/ 의 예제 이름 (예: surya_a)")
    sp = add("inspect-video", cmd_inspect_video, "영상 정보")
    sp.add_argument("video")
    add("check-script", cmd_check_script, "대본·발음·글자 수 예산 검증")
    add("plan", cmd_plan, "[video-first] 타임라인 확정")
    sp = add("build", cmd_build, "[video-first] plan -> tts -> mix -> render -> qa 한 번에")
    sp.add_argument("--provider")
    sp.add_argument("--preview", action="store_true", help="미리듣기 렌더로 강제")
    sp = add("guide", cmd_guide, "[audio-first] 시간표·가이드 소리 생성/내보내기")
    sp.add_argument("--provider")
    sp = add("merge", cmd_merge, "[audio-first] 촬영본과 가이드 소리를 오프셋으로 합성")
    sp.add_argument("--video", required=True)
    sp.add_argument("--guide", required=True, help="guide 명령이 내보낸 WAV (옆의 .json 필요)")
    sp.add_argument("--offset", help="raw 영상 기준, 가이드 0초가 놓이는 시각 (예: 00:03.240, 97f, -00:01.0)")
    sp.add_argument("--tone-at", help="영상에서 찾은 시작 톤(또는 첫 동작) 시각. offset = tone-at - 가이드의 톤 위치")
    sp.add_argument("--auto-align", action="store_true", help="영상 소리에서 시작 톤을 찾아 오프셋을 자동 결정 (톤이 녹음된 경우만)")
    sp.add_argument("--stretch", action="store_true", help="첫/마지막 마크로 기기 간 드리프트를 계산해 가이드를 미세 보정 (atempo)")
    sp.add_argument("--mark", action="append", help="단계=시각 (예: l06=01:00.2). 중간 카운트의 어긋남을 독립 검증. 반복 가능")
    sp.add_argument("--first-mark", help="첫 카운트 동작이 시작되는 영상 시각 (정렬 검증용)")
    sp.add_argument("--last-mark", help="마지막 카운트 동작이 시작되는 영상 시각 (드리프트 측정용)")
    sp.add_argument("--trim-start"), sp.add_argument("--trim-end")
    sp.add_argument("--keyframe-trim", action="store_true", help="트림을 키프레임 단위 스트림 복사로 (부정확)")
    sp.add_argument("--original", choices=["keep", "duck", "remove"], help="원본 소리 정책 (스피커 유출이면 remove)")
    sp.add_argument("--shot-hash", help="촬영에 쓴 가이드의 해시 앞 8자리. 다르면 재촬영 필요 오류")
    sp.add_argument("--preview", action="store_true", help="미승인 가이드 허용")
    sp.add_argument("--reframe", choices=["none", "blur_pad", "crop_center", "pad_solid"])
    sp.add_argument("--burn-in", action="store_true", help="자막을 영상에 입힘 (project.yaml의 output.caption_style 폰트·라이선스 필요)")
    sp.add_argument("--out")
    sp = add("align", cmd_align, "[audio-first] 영상 소리에서 가이드 시작 톤을 찾아 오프셋 제안")
    sp.add_argument("--video", required=True)
    sp.add_argument("--guide", required=True)
    sp = add("tts", cmd_tts, "[video-first] 합성 + 길이 맞춤")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--line", action="append", help="특정 항목만 (여러 번 지정 가능)")
    sp.add_argument("--provider")
    add("fit-report", cmd_fit_report, "겹침·초과 보고")
    sp = add("preview", cmd_preview, "한 항목 미리듣기 합성")
    sp.add_argument("--line", required=True)
    sp.add_argument("--provider")
    sp = add("mix", cmd_mix, "[video-first] 믹스")
    sp.add_argument("--allow-errors", action="store_true")
    sp = add("render", cmd_render, "[video-first] 영상 mux + SRT")
    sp.add_argument("--preview", action="store_true", help="미승인 대본의 미리듣기 렌더")
    add("qa", cmd_qa, "[video-first] 결과 QA")
    bp = sub.add_parser("benchmark", help="벤치마크").add_subparsers(dest="bcmd", required=True)
    sp = bp.add_parser("tts", parents=[common], help="TTS RTF/메모리/콜드 스타트")
    sp.set_defaults(func=cmd_benchmark)
    sp.add_argument("--provider", required=True, help="공급자 이름. 쉼표로 여러 개 (예: kokoro,gemini)")
    sp.add_argument("--voice", help="공급자가 하나일 때의 음색")
    sp.add_argument("--targets", help="측정할 분량(초) 목록. 기본 15,30,60 (gemini는 15)")
    sp.add_argument("--max-sentences", type=int, help="청취용 문장 수 제한 (gemini 기본 6)")
    sp.add_argument("--listen", action="store_true", help="청취 평가용 문장 WAV를 reports/listening/에 출력")
    sp.add_argument("--acceptance", help="acceptance_wait가 적힌 YAML (12.1)")
    sp = add("beats", cmd_beats, "BGM의 템포·박 분석 (음악 우선 모드)")
    sp.add_argument("file")
    gp = sub.add_parser("gemini", help="Gemini TTS 설정").add_subparsers(dest="gcmd", required=True)
    sp = gp.add_parser("confirm-free", parents=[common], help="결제 미연결 프로젝트의 키임을 확인(5.7 free_only)")
    sp.set_defaults(func=cmd_gemini)
    sp.add_argument("--yes", action="store_true")
    sp = gp.add_parser("status", parents=[common], help="CLI·확인 상태")
    sp.set_defaults(func=cmd_gemini)
    bg = sub.add_parser("bgm", help="BGM 라이브러리").add_subparsers(dest="bgcmd", required=True)
    for name, hlp in (("remote-plan", "원격 생성 계획 검증"), ("remote-run", "Kaggle 커널 생성·푸시"),
                      ("remote-status", "커널 상태"), ("remote-fetch", "결과 다운로드·검증·등록")):
        sp = bg.add_parser(name, parents=[common], help=hlp)
        sp.set_defaults(func=cmd_bgm_remote)
        if name in ("remote-plan", "remote-run"):
            sp.add_argument("prompts", help="prompts.yaml (6.4 형식)")
        if name == "remote-run":
            sp.add_argument("--owner", help="Kaggle 사용자명")
            sp.add_argument("--accelerator", help="예: NvidiaTeslaT4 (생략하면 Kaggle 기본 GPU)")
            sp.add_argument("--revision", help="ACE-Step 저장소의 커밋/태그를 고정")
    sp = bg.add_parser("check", parents=[common], help="음원 검사(보컬 휴리스틱, 음량, 피크)")
    sp.set_defaults(func=cmd_bgm_check)
    sp.add_argument("file")
    sp = bg.add_parser("import", parents=[common], help="음원 등록")
    sp.set_defaults(func=cmd_bgm_import)
    sp.add_argument("file")
    sp.add_argument("--note", required=True, help="출처·라이선스")
    sp.add_argument("--vocals", default="unknown", choices=["yes", "no", "unknown"])
    return ap


_TIME_FLAGS = {"--offset", "--tone-at", "--first-mark", "--last-mark", "--trim-start", "--trim-end"}
_NEG_TIME = re.compile(r"^-[\d:.]+f?$")


def _fix_negative_times(argv: list[str]) -> list[str]:
    """`--offset -00:01.0` 은 argparse가 옵션으로 오인하므로 `--offset=-00:01.0` 으로 바꾼다."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _TIME_FLAGS and i + 1 < len(argv) and _NEG_TIME.match(argv[i + 1]):
            out.append(f"{a}={argv[i + 1]}")
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(_fix_negative_times(list(sys.argv[1:] if argv is None else argv)))
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
        return EXIT_INTERRUPT
    except (ScriptError, ConfigError, TimeError, ToolError, ProviderError, ProcessFailed, RemoteError, ValueError,
            FileNotFoundError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
