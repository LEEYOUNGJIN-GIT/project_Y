"""설정 파일의 알 수 없는 키(오타) 경고. 조용히 무시되어 설정이 적용되지 않는 사고를 막는다."""
from __future__ import annotations

import difflib

SKIP = {"__line__"}

PROJECT = {
    "": {"schema_version", "project_id", "input", "tone", "audio", "output"},
    "input": {"video", "script", "guide"},
    "audio": {"original", "duck_under_narration_db", "bgm"},
    "audio.bgm": {"source", "path", "license_note", "level_db", "duck_db"},
    "output": {"profile", "reframe", "captions", "caption_style", "loudness_lufs", "true_peak_dbfs"},
    "output.caption_style": {"font_file", "font_license", "font_size", "margin_v", "margin_h"},
}
SCRIPT = {
    "": {"approved", "voice", "pronunciations", "lines", "no_narration", "cue_words"},
    "voice": {"provider", "voice_id", "base_speed"},
    "line": {"id", "at", "until", "text", "mood", "emphasis", "pause_after_ms"},
}
GUIDE = {
    "": {"mode", "count_in", "breath", "steps", "tail_s", "lead_in_ms"},
    "count_in": {"text", "count_in_s", "start_tone"},
    "breath": {"count_s", "count_beats", "bpm", "beat_align"},
    "step": {"line", "counts", "hold", "hold_mode", "hold_cues"},
}


def unknown_keys(data, allowed: set[str], where: str) -> list[str]:
    if not isinstance(data, dict):
        return []
    out = []
    for k in data:
        if k in SKIP or k in allowed:
            continue
        near = difflib.get_close_matches(str(k), sorted(allowed), n=1, cutoff=0.6)
        hint = f" (혹시 '{near[0]}'?)" if near else f" (허용: {', '.join(sorted(allowed))})"
        out.append(f"{where or '최상위'}: 알 수 없는 키 '{k}'{hint} - 무시됩니다")
    return out


def check_tree(data: dict, spec: dict[str, set[str]], prefix: str = "") -> list[str]:
    """점으로 구분한 경로(audio.bgm 등)별 허용 키로 검사한다."""
    out = unknown_keys(data, spec[""], prefix)
    for path, allowed in spec.items():
        if not path or "." in path and path.split(".")[0] not in (data or {}):
            continue
        node = data
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            out += unknown_keys(node, allowed, f"{prefix}{path}")
    return out
