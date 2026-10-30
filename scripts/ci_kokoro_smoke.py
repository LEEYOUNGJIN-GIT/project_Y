"""CI 전용: Kokoro 벤치마크와 샘플 가이드 합성을 실제로 돌리고 산출물을 ci_artifacts/ 에 모은다.

사용자가 내려받아 직접 들어 볼 수 있도록 청취용 WAV와 가이드 WAV를 함께 남긴다.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from avsync.cli import main

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "ci_artifacts"
VOICE = os.environ.get("KOKORO_VOICE", "af_heart")     # kokoro 소스의 docstring 예시에 나오는 음색


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def kokoro_python() -> Path:
    sub = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return ROOT / "provider_envs" / "kokoro" / ".venv" / sub


MODEL_INFO_CODE = r"""
import json
from huggingface_hub import HfApi
api = HfApi()
files = api.list_repo_files('hexgrad/Kokoro-82M')
voices = sorted(f.split('/')[-1][:-3] for f in files if f.startswith('voices/') and f.endswith('.pt'))
mi = api.model_info('hexgrad/Kokoro-82M')
card = mi.card_data.to_dict() if getattr(mi, 'card_data', None) else {}
print(json.dumps({'voices': voices, 'license': card.get('license'), 'sha': mi.sha, 'tags': mi.tags,
                  'other_files': [f for f in files if not f.startswith('voices/')]}))
"""


def model_info() -> dict | None:
    """가중치 라이선스와 음색 목록을 HuggingFace에서 직접 읽는다 (개발 환경에서는 접근 불가였던 사실 확인)."""
    import subprocess
    r = subprocess.run([str(kokoro_python()), "-c", MODEL_INFO_CODE], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        summary("### Kokoro 모델 정보 조회 실패\n```\n" + r.stderr[-600:] + "\n```")
        return None
    info = json.loads(r.stdout.strip().splitlines()[-1])
    (OUT / "kokoro_model_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    english = [v for v in info["voices"] if v[:2] in ("af", "am", "bf", "bm")]
    summary(f"### Kokoro-82M 모델 정보 (HuggingFace 직접 조회)\n- 가중치 라이선스(model card): **{info['license']}**\n"
            f"- 리비전: {info['sha']}\n- 영어 음색 {len(english)}개: {', '.join(english)}\n- 그 외 파일: {', '.join(info['other_files'])}")
    return info


VOICE_SENTENCES = ["Shat. Exhale, downward dog. Stay for five breaths.", "Slow down. This is not a race."]
PREFERRED = ["af_heart", "af_bella", "af_nicole", "af_sarah", "am_adam", "am_michael", "bf_emma", "bm_george"]


def voice_samples(info: dict) -> None:
    """후보 음색별로 같은 두 문장을 합성해 사용자가 직접 비교해 고를 수 있게 한다 (5.6: 후보 3개 청취)."""
    from avsync.config import load_config
    from avsync.platform.ffmpeg import FFmpeg, find_tools
    from avsync.providers.registry import create_tts_provider
    from avsync.providers.tts.base import TTSRequest
    cfg = load_config()
    prov = create_tts_provider("kokoro", cfg, FFmpeg(find_tools(cfg)))
    out = OUT / "voices"
    out.mkdir(exist_ok=True)
    avail = set(info["voices"])
    chosen = [v for v in PREFERRED if v in avail] or sorted(v for v in avail if v[:2] in ("af", "am"))[:6]
    rows = ["### 후보 음색별 합성 길이(초) - 같은 두 문장, 속도 0.95"]
    try:
        for v in chosen:
            durs = []
            for i, text in enumerate(VOICE_SENTENCES):
                res = prov.synthesize(TTSRequest(text, v, 0.95, "en-US", out / f"{v}_{i + 1}.wav"))
                durs.append(f"{res.duration_us / 1e6:.2f}")
            rows.append(f"- {v}: {' / '.join(durs)}")
    finally:
        prov.close()
    summary("\n".join(rows))


PHONEME_CODE = r"""
import json, sys
sys.stdout.reconfigure(encoding='utf-8')      # Windows 기본 콘솔 코덱(cp1252)은 IPA 음소를 인코딩하지 못한다
from kokoro import KPipeline
words = json.loads(sys.argv[1])
p = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', model=False)      # 음소 변환만 (가중치 로드 없음)
out = {}
for label, text in words:
    ph = []
    for gs, ps, _ in p(text):
        ph.append(ps)
    out[label] = ' | '.join(ph)
