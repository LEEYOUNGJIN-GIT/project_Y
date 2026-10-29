"""Kaggle 원격 GPU로 BGM 라이브러리를 만드는 일괄 작업 (6.3). 영상·음성은 보내지 않고 프롬프트 텍스트만 나간다.

`kaggle` CLI(kaggle 2.x)를 부른다: `kernels push -p DIR [--accelerator A]`, `kernels status REF`, `kernels output REF -p DIR`.
인증은 CLI의 표준 방식(KAGGLE_API_TOKEN 또는 ~/.kaggle)에 맡기며 이 도구는 토큰을 읽거나 저장하지 않는다.
커널은 비공개(is_private)로 만든다. 한도·GPU 종류는 코드에 하드코딩하지 않는다.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..audio.loudness import measure_ebur128, measure_peak_db
from ..audio.vocals import vocal_score
from ..bgmlib import import_bgm
from ..domain.manifest import now_iso, read_json, write_json
from ..platform.ffmpeg import FFmpeg
from ..platform.process import run
from ..video.probe import probe_audio_duration_us
from .kaggle_script import render_script
from .plan import BgmPlan


class RemoteError(RuntimeError):
    pass


def find_kaggle(configured: str | None = None) -> list[str]:
    cand = configured or os.environ.get("AVSYNC_KAGGLE_CLI") or shutil.which("kaggle")
    if not cand:
        raise RemoteError("kaggle CLI를 찾을 수 없습니다. `uv tool install kaggle` 또는 `pip install kaggle` 후 "
                          "Kaggle 설정의 API 토큰을 등록하세요 (토큰은 이 도구가 다루지 않습니다)")
    return [cand]


def slugify(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


def parse_status(text: str) -> tuple[str, str | None]:
    """`<ref> has status "KernelWorkerStatus.COMPLETE"` (+ 선택적 `Failure message: "..."`) -> (상태, 실패 메시지)."""
    m = re.search(r'has status "([^"]+)"', text)
    raw = (m.group(1) if m else text).lower()
    fail = re.search(r'Failure message: "(.*)"', text, re.S)
    for key, name in (("complete", "complete"), ("error", "error"), ("fail", "error"), ("cancel", "cancelled"),
                      ("running", "running"), ("queued", "queued")):
        if key in raw:
            return name, fail.group(1) if fail else None
    return "unknown", fail.group(1) if fail else None


@dataclass
class RemoteDirs:
    root: Path

    @property
    def kernel(self) -> Path:
        return self.root / "kernel"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def plan(self) -> Path:
        return self.root / "plan.json"


def build_kernel(plan: BgmPlan, dirs: RemoteDirs, *, owner: str, revision: str | None = None) -> dict:
    h = plan.hash()
    slug = f"avsync-bgm-{h[:8]}"
    title = f"avsync bgm {h[:8]}"
    assert slugify(title) == slug                      # Kaggle은 id의 slug가 title에서 나온 slug와 같아야 한다
    shutil.rmtree(dirs.kernel, ignore_errors=True)
    dirs.kernel.mkdir(parents=True)
    meta = {"id": f"{owner}/{slug}", "title": title, "code_file": "run.py", "language": "python", "kernel_type": "script",
            "is_private": True, "enable_gpu": True, "enable_internet": True,
            "dataset_sources": [], "competition_sources": [], "kernel_sources": []}
    write_json(dirs.kernel / "kernel-metadata.json", meta)
    (dirs.kernel / "run.py").write_text(render_script(plan.as_dict(), h, revision), encoding="utf-8")
    write_json(dirs.plan, {"hash": h, **plan.as_dict()})
    return meta


class KaggleCli:
    def __init__(self, command: list[str]):
        self.command = command

    def _run(self, args: list[str], context: str):
        r = run([*self.command, *args], timeout=600)
        if not r.ok:
            raise RemoteError(f"{context} 실패(종료 코드 {r.returncode}): {(r.stderr or r.stdout).strip()[-400:]}")
        return r

    def push(self, folder: Path, accelerator: str | None) -> str:
        args = ["kernels", "push", "-p", str(folder)]
        if accelerator:
            args += ["--accelerator", accelerator]
        return self._run(args, "kernels push").stdout

    def status(self, ref: str) -> tuple[str, str | None, str]:
        r = self._run(["kernels", "status", ref], "kernels status")
        state, fail = parse_status(r.stdout + r.stderr)
        return state, fail, r.stdout.strip()

    def output(self, ref: str, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self._run(["kernels", "output", ref, "-p", str(folder)], "kernels output")


def remote_run(plan: BgmPlan, dirs: RemoteDirs, cli: KaggleCli | None, *, owner: str, accelerator: str | None,
               revision: str | None, dry_run: bool) -> dict:
    dirs.root.mkdir(parents=True, exist_ok=True)
    meta = build_kernel(plan, dirs, owner=owner, revision=revision)
    cmd = ["kaggle", "kernels", "push", "-p", str(dirs.kernel)] + (["--accelerator", accelerator] if accelerator else [])
    if dry_run or cli is None:
        return {"dry_run": True, "kernel": meta, "command": cmd, "kernel_dir": str(dirs.kernel)}
    out = cli.push(dirs.kernel, accelerator)
    write_json(dirs.state, {"ref": meta["id"], "plan_hash": plan.hash(), "pushed_at": now_iso(), "status": "queued",
                            "accelerator": accelerator, "fetched": [], "push_output": out.strip()[-300:]})
    return {"dry_run": False, "ref": meta["id"], "push_output": out.strip()}


def remote_status(dirs: RemoteDirs, cli: KaggleCli) -> dict:
    if not dirs.state.is_file():
        raise RemoteError("실행 기록이 없습니다. 먼저 `avsync bgm remote-run`을 하세요")
    st = read_json(dirs.state)
    state, fail, raw = cli.status(st["ref"])
    st.update({"status": state, "checked_at": now_iso(), "failure": fail})
    write_json(dirs.state, st)
    return {"ref": st["ref"], "status": state, "failure": fail, "raw": raw}


def remote_fetch(dirs: RemoteDirs, cli: KaggleCli, ff: FFmpeg, gates: dict, *, library: Path | None = None) -> dict:
    """결과를 받아 검증하고 통과한 곡만 라이브러리에 등록한다. 부분 결과도 보존한다 (6.3-5)."""
    if not dirs.state.is_file():
        raise RemoteError("실행 기록이 없습니다")
    st = read_json(dirs.state)
    plan = read_json(dirs.plan)
    shutil.rmtree(dirs.output, ignore_errors=True)         # 이전 실행의 results.json/wav가 이번 결과로 읽히지 않게 (도구 소유 폴더)
    cli.output(st["ref"], dirs.output)
    base = dirs.output / "tracks" if (dirs.output / "tracks").is_dir() else dirs.output
    results_path = base / "results.json"
    if not results_path.is_file():
        raise RemoteError(f"results.json이 없습니다 ({base}). 커널이 아직 끝나지 않았거나 시작 단계에서 실패했을 수 있습니다. "
                          f"`avsync bgm remote-status`와 {base / 'log.txt'}를 확인하세요")
    results = read_json(results_path)
    specs = {t["id"]: t for t in plan["tracks"]}
    accepted, discarded, failed = [], [], []
    g = gates["bgm"]
    for rec in results:
        name = f"{rec['id']}_s{rec['seed']}"
        if rec["id"] not in specs:
            failed.append({"name": name, "error": "실행 계획(plan.json)에 없는 id - 다른 실행의 결과일 수 있어 등록하지 않음"})
            continue
        if not rec.get("file"):
            failed.append({"name": name, "error": rec.get("error")})
            continue
        wav = base / rec["file"]
        meta = read_json(wav.with_suffix(".json")) if wav.with_suffix(".json").is_file() else {}
        spec = specs[rec["id"]]
        reasons, warnings = [], []
        try:
            dur = probe_audio_duration_us(ff.tools, wav)
            ld = measure_ebur128(ff, wav)
            peak = measure_peak_db(ff, wav)
            voc = vocal_score(ff, wav, dirs.root / "work")
        except Exception as e:  # noqa: BLE001 - 손상 파일은 폐기 사유로 기록
            discarded.append({"name": name, "reasons": [f"분석 실패: {e}"]})
            continue
        want = float(spec.get("duration_s", 0)) * 1e6
        if ld.integrated is None:
            reasons.append("무음")
        if want and dur < want * g["min_duration_ratio"]:
            reasons.append(f"요청 길이의 {g['min_duration_ratio']:.0%} 미만 ({dur / 1e6:.1f}s / {want / 1e6:.0f}s)")
        elif want and abs(dur - want) > want * 0.2:
            warnings.append(f"요청 길이와 20% 이상 차이 ({dur / 1e6:.1f}s / {want / 1e6:.0f}s)")
        if voc.score >= g["vocal_discard_score"]:
            reasons.append(f"보컬 가능성 휴리스틱 {voc.score:.2f} >= {g['vocal_discard_score']} (청취로 재확인 가능)")
        if peak is not None and peak > -0.1:
            warnings.append(f"피크 {peak:.1f} dBFS - clipping 가능성")
        if reasons:
            discarded.append({"name": name, "reasons": reasons, "vocal": voc.as_dict()})
            continue
        note = (f"ACE-Step 1.5 생성(Kaggle 원격, 코드 MIT). prompt={spec.get('prompt')!r} seed={rec['seed']} "
                f"dit={meta.get('dit_model')} rev={meta.get('repo_revision')}. 생성물의 저작권 보호 가능성·제3자 권리 비침해·"
                f"플랫폼 콘텐츠 식별 무분쟁은 보장되지 않음(10장)")
        hint = "no" if voc.score < g["vocal_unknown_score"] else "unknown"     # 실제 보컬 음악으로 검증되지 않은 휴리스틱 -> 힌트로만 기록
        info = import_bgm(ff, wav, note, "unknown", library=library,
                          extra={"vocals_hint": hint, "generator": "ace-step-1.5", "prompt": spec.get("prompt"), "seed": rec["seed"],
                                 "generation": meta, "vocal_heuristic": voc.as_dict(), "plan_hash": st["plan_hash"],
                                 "track_id": rec["id"]})
        info["warnings"] += warnings
        accepted.append({"name": name, "path": info["path"], "vocal": voc.as_dict(), "warnings": info["warnings"]})
    st["fetched"] = sorted({a["name"] for a in accepted} | set(st.get("fetched", [])))
    st["fetched_at"] = now_iso()
    write_json(dirs.state, st)
    return {"accepted": accepted, "discarded": discarded, "failed": failed, "results": len(results),
            "complete": len(results) >= sum(len(t["seeds"]) for t in plan["tracks"])}
