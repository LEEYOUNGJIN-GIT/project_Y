"""Windows PowerShell 5.1은 BOM 없는 UTF-8을 ANSI로 읽어 한글 주석에서 파서가 깨진다 (CI에서 실제로 발생)."""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_powershell_scripts_have_utf8_bom():
    files = sorted(SCRIPTS.glob("*.ps1"))
    assert files
    for f in files:
        data = f.read_bytes()
        if any(b > 127 for b in data):
            assert data.startswith(b"\xef\xbb\xbf"), f"{f.name}: 비ASCII 문자가 있으면 UTF-8 BOM이 필요하다"
