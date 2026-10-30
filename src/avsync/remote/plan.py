"""BGM 생성 계획(prompts.yaml) 검증 (6.4)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..domain.hashing import sha256_json

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,48}$")
# 아티스트·곡 모사를 시사하는 표현 (6.4: 아티스트명·곡명을 프롬프트에 넣지 않는다). 이름 자체는 알 수 없으므로 표현 패턴만 거른다
_IMITATION_RE = re.compile(
    r"\b(in the style of|style of|sounds? like|similar to|reminiscent of|inspired by|cover of|remix of|"
    r"feat\.?|featuring|ft\.)\b|\bby\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", re.I)
_PRIVATE_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|www\.|\b\d{3}[- ]?\d{3,4}[- ]?\d{4}\b")
_VOCAL_WORDS = re.compile(r"\b(vocals?|singing|singer|choir|lyrics|voice|chant|humming)\b", re.I)


class PlanError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


@dataclass
class TrackSpec:
    id: str
    prompt: str
    duration_s: float
    bpm_hint: tuple[int, int] | None
    seeds: list[int]
    vocals: str = "forbidden"


@dataclass
class BgmPlan:
    tracks: list[TrackSpec]
    avoid: list[str] = field(default_factory=list)

    @property
    def jobs(self) -> int:
        return sum(len(t.seeds) for t in self.tracks)

    @property
    def total_audio_s(self) -> float:
        return sum(t.duration_s * len(t.seeds) for t in self.tracks)

    def as_dict(self) -> dict:
        return {"tracks": [{"id": t.id, "prompt": t.prompt, "duration_s": t.duration_s,
                            "bpm_hint": list(t.bpm_hint) if t.bpm_hint else None, "seeds": t.seeds, "vocals": t.vocals}
                           for t in self.tracks], "avoid": self.avoid}

    def hash(self) -> str:
        return sha256_json(self.as_dict())[:12]


def load_plan(path: Path, *, min_s: float = 10, max_s: float = 600) -> BgmPlan:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PlanError([f"{path}: 파일이 없습니다"]) from None
    except yaml.YAMLError as e:
        raise PlanError([f"{path}: YAML 문법 오류: {e}"]) from None
    problems: list[str] = []
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list) or not data["tracks"]:
        raise PlanError([f"{path}: tracks는 비어 있지 않은 목록이어야 합니다"])
    tracks: list[TrackSpec] = []
    seen: set[str] = set()
    for i, t in enumerate(data["tracks"], 1):
        where = f"{path}: tracks[{i}]"
        if not isinstance(t, dict):
            problems.append(f"{where}: 매핑이 아닙니다")
            continue
        tid = str(t.get("id", ""))
        if not _ID_RE.match(tid):
            problems.append(f"{where}: id는 소문자·숫자·_·- 만 (최대 49자): {tid!r}")
        elif tid in seen:
            problems.append(f"{where}: id {tid!r} 중복")
        seen.add(tid)
        prompt = str(t.get("prompt", "")).strip()
        if not prompt:
            problems.append(f"{where}({tid}): prompt가 비었습니다")
        if _IMITATION_RE.search(prompt):
            problems.append(f"{where}({tid}): 아티스트/곡을 모사하는 표현이 있습니다 ({_IMITATION_RE.search(prompt).group(0)!r}). 음악적 특징으로 풀어 쓰세요")
        if _PRIVATE_RE.search(prompt):
            problems.append(f"{where}({tid}): 이메일·URL·전화번호 같은 개인 정보 형태가 프롬프트에 있습니다 (커널로 전송됨)")
        vocals = str(t.get("vocals", "forbidden"))
        if vocals != "forbidden":
            problems.append(f"{where}({tid}): vocals는 forbidden만 지원합니다 (내레이션과 충돌)")
        if _VOCAL_WORDS.search(prompt) and not re.search(r"\b(no|without|instrumental)\b", prompt, re.I):
            problems.append(f"{where}({tid}): 프롬프트에 보컬 관련 단어가 있는데 'no vocals'/'instrumental' 표기가 없습니다")
        try:
            dur = float(t.get("duration_s", 0))
        except (TypeError, ValueError):
            dur = 0
        if not (min_s <= dur <= max_s):
            problems.append(f"{where}({tid}): duration_s는 {min_s}-{max_s}초여야 합니다 (ACE-Step 1.5 문서의 범위): {t.get('duration_s')!r}")
        bpm = t.get("bpm_hint")
        bpm_t = None
        if bpm is not None:
            if not (isinstance(bpm, list) and len(bpm) == 2 and all(isinstance(x, int) for x in bpm) and 30 <= bpm[0] <= bpm[1] <= 300):
                problems.append(f"{where}({tid}): bpm_hint는 [최소, 최대] 정수 (30-300)여야 합니다")
            else:
                bpm_t = (bpm[0], bpm[1])
        seeds = t.get("seeds") or [-1]
        if not (isinstance(seeds, list) and all(isinstance(s, int) for s in seeds)):
            problems.append(f"{where}({tid}): seeds는 정수 목록이어야 합니다")
            seeds = [-1]
        tracks.append(TrackSpec(tid, prompt, dur, bpm_t, [int(s) for s in seeds], vocals))
    if problems:
        raise PlanError(problems)
    return BgmPlan(tracks, [str(a) for a in (data.get("avoid") or [])])
