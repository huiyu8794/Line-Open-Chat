param([string]$Python='python')
$ErrorActionPreference='Stop'
$Root=Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python tools/generate_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }
    & $Python -m PyInstaller --noconfirm --clean --onefile --console --name line_openchat_capture --distpath build/worker-dist --workpath build/worker-work --specpath build/spec --collect-all mss --collect-all PIL line_openchat_capture.py
    if ($LASTEXITCODE -ne 0) { throw 'Worker build failed' }
    & $Python -m PyInstaller --noconfirm --clean --onedir --windowed --name LineOpenChatBackup --distpath dist --workpath build/tray-work --specpath build/spec --collect-all pystray --icon line_openchat_robot.ico --add-data 'line_openchat_robot.png;.' line_openchat_tray.pyw
    if ($LASTEXITCODE -ne 0) { throw 'Tray build failed' }
    Copy-Item -LiteralPath build/worker-dist/line_openchat_capture.exe -Destination dist/LineOpenChatBackup/_internal/line_openchat_capture.exe -Force
    Copy-Item -LiteralPath dist/LineOpenChatBackup/LineOpenChatBackup.exe -Destination dist/LineOpenChatBackup/line_openchat_capture.exe -Force
    Write-Output 'Built dist/LineOpenChatBackup. This script does not deploy or start the app.'
} finally { Pop-Location }
