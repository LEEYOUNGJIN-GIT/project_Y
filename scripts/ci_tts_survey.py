"""CI 전용: HuggingFace에서 TTS 모델 후보를 직접 조회해 표로 만든다 (이름·라이선스·크기를 기억이나 추정으로 쓰지 않기 위해).

기준: 다운로드순 / 좋아요순 / 최근 인기순 상위를 합친 뒤 모델 카드의 라이선스, 가중치 파일 크기, 영어 지원 태그,
카드 본문의 기능 키워드(감정/스타일 지시/음성 복제/CPU·ONNX·GGUF 언급 등)를 모은다. 소리를 듣고 고른 것이 아니라
"어떤 후보를 청취 시험에 올릴지" 정하기 위한 목록이다. 키워드는 본문에 단어가 있는지만 보며 기능을 보증하지 않는다.
huggingface_hub만 필요하다: uv run --no-project --with huggingface_hub python scripts/ci_tts_survey.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

OUT = Path(__file__).resolve().parents[1] / "ci_artifacts" / "tts_survey"
PERMISSIVE = {"apache-2.0", "mit", "bsd-3-clause", "bsd-2-clause", "cc-by-4.0", "cc0-1.0", "unlicense", "bsd"}
WEIGHT_EXT = (".safetensors", ".bin", ".pt", ".pth", ".onnx", ".gguf", ".ckpt")
KEYWORDS = {
    "감정": r"emotion", "스타일/프롬프트 지시": r"style prompt|prompt|description|instruction", "표현력": r"expressive|prosod",
    "속삭임": r"whisper", "음성 복제": r"voice clon|zero-shot|reference audio|speaker prompt", "스트리밍": r"streaming",
    "CPU": r"\bcpu\b", "ONNX/GGUF": r"onnx|gguf|llama\.cpp", "실시간": r"real-?time",
}
SORTS = ("downloads", "likes", "trending_score")
PER_SORT = 40


def summary(text: str) -> None:
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def license_of(tags: list[str], card: dict | None) -> str:
    for t in tags or []:
        if t.startswith("license:"):
            return t.split(":", 1)[1]
    return str((card or {}).get("license") or "미표기")


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    api = HfApi()
    seen: dict[str, dict] = {}
    notes = []
    for sort in SORTS:
        try:
            for m in api.list_models(filter="text-to-speech", sort=sort, limit=PER_SORT):
                seen.setdefault(m.id, {"id": m.id, "found_by": []})["found_by"].append(sort)
        except Exception as e:  # noqa: BLE001 - 정렬 키가 버전에 없을 수 있음
            notes.append(f"정렬 '{sort}' 조회 실패: {type(e).__name__}: {str(e)[:120]}")
    rows = []
    for mid, rec in seen.items():
        try:
            info = api.model_info(mid, files_metadata=True)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            rows.append(rec)
            continue
        tags = list(info.tags or [])
        card = info.card_data.to_dict() if getattr(info, "card_data", None) else {}
        size = sum((s.size or 0) for s in (info.siblings or []) if s.rfilename.lower().endswith(WEIGHT_EXT))
        st = getattr(info, "safetensors", None)
        params = getattr(st, "total", None) if st else None
        text = ""
        try:
            text = Path(hf_hub_download(mid, "README.md")).read_text(encoding="utf-8", errors="replace").lower()
        except Exception:  # noqa: BLE001 - 카드 없음/접근 제한
            pass
        feats = [k for k, pat in KEYWORDS.items() if re.search(pat, text)]
        rec.update({
            "license": license_of(tags, card), "downloads": info.downloads, "likes": info.likes,
            "weights_gb": round(size / 1e9, 2) if size else None, "params_m": round(params / 1e6) if params else None,
            "english": ("en" in tags) or ("multilingual" in tags) or bool(re.search(r"english", text)),
            "gated": bool(getattr(info, "gated", False)), "last_modified": str(info.last_modified)[:10],
            "library": getattr(info, "library_name", None), "features": feats,
        })
        rows.append(rec)
    rows.sort(key=lambda r: -(r.get("downloads") or 0))
    (OUT / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def line(r: dict) -> str:
        if "error" in r:
            return f"| {r['id']} | 조회 실패 | | | | | | | {r['error']} |"
        lic = r["license"]
        mark = "허용형" if lic in PERMISSIVE else ("비상업/제한 가능" if "nc" in lic or "openrail" in lic or "other" in lic or lic == "미표기" else "확인 필요")
        return (f"| {r['id']} | {lic} ({mark}) | {r['weights_gb']} | {r['params_m'] or '-'} | {r['downloads']:,} | {r['likes']} | "
                f"{'예' if r['english'] else '-'} | {r['last_modified']} | {', '.join(r['features']) or '-'} |")

    head = "| 모델 | 라이선스 | 가중치(GB) | 파라미터(M) | 다운로드 | 좋아요 | 영어 | 최근 갱신 | 카드 키워드 |\n|---|---|---|---|---|---|---|---|---|"
    ok = [r for r in rows if "error" not in r]
    small = [r for r in ok if r["license"] in PERMISSIVE and r["english"] and r["weights_gb"] and r["weights_gb"] <= 4 and not r["gated"]]
    md = ["# TTS 후보 조사 (HuggingFace 직접 조회)", "",
          f"조회: `filter=text-to-speech`, 정렬 {', '.join(SORTS)} 각 상위 {PER_SORT}, 합쳐서 {len(rows)}개. 소리는 듣지 않았다.", ""]
    md += notes
    md += ["", "## 1순위 필터: 허용형 라이선스 + 영어 + 가중치 4 GB 이하 + 비게이트", "", head] + [line(r) for r in small]
    md += ["", "## 전체 (다운로드순)", "", head] + [line(r) for r in rows]
    (OUT / "README.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    summary("### TTS 후보 조사 - 1순위 필터 통과\n" + head + "\n" + "\n".join(line(r) for r in small))
    summary(f"\n전체 {len(rows)}개는 아티팩트 tts_survey/README.md 참조. " + " ".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
