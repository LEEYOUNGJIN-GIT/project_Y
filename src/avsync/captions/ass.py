"""burn-in 자막용 ASS 생성 (8장, Phase 4).

- 폰트는 시스템 폰트를 임의로 쓰지 않고 **파일과 라이선스를 지정**한다 (8장). 폰트 파일의 family 이름은 TTF/OTF의
  name 테이블에서 직접 읽어 ASS 스타일과 libass fontsdir 매칭에 쓴다.
- 단어 시각이 있으면 `\\kf` 카라오케 하이라이트, 없으면 문장 단위로 표시한다.
- 안전 영역(플랫폼 UI에 가려지는 아래쪽 여백)의 정확한 값은 확인하지 못했다. margin_v는 시작값이다 [재확인].
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Caption:
    start_us: int
    end_us: int
    text: str
    words: list[dict] = field(default_factory=list)      # [{text,start_us,end_us}] 같은 시간 기준


@dataclass
class CaptionStyle:
    font_file: Path
    font_license: str
    font_size: int = 64
    margin_v: int = 420
    margin_h: int = 90
    primary: str = "&H0000E6FF&"     # 읽는 중(하이라이트): 노랑 (ASS는 BGR)
    secondary: str = "&H00FFFFFF&"   # 읽기 전: 흰색
    outline_color: str = "&H00000000&"
    outline: int = 4
    width: int = 1080
    height: int = 1920


class FontError(ValueError):
    pass


def read_font_family(path: Path) -> str:
    """TTF/OTF의 name 테이블에서 family 이름(ID 16 우선, 없으면 1)을 읽는다. TTC는 첫 폰트만."""
    data = Path(path).read_bytes()
    base = 0
    if data[:4] == b"ttcf":
        base = struct.unpack(">I", data[12:16])[0]
    if data[base:base + 4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        raise FontError(f"TTF/OTF 폰트가 아닙니다: {path}")
    n_tables = struct.unpack(">H", data[base + 4:base + 6])[0]
    name_off = None
    for i in range(n_tables):
        rec = data[base + 12 + 16 * i: base + 28 + 16 * i]
        if rec[:4] == b"name":
            name_off = struct.unpack(">I", rec[8:12])[0]
            break
    if name_off is None:
        raise FontError(f"name 테이블이 없습니다: {path}")
    count, str_off = struct.unpack(">HH", data[name_off + 2:name_off + 6])
    found: dict[int, str] = {}
    for i in range(count):
        plat, enc, lang, nid, length, off = struct.unpack(">HHHHHH", data[name_off + 6 + 12 * i: name_off + 18 + 12 * i])
        if nid not in (1, 16):
            continue
        raw = data[name_off + str_off + off: name_off + str_off + off + length]
        try:
            text = raw.decode("utf-16-be") if plat in (0, 3) else raw.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if text and (nid not in found or (plat == 3 and lang == 0x409)):
            found[nid] = text
    family = found.get(16) or found.get(1)
    if not family:
        raise FontError(f"family 이름을 읽지 못했습니다: {path}")
    return family


def _ts(us: int) -> str:
    cs = max(0, (us + 5000) // 10000)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _karaoke(c: Caption) -> str:
    """단어 길이를 centisecond로 누적해 \\kf 태그를 만든다. 단어 사이 틈과 앞 공백은 이전 단어 길이에 포함한다."""
    out = []
    cursor = c.start_us
    words = c.words
    for i, w in enumerate(words):
        end = words[i + 1]["start_us"] if i + 1 < len(words) else max(w["end_us"], c.end_us)
        cs = max(1, round((end - cursor) / 10000))
        cursor += cs * 10000
        out.append(f"{{\\kf{cs}}}{_esc(w['text'])}")
    return " ".join(out)


def build_ass(captions: list[Caption], style: CaptionStyle, family: str) -> str:
    s = style
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {s.width}", f"PlayResY: {s.height}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding",
        f"Style: Default,{family},{s.font_size},{s.primary},{s.secondary},{s.outline_color},&H64000000&,0,0,0,0,100,100,0,0,1,"
        f"{s.outline},0,2,{s.margin_h},{s.margin_h},{s.margin_v},1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for c in captions:
        if c.end_us <= 0 or c.end_us <= c.start_us:
            continue
        text = _karaoke(c) if c.words else _esc(c.text)
        lines.append(f"Dialogue: 0,{_ts(max(0, c.start_us))},{_ts(c.end_us)},Default,,0,0,0,,{text}")
    return "\n".join(lines) + "\n"


def prepare_ass_dir(work: Path, captions: list[Caption], style: CaptionStyle) -> Path:
    """work/ 아래에 captions.ass와 fonts/<폰트>를 만든다. 필터 인자에 경로 특수문자(Windows의 `C:`, 한글 경로 등)가
    들어가지 않도록, 호출 쪽은 이 디렉터리를 cwd로 삼고 상대 이름('captions.ass', 'fonts')만 쓴다."""
    if not style.font_file or not Path(style.font_file).is_file():
        raise FontError(f"자막 폰트 파일이 없습니다: {style.font_file} (output.caption_style.font_file에 파일을 지정)")
    if not style.font_license.strip():
        raise FontError("자막 폰트의 라이선스(output.caption_style.font_license)를 적어야 합니다 (10장: 폰트 파일과 라이선스 기록)")
    work.mkdir(parents=True, exist_ok=True)
    fonts = work / "fonts"
    fonts.mkdir(exist_ok=True)
    dst = fonts / Path(style.font_file).name
    dst.write_bytes(Path(style.font_file).read_bytes())
    family = read_font_family(dst)
    (work / "captions.ass").write_text(build_ass(captions, style, family), encoding="utf-8")
    return work