print(json.dumps(out, ensure_ascii=False))
"""


# 1차 실행(run 12)에서 사전 치환이 의도와 어긋난 단어: Ekam('ehkum'->에이), Dve, Ashtau('ashtow'->오우)
PHONEME_CANDIDATES = [
    ("Ekam", "ekum"), ("Ekam", "ekkum"), ("Ekam", "[Ekam](/ˈɛkəm/)"),
    ("Dve", "dvay"), ("Dve", "dway"), ("Dve", "[Dve](/dvˈA/)"),
    ("Ashtau", "ashtau"), ("Ashtau", "[Ashtau](/ˈæʃtW/)"),
]


def phoneme_diagnostics(script_yaml: Path) -> None:
    """카운트 단어를 Kokoro G2P가 어떤 음소로 바꾸는지 철자 그대로 / 발음 사전의 철자 치환 둘 다 출력한다.

    지시서의 respelling(예: EH-kum)이 실제로 의도한 소리에 가까워지는지 사람이 판단할 근거다. 이 진단은 소리를 들려주지는 않는다.
    """
    import subprocess

    import yaml
    pron = yaml.safe_load(script_yaml.read_text(encoding="utf-8")).get("pronunciations") or {}
    items = []
    for word, respell in pron.items():
        items.append([f"{word} (철자 그대로)", word])
        items.append([f"{word} (사전 '{respell}' -> '{respell.replace('-', '').lower()}')", respell.replace("-", "").lower()])
    for word, cand in PHONEME_CANDIDATES:         # 사전 항목이 의도와 어긋난 단어의 후보 (철자 치환 / 음소 표기)
        items.append([f"{word} 후보 {cand}", cand])
    r = subprocess.run([str(kokoro_python()), "-c", PHONEME_CODE, json.dumps(items, ensure_ascii=False)],
                       capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if r.returncode != 0:
        summary("### 음소 진단 실패\n```\n" + r.stderr[-500:] + "\n```")
        return
    res = json.loads(r.stdout.strip().splitlines()[-1])
    (OUT / "phonemes.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    summary("### 카운트 단어의 Kokoro 음소 (misaki 표기; 철자 그대로 vs 발음 사전 치환)\n"
            + "\n".join(f"- {k}: `{v}`" for k, v in res.items()))


def mix_diagnostics(proj: Path) -> None:
    """실제 Kokoro 음성으로 만든 가이드의 음량 정규화 결과 (합성 flite 시험에서는 -16.5 LUFS에서 멈췄다)."""
    rep = json.loads((proj / "manifests" / "mix_report.json").read_text(encoding="utf-8"))
    ln = rep["loudnorm"]
    bgm = rep.get("bgm") or {}
    summary("### 실제 Kokoro 음성 + 합성 BGM의 마스터 음량\n"
            f"- 입력 {ln['input_i']:.1f} LUFS / 피크 {ln['input_tp']:.1f} dBTP -> 이득 {ln['gain_db']:+.1f} dB"
            f"{' (리미터 사용)' if ln['limited'] else ''}\n"
            f"- 출력 {ln['output_i']:.1f} LUFS / 피크 {ln['output_tp']:.1f} dBTP, 목표 도달: **{ln['reached_target']}** "
            f"(목표 {rep['target']['lufs']} LUFS, 리미터 예산 {ln['limiter_budget_db']} dB)\n"
            f"- BGM 이득 {bgm.get('gain_db')} dB, 내레이션 {bgm.get('narration_lufs')} LUFS, 덕킹 {bgm.get('duck_db')} dB\n"
            + "".join(f"- 경고: {w}\n" for w in rep.get("warnings", [])))


COMPRESSORS = {
    "압축 없음": None,
    "acompressor 2:1 (-30 dB)": "acompressor=threshold=-30dB:ratio=2:attack=5:release=120:makeup=1",
    "acompressor 3:1 (-30 dB)": "acompressor=threshold=-30dB:ratio=3:attack=5:release=120:makeup=1",
    "acompressor 4:1 (-34 dB)": "acompressor=threshold=-34dB:ratio=4:attack=5:release=120:makeup=1",
}


def loudness_experiment(proj: Path) -> None:
    """내레이션 PLR(피크-음량 차)이 얼마인지, 압축을 앞에 두면 목표 -14 LUFS에 닿는지 실측한다. 소리를 바꾸는 코드는 아직 넣지 않았다.

    측정 근거만 남긴다: 어떤 설정이 듣기에 좋은지는 사람이 청취로 판단해야 한다.
    """
    from avsync.audio.loudness import master_normalize, measure_ebur128
    from avsync.platform.ffmpeg import FFmpeg, Tools

    rep = json.loads((proj / "manifests" / "mix_report.json").read_text(encoding="utf-8"))
    stems = {k: Path(v) for k, v in (rep.get("stems") or {}).items() if Path(v).is_file()}
    ff = FFmpeg(Tools(Path(shutil.which("ffmpeg")), Path(shutil.which("ffprobe")), True))
    lines = ["### 스템별 음량/피크 (PLR = 피크 - 통합음량)"]
    for name, path in sorted(stems.items()):
        m = measure_ebur128(ff, path)
        if m.integrated is None or m.true_peak is None:
            lines.append(f"- {name}: 무음")
            continue
        lines.append(f"- {name}: {m.integrated:.1f} LUFS / {m.true_peak:.1f} dBTP / PLR {m.true_peak - m.integrated:.1f} dB / LRA {m.lra}")
    narr, bgm, cue = stems.get("narration"), stems.get("bgm_ducked"), stems.get("cue")
    if narr is not None and bgm is not None:
        from avsync.audio import mix as M
        rest = [p for p in (bgm, cue) if p is not None]
        lines.append("### 내레이션 스템만 압축한 뒤 다시 합산 (목표 -14 LUFS / -1 dBTP, 리미터 예산 4 dB)")
        work = Path(tempfile.mkdtemp(prefix="avsync_exp_"))

        def report(label: str, parts: list[Path], keep: str | None) -> None:
            pre = work / "pre.wav"
            M.sum_stems(ff, parts, pre, work)
            res = master_normalize(ff, pre, work / "out.wav", target_i=-14.0, target_tp=-1.0, limiter_budget_db=4.0)
            lines.append(f"- {label}: 합산 {res['input_i']:.1f} LUFS/{res['input_tp']:.1f} dBTP -> 출력 {res['output_i']:.1f} LUFS/"
                         f"{res['output_tp']:.1f} dBTP, LRA {res['output_lra']}, 목표 도달 {res['reached_target']}")
            if keep:
                (OUT / "listening_compress").mkdir(exist_ok=True)
                shutil.copy(work / "out.wav", OUT / "listening_compress" / keep)

        for i, (label, flt) in enumerate(COMPRESSORS.items()):
            n = narr
            if flt:
                n = work / f"narr_{i}.wav"
                ff.run(["-i", str(narr), "-af", flt, "-c:a", "pcm_s16le", str(n)], context="내레이션 압축 실험")
            report(f"내레이션 {label}", [n, *rest], f"narr_{i}.wav" if flt else "narr_none.wav")
        report("내레이션 압축 없음, BGM 없음(게이팅 영향 확인)", [p for p in (narr, cue) if p is not None], None)
        # BGM 레벨 스윕: 게이팅 가설 확인. bgm_ducked를 dB만큼 키운 뒤 합산 (현재 BGM은 내레이션보다 14.5 dB 낮다)
        lines.append("### BGM 레벨 스윕 (bgm_ducked를 +x dB; 예산 4 dB 고정) / 목표를 맞추는 데 필요한 리미터 예산")
        for i, up in enumerate((0, 4, 8, 12)):
            b = bgm
            if up:
                b = work / f"bgm_up{up}.wav"
                ff.run(["-i", str(bgm), "-af", f"volume={up}dB", "-c:a", "pcm_s16le", str(b)], context="BGM 레벨 실험")
            pre = work / f"pre_up{up}.wav"
            M.sum_stems(ff, [narr, b, *([cue] if cue else [])], pre, work)
            m = measure_ebur128(ff, pre)
            need = (-14.0 - m.integrated) - (-1.0 - m.true_peak)       # 목표 음량 이득 - 한도까지의 여유 = 필요한 리미터 예산
            res = master_normalize(ff, pre, work / "out.wav", target_i=-14.0, target_tp=-1.0, limiter_budget_db=4.0)
            lines.append(f"- BGM 내레이션 대비 {-14.5 + up:+.1f} dB: 합산 {m.integrated:.1f} LUFS/{m.true_peak:.1f} dBTP, "
                         f"필요 예산 {need:.1f} dB, 예산 4 dB 결과 {res['output_i']:.1f} LUFS")
    summary("\n".join(lines))


ALIGN_CASES = [
    # (라벨, 반향 필터, 소음 진폭)
    ("지연만(깨끗)", None, 0.0),
    ("방 반향 + 약한 소음", "aecho=0.8:0.6:40|90|160:0.35|0.25|0.15,lowpass=f=6000", 0.002),
    ("방 반향 + 중간 소음", "aecho=0.8:0.6:40|90|160:0.35|0.25|0.15,lowpass=f=6000", 0.006),
    ("긴 반향 + 큰 소음", "aecho=0.8:0.7:80|180|320:0.5|0.35|0.25,lowpass=f=5000", 0.02),
]


def align_experiment(proj: Path) -> None:
    """실제 Kokoro 가이드를 휴대폰이 스피커 소리를 녹음한 것처럼 열화시켜 시작 톤을 찾는 `align`을 시험한다.

    이전에는 합성 믹스로만 검증했다. 여기서도 사람이 말하는 소리나 실제 휴대폰 마이크는 없으므로 실녹음 검증은 아니다.
    """
    import subprocess

    from avsync.guide.align import find_tone
    from avsync.platform.ffmpeg import FFmpeg, Tools

    gj = sorted((proj / "outputs" / "guide").glob("guide_*.json"))
    if not gj:
        return
    meta = json.loads(gj[-1].read_text(encoding="utf-8"))
    wav = Path(meta["wav"])
    if not wav.is_file():
        wav = gj[-1].with_suffix(".wav")
    ff = FFmpeg(Tools(Path(shutil.which("ffmpeg")), Path(shutil.which("ffprobe")), True))
    delay_s = 3.24
    dur = meta["total_us"] / 1e6 + delay_s + 1.0
    work = Path(tempfile.mkdtemp(prefix="avsync_align_"))
    expect_us = int(delay_s * 1e6) + meta["tone_at_us"]
    lines = [f"### align 시험: 가이드 WAV를 {delay_s}초 지연 + 열화해 영상 소리로 만든 뒤 시작 톤 탐색 (실제 톤 위치 {expect_us / 1e6:.3f}s)"]
    for i, (label, room, noise) in enumerate(ALIGN_CASES):
        chain = f"[1:a]aformat=channel_layouts=mono,aresample=48000,adelay={int(delay_s * 1000)}:all=1" + (f",{room}" if room else "") + ",volume=-6dB[g]"
        graph = chain + (f";anoisesrc=color=pink:amplitude={noise}:d={dur:.1f}:r=48000,aformat=channel_layouts=mono[n];"
                         "[g][n]amix=inputs=2:normalize=0:duration=longest[a]" if noise else ";[g]anull[a]")
        out = work / f"rec_{i}.mp4"
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=320x568:r=30:d={dur:.1f}",
                            "-i", str(wav), "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-t", f"{dur:.1f}", str(out)], capture_output=True, text=True, encoding="utf-8")
        if r.returncode != 0:
            lines.append(f"- {label}: 녹음 합성 실패 {r.stderr[-200:]}")
            continue
        hits = find_tone(ff, out, work / f"w{i}", freq_hz=meta["tone_hz"], tone_ms=meta["tone_ms"])
        if not hits:
            lines.append(f"- {label}: **톤을 찾지 못함**")
            continue
        best = hits[0]
        err = (best.time_us - expect_us) / 1000
        lines.append(f"- {label}: 후보 {len(hits)}개, 1순위 {best.time_us / 1e6:.3f}s (오차 {err:+.0f} ms), 점수 {best.score:.1f}, "
                     f"톤 비율 {best.tonality:.2f}, 대비 {best.contrast_db:.1f} dB")
    summary("\n".join(lines))


def main_() -> int:
    OUT.mkdir(exist_ok=True)
    rc = main(["benchmark", "tts", "--provider", "kokoro", "--voice", VOICE, "--targets", "15,30", "--listen"])
    bench = ROOT / "reports" / "benchmark_kokoro.json"
    if bench.is_file():
        shutil.copy(bench, OUT / bench.name)
        b = json.loads(bench.read_text(encoding="utf-8"))
        summary(f"### Kokoro 벤치마크 (러너 기준, 사용자 PC 아님)\n- 콜드 스타트 {b['cold_start_s']}s\n"
                + "\n".join(f"- {r['target_s']}초 분량: 소요 {r['wall_s']}s, RTF {r['rtf']}" for r in b["runs"])
                + f"\n- 최대 메모리 {b['peak_memory_mb']} MB ({b['peak_memory_method']})")
    listen = ROOT / "reports" / "listening" / "kokoro"
    if listen.is_dir():
        shutil.copytree(listen, OUT / "listening", dirs_exist_ok=True)
    if rc != 0:
        return rc
    info = model_info()
    if info:
        try:
            voice_samples(info)
        except Exception as e:  # noqa: BLE001 - 음색 비교는 부가 산출물이라 실패해도 가이드 합성은 계속
            summary(f"### 음색 샘플 생성 실패: {type(e).__name__}: {e}")

    work = Path(tempfile.mkdtemp(prefix="avsync_ci_"))
    main(["init", "surya_a", "--base", str(work), "--example", "surya_a"])
    proj = work / "surya_a"
    s = (proj / "script.yaml").read_text(encoding="utf-8")
    s = s.replace("approved: false", "approved: true").replace(
        "voice: {provider: null, voice_id: null, base_speed: 0.95}",
        f"voice: {{provider: kokoro, voice_id: {VOICE}, base_speed: 0.95}}")
    (proj / "script.yaml").write_text(s, encoding="utf-8")
    phoneme_diagnostics(proj / "script.yaml")
    # 믹스 음량 측정을 위해 합성 BGM(사인 화음 + 느린 트레몰로)을 붙인다
    import subprocess
    bgm = work / "bgm_ci.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=40", "-f", "lavfi",
                    "-i", "sine=f=277.18:d=40", "-f", "lavfi", "-i", "sine=f=329.63:d=40", "-filter_complex",
                    "[0][1][2]amix=inputs=3:normalize=0,tremolo=f=0.3:d=0.4,aformat=channel_layouts=stereo,volume=0.5",
                    "-c:a", "pcm_s16le", str(bgm)], check=True)
    pj = (proj / "project.yaml").read_text(encoding="utf-8")
    pj = pj.replace("source: none", f"source: imported\n    path: '{bgm.as_posix()}'").replace('license_note: ""', 'license_note: "CI 합성 사인파"')
    (proj / "project.yaml").write_text(pj, encoding="utf-8")
    rc = main(["guide", "--project", str(proj / "project.yaml")])
    if (proj / "outputs").is_dir():
        shutil.copytree(proj / "outputs" / "guide", OUT / "guide", dirs_exist_ok=True)
        fit = proj / "manifests" / "fit_report.json"
        if fit.is_file():
            shutil.copy(fit, OUT / "fit_report.json")
            rep = json.loads(fit.read_text(encoding="utf-8"))
            lines = ["### Kokoro 단어 타임스탬프 진단 (공급자 보고: word_timestamps=%s)" % rep["provider"]["word_timestamps"]]
            for r in rep["items"][:4]:
                for seg in r["audio"]["segments"]:
                    ws = " ".join(f"{w['text']}[{w['start_us'] / 1e6:.2f}-{w['end_us'] / 1e6:.2f}]" for w in seg["words"])
                    lines.append(f"- {r['id']} \"{seg['original']}\" ({seg['start_us'] / 1e6:.2f}-{seg['end_us'] / 1e6:.2f}s) words: {ws or '(없음)'}")
            lines.append("- 발음 사전 적용 문장: " + "; ".join(
                f"{seg['original']} -> {seg['spoken']}" for r in rep["items"][:3] for seg in r["audio"]["segments"] if seg["spoken"] != seg["normalized"]))
            summary("\n".join(lines))
    if (proj / "manifests" / "mix_report.json").is_file():
        mix_diagnostics(proj)
        for exp in (loudness_experiment, align_experiment):
            try:
                exp(proj)
            except Exception as e:  # noqa: BLE001 - 실험은 부가 산출물
                summary(f"### {exp.__name__} 실패: {type(e).__name__}: {e}")
    summary(f"### 샘플 가이드 합성 종료 코드: {rc} (0=성공, 2=길이 초과/겹침 보고)")
    return 0 if rc in (0, 2) else rc


if __name__ == "__main__":
    sys.exit(main_())
