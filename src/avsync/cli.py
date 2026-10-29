"""avsync CLI. 종료 코드: 0 성공 / 1 오류 / 2 내용 검사 실패(fit·QA) / 130 사용자 중단."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import pipeline as P
from .config import ConfigError, load_config
from .domain.manifest import read_json
from .domain.script import ScriptError
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
    from .domain.script import load_script
    ctx = _ctx(args)
    af = _is_audio_first(ctx)
    fps = None
    if not af:
        fps = P.video_info(ctx).fps
    script = load_script(ctx.project.script, require_at=not af, fps=fps)
    budgets: dict[str, int | None] = {}
    if af:
        from .guide.schedule import load_guide
        spec = load_guide(ctx.project.guide)
        lead = (spec.lead_in_ms if spec.lead_in_ms is not None else ctx.gates["guide"]["lead_in_ms"]) * 1000
        for st in spec.steps:
            budgets[st.line] = st.counts * spec.count_us - lead
    else:
        info = P.video_info(ctx)
        L = script.lines
        for i, ln in enumerate(L):
            end = ln.until_us or (L[i + 1].at_us if i + 1 < len(L) else info.duration_us)
            budgets[ln.id] = end - ln.at_us
    findings = check_script(script, ctx.tts_cfg, ctx.cfg.defaults["tts"]["words_per_second"],
                            budgets_us=budgets, audio_first=af)
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
    if (off is None) == (tone is None):
        raise ConfigError("--offset 과 --tone-at 중 정확히 하나를 지정하세요")
    project = None
    if args.project:
        project = P.make_context(Path(args.project)).project
    out = Path(args.out) if args.out else video.with_name(video.stem + ".final.mp4")
    policy = args.original or (project.original if project else "duck")
    duck_under = (project.duck_under_narration_db if project and project.duck_under_narration_db is not None else
                  cfg.tone(project.tone if project else "yoga_flow_calm").get("duck_under_narration_db", -15))
    if args.dry_run:
        _emit(args, {"would_run": "merge"}, f"[dry-run] {video.name} + {guide.name}, 오프셋 {args.offset or args.tone_at}, 원본 소리 {policy} -> {out}")
        return EXIT_OK
    rep = merge_guide(
        cfg, ff, video=video, guide_wav=guide, out=out, offset_us=off, tone_at_us=tone,
        first_mark_us=parse_time(args.first_mark, fps=fps) if args.first_mark else None,
        last_mark_us=parse_time(args.last_mark, fps=fps) if args.last_mark else None,
        original=policy, trim_start_us=parse_time(args.trim_start, fps=fps) if args.trim_start else 0,
        trim_end_us=parse_time(args.trim_end, fps=fps) if args.trim_end else None,
        accurate_trim=not args.keyframe_trim, shot_hash8=args.shot_hash, preview_ok=args.preview,
        reframe=args.reframe or (project.reframe if project else "blur_pad"),
        profile_name=project.profile if project else "reels_9x16",
        work=(project.cache / "merge") if project else out.parent / f".avsync_merge_{out.stem}",
        duck_under_db=duck_under)
    rows = [f"{c['status'].upper():<5} {c['name']:<16} {c['detail']}" for c in rep["checks"]]
    rows += [f"참고: {n}" for n in rep["notes"]]
    rows.append(f"종합: {rep['status'].upper()}  출력: {rep['output']}  (영상 {rep['render']['render_mode']})")
    _emit(args, rep, "\n".join(rows))
    return EXIT_CONTENT if rep["status"] == "fail" else EXIT_OK


def cmd_benchmark(args) -> int:
    from .benchmark import run_benchmark
    cfg = load_config()
    ff = FFmpeg(find_tools(cfg))
    root = cfg.root
    acc = None
    if args.acceptance:
        from .config import load_yaml
        acc = (load_yaml(Path(args.acceptance)) or {}).get("acceptance_wait")
    listen = (root / "reports" / "listening" / args.provider) if args.listen else None
    if args.dry_run:
        _emit(args, {"would_run": "benchmark"}, f"[dry-run] {args.provider}: 목표 {args.targets}초 분량 합성 측정")
        return EXIT_OK
    rep = run_benchmark(cfg, ff, args.provider, voice=args.voice, targets_s=[int(x) for x in args.targets.split(",")],
                        sentences_file=root / "benchmarks" / "sentences.txt", listen_dir=listen, acceptance=acc,
                        report_dir=root / "reports")
    rows = [f"공급자 {args.provider} (voice={rep['voice']}) - 측정 기기: {rep['environment']['platform']}",
            f"콜드 스타트(기동+모델 로드+첫 합성): {rep['cold_start_s']}s"]
    rows += [f"  {r['target_s']}초 분량: 오디오 {r['audio_s']}s / 소요 {r['wall_s']}s / RTF {r['rtf']}" for r in rep["runs"]]
    rows.append(f"최대 메모리: {rep['peak_memory_mb']} MB ({rep['peak_memory_method']})")
    rows += [f"판정: {v}" for v in rep["verdict"]]
    if listen:
        rows.append(f"청취 파일 {len(rep['listening'])}개: {listen}")
    _emit(args, rep, "\n".join(rows))
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
    sp = add("guide", cmd_guide, "[audio-first] 시간표·가이드 소리 생성/내보내기")
    sp.add_argument("--provider")
    sp = add("merge", cmd_merge, "[audio-first] 촬영본과 가이드 소리를 오프셋으로 합성")
    sp.add_argument("--video", required=True)
    sp.add_argument("--guide", required=True, help="guide 명령이 내보낸 WAV (옆의 .json 필요)")
    sp.add_argument("--offset", help="raw 영상 기준, 가이드 0초가 놓이는 시각 (예: 00:03.240, 97f, -00:01.0)")
    sp.add_argument("--tone-at", help="영상에서 찾은 시작 톤(또는 첫 동작) 시각. offset = tone-at - 가이드의 톤 위치")
    sp.add_argument("--first-mark", help="첫 카운트 동작이 시작되는 영상 시각 (정렬 검증용)")
    sp.add_argument("--last-mark", help="마지막 카운트 동작이 시작되는 영상 시각 (드리프트 측정용)")
    sp.add_argument("--trim-start"), sp.add_argument("--trim-end")
    sp.add_argument("--keyframe-trim", action="store_true", help="트림을 키프레임 단위 스트림 복사로 (부정확)")
    sp.add_argument("--original", choices=["keep", "duck", "remove"], help="원본 소리 정책 (스피커 유출이면 remove)")
    sp.add_argument("--shot-hash", help="촬영에 쓴 가이드의 해시 앞 8자리. 다르면 재촬영 필요 오류")
    sp.add_argument("--preview", action="store_true", help="미승인 가이드 허용")
    sp.add_argument("--reframe", choices=["none", "blur_pad", "crop_center", "pad_solid"])
    sp.add_argument("--out")
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
    sp.add_argument("--provider", required=True)
    sp.add_argument("--voice")
    sp.add_argument("--targets", default="15,30,60")
    sp.add_argument("--listen", action="store_true", help="청취 평가용 문장 WAV를 reports/listening/에 출력")
    sp.add_argument("--acceptance", help="acceptance_wait가 적힌 YAML (12.1)")
    bg = sub.add_parser("bgm", help="BGM 라이브러리").add_subparsers(dest="bgcmd", required=True)
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
    except (ScriptError, ConfigError, TimeError, ToolError, ProviderError, ProcessFailed, ValueError, FileNotFoundError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
