# 전체 테스트 (FFmpeg 필요). 통합 테스트만 빼려면:  uv run pytest -m "not integration"
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
uv run pytest -q
