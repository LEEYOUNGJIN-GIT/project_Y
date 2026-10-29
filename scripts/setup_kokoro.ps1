# Kokoro 공급자 환경 설치 (Windows PowerShell 5.1+).
# 실행 정책으로 막히면:  powershell -ExecutionPolicy Bypass -File scripts\setup_kokoro.ps1
# 또는 아래 "수동 명령"을 한 줄씩 직접 실행한다.
#
# 이 스크립트는 작성 환경(Linux, 외부 호스트 차단)에서 실행해 보지 못했다. 첫 실행 결과를
# reports/phase0_report.md의 "사용자 PC 실측" 칸에 기록하고, 실패하면 오류 메시지를 알려 달라.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$env_dir = Join-Path $root "provider_envs\kokoro"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv가 없습니다. https://docs.astral.sh/uv/ 의 설치 안내를 따른 뒤 다시 실행하세요."
}

# kokoro 0.9.4는 Python >=3.10,<3.13 이 필요하다 (PyPI 메타데이터 확인). uv가 3.12를 내려받아 쓴다.
Push-Location $env_dir
try {
    uv sync --python 3.12
    Write-Host "설치 완료. 다음: avsync benchmark tts --provider kokoro --voice <음색> --listen"
    Write-Host "첫 합성 때 가중치/음색 파일을 HuggingFace에서 내려받는다 (모델 캐시: %LOCALAPPDATA%\avsync\models\hf)."
}
finally { Pop-Location }

# 수동 명령:
#   cd provider_envs\kokoro
#   uv sync --python 3.12
